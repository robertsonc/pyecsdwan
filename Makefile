VENV ?= .venv
PY := $(VENV)/bin/python
UV := $(shell command -v uv 2>/dev/null)

.PHONY: install venv check lint type test clean

venv:
ifdef UV
	@test -d $(VENV) || uv venv $(VENV)
	uv pip install --python $(PY) -e '.[dev]'
else
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PY) -m pip install -e '.[dev]'
endif

# Repo-local install: venv + ./ec-cli symlink at repo root. No sudo anywhere.
install: venv
	ln -sf $(VENV)/bin/ec-cli ec-cli
	@echo "Installed. Run ./ec-cli --help"

lint:
	$(VENV)/bin/ruff check src tests tools

type:
	$(VENV)/bin/mypy

test:
	$(VENV)/bin/pytest

# The local gate (this repo has no CI): ruff + mypy + pytest, all green or bust.
check: lint type test

clean:
	rm -rf $(VENV) ec-cli build dist *.egg-info src/*.egg-info
