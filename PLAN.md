# PLAN.md — Improvement Plan

_Round 4 — Senior technical review (2026-05-02). Previous rounds 1-3 completed 45/60 items._
_Remaining 15 items from Round 3 tracked in `.opencode/context/project/lookup/plan-progress.md`._

---

## Architecture & Refactoring

- [x] Consolidate `requirements-dev.txt` into `pyproject.toml` `[project.optional-dependencies.dev]` — the two files have drifted (e.g. `pytest==9.0.3` in txt vs `pytest>=8.0.0` in toml) causing CI/local version mismatches. Remove requirements-dev.txt after migration.
- [x] Reduce `perform_shutdown()` parameter count from 12 positional args to a `ShutdownContext` dataclass — the current signature is a maintenance burden and every new component requires updating all callers.
- [x] Make `_build_bot()` in `src/builder.py` a public API (rename to `build_bot`) — the leading underscore suggests it's private, but it's called across module boundaries from `src/core/startup.py`.
- [x] Extract `src/config/config.py` (785 lines) into separate modules — split dataclass definitions (`config_schema_defs.py`), load/save logic (`config_loader.py`), and validation helpers (`config_validation.py`). The current file mixes data model, I/O, logging, and validation concerns.
- [x] Move the misplaced `from typing import Callable, Awaitable` import on line 29 of `src/llm.py` up to the main imports block for consistency with the rest of the codebase.

## Performance & Scalability

- [x] Add a bounded concurrency semaphore to `Application._on_message()` — under load, unlimited concurrent message processing can exhaust memory and LLM rate limits. A configurable `max_concurrent_messages` semaphore (default 10) would cap resource usage without blocking the event loop.
- [x] Close the `ThreadPoolExecutor` with `wait=True` and a timeout in `lifecycle.py` step 6 — `executor.shutdown(wait=False)` can orphan submitted work (e.g. pending DB writes, vector memory batches) leading to data loss on crash. Use `wait=True` with a short timeout instead.
- [x] Detect embedding model changes across restarts in `VectorMemory` — store the embedding model name in a metadata table on first write, and warn loudly (or offer re-indexing) when the configured model changes, since existing vectors become silently incompatible.
- [x] Add connection pooling abstraction for the three SQLite databases (main `.data/`, `vector_memory.db`, `projects.db`) — each creates its own connection independently. A shared `ConnectionPool` would reduce file handle usage and enable WAL-mode consistency across databases.

## Error Handling & Resilience

- [x] Fix silent error swallowing in `config.py` `_from_dict()` — when `data` is not a dict, it returns `cls()` (default-constructed) with no warning. This masks malformed config sections silently. Log a warning and raise `ConfigurationError` instead.
- [x] Make `_load_pending()` error handling in `message_queue.py` consistent — the method uses `log_errors=True` during normal load but `log_errors=False` during repair. Both paths should use the same logging level to ensure corruption is equally visible.
- [x] Add TOCTOU-safe workspace seeding in `Memory.ensure_workspace()` — the `if not agents_path.exists()` check followed by write is racy across concurrent coroutines. The tmp→rename pattern helps, but the initial exists check should use an atomic `os.O_EXCL` open or a lock.
- [x] Guard against in-place mutation of shared task dicts in `scheduler.py` `_execute_task()` — the method mutates `task["last_result"]`, `task["last_run"]` directly on the shared `_tasks` dict while the scheduler loop iterates over it. Snapshot the task or use a copy-on-write pattern.

## Testing & Quality

- [x] Add `__all__` exports to all public modules (currently only `exceptions.py` and `llm.py` define `__all__`) — makes the public API explicit, helps prevent accidental internal imports, and enables `from src.module import *` to work correctly.
- [x] Consolidate duplicate `test_routing.py` — exists in both `tests/` root and `tests/unit/`. This can cause double-discovery and conflicting test results. Remove the root-level file and keep the unit/ version.
- [ ] Add integration test for config hot-reload via `ConfigWatcher` — verify that changing a config value on disk triggers the callback with the new value, and that malformed JSON doesn't crash the watcher loop.
- [ ] Add property-based test for `_from_dict()` roundtrip in `config.py` — use hypothesis to generate random Config dicts, roundtrip through `_from_dict` → `asdict`, and verify equality. Catches missing field mappings early.
- [ ] Add a `conftest.py` fixture for a fully-wired `Bot` instance with mocked LLM, DB, and Memory — currently each test file constructs its own partial mock. A shared fixture reduces duplication and ensures consistent test isolation.

## Security

- [ ] Redact secrets in `Config.__repr__()` — while `_redact_secrets()` exists for logging, calling `repr(config)` directly (e.g. in error traces or debugger) leaks the API key via `LLMConfig.__repr__` which shows `api_key='sk-...'`. Override `Config.__repr__` to use redaction.
- [ ] Add supply-chain pinning to `Dockerfile` — pin the base image by digest (`python:3.11.12-slim-bookworm@sha256:...`) instead of just tag, and add `pip install --require-hashes` support for production builds.
- [ ] Validate `IncomingMessage` fields before use in `Bot.handle_message()` — currently only `msg.text` is checked for emptiness, but `msg.message_id`, `msg.chat_id`, and `msg.sender_id` are used without validation. Add basic format checks to prevent injection through crafted IDs.

## DevOps & CI

- [ ] Add `pyproject.toml` target for `requirements.txt` generation — currently `requirements.txt` duplicates dependencies from `pyproject.toml`. Use `pip-compile` (pip-tools) to generate `requirements.txt` from `pyproject.toml` as the single source of truth.
- [ ] Add pre-commit hook to run `ruff check --fix` and `ruff format` — the `.pre-commit-config.yaml` exists but should include ruff for consistent local enforcement matching CI.
- [ ] Add `--strict` mode to `mypy` CI step for `src/` (non-blocking initially) — currently `disallow_untyped_defs` is False. Incrementally enabling strict checks on new files would improve type safety without breaking existing code.
- [ ] Pin `neonize` and `sqlite-vec` versions in `requirements.txt` and add a `pip-audit` CI step — these native dependencies have frequent breaking changes and aren't covered by Dependabot (which only handles GitHub Actions currently).
- [ ] Add smoke test to Dockerfile build in CI — verify the built image can start and respond to `--help` without crashing, catching dependency or import errors before deployment.