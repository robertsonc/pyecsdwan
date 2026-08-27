VENV ?= .venv
PY := $(VENV)/bin/python
UV := $(shell command -v uv 2>/dev/null)

.PHONY: install venv check lint type test build smoke clean

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
	$(VENV)/bin/ruff check src tests tools contrib

type:
	$(VENV)/bin/mypy

test:
	$(VENV)/bin/pytest

# The local gate: ruff + mypy + pytest, all green or bust. CI runs exactly
# these three steps on 3.10/3.11/3.12 (.github/workflows/ci.yml), plus the
# wheel-install job that `smoke` mirrors below.
check: lint type test

build:
	rm -rf dist
	uv build --out-dir dist

# What `check` cannot see: a file that exists in the repository and never
# reaches the wheel. Installs the built artifact into a throwaway environment
# and exercises the CLI from outside the source tree — the vendored API
# baselines in particular, whose absence `show coverage` reports as an empty
# universe rather than as an error.
smoke: build
	rm -rf .smoke-env
	uv venv .smoke-env
	uv pip install --python .smoke-env/bin/python dist/*.whl
	cd /tmp && $(CURDIR)/.smoke-env/bin/ec-cli --help >/dev/null
	cd /tmp && $(CURDIR)/.smoke-env/bin/python -c "\
from pyecsdwan import specs; \
n = len(list(specs.iter_endpoints())); \
assert specs.specs_dir() is not None, 'wheel ships no API baselines'; \
assert n == 1833, f'expected 1833 endpoints, got {n}'; \
assert specs.payload_examples(), 'payload examples missing from the wheel'; \
print(f'smoke ok: {n} endpoints')"
	cd /tmp && $(CURDIR)/.smoke-env/bin/ec-cli show coverage | grep -q "of 1833 endpoints"
	@echo "smoke ok: wheel installs and reports the full endpoint universe"

clean:
	rm -rf $(VENV) .smoke-env ec-cli build dist *.egg-info src/*.egg-info
