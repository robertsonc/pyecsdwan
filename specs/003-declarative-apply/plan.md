# Implementation plan: declarative apply from a desired-state directory

**Feature:** `003-declarative-apply` · **Spec:** `./spec.md` · **Date:** 2026-08-29
**Status:** design ratified 2026-08-29 — implementation blocked pending the safety gates named in `spec.md`

## Approach

Build declarative apply as a new intent source over the existing resource and
transaction contracts, not as a second reconciliation or transaction engine.

### 1. Versioned declaration model

`desired.py` owns a versioned declaration envelope, canonical reference
resolution, lifecycle state, schema validation, deterministic traversal, and
atomic loading.

Loading has two phases:

1. read and validate every filesystem entry without constructing an API client;
2. resolve every declaration into one immutable declaration set with a
   canonical digest.

Any unreadable file, unsupported schema version, symlink/path escape, duplicate
reference, alias conflict, path/document identity mismatch, unsupported kind,
invalid scope, or empty set invalidates the whole load.

A present declaration is typed partial intent. An absent declaration has no
desired body and is accepted only when its resource advertises deletion and
verified rollback capability.

### 2. Declarative resource capability

Extend the resource contract with explicit declarative capability metadata and
a materialization operation. Each resource declares one of:

- unsupported;
- present-only;
- present-and-absent.

For `present`, materialization receives current normalized state plus typed
intent and returns either a safe complete target or a structured blocker. A
resource that cannot preserve unknown, unmodeled, redacted, or write-only
fields is unsupported for declarative replacement.

For `absent`, planning requires evidence-backed delete, snapshot, restore, and
post-restore verification. No generic deletion inference is permitted.

A registry test requires every resource to declare its capability and evidence
state explicitly.

### 3. Shared intent universe and preflight

Replace the current live-only drift enumeration with a common intent-universe
builder. For the requested scope it evaluates the stable union of:

- live references returned by each selected resource;
- declared references from the immutable declaration set.

Enumeration failures create explicit incomplete scope records rather than empty
lists or notes that do not affect completeness.

The same non-mutating preflight is used by drift, dry-run, and apply. It
produces:

- canonical declaration-set and plan identities;
- operation per reference: no-op, create, update, delete, out-of-scope;
- normalized diff;
- capability and evidence;
- ownership result;
- shared write target and collisions;
- reversibility;
- transaction-state/confirm-window blockers;
- completeness, freshness, and structured errors.

Dry-run ends after this preflight. Apply reuses its plan, then revalidates every
race-sensitive invariant under the host commit lock.

### 4. Transaction integration

Keep `candidate.IntentSource` (or a renamed neutral protocol) as the single
planner input implemented by both candidate and declaration sources.
`txn.build_plan` and `txn.commit` remain the only planning and write paths.

Before enabling directory apply:

- #63 supplies revisioned/CAS candidate acknowledgement and canonical target
  identity;
- #100 moves lifecycle/confirm-window exclusion into the engine and makes
  pending recovery state-checked under the lock;
- #103 turns verify exceptions into recovery and verifies restored snapshots;
- #110 makes journal parsing/append recovery fail closed;
- #112 reconciles confirm decisions idempotently;
- #24/#64 establish quiescent and complete async-job outcomes.

Directory apply refuses a nonempty host candidate. A successful directory
transaction never clears or rewrites candidate state.

Explicit rebase discards the old plan identity, rereads state, recomputes all
guards, and renders a new plan. Confirmation of the old plan does not authorize
the new one.

### 5. Interface and outcome integration

Expose the same canonical `apply --from <dir>` and dry-run semantics through
the scriptable CLI and interactive shell. If filesystem access is deliberately
unavailable on a future interface, that interface rejects the command and names
the exact supported path; it never implements a weaker meaning.

Human and JSON renderers consume one result model. #109 owns the shared numeric
exit-code mapping. This feature contributes the named outcomes in `spec.md`
and must not overload an existing numeric code with a contradictory meaning.

Plans redact secret values before rendering. Journal audit export contains
redacted public events; rollback-private material follows #106.

### 6. Evidence and promotion

The evidence ledger becomes executable capability metadata rather than stale
prose. A consistency test fails when code, registry capability, coverage
output, ledger, README, or roadmap make different support claims.

