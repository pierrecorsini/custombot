# ==============================================================================
# Makefile — dependency management for CustomBot
#
# pyproject.toml is the single source of truth for all dependencies.
# requirements.txt and requirements-lock.txt are auto-generated; do not edit.
#
# Targets:
#   requirements       — regenerate requirements.txt from pyproject.toml
#   requirements-lock  — regenerate requirements-lock.txt (with hashes)
#   requirements-all   — regenerate both files
#   check-deps         — verify generated files are in sync with pyproject.toml
#   health             — single-command pre-push health check (lint, typecheck, tests, audit)
#   lint-fix           — auto-fix lint issues and format code in one pass
#   typecheck-strict   — preview full strict mypy results (non-blocking, informational)
#   test-quick         — run tests excluding @pytest.mark.slow for fast feedback
#   test-unit          — run only tests/unit/ directory
#   test-integration   — run integration and e2e tests
#   test-all           — run the full test suite (all markers)
#   coverage-push      — run tests with coverage and ratchet .coverage-floor upward
#   test-coverage      — run tests with coverage and generate HTML report
#   diagnose           — run config, connectivity, and workspace integrity checks
#   test-categories    — run unit, integration, and e2e tests separately with summary
#   benchmark          — run performance benchmarks from tests/unit/bench_*.py
#   profile            — interactive performance profiling with pyinstrument
#   mutation-test     — run mutation testing on core modules (non-blocking, optional)
#   load-test         — run load testing harness simulating 100+ concurrent chats
# ==============================================================================

.PHONY: requirements requirements-lock requirements-all check-deps check-config-example health lint-fix typecheck-strict test-quick test-unit test-integration test-all coverage-push test-coverage diagnose test-categories benchmark profile mutation-test load-test

PYTHON    ?= python
PIP-COMPILE := $(PYTHON) -m piptools compile

# ---------------------------------------------------------------------------
# Regenerate requirements.txt from pyproject.toml (single source of truth)
# ---------------------------------------------------------------------------
requirements:
	$(PIP-COMPILE) --strip-extras --output-file=requirements.txt pyproject.toml

# ---------------------------------------------------------------------------
# Regenerate requirements-lock.txt with hashes for supply-chain integrity
# ---------------------------------------------------------------------------
requirements-lock: requirements
	$(PIP-COMPILE) --generate-hashes --output-file=requirements-lock.txt requirements.txt

# ---------------------------------------------------------------------------
# Regenerate everything
# ---------------------------------------------------------------------------
requirements-all: requirements-lock

# ---------------------------------------------------------------------------
# CI gate: fail if generated files are out of sync
# ---------------------------------------------------------------------------
check-deps:
	@$(PIP-COMPILE) --strip-extras --output-file=requirements.txt pyproject.toml --quiet
	@$(PIP-COMPILE) --generate-hashes --output-file=requirements-lock.txt requirements.txt --quiet
	@git diff --exit-code requirements.txt requirements-lock.txt \
		|| (echo "ERROR: requirements files are out of sync with pyproject.toml. Run 'make requirements-all' to regenerate." && exit 1)

# ---------------------------------------------------------------------------
# Pre-push health check — runs lint, type-check, test collection, and security
# audit in sequence.  Fails fast on the first error.
# ---------------------------------------------------------------------------
health:
	@echo "=== ruff check ===" && ruff check src tests main.py \
		&& echo "=== mypy ===" && mypy src main.py \
		&& echo "=== pytest --co (test collection) ===" && pytest --co -q \
		&& echo "=== pip-audit ===" && pip-audit \
		&& echo "" && echo "✅ All health checks passed"

# ---------------------------------------------------------------------------
# Auto-fix lint issues and format code in one pass
# ---------------------------------------------------------------------------
lint-fix:
	@echo "=== ruff check --fix ===" && ruff check --fix src tests main.py \
		&& echo "=== ruff format ===" && ruff format src tests main.py \
		&& echo "" && echo "✅ Lint fixes and formatting applied"

