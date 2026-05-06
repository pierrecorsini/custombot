<!-- Context: project-intelligence/errors/bug-fixes | Priority: high | Version: 6.0 | Updated: 2026-05-06 -->

# Bug Fixes Applied

> Record of bugs fixed in the codebase — patterns to watch for in future.

## 2026-05-05 Fixes

### Fix 16: DB flush fails at shutdown (asyncio executor shutdown)

- **File**: `src/db/db.py`, `close()` method (line ~556)
- **Error**: `RuntimeError: cannot schedule new futures after shutdown`
- **Root cause**: `asyncio.to_thread` fails when loop executor already shut down during graceful shutdown
- **Fix**: Add synchronous fallback directly in `close()` before trying async path
- **Pattern to watch**: During shutdown, `asyncio.to_thread()` may fail — always have a sync fallback for critical cleanup

### Fix 17: Executor join deadlock at shutdown

- **File**: `src/lifecycle.py`, step 7 executor shutdown (line ~399)
- **Error**: `cannot join current thread`
- **Root cause**: `executor.shutdown(wait=True)` can deadlock when called from executor's own thread
- **Fix**: Add `RuntimeError` catch for "cannot join current thread" pattern
- **Pattern to watch**: Never call `executor.shutdown(wait=True)` from a thread owned by that executor

### Fix 18: Corrupt JSONL last lines never auto-repaired

- **File**: `src/workspace_integrity.py`, `_spot_check_jsonl()` (line ~85)
- **Error**: Corrupt last lines detected every startup but never repaired
- **Root cause**: Detection-only logic — no repair path existed
- **Fix**: Truncate corrupt last line when detected during spot check
- **Pattern to watch**: Detection without repair just adds noise — always pair detection with auto-repair for data integrity

### Fix 19: Dependency checker hyphen vs underscore name mismatch

- **File**: `src/dependency_check.py`
- **Error**: Installed packages not detected by dependency checker
- **Root cause**: Python normalizes package names (hyphens→underscores) but checker didn't normalize
- **Fix**: Normalize package names in `_pip_installed_versions()` using `replace("-", "_").lower()`
- **Pattern to watch**: Python packaging uses both hyphens and underscores — always normalize when comparing package names

### Fix 20: Embedding API missing encoding_format parameter

- **File**: `src/vector_memory/__init__.py`
- **Error**: Embedding API calls fail or return wrong format
- **Root cause**: Missing `encoding_format="float"` parameter in embedding API calls
- **Fix**: Add `encoding_format="float"` to all embedding API call sites
- **Pattern to watch**: OpenAI-compatible providers require explicit encoding format — always specify it

### Fix 21: Config schema missing runtime fields

- **File**: `src/config/config_schema_defs.py`
- **Error**: Config validation rejects valid runtime config fields (stream_response, middleware, max_thread_pool_workers)
- **Root cause**: Schema definitions not updated when new fields were added to runtime config
- **Fix**: Add missing fields to schema definitions
- **Pattern to watch**: When adding config fields, update both `config_schema_defs.py` AND `config.example.json`

### Fix 22: sender_id AttributeError in message processing (212 occurrences)

- **File**: `src/bot/`
- **Error**: `AttributeError: sender_id` — 212 occurrences in runtime logs
- **Root cause**: IncomingMessage attribute name mismatch — code expected `sender_id` but the field uses a different name
- **Fix**: Align attribute access with actual IncomingMessage field name
- **Pattern to watch**: When accessing dataclass/protocol fields, verify the actual field name matches expected name

### Fix 23: Embedding probe fails for non-OpenAI providers

- **File**: `src/diagnose.py`
- **Error**: `check_embedding_model()` assumes OpenAI-specific API response format
- **Root cause**: Probe only tested against OpenAI, not OpenRouter or other providers
- **Fix**: Handle non-OpenAI provider response formats in embedding probe check
- **Pattern to watch**: Diagnostic probes must support all configured providers, not just default

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
| `RuntimeError: cannot schedule new futures` | Executor shut down during cleanup | Add synchronous fallback for shutdown paths |
| `cannot join current thread` | Executor shutdown from own thread | Catch RuntimeError, use non-blocking shutdown |
| Corrupt data detected every startup | Detection without repair logic | Pair detection with auto-repair |
| Package name mismatch | Hyphens vs underscores in Python | Normalize with `replace("-", "_").lower()` |
| Embedding API format error | Missing encoding_format param | Always specify `encoding_format="float"` |
| Config validation rejects valid fields | Schema not updated with new fields | Sync schema + example config on field additions |
| `AttributeError` on dataclass field | Field name mismatch | Verify actual field name matches usage |

## Codebase References

- `src/llm.py` — LLM client (Fix 1)
- `src/bot.py` — Bot orchestrator (Fix 2)
- `src/skills/builtin/media.py` — Media skill with OGG conversion (Fix 8)
- `src/channels/neonize_backend.py` — WhatsApp backend with PTT fields (Fix 9)
- `src/channels/whatsapp.py` — WhatsApp channel with timestamp normalization (Fix 10)
- `src/db/db.py` — Database close with sync fallback (Fix 16)
- `src/lifecycle.py` — Executor shutdown handling (Fix 17)
- `src/workspace_integrity.py` — JSONL auto-repair (Fix 18)
- `src/dependency_check.py` — Package name normalization (Fix 19)
- `src/vector_memory/__init__.py` — Embedding encoding format (Fix 20)
- `src/config/config_schema_defs.py` — Config schema fields (Fix 21)
- `src/diagnose.py` — Multi-provider embedding probe (Fix 23)

## Harvested From

- Session snapshots: `ses_212de4615ffemFIxfxagng93na.json`, `ses_212e5748effesxiUVNf2tTCFkL.json` (2026-05-04)
- `.tmp/sessions/2026-05-05-fix-diagnostic-errors/context.md` — Fixes 19-23
- `.tmp/sessions/2026-05-05-log-error-fixes/context.md` — Fixes 16-18

## Related Files

- `errors/known-issues.md` — Current open issues
- `lookup/completed-sessions.md` — Sessions where fixes were applied
