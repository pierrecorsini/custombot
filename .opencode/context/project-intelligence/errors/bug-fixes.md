<!-- Context: project-intelligence/errors/bug-fixes | Priority: high | Version: 5.0 | Updated: 2026-05-04 -->

# Bug Fixes Applied

> Record of bugs fixed in the codebase — patterns to watch for in future.

## 2026-05-04 Fixes

### Fix 8: WhatsApp voice note sent as MP3 instead of OGG/Opus

- **File**: `src/skills/builtin/media.py`
- **Error**: Voice notes sent as MP3 files to WhatsApp — not playable as push-to-talk
- **Root cause**: `_convert_to_ogg()` function existed but was never called before `send_media()`
- **Fix**: Added `_convert_to_ogg(mp3_path)` call before `send_media()` in the media skill
- **Pattern to watch**: Format-conversion helpers must be verified in the call chain, not just exist

### Fix 9: neonize library missing PTT fields for voice notes

- **File**: `src/channels/neonize_backend.py`
- **Error**: WhatsApp voice notes missing streamingSidecar, waveform, and mimetype with codecs
- **Root cause**: neonize library doesn't populate PTT-specific AudioMessage fields
- **Fix**: New `_send_voice_note()` method that manually constructs AudioMessage with correct fields (streamingSidecar, waveform, mimetype with opus codecs)
- **Pattern to watch**: WhatsApp library abstractions may omit protocol-required fields — verify with actual client behavior

### Fix 10: WhatsApp timestamp milliseconds vs seconds mismatch

- **File**: `src/channels/whatsapp.py`
- **Error**: `_validate_timestamp` rejects valid timestamps (e.g. 1777630140000 exceeds max 4102444800.0)
- **Root cause**: WhatsApp backends return timestamps in milliseconds; validator expects seconds
- **Fix**: Normalize at WhatsApp channel boundary — if timestamp > 1e12, divide by 1000
- **Pattern to watch**: External APIs may use different epoch units — always normalize at the boundary layer

## 2026-05-02 Fixes

### Fix 3: Inline (?i) regex flags in security detection

- **File**: `src/security/prompt_injection.py`
- **Error**: Combined detection regex with inline `(?i)` flags caused incorrect pattern matching
- **Fix**: Strip inline `(?i)` flags from combined detection regex
- **Pattern to watch**: Python `re` inline flags in combined patterns can cause unexpected behavior

### Fix 4: Pattern lookup using lastindex vs lastgroup

- **File**: `src/security/prompt_injection.py`
- **Error**: `lastindex` returned wrong group index for named pattern lookup
- **Fix**: Use `lastgroup` instead of `lastindex` for pattern lookup
- **Pattern to watch**: Always use `lastgroup` for named group matching, not `lastindex`

### Fix 5: Regex channel patterns not evaluated in routing

- **File**: `src/routing.py`
- **Error**: Regex channel patterns in routing rules were not included in match evaluation
- **Fix**: Include regex channel patterns in match evaluation
- **Pattern to watch**: When adding new match dimensions, ensure all rule types participate in matching

### Fix 6: Empty API key rejected for private networks

- **File**: `src/llm.py`
- **Error**: Empty API key was rejected even for RFC 1918 private network addresses (local models)
- **Fix**: Allow empty API key when base_url resolves to a private network address
- **Pattern to watch**: Validation rules should account for local-only deployment scenarios

### Fix 7: Rate limiter monotonic timestamp docstring

- **File**: `src/rate_limiter.py`
- **Error**: `reset_at` docstring said "Unix timestamp" but implementation uses `time.monotonic()`
- **Fix**: Correct docstring from Unix to monotonic timestamp
- **Pattern to watch**: Docstrings for time values must specify whether Unix or monotonic

## 2026-03-23 Fixes

### Fix 1: LLM usage dict access

- **File**: `src/llm.py:121`
- **Error**: `'dict' object has no attribute 'prompt_tokens'`
- **Root cause**: LLM API returns usage as dict in some responses, but code expected object attribute access
- **Fix**: Added handling for both dict and object access patterns in usage response
- **Pattern to watch**: When consuming external API responses, always handle both dict and object access patterns

### Fix 2: Unawaited coroutine in bot

- **File**: `src/bot.py:363,366`
- **Error**: `RuntimeWarning: coroutine was never awaited`
- **Root cause**: `_process_tool_calls` is async but was called without `await`
- **Fix**: Added `await` to `_process_tool_calls` calls
- **Pattern to watch**: When calling async functions in async context, always use `await`. RuntimeWarning about unawaited coroutines is a signal.

## Diagnostic Pattern

When similar issues appear:

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `AttributeError` on API response | Response format varies (dict vs object) | Use `.get()` or `getattr()` with fallback |
| `RuntimeWarning: coroutine was never awaited` | Missing `await` on async call | Add `await` keyword |
| `'dict' object has no attribute 'X'` | API response shape mismatch | Handle both access patterns |
| Media sent in wrong format | Conversion helper not wired into call chain | Verify helper is called, not just defined |
| Timestamp validation rejects valid values | Epoch unit mismatch (ms vs s) | Normalize at integration boundary |

## Codebase References

- `src/llm.py` — LLM client (Fix 1)
- `src/bot.py` — Bot orchestrator (Fix 2)
- `src/skills/builtin/media.py` — Media skill with OGG conversion (Fix 8)
- `src/channels/neonize_backend.py` — WhatsApp backend with PTT fields (Fix 9)
- `src/channels/whatsapp.py` — WhatsApp channel with timestamp normalization (Fix 10)

## Harvested From

- Session snapshots: `ses_212de4615ffemFIxfxagng93na.json`, `ses_212e5748effesxiUVNf2tTCFkL.json` (2026-05-04)

## Related Files

- `errors/known-issues.md` — Current open issues
- `lookup/completed-sessions.md` — Sessions where fixes were applied