# ---------------------------------------------------------------------------
# Strict type-check preview — runs mypy --strict on src/ (informational only,
# always exits 0 so it can be used alongside other targets without blocking)
# ---------------------------------------------------------------------------
typecheck-strict:
	@echo "=== mypy --strict src/ (informational) ===" \
		&& (mypy --strict src/ || true) \
		&& echo "" && echo "ℹ️  Strict type-check preview complete (errors are non-blocking)"

# ---------------------------------------------------------------------------
# Fast test feedback — runs all tests excluding @pytest.mark.slow.
# Use -m "not slow" to skip integration/e2e tests marked with the slow marker.
# Fails fast on first error (-x) with quiet output (-q).
# ---------------------------------------------------------------------------
test-quick:
	@echo "=== pytest -m 'not slow' ===" && $(PYTHON) -m pytest tests/ -m "not slow" -x -q \
		&& echo "" && echo "✅ Quick tests passed (slow tests skipped)"

# ---------------------------------------------------------------------------
# Unit tests only — runs tests/unit/ directory for fastest possible feedback.
# ---------------------------------------------------------------------------
test-unit:
	@echo "=== pytest tests/unit/ ===" && pytest tests/unit/ \
		&& echo "" && echo "✅ Unit tests passed"

# ---------------------------------------------------------------------------
# Integration + e2e tests — for thorough validation before merging.
# Includes tests marked with @pytest.mark.slow.
# ---------------------------------------------------------------------------
test-integration:
	@echo "=== pytest tests/integration/ tests/e2e/ ===" && pytest tests/integration/ tests/e2e/ \
		&& echo "" && echo "✅ Integration and e2e tests passed"

# ---------------------------------------------------------------------------
# Full test suite — runs everything including slow/integration/e2e tests.
# Equivalent to pytest tests/ with no marker filtering.
# ---------------------------------------------------------------------------
test-all:
	@echo "=== pytest tests/ (full suite) ===" && $(PYTHON) -m pytest tests/ \
		&& echo "" && echo "✅ All tests passed"

# ---------------------------------------------------------------------------
# Coverage floor ratchet — runs the full test suite with coverage, then
# checks the result against .coverage-floor.  If coverage has improved,
# .coverage-floor is updated automatically so the baseline stays current
# without manual edits.
# ---------------------------------------------------------------------------
coverage-push:
	@echo "=== Running tests with coverage ===" \
		&& pytest --cov=src --cov=main --cov-report=xml:coverage.xml --cov-report=term-missing \
		&& echo "" && echo "=== Checking / updating coverage floor ===" \
		&& python scripts/check_coverage_floor.py --update \
		&& echo "" && echo "✅ Coverage floor check passed (floor updated if improved)"

# ---------------------------------------------------------------------------
# Coverage — run tests with coverage and generate HTML report.
# Output is written to .tmp/coverage-html/index.html.
# ---------------------------------------------------------------------------
test-coverage:
	@mkdir -p .tmp
	@echo "=== Running tests with coverage (HTML report) ===" \
		&& $(PYTHON) -m pytest --cov=src --cov=main --cov-report=html:.tmp/coverage-html \
		&& echo "" && echo "✅ HTML coverage report: .tmp/coverage-html/index.html"

# ---------------------------------------------------------------------------
# Diagnose — run config, connectivity, and workspace integrity checks
# ---------------------------------------------------------------------------
diagnose:
	@echo "=== Running diagnostics ===" && $(PYTHON) main.py diagnose \
		&& echo "" && echo "✅ Diagnostics passed"

# ---------------------------------------------------------------------------
# Test categories — run unit, integration, and e2e tests as separate pytest
# invocations with summary output, so developers can see which layer failed
# without scanning the full suite output.
# ---------------------------------------------------------------------------
test-categories:
	@echo "════════════════════════════════════════════════════" \
		&& echo "  UNIT TESTS" \
		&& echo "════════════════════════════════════════════════════" \
		&& pytest tests/unit/ -q \
		&& echo "" \
		&& echo "════════════════════════════════════════════════════" \
		&& echo "  INTEGRATION TESTS" \
		&& echo "════════════════════════════════════════════════════" \
		&& pytest tests/integration/ -q \
		&& echo "" \
		&& echo "════════════════════════════════════════════════════" \
		&& echo "  E2E TESTS" \
		&& echo "════════════════════════════════════════════════════" \
		&& pytest tests/e2e/ -q \
	&& echo "" \
	&& echo "✅ All test categories passed"

