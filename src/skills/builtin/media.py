"""
skills/builtin/media.py — Audio (TTS) and PDF report generation skills.

Two media output skills that generate files and send them via the
send_media callback injected through ToolExecutor:

  SendVoiceNote       — edge-tts text-to-speech → WhatsApp voice note
  GeneratePDFReport   — Markdown content → styled PDF document

Both skills receive an optional ``send_media`` async callback that lets
them deliver media directly to the channel without changing the skill
return type (still str). If the callback is unavailable (e.g. during
testing), the skill returns a descriptive message with the file path.
"""

from __future__ import annotations

import asyncio
import io
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

import markdown

from src.skills.base import BaseSkill, validate_input

log = logging.getLogger(__name__)

# Type alias matching channels.base.SendMediaCallback
SendMediaFn = Callable[[str, Path, str], Awaitable[None]]


def _get_send_media(kwargs: dict) -> Optional[SendMediaFn]:
    """Extract the send_media callback from kwargs if present."""
    return kwargs.get("send_media")


def _convert_to_ogg(source: Path) -> Path:
    """Convert an audio file to OGG/Opus format for WhatsApp voice notes.

    Args:
        source: Path to the source audio file (e.g. MP3).

    Returns:
        Path to the converted OGG file.

    Raises:
        RuntimeError: If ffmpeg is not installed or conversion fails.
    """
    ogg_path = source.with_suffix(".ogg")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-vn",
        str(ogg_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg is required for voice notes but not found on PATH. "
            "Install ffmpeg and try again."
        )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr.strip()}")
    source.unlink(missing_ok=True)
    return ogg_path


# ─────────────────────────────────────────────────────────────────────────────
# Voice Note Skill (edge-tts)
# ─────────────────────────────────────────────────────────────────────────────

# Common voices mapped by language for easy selection
_VOICE_MAP: Dict[str, str] = {
    "en": "en-US-EmmaMultilingualNeural",
    "es": "es-ES-ElviraNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "nl": "nl-NL-ColetteNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "ar": "ar-SA-ZariyahNeural",
    "hi": "hi-IN-SwaraNeural",
}


class SendVoiceNote(BaseSkill):
    """Convert text to speech and send as a WhatsApp voice note.

    Uses edge-tts (free, no API key) for high-quality TTS synthesis.
    The MP3 output is converted to OGG/Opus via ffmpeg before sending,
    since WhatsApp voice notes (PTT) only support the Opus codec.
    """

    name = "send_voice_note"
    description = (
        "Convert text to speech and send as a voice note. "
        "Use when the user explicitly asks for audio, voice, or to hear the response."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The text to convert to speech.",
            },
            "language": {
                "type": "string",
                "description": (
                    "Language code (e.g. 'en', 'es', 'fr'). Defaults to English if not specified."
                ),
            },
        },
        "required": ["text"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        text = kwargs["text"]
        language = kwargs.get("language", "en")
        send_media = _get_send_media(kwargs)

        # Resolve voice from language code
        voice = _VOICE_MAP.get(language.lower(), _VOICE_MAP["en"])

        # Generate unique filename in workspace temp dir
        temp_dir = workspace_dir / ".media"
        temp_dir.mkdir(exist_ok=True)
        mp3_path = temp_dir / f"voice_{uuid.uuid4().hex[:8]}.mp3"

        try:
            import edge_tts

            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(mp3_path))

            char_count = len(text)
            log.info(
                "Generated voice note: %s (%d chars, voice=%s)",
                mp3_path.name,
                char_count,
                voice,
            )

            # Send via callback if available
            if send_media:
                await send_media("audio", mp3_path, "")
                return f"Voice note sent ({char_count} characters, voice: {voice})"
            else:
                return f"Voice note generated at {audio_path} (no send_media callback)"

        except Exception as exc:
            log.error("Failed to generate voice note: %s", exc)
            # Clean up partial files
            if mp3_path.exists():
                mp3_path.unlink(missing_ok=True)
            return f"Failed to generate voice note: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# PDF Report Skill (markdown → HTML → PDF via xhtml2pdf)
# ─────────────────────────────────────────────────────────────────────────────

