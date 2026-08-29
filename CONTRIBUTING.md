# Contributing to pyecsdwan

This file used to be the vendored `pyedgeconnect` SDK's, describing `black`,
`flake8` and PyCharm — none of which this project uses (#68). What follows is
this repository's actual workflow.

## The gate

```
make install     # venv + editable install + ./ec-cli symlink, no sudo
make check       # ruff + mypy --strict + pytest — all green or bust
```

`make check` is exactly what CI runs, on Python 3.10, 3.11 and 3.12
(`.github/workflows/ci.yml`), plus a wheel-install job that `make smoke`
mirrors locally. If `make check` is green and `make smoke` passes, CI will
agree.

- **`ruff`** — lint and import sorting, line length 100. `ruff format` is *not*
  part of the gate and has never been run over this repository; running it
  would produce a diff touching nearly every file. Don't.
- **`mypy --strict`** — the whole package. The vendored SDK, `examples/` and
  `docs/` are excluded (`pyproject.toml`), because they are reference material
  rather than product code.
- **`pytest`** — no network. Everything runs against the bundled mock
  Orchestrator (`pyecsdwan.mock.server`).

`make smoke` catches the one class of bug `make check` cannot see: a file that
exists in the repository and never reaches the wheel. It builds, installs into
a throwaway environment, and exercises the CLI from outside the source tree.

## What a change is expected to carry

**Prove the guard bites.** A test that passes against correct code has not
shown it would fail against broken code. Delete the guard, confirm a test
fails, restore it. Several tests in this suite exist because that sweep
reported a guard as unprotected — and at least three tests were found to be
asserting the wrong thing entirely by exactly this method.

**Derive claims, don't assert them.** Where a document says what the tool does,
prefer a test that *runs* the tool and checks the document against the result:
`tests/test_docs_examples.py` executes every README command,
`tests/test_tier_claims.py` plans a stub and checks the tier tables against
where it actually stops, `tests/test_retry.py` re-derives the mutating-GET
classification from the vendored specs. A claim nobody runs is a claim that
drifts.

**Verify the brief against the code.** Issue text and prior notes have been
wrong here more than once. Read the source, run it against the mock, and say so
in the change if the brief turns out to be inaccurate.

**Say why, not what.** Comments and commit messages carry the reasoning and the
counterexample considered. `git blame` recovers what changed; nothing recovers
why the obvious alternative was rejected.

## Adding a resource plugin

Read `src/pyecsdwan/contract.py` first — it is the frozen contract every
resource implements — then `docs/plugin-promotion.md` for the Tier 0 → 1 → 2
checklist. Two independent axes govern a resource:

- **Tier** — how carefully it was written; a code-review decision, capped at
  Tier 2. `ec-cli plugin promote <noun>` runs the machine-checkable boxes.
- **Evidence** — what anyone has seen it do on real gear
  (`docs/live-validation.md`, `ec-cli show coverage --evidence`). Nothing in
  this repository can raise a resource above `mock-verified`; that needs a
  fabric and a recorded version.

Never invent a payload. Where the spec is silent and no primary source has the
shape, the resource stays a stub with a TODO naming the missing data.

## Feature work

Larger changes go through the Spec Kit workflow in `.specify/`, with the
constitution at `.specify/memory/constitution.md` and feature specs under
`specs/`. The constitution is ratified and binding: intent separation, safety
truth, one grammar across interfaces, evidence-gated claims.

## Primary sources

`pyedgeconnect/` (the vendored upstream SDK), `examples/`, and
`src/pyecsdwan/_specs/` (the OpenAPI baselines and Postman payload examples)
are kept as the endpoint reference the plugins are built from — several
research notes in `docs/research/` cite them directly. They are excluded from
lint and type-checking; do not edit them to make a check pass.

## Reporting an unrecognised job shape

Since #64 the async-job poller allowlists success. If a real fabric returns a
terminal shape it does not recognise, the failure detail quotes the exact
`taskStatus` and `result`. That text belongs in `docs/research/job-shapes.md`
with the Orchestrator version it came from — that table only grows from
observation.