# ---------------------------------------------------------------------------
# Benchmark — run performance benchmarks with summary output.
# bench_regression.py uses pytest-benchmark (--benchmark-only);
# bench_serialization.py uses custom timing with print() output (-s).
# ---------------------------------------------------------------------------
benchmark:
	@echo "════════════════════════════════════════════════════" \
		&& echo "  BENCHMARK: Regression (pytest-benchmark)" \
		&& echo "════════════════════════════════════════════════════" \
		&& pytest tests/unit/bench_regression.py -v --benchmark-only \
		&& echo "" \
		&& echo "════════════════════════════════════════════════════" \
		&& echo "  BENCHMARK: Serialization (orjson vs msgpack)" \
		&& echo "════════════════════════════════════════════════════" \
		&& pytest tests/unit/bench_serialization.py -v -s \
		&& echo "" \
		&& echo "✅ All benchmarks passed"

# ---------------------------------------------------------------------------
# Profile — interactive performance profiling with pyinstrument.
# Generates an HTML call-tree report in .tmp/profile.html.
# Requires pyinstrument (pip install pyinstrument).
# ---------------------------------------------------------------------------
profile:
	@$(PYTHON) -c "import pyinstrument" 2>/dev/null \
		|| (echo "ERROR: pyinstrument not installed. Run: pip install pyinstrument" && exit 1)
	@mkdir -p .tmp
	@echo "════════════════════════════════════════════════════" \
		&& echo "  PYINSTRUMENT — Interactive Call-Tree Profiler" \
		&& echo "════════════════════════════════════════════════════" \
		&& echo "" \
		&& echo "Generate a profile report:" \
		&& echo "  python -m pyinstrument --html -o .tmp/profile.html main.py start" \
		&& echo "" \
		&& echo "Profile specific tests:" \
		&& echo "  python -m pyinstrument -m pytest tests/unit/test_bot.py -k test_xxx" \
		&& echo "" \
		&& echo "Programmatic usage in code:" \
		&& echo "  from pyinstrument import Profiler" \
		&& echo "  p = Profiler(); p.start()" \
		&& echo "  # ... code to profile ..." \
		&& echo "  p.stop(); p.write_html_report('.tmp/profile.html')" \
		&& echo "" \
		&& echo "✅ pyinstrument is installed and ready"

# ---------------------------------------------------------------------------
# Mutation testing — runs mutmut on core modules (non-blocking, optional).
# Requires mutmut (pip install mutmut).
#
# Configuration is in mutmut_config.py.  See that file for:
#   - paths_to_mutate: source modules to mutate
#   - test_command: pytest invocation for mutant validation
#   - pre_mutation / post_mutation hooks
#
# Run locally:
#   make mutation-test
#   mutmut results        # view summary
#   mutmut show <id>      # inspect a specific mutant
# ---------------------------------------------------------------------------
mutation-test:
	@echo "Running mutation testing (this may take a while)..."
	mutmut run --config-file mutmut_config.py
	@echo "Mutation testing complete. Run 'mutmut results' to see summary."

# ---------------------------------------------------------------------------
# Load testing — simulates 100+ concurrent chats and reports latency stats.
# Requires pytest (pip install ".[dev]").
#
# Configurable via environment variables:
#   CHAT_COUNT=100  MESSAGE_COUNT=3  python -m pytest tests/load/ -v
# ---------------------------------------------------------------------------
load-test:
	@echo "Running load testing (100+ concurrent chats)..."
	$(PYTHON) -m pytest tests/load/ -v -s
	@echo "" && echo "✅ Load testing complete"
