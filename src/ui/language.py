"""
src/ui/language.py — Language detection and response language tracking.

Detects the user's language from message text using Unicode character
range analysis. No external dependencies required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Unicode range patterns → ISO 639-1 codes ────────────────────────────────

_LANG_RULES: list[tuple[re.Pattern[str], str]] = [
    # CJK Unified Ideographs (Chinese characters)
    (re.compile(r"[\u4e00-\u9fff]"), "zh"),
    # Hiragana + Katakana (Japanese)
    (re.compile(r"[\u3040-\u309f\u30a0-\u30ff]"), "ja"),
    # Hangul Syllables (Korean)
    (re.compile(r"[\uac00-\ud7af]"), "ko"),
    # Cyrillic (Russian, Ukrainian, etc.)
    (re.compile(r"[\u0400-\u04ff]"), "ru"),
    # Arabic
    (re.compile(r"[\u0600-\u06ff]"), "ar"),
    # Devanagari (Hindi, etc.)
    (re.compile(r"[\u0900-\u097f]"), "hi"),
    # Thai
    (re.compile(r"[\u0e00-\u0e7f]"), "th"),
    # Greek
    (re.compile(r"[\u0370-\u03ff]"), "el"),
    # Hebrew
    (re.compile(r"[\u0590-\u05ff]"), "he"),
]

# Latin-1 supplement + Basic Latin → treated as English (default for Western scripts)
_LATIN_RE = re.compile(r"[a-zA-Z\u00c0-\u024f]")

# Common short-word heuristics for Latin-script language disambiguation.
_LATIN_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(el|la|los|las|que|por|con|una)\b", re.I), "es"),
    (re.compile(r"\b(le|de|des|une|est|que|pour|dans)\b", re.I), "fr"),
    (re.compile(r"\b(der|die|das|und|ist|ein|mit)\b", re.I), "de"),
    (re.compile(r"\b(il|che|per|una|con|del|della)\b", re.I), "it"),
    (re.compile(r"\b(o|a|e|de|em|um|uma|para)\b", re.I), "pt"),
]


def detect_language(text: str, default: str = "en") -> str:
    """Return ISO 639-1 language code for *text*.

    Scans for non-Latin scripts first (CJK, Arabic, Cyrillic, etc.),
    then falls back to Latin-script word heuristics. Returns *default*
    when no strong signal is found.
    """
    if not text:
        return default

    # Check non-Latin scripts — high confidence.
    for pattern, lang in _LANG_RULES:
        if pattern.search(text):
            return lang

    # Disambiguate Latin-script languages via common words.
    if _LATIN_RE.search(text):
        lower = text.lower()
        best_lang: str | None = None
        best_count = 0
        for pattern, lang in _LATIN_HINTS:
            count = len(pattern.findall(lower))
            if count > best_count:
                best_count = count
                best_lang = lang
        if best_count >= 2:
            return best_lang  # type: ignore[return-value]

    return default


# ── Per-chat language tracking ───────────────────────────────────────────────


@dataclass
class LanguageDetector:
    """Track detected language per chat for system-prompt injection.

    Configuration:
        default_language: Fallback when detection is disabled or text is empty.
        auto_detect_language: Whether to run detection on incoming messages.
    """

    default_language: str = "en"
    auto_detect_language: bool = True
    _chat_languages: dict[str, str] = field(default_factory=dict)

    def detect(self, chat_id: str, text: str) -> str:
        """Detect language and store it for *chat_id*. Returns ISO 639-1 code."""
        if not self.auto_detect_language:
            return self._chat_languages.get(chat_id, self.default_language)

        lang = detect_language(text, default=self.default_language)
        self._chat_languages[chat_id] = lang
        return lang

    def get_language(self, chat_id: str) -> str:
        """Return the last detected language for *chat_id*."""
        return self._chat_languages.get(chat_id, self.default_language)

    def system_prompt_instruction(self, chat_id: str) -> str:
        """Return language instruction to append to the system prompt."""
        lang = self.get_language(chat_id)
        if lang == self.default_language and not self._chat_languages.get(chat_id):
            return ""
        return f"\n\nRespond in the user's detected language ({lang})."

    def clear_chat(self, chat_id: str) -> None:
        """Remove stored language for *chat_id*."""
        self._chat_languages.pop(chat_id, None)
