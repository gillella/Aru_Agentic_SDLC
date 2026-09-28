# Local preflight with the same lint and test scope as the `aru-governed-pr`
# workflow. It is preflight evidence only: it never authenticates, publishes,
# changes GitHub, installs packages or deploys, and a pass does not authorize a
# merge. Only the exact-head server check and another account's approval do.
#
# PYTHON defaults to this checkout's .venv when one exists, else python3.
# Override it with an interpreter that has requirements-dev.txt installed:
#   make verify PYTHON=/path/to/venv/bin/python

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
LINT_PATHS := scripts hooks tests integrations

.PHONY: verify prerequisites lint test

verify: prerequisites lint test
	@echo "verify: lint and tests passed at $$(git rev-parse HEAD 2>/dev/null || echo unknown-revision)"

prerequisites:
	@$(PYTHON) -c 'import sys; assert sys.version_info >= (3, 11), sys.version' \
		|| { echo "verify: $(PYTHON) is missing or older than Python 3.11" >&2; exit 1; }
	@$(PYTHON) -c 'import pytest, ruff, yaml' \
		|| { echo "verify: $(PYTHON) lacks requirements-dev.txt; install it into a venv and pass PYTHON=" >&2; exit 1; }

lint: prerequisites
	$(PYTHON) -m ruff check $(LINT_PATHS)

test: prerequisites
	$(PYTHON) -m pytest -q
