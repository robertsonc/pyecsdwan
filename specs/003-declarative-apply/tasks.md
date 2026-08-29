# Tasks: declarative apply from a desired-state directory

**Feature:** `003-declarative-apply` · **Plan:** `./plan.md` · **Date:** 2026-08-29

Ordered and independently reviewable. Status lives in the linked issue, never
here — see `.specify/README.md`. An unlinked task has not been started. A
linked pull request identifies work already drafted but not approved.

| # | Task | Depends on | Acceptance | Issue |
|---|---|---|---|---|
| T0 | Owner ratifies D1–D16; mark package 1.0.0 ratified | — | Spec PR records approval; no blocking question remains; constitution check passes | #101 |
| T1 | Make journal append/read/recovery corruption-safe | T0 | Torn tail repaired durably; malformed interior and missing snapshot fail closed | #110 |
| T2 | Make local intent host identity and schema concurrency-safe | T0 | Candidate CAS preserves concurrent staging; canonical target identity cannot collide; future local-state formats refuse unchanged | #63, #108 |
| T3 | Enforce transaction lifecycle and confirmation state in the engine | T1, T2 | Live commits cannot be recovered; active confirm blocks every entry point; confirm crash points reconcile idempotently | #100, #112 |
| T4 | Make async completion, verification, and rollback trustworthy | T1, T3 | Timed-out jobs quiesce; expected targets are complete; verify exceptions recover; restoration is post-verified | #24, #64, #103 |
| T5 | Separate rollback-private secrets from public plan/audit state | T0 | Seeded secrets absent from plaintext/output; encrypted rollback remains functional | #106 |
| T6 | Complete ownership and shared-write-target preflight | T0, T2 | Every enabled kind has fail-closed ownership and target declarations; conflicts are structured and pre-write | #20, #69 |
| T7 | Implement the versioned declaration envelope and atomic loader | T0, T2 | R1–R5 pass; empty/invalid input performs no API access; canonical declaration digest is stable | #98 |
| T8 | Add per-resource declarative capability and safe materialization | T5, T6, T7 | Registry coverage is total; unsafe unknown/redacted fields block; capability matches evidence | #98 |
| T9 | Build the shared live+declared intent universe | T7, T8 | R6–R9 pass; desired-only creates are visible; list failure is incomplete; undeclared is out of scope | #102 |
| T10 | Extract one non-mutating preflight for drift, dry-run, and apply | T3, T6, T9 | R10–R12 and R16 pass; same plan/guards on every path; apply revalidates under lock | #98 |
| T11 | Implement explicit absent declarations for proven-safe resources | T4, T8, T10 | No implicit deletion; capability/deletability checked; delete and restore are live verified |  |
| T12 | Implement shared result schema, rendering, and exit mapping | T9, T10 | R17, R18, R22, R23 pass across shell/scriptable/library; mapping comes from #109 | #109, #98 |
| T13 | Complete single-transaction directory apply and explicit rebase | T3, T4, T10, T12 | R11, R13–R15, R21 pass; nonempty candidate refuses; rebase yields a new visible plan | #98 |
| T14 | Add installed-wheel/sdist and required CI gates | T7, T9, T12, T13 | Clean artifacts provide schema, drift, dry-run, apply, help; CI is required and reproducible | #65 |
| T15 | Record per-kind versioned live evidence and gate promotion | T8, T11, T13 | R19/R20 pass; no mock-only kind is advertised live-safe | #66, #104, #105 |
| T16 | Reconcile README, help, coverage, roadmap, and epic state | T12, T14, T15 | Partial authority and actual evidence agree everywhere; no incomplete epic is marked shipped | #68, #8 |
| T17 | Rebase implementation draft on the ratified package and run final gate | T1–T16 | #98 matches spec 1.0; full required CI/evidence passes; blocker checklist is closed | #98, #101 |

## Sequencing notes

### Ratification is the first gate

T0 approves product semantics, not implementation. No task may reinterpret
authority, deletion, empty input, materialization, output, or evidence in code.
A material change returns to the spec and owner review.

### Safety foundation precedes the public command

T1–T6 are not optional polish. They establish the guarantees that make one
multi-resource transaction honest. Draft #98 may be used as implementation
input, but its command cannot leave draft or be documented as shipped while
these dependencies are open.

T1–T4 should land as independently reviewable fixes before rebasing the larger
feature branch. That keeps transaction fault tests attributable and avoids
hiding safety fixes inside a GitOps feature.

### Declaration loading precedes API work

T7 must validate a directory completely without credentials, resolver calls, or
API construction. Only the immutable declaration set crosses into T8–T10.
This lets malformed input remain an offline error and gives every subsequent
stage one stable identity.

### Capability is per resource

T8 does not enable all resources. It creates an explicit contract that defaults
to unsupported. Resource-specific promotion occurs through T11/T15 and can be
sharded into issues when work is ready.

Static routes remain blocked by #104. BGP and OSPF remain blocked by #105.
Secret-bearing or full-replacement resources remain unsupported until their
preservation and rollback evidence exists.

### Drift and dry-run are one preflight

T9 and T10 land before write integration. A desired-only create, incomplete
kind, ownership refusal, collision, irreversibility, or unsafe materialization
must be visible before T13 can write it. A dry-run implementation that exits
before the shared guards does not satisfy T10.

### Explicit deletion is deliberately late

T11 follows safe materialization and transaction recovery. Present-only support
may ship for a resource without absent support. No implementation should add a
generic `--prune` while completing T11; pruning needs a separate feature spec.

### Outcome mapping is shared

T12 consumes #109. The feature may define named outcomes and JSON fields, but
must not independently assign conflicting numeric exits. Human and machine
results are two renderings of one model.

### Evidence controls scope, not schedule claims

T15 may leave individual resources unsupported. That is a valid feature result
when stated explicitly. It is not valid to weaken the evidence requirement to
make a roadmap checkbox green.

### Closing #101

#101 closes only after:

1. the package is ratified;
2. #98 conforms to it;
3. all enabled capability blockers and acceptance criteria are verified;
4. #8 and `docs/ROADMAP.md` reflect the actual shipped/evidence state.

Ratification alone is therefore a milestone inside #101, not completion of the
issue.
