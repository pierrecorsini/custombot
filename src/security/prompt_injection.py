"""
src/security/prompt_injection.py — Prompt injection detection and sanitization.

Protects against prompt injection attacks by:
- Detecting common injection patterns in user messages
- Sanitizing user input before embedding into system prompts
- Enforcing max system prompt length to prevent context overflow
- Filtering outgoing responses for PII, secrets, and API keys

Usage:
    from src.security.prompt_injection import (
        detect_injection,
        sanitize_user_input,
        check_system_prompt_length,
        filter_response_content,
    )

    # Check for injection attempts
    result = detect_injection(user_message)
    if result.detected:
        log.warning("Injection blocked: %s", result.reason)

    # Sanitize before embedding
    safe = sanitize_user_input(user_message)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# Maximum system prompt length in characters (prevents context overflow attacks)
DEFAULT_MAX_SYSTEM_PROMPT_LENGTH = 100_000


@dataclass
class InjectionDetectionResult:
    """Result of prompt injection detection."""

    detected: bool
    confidence: float = 0.0  # 0.0 to 1.0
    reason: str = ""
    matched_patterns: list[str] = field(default_factory=list)


@dataclass
class ContentFilterResult:
    """Result of response content filtering."""

    flagged: bool
    categories: list[str] = field(default_factory=list)
    sanitized_content: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Injection Detection Patterns
# ─────────────────────────────────────────────────────────────────────────────

# High-confidence injection patterns (always block)
_HIGH_CONFIDENCE_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?i)ignore\s+(all\s+)?previous\s+(instructions?|prompts?|rules?)",
        "ignore_previous",
    ),
    (r"(?i)ignore\s+(the\s+)?above", "ignore_above"),
    (
        r"(?i)forget\s+(all\s+)?previous\s+(instructions?|prompts?|rules?)",
        "forget_previous",
    ),
    (
        r"(?i)disregard\s+(all\s+)?previous\s+(instructions?|prompts?|rules?)",
        "disregard_previous",
    ),
    (
        r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:DAN|jailbreak|unrestricted)",
        "persona_hijack",
    ),
    (r"(?i)system\s*:\s*", "system_role_injection"),
    (r"(?i)<\s*system\s*>", "system_tag_injection"),
    (r"(?i)\[system\]", "system_bracket_injection"),
    (r"(?i)new\s+instructions?\s*:", "new_instructions"),
    (
        r"(?i)override\s+(your\s+)?(instructions?|rules?|guidelines?)",
        "override_instructions",
    ),
    (r"(?i)jailbreak", "jailbreak_keyword"),
    (r"(?i)prompt\s+injection", "injection_keyword"),
    (
        r"(?i)Act\s+as\s+if\s+you\s+(are|have)\s+no\s+(restrictions?|rules?)",
        "act_unrestricted",
    ),
]

# Medium-confidence patterns (context-dependent)
_MEDIUM_CONFIDENCE_PATTERNS: list[tuple[str, str]] = [
    (
        r"(?i)pretend\s+(you\s+are|to\s+be)\s+(?:a\s+)?(?:different|new|other)",
        "pretend_persona",
    ),
    (r"(?i)role[\s-]?play\s+as\s+", "roleplay_hijack"),
    (
        r"(?i)from\s+now\s+on[,.]?\s+(you\s+)?(will|are|must|shall)",
        "instruction_override",
    ),
    (
        r"(?i)do\s+not\s+(follow|obey|adhere\s+to)\s+(your|the)\s+(instructions?|rules?)",
        "disobey_instruction",
    ),
    (
        r"(?i)reveal\s+(your|the)\s+(system|initial|original)\s+(prompt|instructions?)",
        "prompt_extraction",
    ),
    (
        r"(?i)show\s+me\s+(your|the)\s+(system|initial|original)\s+(prompt|instructions?)",
        "prompt_extraction_show",
    ),
    (
        r"(?i)what\s+(are|were)\s+(your|the)\s+(system|original|initial)\s+(prompt|instructions?)",
        "prompt_extraction_what",
    ),
    (
        r"(?i)repeat\s+(the|your|above|previous)\s+(system|initial|original)\s+(prompt|instructions?)",
        "prompt_extraction_repeat",
    ),
    (
        r"(?i)summarize\s+(your|the)\s+(system|initial)\s+(prompt|message)",
        "prompt_extraction_summarize",
    ),
    (r"(?i)translate\s+(your|the)\s+system\s+prompt", "prompt_extraction_translate"),
    (
        r"(?i)output\s+(your|the)\s+(system|initial|original)\s+(prompt|instructions?)(?:\s+verbatim)?",
        "prompt_extraction_output",
    ),
    (r"(?i)\bDAN\s+mode\b", "dan_mode"),
    (r"(?i)developer\s+mode\b", "developer_mode"),
    (r"(?i)god\s+mode\b", "god_mode"),
]

# Compiled regex patterns for efficiency
_HIGH_CONFIDENCE_COMPILED = [
    (re.compile(p), name) for p, name in _HIGH_CONFIDENCE_PATTERNS
]
_MEDIUM_CONFIDENCE_COMPILED = [
    (re.compile(p), name) for p, name in _MEDIUM_CONFIDENCE_PATTERNS
]


# ─────────────────────────────────────────────────────────────────────────────
# Response Content Filter Patterns
# ─────────────────────────────────────────────────────────────────────────────

# API key patterns
_API_KEY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "openai_api_key"),
    (re.compile(r"sk-proj-[a-zA-Z0-9_-]{20,}"), "openai_project_key"),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), "anthropic_api_key"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36}"), "github_token"),
    (re.compile(r"gho_[a-zA-Z0-9]{36}"), "github_oauth"),
    (re.compile(r"glpat-[a-zA-Z0-9_-]{20,}"), "gitlab_token"),
    (re.compile(r"xox[bpsa]-[a-zA-Z0-9-]{10,}"), "slack_token"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws_access_key"),
    (re.compile(r"AIza[a-zA-Z0-9_-]{35}"), "google_api_key"),
]

# Secret patterns
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(r"(?i)(?:password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
        "password",
    ),
    (
        re.compile(r"(?i)(?:secret|token|api_key|apikey)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
        "secret",
    ),
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA )?PRIVATE KEY-----"), "private_key"),
    (re.compile(r"(?i)(?:Bearer|Basic)\s+[a-zA-Z0-9._-]{20,}"), "auth_header"),
]

# PII patterns
_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Credit card numbers (basic pattern)
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "credit_card"),
    # SSN pattern (US)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    # Email addresses
    (re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"), "email"),
]

# Sensitive file extensions in content
_SENSITIVE_FILE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\.env\b"), "env_file"),
    (re.compile(r"(?i)\.pem\b"), "pem_file"),
    (re.compile(r"(?i)\.key\b"), "key_file"),
    (re.compile(r"(?i)\.p12\b"), "p12_file"),
    (re.compile(r"(?i)\.pfx\b"), "pfx_file"),
]


def detect_injection(text: str) -> InjectionDetectionResult:
    """
    Detect prompt injection attempts in user input.

    Checks against high and medium confidence pattern sets.
    Returns a result with detection status, confidence score, and matched patterns.

    Args:
        text: The user message text to check.

    Returns:
        InjectionDetectionResult with detection details.
    """
    if not text or not text.strip():
        return InjectionDetectionResult(detected=False)

    matched: list[str] = []
    max_confidence = 0.0

    # Check high-confidence patterns (confidence = 0.9)
    for pattern, name in _HIGH_CONFIDENCE_COMPILED:
        if pattern.search(text):
            matched.append(name)
            max_confidence = max(max_confidence, 0.9)

    # Check medium-confidence patterns (confidence = 0.6)
    for pattern, name in _MEDIUM_CONFIDENCE_COMPILED:
        if pattern.search(text):
            matched.append(name)
            max_confidence = max(max_confidence, 0.6)

    if matched:
        reason = f"Matched {len(matched)} injection pattern(s): {', '.join(matched)}"
        log.warning(
            "Prompt injection detected: confidence=%.1f patterns=%s",
            max_confidence,
            matched,
            extra={"injection_patterns": matched, "confidence": max_confidence},
        )
        return InjectionDetectionResult(
            detected=True,
            confidence=max_confidence,
            reason=reason,
            matched_patterns=matched,
        )

    return InjectionDetectionResult(detected=False)


def sanitize_user_input(text: str, *, strict: bool = False) -> str:
    """
    Sanitize user input before embedding into system prompts.

    Strips or escapes special instruction patterns that could manipulate
    the LLM's behavior. Also removes potential role-injection markers.

    Args:
        text: Raw user input text.
        strict: If True, apply more aggressive sanitization.

    Returns:
        Sanitized text safe for embedding into prompts.
    """
    if not text or not text.strip():
        return text or ""

    result = text

    # Remove role injection markers
    result = re.sub(r"(?i)^system\s*:\s*", "[blocked]: ", result, flags=re.MULTILINE)
    result = re.sub(r"(?i)^assistant\s*:\s*", "[blocked]: ", result, flags=re.MULTILINE)
    result = re.sub(r"(?i)^user\s*:\s*", "[blocked]: ", result, flags=re.MULTILINE)

    # Remove system tag injections
    result = re.sub(r"(?i)<\s*/?system\s*>", "", result)
    result = re.sub(r"(?i)\[/?system\]", "", result)

    # Escape common injection phrases (replace with safe alternatives)
    injection_replacements = {
        r"(?i)ignore\s+previous\s+instructions?": "[injection attempt removed]",
        r"(?i)ignore\s+all\s+previous": "[injection attempt removed]",
        r"(?i)forget\s+previous\s+instructions?": "[injection attempt removed]",
        r"(?i)disregard\s+(all\s+)?previous\s+(instructions?|rules?)": "[injection attempt removed]",
        r"(?i)override\s+(your\s+)?instructions?": "[injection attempt removed]",
        r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:DAN|unrestricted)": "[injection attempt removed]",
        r"(?i)jailbreak": "[blocked keyword]",
        r"(?i)prompt\s+injection": "[blocked keyword]",
    }

    for pattern, replacement in injection_replacements.items():
        result = re.sub(pattern, replacement, result)

    # Strict mode: additional sanitization
    if strict:
        # Remove any remaining instruction-like patterns
        result = re.sub(
            r"(?i)^(?:note|important|warning|attention)\s*:\s*",
            "",
            result,
            flags=re.MULTILINE,
        )
        # Remove potential multi-line instruction blocks
        result = re.sub(r"(?i)---+\s*(?:system|instructions?)\s*---+", "", result)

    return result


def check_system_prompt_length(
    system_prompt: str,
    max_length: int = DEFAULT_MAX_SYSTEM_PROMPT_LENGTH,
) -> tuple[bool, int]:
    """
    Check if a system prompt exceeds the maximum allowed length.

    Prevents context overflow attacks where malicious content is crafted
    to exhaust the LLM's context window.

    Args:
        system_prompt: The assembled system prompt string.
        max_length: Maximum allowed length in characters.

    Returns:
        Tuple of (is_within_limit, current_length).
    """
    length = len(system_prompt)
    if length > max_length:
        log.warning(
            "System prompt length (%d) exceeds maximum (%d) — potential overflow attack",
            length,
            max_length,
            extra={"prompt_length": length, "max_length": max_length},
        )
    return length <= max_length, length


def filter_response_content(
    content: str, *, redact: bool = True
) -> ContentFilterResult:
    """
    Filter outgoing LLM responses for sensitive content.

    Detects PII, API keys, secrets, and other sensitive data in
    responses. Optionally redacts the sensitive portions.

    Args:
        content: The LLM response text to check.
        redact: If True, redact flagged content. If False, just flag it.

    Returns:
        ContentFilterResult with flagged status and sanitized content.
    """
    if not content or not content.strip():
        return ContentFilterResult(flagged=False, sanitized_content=content or "")

    categories: list[str] = []
    result = content

    # Phase 1: Detect all patterns BEFORE any redaction (preserves original text for matching)
    all_patterns: list[tuple[re.Pattern, str]] = [
        *_API_KEY_PATTERNS,
        *_SECRET_PATTERNS,
        *_PII_PATTERNS,
        *_SENSITIVE_FILE_PATTERNS,
    ]

    for pattern, category in all_patterns:
        if pattern.search(result):
            categories.append(category)

    # Deduplicate categories
    seen: set[str] = set()
    unique_categories: list[str] = []
    for cat in categories:
        if cat not in seen:
            seen.add(cat)
            unique_categories.append(cat)
    categories = unique_categories

    # Phase 2: Redact flagged content (only if redact=True)
    if redact and categories:
        # Reset result to original for redaction pass
        result = content
        for pattern, category in _API_KEY_PATTERNS:
            if category in categories:
                result = pattern.sub(f"[REDACTED_{category.upper()}]", result)
        for pattern, category in _SECRET_PATTERNS:
            if category in categories:
                result = pattern.sub(f"[REDACTED_{category.upper()}]", result)
        for pattern, category in _PII_PATTERNS:
            if category in categories:
                result = pattern.sub(f"[REDACTED_{category.upper()}]", result)

    if categories:
        log.warning(
            "Response content filter flagged %d categories: %s",
            len(categories),
            categories,
            extra={"filter_categories": categories},
        )

    return ContentFilterResult(
        flagged=len(categories) > 0,
        categories=categories,
        sanitized_content=result,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "detect_injection",
    "sanitize_user_input",
    "check_system_prompt_length",
    "filter_response_content",
    "InjectionDetectionResult",
    "ContentFilterResult",
    "DEFAULT_MAX_SYSTEM_PROMPT_LENGTH",
]