A resource can progress independently:

1. declaration/schema confirmed;
2. unit/property verified;
3. exact-request/mock verified;
4. live-read/materialization verified;
5. live-write/persist/verify/rollback verified;
6. live-delete/restore verified, if absent is supported.

The CLI reports the actual stage. Directory apply may ship for a safe subset
without implying every registered resource is supported.

## Constitution check

Constitution **1.0.0, ratified 2026-08-28**. These checks are binding.

| Principle | Applies? | How this design satisfies it | Risk |
|---|---|---|---|
| **I. Intent-separated interfaces** *(non-negotiable)* | Yes | `apply --from` and `--dry-run` are explicitly desired-configuration operations. Operational reads are inputs to preflight but never presented as desired intent. Each output names source, scope, and operation. | The word "desired state" can imply whole-fabric authority; help and output must always label v1 as partial declaration scope. |
| **II. Safety truth over convenience** *(non-negotiable)* | Yes | Empty input is invalid; enumeration failures make the result incomplete; desired-only refs are visible; undeclared refs are out of scope; unknown/write-only fields block; dry-run runs real guards; writes and rollback are verified. | A missing error path could still collapse to no-change; every negative state requires an adversarial test that fails when the guard is removed. |
| **III. Model-first, labelled native** | Yes | Declarations and plans use typed resource models. Native text is neither an input nor an implicit fallback. Unsupported modeling blocks visibly. | Some vendor objects may not be safely modelable; they remain unsupported rather than accepting native payloads. |
| **IV. One grammar across interfaces** | Yes | Shell and scriptable entry points use the same `apply --from` nouns, declaration resolver, result model, guard semantics, and errors. Library callers use the same preflight/transaction contracts. | Filesystem behavior and prompting differ by environment; parity tests compare semantic results, and noninteractive confirmation is explicit. |
| **V. Evidence-gated support** *(non-negotiable)* | Yes | Declarative capability and evidence are explicit per kind; mock-only behavior cannot claim live safety; docs/coverage/ledger consistency is tested. | Existing BGP/OSPF/routes code outruns live evidence and must remain disabled until #104/#105 close. |
| **VI. Reversible evolution** | Yes | This adds a new command and versioned declaration format. Future schema evolution fails closed and uses explicit migration. No existing command silently changes meaning. | PR #98 already contains an unratified draft format; because it is unmerged, the ratified schema may replace it without a public compatibility claim. |

**No non-negotiable principle is unsatisfied.** Implementation remains blocked
until the spec's named safety prerequisites are complete or the affected
capabilities are unavailable.

### Exceptions claimed

None.

## Alternatives rejected

1. **Authoritative directory by default.** Familiar GitOps language, but unsafe
   without a durable ownership inventory and explicit prune boundary. A missing
   checkout could become mass deletion.
2. **Infer delete from file removal.** Convenient, but Git cannot distinguish
   deliberate deletion from branch/path/render errors. Rejected for v1.
3. **Add `--prune` immediately.** A flag is not an ownership model. Deferred
   to a separate spec requiring manifest, allowlist, preview, confirmation, and
   rollback evidence.
4. **Treat empty input as a valid no-op.** Mathematically consistent with
   partial intent, operationally indistinguishable from a bad path or empty
   render. Rejected fail closed.
5. **Treat YAML as complete replacement documents.** Simple implementation,
   but can erase server-defaulted, unknown, unmodeled, redacted, and write-only
   fields. Rejected in favor of typed materialization.
6. **Apply declared refs while drift enumerates only live refs.** Keeps the
   current report cheaper, but permits an unpreviewed create. Rejected because
   preview and execution would have different intent universes.
7. **Run safety guards only during real apply.** Faster dry-run, but labels an
   uncommittable plan as applicable. Rejected; dry-run uses non-mutating
   preflight and apply revalidates race-sensitive state.
8. **Combine directory intent with the current candidate.** Flexible, but the
   directory no longer describes the transaction and provenance is ambiguous.
   Rejected; a nonempty candidate refuses.
