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
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Maximum system prompt length in characters (prevents context overflow attacks)
DEFAULT_MAX_SYSTEM_PROMPT_LENGTH = 100_000


@dataclass(slots=True)
class InjectionDetectionResult:
    """Result of prompt injection detection."""

    detected: bool
    confidence: float = 0.0  # 0.0 to 1.0
    reason: str = ""
    matched_patterns: list[str] = field(default_factory=list)


@dataclass(slots=True)
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
    # --- Multi-language injection patterns ---
    # German
    (
        r"(?i)ignoriere\s+(alle\s+)?vorherigen\s+(anweisungen|befehle|regeln)",
        "ignore_previous_de",
    ),
    (
        r"(?i)vergiss\s+(alle\s+)?vorherigen\s+(anweisungen|befehle)",
        "forget_previous_de",
    ),
    (
        r"(?i)missachte\s+(alle\s+)?vorherigen\s+(anweisungen|regeln)",
        "disregard_previous_de",
    ),
    # French
    (
        r"(?i)ignore?s?\s+(toutes?\s+)?les?\s+(instructions?|consignes?|r[èe]gles?)\s+(pr[ée]c[ée]dentes?)",
        "ignore_previous_fr",
    ),
    (
        r"(?i)oublie?s?\s+(toutes?\s+)?les?\s+(instructions?|consignes?)\s+(pr[ée]c[ée]dentes?)",
        "forget_previous_fr",
    ),
    # Spanish
    (
        r"(?i)ignora\s+(todas?\s+)?las?\s+(instrucciones?|reglas?)\s+(anteriores)",
        "ignore_previous_es",
    ),
    (
        r"(?i)olvida\s+(todas?\s+)?las?\s+(instrucciones?)\s+(anteriores)",
        "forget_previous_es",
    ),
    # Russian (transliterated)
    (
        r"(?i)ignoriruj(?:te)?\s+(vse\s+)?predyduw(?:ie|ie)\s+(instrukcii|komandy|pravila)",
        "ignore_previous_ru",
    ),
    # Chinese (simplified/traditional common phrases)
    ("忽略(?:所有)?之前的(?:指令|规则|提示)", "ignore_previous_zh"),
    ("忘记(?:所有)?之前的(?:指令|规则)", "forget_previous_zh"),
    ("无视(?:所有)?之前的(?:指令|规则)", "disregard_previous_zh"),
    # Portuguese
    (
        r"(?i)ignor[ae]\s+(tod[oa]s?\s+)?[oa]s?\s+(instru[cç][õo]es|regras)\s+(anteriores)",
        "ignore_previous_pt",
    ),
    # Japanese (romaji + hiragana common forms)
    (r"(?i)muk[ōo]shi?te\s+(subete\s+)?no\s+(shiji|meirei)", "ignore_previous_ja"),
]

# Compiled regex patterns for efficiency
_HIGH_CONFIDENCE_COMPILED = [(re.compile(p), name) for p, name in _HIGH_CONFIDENCE_PATTERNS]
_MEDIUM_CONFIDENCE_COMPILED = [(re.compile(p), name) for p, name in _MEDIUM_CONFIDENCE_PATTERNS]

# Combined single-pass patterns: all patterns OR'd together for one regex.search()
# instead of iterating N patterns sequentially. Used by detect_injection().
_HIGH_COMBINED = re.compile(
    "(?i)"
    + "|".join(
        f"(?P<_{i}>{p.removeprefix('(?i)')})" for i, (p, _) in enumerate(_HIGH_CONFIDENCE_PATTERNS)
    )
)
_MEDIUM_COMBINED = re.compile(
    "(?i)"
    + "|".join(
        f"(?P<_{i}>{p.removeprefix('(?i)')})"
        for i, (p, _) in enumerate(_MEDIUM_CONFIDENCE_PATTERNS)
    )
)
_HIGH_NAMES = [name for _, name in _HIGH_CONFIDENCE_PATTERNS]
_MEDIUM_NAMES = [name for _, name in _MEDIUM_CONFIDENCE_PATTERNS]


