<!-- Context: project/concepts/media-output | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Media Output (TTS + PDF)

**Core Idea**: Skill-based media generation using callback injection. Two skills — `SendVoiceNote` (edge-tts for free TTS) and `GeneratePDFReport` (markdown → HTML → PDF via xhtml2pdf) — generate files in workspace temp dirs and send them via a `send_media` callback threaded from the channel through the bot and ToolExecutor to the skill.

**Source**: `.tmp/sessions/2026-04-12-media-output/context.md` (archived 2026-04-16)

---

## Key Points

- **Skill-based trigger**: Only produces media when user explicitly asks (LLM calls tool)
- **edge-tts**: Free TTS via Microsoft Edge — no API key, no cost
- **xhtml2pdf**: Pure Python HTML→PDF — no system dependencies
- **Callback injection**: `send_media` callback threaded through ToolExecutor (Option 2c pattern)
- **Temp file cleanup**: Files generated in workspace temp dir, cleaned up after sending

---

## Architecture: Callback Bridge

```
Channel (send_audio, send_document)
     │
     ▼
Bot._process_tool_calls() — creates callback from channel
     │
     ▼
ToolExecutor — accepts and passes send_media callback
     │
     ▼
Skill.execute(send_media=callback) — generates file, calls callback
```

---

## Components

| Component | Responsibility |
|-----------|---------------|
| `BaseChannel` | Abstract `send_audio()` + `send_document()` |
| `WhatsAppChannel` | Implements via neonize (sync via `asyncio.to_thread()`) |
| `CLI Channel` | Working stub for local testing |
| `SendVoiceNote` skill | edge-tts → audio file → `send_media(audio_path)` |
| `GeneratePDFReport` skill | markdown → HTML (styled) → PDF → `send_media(pdf_path)` |

---

## Dependencies

| Library | Purpose | Install |
|---------|---------|---------|
| `edge-tts` | Free TTS via Microsoft Edge | `pip install edge-tts` |
| `xhtml2pdf` | HTML/CSS to PDF (pure Python) | `pip install xhtml2pdf` |
| `markdown` | Markdown to HTML conversion | `pip install markdown` |

---

## Constraints

- neonize sync client used via `asyncio.to_thread()` (same pattern as `send_message`)
- Stealth delays should account for upload time
- FFmpeg needed by neonize for audio duration detection
- Existing skills untouched (no return type changes)

---

## Codebase

- `src/channels/base.py` — `send_audio()`, `send_document()` abstract methods
- `src/channels/whatsapp.py` — Neonize media sending implementation
- `src/core/tool_executor.py` — `send_media` callback passthrough
- `src/bot.py` — Callback creation in `_process_tool_calls()`
- `skills/builtin/` — `SendVoiceNote`, `GeneratePDFReport` skills

## Related

- `concepts/skills-system.md` — How skills are registered and executed
- `concepts/react-loop.md` — Where tool calls happen in the pipeline
