# Feature specification: intent-separated CLI command taxonomy

**Feature:** `001-cli-command-taxonomy` · **Version:** 0.3.0 · **Status:** draft
**Changed in 0.2.0:** Q1 and Q2 answered by the owner; all eight required
decisions are now resolved. Grammar updated accordingly.
**Issues:** #71 (this spec), under epic #70. Consumes #75 (constitution) and
#73 (design corpus). Feeds #72 (BGP views), #74 (migration), #77, #78.
**Date:** 2026-08-27

> This is also the **pilot** #75 asks for: the first feature to run the
> spec → clarify → plan → tasks → implementation → evidence flow end to end.

## Problem

The `show` root means four different things, and the command does not say
which. At `42e5b87`:

* `show appliance <name> <kind> [<instance>]` fetches a resource, normalizes
  it, and prints **modeled configuration**.
* `show run appliance <name>` sends `show running-config` to the appliance and
  prints **native vendor text**.
* `show run [<section>]` is a **modeled fabric configuration** report.
* `show version`, `show flows ...`, `show journal`, `show pending` are
  **operational or CLI state**, under the same root.

The shell scopes appliance-first while scriptable `set`/`delete` use
`--appliance NAME`, so the same operation reads differently by invocation.
Resource kinds are addressed by internal registry keys, so an operator must
type `appliance/nat-maps` after the command already established the appliance
scope (#77). And at least one command produces no visible output at all, with
no way to tell waiting from empty from unsupported from swallowed exception
(#78).

#48 and #49 are the same root cause surfacing as scope and discoverability
confusion.

## Goal

An operator can tell, from the command tokens alone, which of four things they
are asking for — operational state, running configuration, staged candidate
intent, or native vendor text — and what it will cost. Every command resolves
to exactly one intent, one source, one scope, one cost class, and one output
schema. Incomplete commands list valid next tokens instead of guessing.

## Non-goals

* **Not** implementing the migration. #74 does that, after this is approved.
* **Not** adding new resource kinds or endpoints.
* **Not** redesigning `set`/`delete`/`commit`/`rollback`. Their spelling
  already matches the scope ordering this spec adopts.
* **Not** designing epic #8's `drift`. This spec only reserves the point that
  config-vs-state comparison is a named command, not a column (D-NVUE-2).
* **Not** settling the evidence ladder. That is #66; this spec consumes it if
  it lands first and works without it if not.

## Requirements

| # | Requirement | How it is verified |
|---|---|---|
| R1 | Every read command maps to exactly one intent, source, scope, cost class and output schema | The table in `grammar.md` §7; a command absent from it is not shippable |
| R2 | No token sequence resolves to both operational state and configuration | Parser test enumerating the grammar; the `show appliance X <kind>` split is the worked case |
| R3 | A nonterminal lists valid next tokens and exits 0 | Test per nonterminal: scope, domain, collection |
| R4 | Shell and scriptable forms map one-to-one, same nouns, same order | Paired tests over one fixture, both surfaces |
| R5 | User-facing nouns never expose registry keys (`appliance/`, `generated/`) | Completion, help, usage, error and doc output asserted prefix-free |
| R6 | Alias uniqueness is per scope and checked at startup | Startup check + test; `zones` is the live collision |
| R7 | Every outcome in `grammar.md` §5 is distinguishable in human and JSON output | One test per outcome, both modes |
| R8 | No renderer reduces a valid response to zero visible characters | Test over `{}`, `None`, `""`, HTTP 204, `[]` (#78) |
| R9 | Every removed command form has a recorded replacement, and a test asserting the old spelling is no longer accepted | `compatibility.md`; removal cannot silently regress into a half-supported form |
| R10 | Native output is reachable only by explicit request | Grammar test: no default path yields native |
| R11 | Fan-out commands declare cost before running — prompting when interactive, warning on stderr when not; one unreachable target is a marked row | Existing `fanout` tests extended per D-EC-3; both TTY and piped paths tested |
| R12 | `running`, `candidate`, `appliance`, `fabric`, `configuration` are reserved and rejected as kind aliases | Alias validator test; a synthetic kind named `running` fails at startup |
| R13 | Cached data is served only under `--stale-ok`; without it a read is live or it fails | Test that a stale cache entry is not silently used |

## Required decisions

#71 lists eight. Six are resolved here from the corpus; **two are escalated**
because they are judgement calls about operator experience that the owner
should make, and picking a default would be absorbing a decision rather than
making one.

| # | Decision | Resolution |
|---|---|---|
| 1 | Is `running` mandatory under `show configuration`? | **Optional, defaults to `running`.** Shorter for the common read and matches IOS. `candidate` is never implicit, so the only unnamed datastore is the live one. Introduces reserved words — see R12. |
| 2 | Does `show compare` replace or alias `show \| compare`? | **Alias.** Both kept (D-NSO-3). It is in operators' fingers and costs nothing. |
| 3 | Canonical scope ordering | **Outermost-first**, uniform across interfaces: scope → kind → instance (D-NSO-1, D-EC-1). |
| 4 | Semantics of "candidate" | **Staged intent**, not a materialized tree. Junos's candidate lives on the device; ours is a client-side changeset materialized against server state at compare/commit (`candidate.py`). The grammar must not imply a tree. |
| 5 | Is native a format, a source, or both? | **A format of running configuration.** Junos (`\| display`), NSO (`ios:` namespace) and kubectl (`-o`) agree independently (D-JUN-3, D-NSO-4, D-K8S-4). |
| 6 | Resource-kind aliases | **Per-scope user-facing nouns**, registry keys internal. `zones` is the one live collision and is why the namespace is scoped, not flat (#77). |
| 7 | Cost/freshness flags and defaults | **Fan-out warns about elapsed time**: prompt naming appliance count and expected duration where a TTY can answer it; the same two figures on stderr where it cannot. `--stale-ok` is **opt-in**. |
| 8 | Exit codes and output schema | **Resolved** — `grammar.md` §5, from gNMI's taxonomy extended with the distributed cases (D-GNMI-2). |
| 9 | Noun for selecting *which* Orchestrator a command addresses (#121) | **`orchestrator`.** `fabric` is the obvious word and is already taken: §3 defines it as a scope noun meaning *every appliance, bounded fan-out*, so `--fabric prod show fabric version` would carry two senses one space apart. `orchestrator` is the word §3 already uses for the bare no-scope subject — the Orchestrator itself. Reserves `orchestrator` and `orchestrators` as kind aliases (R12). |

| 10 | When is a new top-level verb admissible? | **Three tests, all required** — it acts rather than reports; no existing verb can carry it without changing that verb's meaning; and its intent is neither one of §1's four nor a transaction-lifecycle transition. `grammar.md` §8 states them and applies them to `adopt` (#63/#120), which also carries a contract for verbs that assert what the tool cannot verify. |

Decision 10 closes a gap rather than reversing anything. The Non-goals below
grandfather `set`/`delete`/`commit`/`rollback` as already correctly spelled —
true, and not a rule: it said the existing set was fine and never said what
makes a *new* one admissible. The first genuinely new verb therefore had
nothing to be measured against, and would have been settled by whoever wrote it
first. §8 supplies the measure and shows it has teeth by naming what fails it.

Decision 9 was escalated the same way 1 and 2 were, and answered by the owner.
It settles the **noun only**; the command shape that carries it is Q5.

It overlaps one existing use: `Scope.ORCHESTRATOR`, spelled `--scope
orchestrator`, meaning *kinds that live at the Orchestrator rather than on an
appliance*. That is a different question — which kinds, not which target — and
the two are one flag apart rather than one space apart. kubectl does the same
double duty, selecting with `--namespace` while also calling resources
namespaced, and it does not confuse anyone. Accepted rather than worked
around, and written down here so the overlap is on the record as considered.

## Open questions

| # | Question | Blocks? | Owner | State |
|---|---|---|---|---|
| Q1 | Is the datastore token mandatory, or does it default to running? | was blocking | owner | **Answered: default to running.** Grammar 0.2.0. |
| Q2 | Fan-out cost behavior, and is `--stale-ok` opt-in? | was blocking | owner | **Answered: confirm then warn; `--stale-ok` opt-in.** Grammar 0.2.0. |
| Q3 | Removal boundary for compatibility aliases | — | owner | **Answered: not needed.** pyecsdwan has not shipped to production, so old forms are removed rather than aliased. See `compatibility.md` 0.3.0. |
| Q4 | Does `show fabric <domain>` warrant existing where the Orchestrator has a single-call answer, or should those stay unscoped (`show version`)? Currently inconsistent: `show version` is Orchestrator+fanout but unscoped, while `show flows summary` gains `fabric`. | No — resolvable during #74 | owner or implementer | open |
| Q5 | Command shape for the orchestrator registry. §2 puts CLI-state *reads* under bare `show`, which makes `show orchestrators` the consistent listing; where the mutations live (a top-level `orchestrator` verb group, flags on an existing one, or a config file only) is unsettled. | No — #121 is unstarted | owner | **open — recommendation below** |

### Q5 recommendation: a file, `show orchestrators`, and no mutation verbs in v1

Not because mutation verbs are wrong, but because they are the half that cannot
be taken back. Adding `orchestrator add` later is additive; removing it is a
break, and it arrives with a surface behind it — `add`, `remove`, `rename`,
`set-default`, and a default that is then ambient state, which is the failure
`ROADMAP.md` already argues against for this feature.

Three things point the same way.

**The tool does not configure itself today.** Credentials go to the keyring or
the environment; everything else is an `ECSDWAN_*` variable. There is no
`ec-cli configure`, and the registry would be the first command that writes
this tool's own configuration — a new category, introduced for a feature that
has not shipped.

**A mapping that decides which fabric a write lands on wants a review trail.**
A file is diffable, reviewable and version-controllable; a registry edited by
command is one no one reviewed. That argument is this project's own — it is why
a declaration is a file and not a sequence of `set` commands.

**Commands would buy no safety here.** Validation at write time is the usual
argument for them, and it does not apply: the file is validated on load, the
origin is shown in the banner and in every refusal, and no command can catch an
operator typing a real hostname they did not mean.

Consistent with Decision 10, incidentally: listing the registry *reports*, so
V1 sends it to `show orchestrators` on its own.

What would change this: operators actually asking, once the selector ships; or
the registry needing to hold something they should not hand-edit — a credential
handle with a lifecycle, say — at which point the file stops being the whole
truth and a command has something to do.

**Nothing blocks the plan now.** One reading is flagged rather than assumed
silently: "confirm, then warn" is implemented as *confirm where a prompt can be
answered, warn where it cannot*, since a prompt in a pipeline would hang — the
failure class #78 exists to remove. See `grammar.md` §6; correcting it changes
that section and nothing else.

Q1's answer introduced a constraint that did not exist under the mandatory
form: with the token optional, `show configuration <token>` must disambiguate
datastore from scope from kind, so five words are now reserved (R12). No kind
collides today — 42 bare names checked against them.

## Acceptance criteria

- [ ] Every command maps to one intent, datastore/source, scope, cost class, output schema
- [ ] No token sequence can mean both operational state and configuration
- [ ] Incomplete commands return valid next tokens and examples
- [ ] Grammar examples cover fabric, appliance, resource, instance, list, detail, multi-appliance fan-out
- [ ] Shell and scriptable CLI have a one-to-one canonical mapping
- [x] A migration table maps every changed command to its replacement (removal, not aliasing — Q3)
- [ ] Golden UX tests are derivable directly from the spec
- [ ] #48/#49-style scope and discoverability confusion is covered by acceptance tests
- [x] Alias collisions fail at startup/test time, never at operator runtime
- [x] Q1 and Q2 answered and the grammar updated accordingly
- [x] Reserved words rejected as kind aliases at startup (R12)
- [x] Owner approves the grammar — epic #70's migration gate (approved 2026-08-28)

## Evidence expected

Per Principle V, stated before implementation rather than discovered in review:

* **This spec:** design only. No code, no runtime evidence. The registry facts
  it rests on (43 kinds, 21 appliance-scope, the `zones` collision, the two
  prefix forms) were read from the live registry in this session, not assumed.
* **On #74's merge:** mock-verified. The grammar, outcomes and aliases are
  testable against the bundled mock Orchestrator.
* **Not obtainable without gear:** that the real appliance API returns the
  outcome classes this taxonomy claims — particularly `unsupported` versus
  `not_found`, which the mock cannot distinguish faithfully. That distinction
  is asserted against the mock and **unverified live**, and must be labelled so
  until someone runs it against real hardware.

## Documents

| File | Contents |
|---|---|
| `spec.md` | this — problem, requirements, decisions, open questions |
| `grammar.md` | the four intents, canonical shape, scopes, kinds, outcomes, flags, worked examples |
| `compatibility.md` | every existing command → replacement, and the one meaning-change that must hard-fail |
| `plan.md` | approach and constitution check |
| `tasks.md` | ordered work items for #72/#74 |