# ─────────────────────────────────────────────────────────────────────────────
# Response Content Filter Patterns
# ─────────────────────────────────────────────────────────────────────────────

# API key patterns
_API_KEY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
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
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
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
_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Credit card numbers (basic pattern)
    (re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"), "credit_card"),
    # SSN pattern (US)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    # Email addresses
    (re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"), "email"),
]

# Combined single-pass detection pattern for all redactable categories.
# Maps named group index to category name for O(1) lookup.
_REDACTABLE_ALL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    *_API_KEY_PATTERNS,
    *_SECRET_PATTERNS,
    *_PII_PATTERNS,
]
_REDACTABLE_COMBINED = re.compile(
    "|".join(
        f"(?P<_r{i}>{p.pattern})"
        for i, (p, _) in enumerate(_REDACTABLE_ALL_PATTERNS)
    )
)
_REDACTABLE_NAMES = [name for _, name in _REDACTABLE_ALL_PATTERNS]

# Sensitive file extensions in content
_SENSITIVE_FILE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\.env\b"), "env_file"),
    (re.compile(r"(?i)\.pem\b"), "pem_file"),
    (re.compile(r"(?i)\.key\b"), "key_file"),
    (re.compile(r"(?i)\.p12\b"), "p12_file"),
    (re.compile(r"(?i)\.pfx\b"), "pfx_file"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Pre-compiled sanitization patterns (module-level for reuse)
# ─────────────────────────────────────────────────────────────────────────────

# Role injection markers
_ROLE_INJECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)^system\s*:\s*", re.MULTILINE), "[blocked]: "),
    (re.compile(r"(?i)^assistant\s*:\s*", re.MULTILINE), "[blocked]: "),
    (re.compile(r"(?i)^user\s*:\s*", re.MULTILINE), "[blocked]: "),
]

# System tag patterns
_SYSTEM_TAG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)<\s*/?system\s*>"), ""),
    (re.compile(r"(?i)\[/?system\]"), ""),
]

# Injection phrase replacements
_INJECTION_REPLACEMENTS_COMPILED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)ignore\s+previous\s+instructions?"), "[injection attempt removed]"),
    (re.compile(r"(?i)ignore\s+all\s+previous"), "[injection attempt removed]"),
    (re.compile(r"(?i)forget\s+previous\s+instructions?"), "[injection attempt removed]"),
    (re.compile(r"(?i)disregard\s+(all\s+)?previous\s+(instructions?|rules?)"), "[injection attempt removed]"),
    (re.compile(r"(?i)override\s+(your\s+)?instructions?"), "[injection attempt removed]"),
    (re.compile(r"(?i)you\s+are\s+now\s+(?:a\s+)?(?:DAN|unrestricted)"), "[injection attempt removed]"),
    (re.compile(r"(?i)jailbreak"), "[blocked keyword]"),
    (re.compile(r"(?i)prompt\s+injection"), "[blocked keyword]"),
]

# Strict-mode patterns
_STRICT_INSTRUCTION_PATTERN = re.compile(
    r"(?i)^(?:note|important|warning|attention)\s*:\s*", re.MULTILINE
)
_STRICT_BLOCK_PATTERN = re.compile(r"(?i)---+\s*(?:system|instructions?)\s*---+")


