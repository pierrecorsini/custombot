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
# ==============================================================================

.PHONY: requirements requirements-lock requirements-all check-deps check-config-example

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
