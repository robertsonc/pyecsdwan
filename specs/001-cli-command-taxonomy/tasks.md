# Tasks: intent-separated CLI command taxonomy

**Feature:** `001-cli-command-taxonomy` · **Plan:** `./plan.md` · **Date:** 2026-08-27

Status lives in the linked issue, never here (`.specify/README.md`). An
unlinked task has not been started.

| # | Task | Depends on | Acceptance | Issue |
|---|---|---|---|---|
| ~~T0~~ | ~~Owner answers Q1 and Q2; grammar updated~~ | — | **Done** — grammar 0.2.0; both recorded in `spec.md` | — |
| ~~T1~~ | ~~Owner approves the grammar and the acceptance table~~ | T0 | **Done** — approved 2026-08-28; epic #70's migration gate open | #70 |
| ~~T2~~ | ~~CLI name/alias contract on `Resource`~~ | T1 | **Done** — `cli_name`/`cli_aliases`, per-scope index, reserved words and collisions refused at registration | #77 |
| T3 | User-facing nouns throughout parsing, completion, help, usage, errors, docs | T2 | **Partly done** — both surfaces resolve nouns through `Registry.resolve_cli` and print them in errors; `main.py` subcommand *help text* still to do | #77 |
| T4 | Offline command reference view (domain, scope, instances, mutability, support status) | T2 | **Done** — `show commands` on both surfaces, generated from the parser's own tables and round-tripped through the dispatcher; runs with no Orchestrator and no credentials | #77 |
| T6 | Bounded timeout on every appliance read; every terminal path returns to the prompt | T5 | **Partly done** — every terminal path returns to the prompt with a classified exit code; the per-read timeout budget is the client's existing connect/read timeouts, not yet a per-command `--timeout` on the new views | #78 |
| T7 | Audit remaining appliance-scoped resources for the same silent path | T6 | Every kind exercised; findings filed | #78 |
| T8 | Parser: new grammar, scope nouns, nonterminal listing | T1, T3 | **Done** — both surfaces: `show configuration` subtree, `fabric`/`appliance` scope nouns, nonterminals listing next tokens at exit 0, correspondence tested | #74 |
| T9 | **Remove** the old forms (no aliases — Q3), including #77's legacy `appliance/<kind>` acceptance | T8 | **Done** — parametrized absence test per row on both surfaces; the registry-key acceptance is withdrawn | #74 |
| T10 | BGP operational views — **spec** in `specs/002-appliance-operational-views/` | T1 | **Done** — source-verified; `routes` reported unsupported (no endpoint exists) | #72 |
| T14 | BGP operational views — **implementation** | T8, T10 | **Done** — `show appliance NAME bgp summary\|neighbors [PEER]\|routes` on both surfaces; the four schema traps each have a test that the obvious implementation fails | #72, #74 |
| T5 | Outcome classifier + exit codes for the eleven outcomes | — | **Done** — `cli/outcomes.py` carries the table (asserted against `grammar.md` §5 by parsing it) and `classify()` maps every failure at both dispatch boundaries; all eleven are reachable | #74, #78 |
| T11 | Golden UX tests derived from `grammar.md` §7 | T8, T10 | **Partly done** — `tests/test_grammar_parity.py` covers every configuration row on both surfaces; the operational rows wait on T10 | #74 |
| T13 | Fan-out cost gate (§6, Decision 7) | T8 | **Done** — prompt when a TTY can answer, warn on stderr and proceed when not; count from the resolver cache, duration from observed latency; only the sections that actually fan out are gated | #74 |
| ~~T12~~ | ~~Removal boundary decided (Q3)~~ | — | **Withdrawn** — no aliases to remove; nothing has shipped | — |

## Sequencing notes

* **T4 says "instances" and the view says "address".** An offline reference
  cannot know how many instances of a kind exist on a fabric, and one that
  answered anyway would be guessing — Principle V. The column reports how a
  kind is *addressed*, derived from `Resource.deletable`, whose own contract
  ties it to singleton tables with no absent state. Recorded as a deviation
  rather than silently reinterpreted.

* **T0 and T1 are a gate, not a formality.** Epic #70 states no migration
  begins until the governing decisions and grammar acceptance table are
  approved. T2 onward stay unstarted until then.
* **T5–T7 (#78) do not depend on the new grammar** — dependency dropped to `—`
  now that this is confirmed. The silent-command bug is live today and its fix
  is valuable under either grammar, so it can proceed while T1's approval is
  outstanding. This is the one part of the epic worth doing out of order.
* **T2–T4 (#77) must land before T8**, or the parser change would have to be
  made twice — once against registry keys and again against aliases.
* **T8 landed shell-first, and the drift it risked was real.** The tree is one
  function in `shell.py` and a set of Typer subcommands in `main.py`, so the
  shell went first and the grammar was exercised end to end before the second
  surface was written to match. That gap is exactly what `test_grammar_parity`
  was written to close, and on its first run it found a form the shell accepted
  and the scriptable CLI did not. Shell-first is a reasonable order; shipping
  it without the correspondence test would not have been.

* **The fan-out gate is not a safety prompt, and the tests say so.** Every
  fan-out command here is read-only; what is being guarded is elapsed time.
  Writing it as an "are you sure" would train the operator to skip it, which is
  why it stays quiet below ten seconds and why only the `deployment` section of
  the fabric report is gated — the others are Orchestrator-level GETs.

* **T10 (#72) comes *before* T8/T9 (#74), not after.** This table originally
  had it last, on the reasoning that BGP proves the taxonomy on a real domain.
  That was wrong: #74's own text says it "depends on the approved taxonomy and
  BGP operational-view spec", and #72 is a *define* issue, not an
  implementation. The migration needs to know the target tree before it builds
  it. Corrected 2026-08-28; the issues were right and this plan was not.