def detect_injection(text: str) -> InjectionDetectionResult:
    """
    Detect prompt injection attempts in user input.

    Checks against high and medium confidence pattern sets.
    Returns a result with detection status, confidence score, and matched patterns.

    IMPORTANT: This detection is heuristic-only and can be bypassed by
    creative attackers. It should not be the sole defense against prompt
    injection. Use it as a layer in a defense-in-depth strategy.

    Supports English, German, French, Spanish, Russian, Chinese,
    Portuguese, and Japanese injection patterns.

    Applies NFKC Unicode normalization to prevent bypasses via confusable
    characters (e.g., Cyrillic 'о' instead of Latin 'o').

    Args:
        text: The user message text to check.

    Returns:
        InjectionDetectionResult with detection details.
    """
    if not text or not text.strip():
        return InjectionDetectionResult(detected=False)

    # Normalize Unicode to catch confusable character bypasses
    normalized = unicodedata.normalize("NFKC", text)

    matched: list[str] = []
    max_confidence = 0.0

    # Single-pass scan using combined alternation regex (much faster than
    # iterating N patterns sequentially — one regex engine pass per tier).
    # Uses m.lastgroup for O(1) lookup instead of iterating all groups.
    # Named groups are "_0", "_1", etc. — extract the index to find the name.
    m = _HIGH_COMBINED.search(normalized)
    if m and m.lastgroup:
        idx_str = m.lastgroup.lstrip("_")
        idx = int(idx_str) if idx_str.isdigit() else -1
        if 0 <= idx < len(_HIGH_NAMES):
            matched.append(_HIGH_NAMES[idx])
        max_confidence = 0.9

    m = _MEDIUM_COMBINED.search(normalized)
    if m and m.lastgroup:
        idx_str = m.lastgroup.lstrip("_")
        idx = int(idx_str) if idx_str.isdigit() else -1
        if 0 <= idx < len(_MEDIUM_NAMES):
            matched.append(_MEDIUM_NAMES[idx])
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

    # Normalize Unicode to catch confusable character bypasses
    result = unicodedata.normalize("NFKC", text)

    # Remove role injection markers (pre-compiled patterns)
    for pattern, replacement in _ROLE_INJECTION_PATTERNS:
        result = pattern.sub(replacement, result)

    # Remove system tag injections (pre-compiled patterns)
    for pattern, replacement in _SYSTEM_TAG_PATTERNS:
        result = pattern.sub(replacement, result)

    # Escape common injection phrases (pre-compiled patterns)
    for pattern, replacement in _INJECTION_REPLACEMENTS_COMPILED:
        result = pattern.sub(replacement, result)

    # Strict mode: additional sanitization
    if strict:
        result = _STRICT_INSTRUCTION_PATTERN.sub("", result)
        result = _STRICT_BLOCK_PATTERN.sub("", result)

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


def filter_response_content(content: str, *, redact: bool = True) -> ContentFilterResult:
    """
    Filter outgoing LLM responses for sensitive content.

    Detects PII, API keys, secrets, and other sensitive data in
    responses. Optionally redacts the sensitive portions.

    Optimisation strategy:
    - ``redact=True``  — sequential per-pattern ``subn()`` (required for
      per-category replacement strings).
    - ``redact=False`` — single-pass scan via ``_REDACTABLE_COMBINED``
      alternation regex, turning O(n × m) into O(n).

    Args:
        content: The LLM response text to check.
        redact: If True, redact flagged content. If False, just flag it.

    Returns:
        ContentFilterResult with flagged status and sanitized content.
    """
    if not content or not content.strip():
        return ContentFilterResult(flagged=False, sanitized_content=content or "")

    categories: set[str] = set()
    result = content

    if redact:
        # Redaction must run per-pattern for proper replacement text.
        # Pattern order matters: _API_KEY_PATTERNS first (most specific),
        # then _SECRET_PATTERNS, then _PII_PATTERNS.
        for pattern, category in _REDACTABLE_ALL_PATTERNS:
            new_result, count = pattern.subn(f"[REDACTED_{category.upper()}]", result)
            if count > 0:
                categories.add(category)
                result = new_result
    else:
        # Detection-only: single-pass scan via combined alternation regex.
        for m in _REDACTABLE_COMBINED.finditer(result):
            if m.lastgroup:
                idx_str = m.lastgroup.lstrip("_r")
                idx = int(idx_str) if idx_str.isdigit() else -1
                if 0 <= idx < len(_REDACTABLE_NAMES):
                    categories.add(_REDACTABLE_NAMES[idx])

    # Detection-only scan for sensitive file extensions (no redaction needed)
    for pattern, category in _SENSITIVE_FILE_PATTERNS:
        if pattern.search(result):
            categories.add(category)

    if categories:
        log.warning(
            "Response content filter flagged %d categories: %s",
            len(categories),
            sorted(categories),
            extra={"filter_categories": sorted(categories)},
        )

    return ContentFilterResult(
        flagged=bool(categories),
        categories=sorted(categories),
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
