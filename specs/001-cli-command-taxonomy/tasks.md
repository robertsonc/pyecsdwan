# Tasks: intent-separated CLI command taxonomy

**Feature:** `001-cli-command-taxonomy` · **Plan:** `./plan.md` · **Date:** 2026-08-27

Status lives in the linked issue, never here (`.specify/README.md`). An
unlinked task has not been started.

| # | Task | Depends on | Acceptance | Issue |
|---|---|---|---|---|
| ~~T0~~ | ~~Owner answers Q1 and Q2; grammar updated~~ | — | **Done** — grammar 0.2.0; both recorded in `spec.md` | — |
| ~~T1~~ | ~~Owner approves the grammar and the acceptance table~~ | T0 | **Done** — approved 2026-08-28; epic #70's migration gate open | #70 |
| ~~T2~~ | ~~CLI name/alias contract on `Resource`~~ | T1 | **Done** — `cli_name`/`cli_aliases`, per-scope index, reserved words and collisions refused at registration | #77 |
| T3 | User-facing nouns throughout parsing, completion, help, usage, errors, docs | T2 | **Partly done** — shell parsing, completion and errors carry nouns; `main.py` subcommand help/usage still to do | #77 |
| T4 | Offline command reference view (domain, scope, instances, mutability, support status) | T2 | Runs with no Orchestrator connection | #77 |
| T5 | Outcome classifier + renderer for the eleven outcomes, human and JSON | — | One test per outcome, both modes; `{}`/`None`/`""`/204/`[]` each intentional | #78 |
| T6 | Bounded timeout on every appliance read; every terminal path returns to the prompt | T5 | `show appliance S1-ecv-01 banners` always produces a visible result | #78 |
| T7 | Audit remaining appliance-scoped resources for the same silent path | T6 | Every kind exercised; findings filed | #78 |
| T8 | Parser: new grammar, scope nouns, nonterminal listing | T1, T3 | R1–R5, R10 tests green; nonterminals list next tokens | #74 |
| T9 | Compatibility aliases with stderr warnings; the hard-fail for the meaning-change form | T8 | Behavior-asserting tests, not acceptance tests; refusal names both replacements | #74 |
| T10 | BGP operational views — **spec** in `specs/002-appliance-operational-views/` | T1 | Source-verified; `routes` reported unsupported (no endpoint exists) | #72 |
| T11 | Golden UX tests derived from `grammar.md` §7 | T8, T10 | Every row of the worked-examples table is a test | #74 |
| T12 | Removal boundary decided (Q3) and documented in `--help` and the constitution record | T1 | Aliases carry an expiry | #74 |

## Sequencing notes

* **T0 and T1 are a gate, not a formality.** Epic #70 states no migration
  begins until the governing decisions and grammar acceptance table are
  approved. T2 onward stay unstarted until then.
* **T5–T7 (#78) do not depend on the new grammar** — dependency dropped to `—`
  now that this is confirmed. The silent-command bug is live today and its fix
  is valuable under either grammar, so it can proceed while T1's approval is
  outstanding. This is the one part of the epic worth doing out of order.
* **T2–T4 (#77) must land before T8**, or the parser change would have to be
  made twice — once against registry keys and again against aliases.
* **T10 (#72) comes *before* T8/T9 (#74), not after.** This table originally
  had it last, on the reasoning that BGP proves the taxonomy on a real domain.
  That was wrong: #74's own text says it "depends on the approved taxonomy and
  BGP operational-view spec", and #72 is a *define* issue, not an
  implementation. The migration needs to know the target tree before it builds
  it. Corrected 2026-08-28; the issues were right and this plan was not.
