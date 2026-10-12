# Full preflight remains `verify`; `verify-story` uses the story CI selector.
# It is preflight evidence only: it never authenticates, publishes,
# changes GitHub, installs packages or deploys, and a pass does not authorize a
# merge. Only the exact-head server check and another account's approval do.
#
# PYTHON defaults to this checkout's .venv when one exists, else python3.
# Override it with an interpreter that has requirements-dev.txt installed:
#   make verify PYTHON=/path/to/venv/bin/python

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
LINT_PATHS := scripts hooks tests integrations
# Literal Factory consumer policy, read as data (never sourced) by the selector.
# Shared/sensitive paths override these roots; unknown source trees require full tests.
TEST_SOURCE_ROOTS := scripts tests integrations

.PHONY: verify verify-story verify-release prerequisites lint test test-story

verify: prerequisites lint test
	@echo "verify: lint and tests passed at $$(git rev-parse HEAD 2>/dev/null || echo unknown-revision)"

# Compatibility: verify remains the full preflight; stories opt into actual-diff selection.
verify-story: prerequisites lint test-story

# Release candidates run the entire suite; release-specific migration rehearsals are separate.
verify-release: verify

prerequisites:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
		|| { echo "verify: $(PYTHON) is missing or older than Python 3.11" >&2; exit 1; }
	@$(PYTHON) -c 'import pytest, ruff, yaml' \
		|| { echo "verify: $(PYTHON) lacks requirements-dev.txt; install it into a venv and pass PYTHON=" >&2; exit 1; }

lint: prerequisites
	$(PYTHON) -m ruff check $(LINT_PATHS)

test: prerequisites
	$(PYTHON) -m pytest -q

test-story: prerequisites
	$(PYTHON) scripts/test_scope.py --run
