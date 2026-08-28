# Implementation plan: intent-separated CLI command taxonomy

**Feature:** `001-cli-command-taxonomy` · **Spec:** `./spec.md` · **Date:** 2026-08-27
**Status:** unblocked — Q1 and Q2 answered (grammar 0.2.0). Remaining gate is
the owner's approval of the grammar itself, per epic #70.

## Approach

Three landings, in order. The spec is design-only; nothing below starts until
the grammar is approved (#70: "No command migration should begin until the
governing decisions and grammar acceptance table are approved").

1. **Naming layer** (#77). A CLI name/alias contract on `Resource`, separate
   from the internal stable kind. Per-scope namespace, uniqueness checked at
   import. Parsing, completion, help, usage, errors and docs read from it;
   `candidate.py`, `journal.py` and the API contracts keep canonical keys
   untouched — renaming those would be a state migration, which this is not.

2. **Outcome layer** (#78). One place that classifies a read into the eleven
   outcomes of `grammar.md` §5 and renders each distinguishably in human and
   JSON. Bounded timeout on every read. This lands *before* the grammar change
   because it is the current silent-failure bug and does not depend on the new
   nouns.

3. **Grammar layer** (#74, then #72). The parser change, the alias/hard-fail
   table from `compatibility.md`, then BGP operational views as the first
   domain built natively on the taxonomy.

Splitting this way means each landing is independently reviewable and the
riskiest piece (the parser) arrives last, on top of naming and outcome layers
that are already tested.

## Constitution check

Constitution **1.0.0, ratified 2026-08-28** — these checks are binding.

| Principle | Applies? | How this design satisfies it | Risk |
|---|---|---|---|
| **I. Intent-separated interfaces** *(non-negotiable)* | Yes — this is the feature | Four named intents; `grammar.md` §7 maps every command to exactly one; R2 forbids overlap | The `show appliance X <kind>` split is a real behavior change; handled by hard-fail, not alias |
| **II. Safety truth over convenience** *(non-negotiable)* | Yes | Eleven distinguishable outcomes (§5); only `ok`/`empty`/`stale` exit 0; R8 forbids zero-character renders | **Resolved by Q2**: `--stale-ok` is opt-in, so `stale` is never reached by default and exiting 0 honours an explicit request |
| **III. Model-first, labelled native** | Yes | Native is a `--format` value on running configuration only (Decision 5); R10 forbids reaching it by default | Retiring `show run appliance` is a visible break; alias + warning |
| **IV. One grammar across interfaces** | Yes | Outermost-first ordering both surfaces (Decision 3); R4 tests them paired | `--appliance` flag vs `appliance` noun stays a spelling difference; acceptable, tested |
| **V. Evidence-gated support** *(non-negotiable)* | Yes | Spec's *Evidence expected* states design-only now, mock-verified at #74, and names `unsupported` vs `not_found` as unverifiable without gear | The mock cannot faithfully distinguish those two; must stay labelled unverified |
| **VI. Reversible evolution** | **No — out of scope** | Its subject is an operator who would be surprised by a change, and pyecsdwan has no production users. Old forms are removed, not aliased (Q3) | Inverts the moment there is a first external installation; named in `compatibility.md` |

**No non-negotiable principle is unsatisfied.** Two risks under II and V are
carried openly rather than designed away: `stale` exiting 0, and the mock's
inability to distinguish `unsupported` from `not_found`.

### Exceptions claimed

None. No convention is departed from.

## Alternatives rejected

1. **Keep `show run`, add `show configuration` beside it.** Cheapest, no break.
   Rejected: it leaves two spellings for one intent permanently, which is the
   ambiguity the epic exists to remove — and the corpus shows three separate
   traditions converging on native-as-format (D-JUN-3/D-NSO-4/D-K8S-4).
2. **kubectl-style verbs (`get`/`describe`/`diff`).** Cleaner intent separation
   per verb. Rejected on audience: network operators reach for `show` first
   (D-K8S-1), and the Junos flavoring was a deliberate choice this spec should
   not silently reverse.
3. **NVUE-style columns** — operational and applied side by side. Rejected: it
   is the same conflation the epic is fixing, rendered side-by-side instead of
   hidden, and it degrades badly across a fabric where each column needs its
   own reachability state (D-NVUE-2).
4. **Alias `show appliance X <kind>` to the configuration form with a warning**
   rather than hard-failing. Rejected: a warning that scrolls past leaves the
   operator reading one kind of data believing it is another. See
   `compatibility.md`.
5. **Flat alias namespace.** Simpler to implement and explain. Rejected on
   evidence: `zones` exists in both scopes today and they are different
   objects.

## Risks

| Risk | Surfaced by |
|---|---|
| The hard-fail window is disruptive for anyone scripting `show appliance X <kind>` | Deliberate; needs a release note, and Q3's boundary bounds it |
| A future kind introduces a second cross-scope collision | R6's startup check fails for a developer, not an operator |
| ~~`stale` exiting 0 lets a cached answer read as fresh~~ | **Closed by Q2** — cached data requires `--stale-ok`, so it is never served unasked |
| The no-compatibility decision is wrong because an unknown user exists | Accepted deliberately; the trigger to revisit is named in `compatibility.md` |
| A kind alias collides with a reserved word once the datastore token is optional | R12's validator fails at startup, for a developer not an operator |
| The outcome layer touches every read path | Landed first and separately, so a regression is attributable |
| ~~Grammar churn if Q1 is answered after tests are written~~ | **Closed** — answered before any test was written, which is what blocking on it bought |

## Verification strategy

Acceptance criteria become tests in three groups: grammar/parser (R1–R5, R10),
naming (R5, R6), outcomes (R7, R8, R11), plus compatibility behavior tests (R9).

**Every guard gets its fix removed and the test re-run**, per this repository's
record: three guards this session passed with the thing they guarded deleted
(the candidate lock without its re-read, the flock re-entrancy, the package-data
declaration). Specifically:

* R2 — remove the split, assert the parser test fails.
* R6 — add a synthetic cross-scope collision, assert startup fails.
* R8 — return `{}` from a fetch, assert the render test fails on zero output.
* R9 — restore the old behavior, assert the compatibility test fails.

A test that passes with its subject removed is recorded as not-yet-written.