# Default CSS for styled PDF output
_PDF_STYLESHEET = """
<style>
    @page {
        size: A4;
        margin: 2cm;
    }
    body {
        font-family: Helvetica, Arial, sans-serif;
        font-size: 11pt;
        line-height: 1.5;
        color: #222;
    }
    h1 {
        font-size: 20pt;
        color: #1a1a2e;
        border-bottom: 2px solid #1a1a2e;
        padding-bottom: 6pt;
        margin-top: 0;
    }
    h2 {
        font-size: 16pt;
        color: #16213e;
        border-bottom: 1px solid #ccc;
        padding-bottom: 4pt;
        margin-top: 16pt;
    }
    h3 {
        font-size: 13pt;
        color: #0f3460;
        margin-top: 12pt;
    }
    table {
        border-collapse: collapse;
        width: 100%;
        margin: 10pt 0;
        font-size: 10pt;
    }
    th {
        background-color: #1a1a2e;
        color: white;
        padding: 6pt 8pt;
        text-align: left;
        font-weight: bold;
    }
    td {
        border: 1px solid #ddd;
        padding: 5pt 8pt;
    }
    tr:nth-child(even) {
        background-color: #f9f9f9;
    }
    code {
        font-family: Courier, monospace;
        background-color: #f4f4f4;
        padding: 2pt 4pt;
        border-radius: 3pt;
        font-size: 10pt;
    }
    pre {
        background-color: #f4f4f4;
        padding: 10pt;
        border-radius: 4pt;
        overflow-x: auto;
        font-size: 9pt;
        line-height: 1.4;
    }
    pre code {
        background-color: transparent;
        padding: 0;
    }
    blockquote {
        border-left: 4px solid #1a1a2e;
        margin: 10pt 0;
        padding: 6pt 12pt;
        background-color: #f9f9f9;
        color: #555;
    }
    hr {
        border: none;
        border-top: 1px solid #ddd;
        margin: 16pt 0;
    }
    ul, ol {
        margin: 6pt 0;
        padding-left: 20pt;
    }
    li {
        margin: 3pt 0;
    }
    strong {
        color: #1a1a2e;
    }
    em {
        color: #333;
    }
</style>
"""


class GeneratePDFReport(BaseSkill):
    """Generate a styled PDF report from Markdown content.

    Converts the provided Markdown text to HTML with proper formatting
    (headers, bold, tables, code blocks, lists, etc.) and renders it
    into a professionally styled PDF document using xhtml2pdf.
    """

    name = "generate_pdf_report"
    description = (
        "Generate a styled PDF report from text content. "
        "Use when the user explicitly asks for a PDF, a report file, "
        "or wants to download/save the response as a document."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Report title (shown as H1 heading and filename).",
            },
            "content": {
                "type": "string",
                "description": (
                    "Report body in Markdown format. "
                    "Supports headers, bold, italic, tables, code blocks, lists, etc."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Custom filename for the PDF (without extension). Defaults to the title."
                ),
            },
        },
        "required": ["title", "content"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        title = kwargs["title"]
        content = kwargs["content"]
        custom_filename = kwargs.get("filename", "")
        send_media = _get_send_media(kwargs)

        # Sanitize filename
        safe_name = custom_filename or title
        safe_name = "".join(c if c.isalnum() or c in (" ", "-", "_") else "_" for c in safe_name)
        safe_name = safe_name.strip()[:60] or "report"

        # Generate paths
        temp_dir = workspace_dir / ".media"
        temp_dir.mkdir(exist_ok=True)
        pdf_path = temp_dir / f"{safe_name}.pdf"

        try:
            # Convert Markdown to HTML with extensions for tables, code, etc.
            html_body = markdown.markdown(
                content,
                extensions=["tables", "fenced_code", "codehilite", "toc"],
                extension_configs={"codehilite": {"cssclass": "highlight"}},
            )

            # Assemble full HTML document
            html_doc = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>{title}</title>
    {_PDF_STYLESHEET}
</head>
<body>
    <h1>{title}</h1>
    {html_body}
</body>
</html>"""

            # Render PDF using xhtml2pdf
            from xhtml2pdf import pisa

            source = io.BytesIO(html_doc.encode("utf-8"))
            dest = io.BytesIO()

            pdf_result = pisa.pisaDocument(
                source,
                dest,
                encoding="utf-8",
                raise_exception=False,
            )

            if pdf_result.err:
                log.error(
                    "PDF generation had %d errors: %s",
                    pdf_result.err,
                    pdf_result.err,
                )
                return f"PDF generation encountered errors. Partial output may be unusable."

            # Write PDF to file
            pdf_path.write_bytes(dest.getvalue())

            log.info(
                "Generated PDF report: %s (%d bytes)",
                pdf_path.name,
                len(dest.getvalue()),
            )

            # Send via callback if available
            if send_media:
                await send_media("document", pdf_path, title)
                return f"PDF report '{title}' sent ({pdf_path.name})"
            else:
                return f"PDF report generated at {pdf_path} (no send_media callback)"

        except Exception as exc:
            log.error("Failed to generate PDF report: %s", exc)
            if pdf_path.exists():
                pdf_path.unlink(missing_ok=True)
            return f"Failed to generate PDF report: {exc}"