9. **Build a GitOps-specific transaction engine.** Avoids adapting candidate
   interfaces, but duplicates the exact journal/ownership/rollback boundary
   the project is designed to centralize. Rejected.
10. **Let every parseable resource participate.** Conflates generated/mock
    implementation with evidence-backed safety. Rejected per Principle V.

## Risks

| Risk | Early signal / control |
|---|---|
| Operators interpret partial declarations as authoritative whole-fabric truth | Every result says declaration scope; undeclared objects are `out_of_scope`; docs avoid unqualified "fabric is clean" |
| Partial materialization erases hidden state | Unknown/redacted-field fixtures and per-resource complete-object capability; block by default |
| Explicit absent targets the wrong object | Canonical ref displayed in plan, deletability/evidence gate, confirmation, snapshot, post-delete and rollback verification |
| Drift and apply diverge as code evolves | Contract test executes both from one declaration set and compares plan identities/operations |
| State changes after approval | Commit-lock revalidation and explicit rebase with a new plan identity |
| A corrupt/partial journal makes rollback lie | #110 plus missing-snapshot and malformed-interior fault tests |
| Async write completes after rollback | #24 requires cancellation/quiescence or an unknown/attention state |
| Secret values leak through diff or audit | Seeded sentinel sweep across all output and persistence boundaries; #106 |
| Numeric exit codes conflict with existing CLI behavior | #109 owns the mapping; this feature consumes named outcomes only |
| Mock replacement semantics prove behavior the live API lacks | Exact-request tests plus evidence stage; BGP/OSPF/routes disabled pending #104/#105 |
| Schema changes strand existing declaration repositories | Explicit version, future-version refusal, migration fixtures, release notes |
| Large directories produce costly preflight | Deterministic bounded concurrency, cost estimate, timeout, per-scope completeness; no silent truncation |

## Verification strategy

### Loader and schema

- Table/property tests generate malformed YAML, duplicate refs, future
  versions, invalid lifecycle states, alias collisions, path/document identity
  mismatches, symlink/path escape, unreadable files, and empty directories.
- Each test asserts no API client or resolver call occurred.
- Remove each validation guard and prove its test fails.

### Intent universe and outcomes

- Fixtures cover live-only, declared-only, both, duplicate, missing, denied,
  timeout, stale, malformed, and unsupported references across fabric and
  appliance scope.
- Assert stable ordering and identical ref/operation/diff output from drift,
  dry-run, and apply preflight.
- Replace a list failure with an empty list and prove the completeness/exit test
  fails.
- Remove desired-only union inclusion and prove the create-preview test fails.

### Materialization and secrets

- Per-resource typed fixtures include unknown extension fields, server defaults,
  redacted credentials, write-only values, null/empty distinctions, and
  partial nested objects.
- Exact request assertions prove safe preservation or a pre-write blocker.
- Seed unique sentinel secrets and scan every human/JSON/error/journal-export
  artifact.
- Bypass redaction or preservation and prove the tests fail.

### Transaction and recovery

- Multiprocess tests cover concurrent staging during apply, concurrent commit,
  active confirm windows, pending recovery racing a live commit, explicit
  rebase, watchdog expiry, and target-identity collisions.
- Fault injection raises after snapshot, apply, verify, journal append,
  rollback, confirm claim, marker write, and terminal-state write.
- Every terminal success is followed by live normalized comparison.
- Remove the engine-level state guard, rollback verification, or journal repair
  and prove the relevant test fails.

### Interface and distribution

- Paired shell/scriptable/library fixtures assert equivalent declarations,
  plans, outcomes, errors, and redaction.
- JSON Schema validates no-change, changes, blocked, invalid, incomplete,
  applied, reverted, and revert-failed examples.
- Build wheel and sdist; install each in a clean environment; run declaration,
  drift, dry-run, and help smoke tests.
- CI must be green and required under #65 before implementation merge.

### Live evidence

For each enabled kind and supported Orchestrator/ECOS version, record:

- no-op;
- create or update;
- persistence job outcome;
- read-after-write verification;
- injected failure and rollback;
- explicit delete and restore where supported;
- secret/redacted-field behavior;
- concurrent-state refusal.

A kind lacking the applicable live record remains labelled and enforced as
unsupported, even if its mock tests pass.
