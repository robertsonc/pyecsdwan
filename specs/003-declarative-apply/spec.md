# Feature specification: declarative apply from a desired-state directory

**Feature:** `003-declarative-apply` · **Version:** 1.0.0 · **Status:** ratified
**Issue(s):** #101, under epic #8; implementation draft #98 · **Author:** repository owner + review agent · **Date:** 2026-08-29
**Ratified:** 2026-08-29 by the repository owner through merge of #115

## Problem

Draft PR #98 adds `apply --from <dir>`, but the repository has not decided what
a directory of YAML owns or what absence means. Today the implementation can
load and apply declared objects, while several materially different product
contracts remain possible:

- the directory could describe only a managed subset, or claim authority over
  every live object in scope;
- removing a file could mean "stop declaring it", "leave it alone", or "delete
  it";
- an empty directory can currently look like a successful no-op even when it
  came from a wrong path, failed checkout, or templating error;
- drift enumerates live references while apply iterates declared references, so
  apply can create an object its own preview did not report;
- a partial YAML object can be unsafe for a resource implemented as a full
  object replacement, particularly when live reads omit unknown or secret
  fields;
- dry-run builds a plan but does not run the same non-mutating ownership,
  collision, reversibility, and transaction-state preflight as real apply.

These are product and safety decisions, not implementation details. Absorbing
them as accidental defaults would violate Constitution Principles II and V.

## Goal

A directory expresses an explicit, partial set of managed configuration intent.
Drift, dry-run, and apply interpret that set identically; absence is
non-destructive; deletion is explicit; unsafe or incomplete knowledge blocks
before the first API write; and every human or machine consumer can distinguish
no change, applicable change, refusal, partial knowledge, and transaction
failure.

## Definitions

| Term | Meaning |
|---|---|
| **Declaration set** | Every valid declaration loaded from one `--from` directory as one atomic input |
| **Declared reference** | A resource reference explicitly present in the declaration set |
| **Desired-only reference** | A declared reference that does not currently exist live |
| **Undeclared live reference** | A live reference absent from the declaration set |
| **Partial/additive authority** | The declaration set governs only its declared references; it does not claim ownership of all live objects |
| **Present declaration** | Intent that the referenced object exists with the declared modeled values |
| **Absent declaration** | Explicit intent that the referenced object be deleted |
| **Preflight** | The non-mutating plan and safety evaluation used by both dry-run and apply |
| **Complete-object safety** | Proof that materializing and writing the target cannot erase unknown, unmodeled, redacted, or write-only state |

## Governing decisions

These decisions were approved by the repository owner through merge of #115
and are the binding 1.0 contract.

| # | Decision | Resolution |
|---|---|---|
| D1 | Authority model | **Partial/additive.** The directory governs only declared references. It is not an authoritative inventory of the fabric. |
| D2 | Undeclared live objects | Report as `out_of_scope`, never as clean, in-sync, drift, or an implied deletion. |
| D3 | Desired-only objects | Include in drift/preflight as planned creates. |
| D4 | Deletion | Only an explicit absent declaration may request deletion. Removing a file never deletes its former object. |
| D5 | Pruning | No `--prune` in v1. Authoritative pruning requires a later spec with an ownership manifest, allowlist, preview, and confirmation contract. |
| D6 | Empty input | An empty declaration set is invalid and refuses before API access. There is no `--allow-empty` escape hatch in v1. |
| D7 | Present-object shape | YAML is typed partial intent. The resource materializes a complete target from current state and the declaration; it must not blindly post a partial document to a full-replacement endpoint. |
| D8 | Unknown/write-only fields | A resource must declare and prove a preservation strategy. If an unknown, unmodeled, redacted, or write-only field cannot be preserved, preflight blocks that reference. |
| D9 | Intent universe | Drift and preflight evaluate the stable union of live and declared references in the requested scope. |
| D10 | Dry-run parity | Dry-run executes the same non-mutating planner and safety preflight as apply. Apply revalidates race-sensitive guards under the commit lock immediately before writes. |
| D11 | Existing candidate | Directory apply refuses while the host-scoped candidate is nonempty; the two intent sources are never silently combined. |
| D12 | Rebase | Rebase is explicit, rebuilds the complete plan, and redisplays it. Concurrent state is never silently absorbed. |
| D13 | Transactions | All changes use one dependency-ordered transaction, shared collision detection, snapshots, verification, journal, rollback, and confirm-window rules. |
| D14 | Output contract | Named outcomes are authoritative and shared across human/JSON interfaces. Numeric exit mapping is supplied by #109; no apply-only conflicting code table is invented here. |
| D15 | Secrets | Plans, diffs, errors, JSON, and audit exports never reveal secret values. Rollback-private material follows #106. |
| D16 | Evidence | A resource is declaratively writable only at its actually verified evidence level. Mock behavior cannot establish live write/delete/reversibility support. |

## Non-goals

- Treating the directory as an authoritative inventory of all fabric or
  appliance configuration.
- Inferring deletion from a missing file.
- Shipping `--prune` or garbage collection.
- Creating a second transaction engine.
- Bypassing candidate, ownership, shared-target, reversibility, async-job,
  verification, journal, or confirm-window safety.
- Making generated Tier-1 bindings operator-writable merely because a YAML
  document can be parsed.
- Promoting a resource to live-write support without the evidence required by
  Constitution Principle V.
- Defining an apply-specific numeric exit-code system independently of #109.
- Amending the constitution.

## Declaration contract

A declaration addresses exactly one canonical resource reference and contains
an explicit lifecycle state:

- `present` carries typed modeled values;
- `absent` carries no desired object body and is valid only for a resource
  with evidence-backed deletion and rollback semantics.

The path may contribute addressing information, but the parsed declaration is
the authority. Conflicting path and document identity is invalid. Duplicate
canonical references, alias collisions, unsupported kinds, invalid scopes,
schema failures, symlink/path escape, and unreadable files invalidate the
entire declaration set before API access.

The serialized envelope and schema version must be explicit and versioned.
Future unsupported versions fail closed without rewriting the input. Resource
aliases may be accepted at the CLI boundary, but plans and persisted metadata
use canonical internal references.

## Requirements

| # | Requirement | How it is verified |
|---|---|---|
| R1 | Loading is atomic: one invalid or unreadable declaration invalidates the entire set before API access | Loader tests inject malformed YAML, schema errors, duplicate refs, unreadable files, and path escape |
| R2 | An empty declaration set is invalid and performs no resolver/API operation | Empty-directory and wrong-directory tests assert the client is never constructed |
| R3 | The declaration format has an explicit schema version and lifecycle state | Schema fixtures cover current, missing, malformed, and future versions |
| R4 | Present declarations are typed partial intent, never an unvalidated full-object POST body | Per-kind schema tests and exact request-body tests |
| R5 | Absent declarations are the only v1 deletion mechanism | Removing a file produces no delete; an explicit tombstone exercises deletability and rollback |
| R6 | Undeclared live references are `out_of_scope` and cannot be reported as clean or scheduled for deletion | Drift fixtures contain undeclared live objects in partial mode |
| R7 | Desired-only references appear as planned creates in drift and dry-run | A declaration absent live yields the same create ref/diff in both paths |
| R8 | Drift/preflight use the stable union of live and declared refs with deterministic ordering and deduplication | Union tests cover live-only, declared-only, both, duplicates, and appliance scope |
| R9 | A selected-kind enumeration failure creates an explicit incomplete outcome and cannot exit 0 | Per-kind timeout/403/malformed-list tests |
| R10 | Dry-run and apply consume the same plan and non-mutating guard results | Contract test compares refs, diffs, blockers, write targets, ownership, and reversibility |
| R11 | Race-sensitive state, ownership, collision, and confirm-window invariants are revalidated under the commit lock | Multiprocess/fault tests tied to #20, #63, #69, and #100 |
| R12 | A resource without complete-object preservation proof blocks before write | Fixtures with unknown fields and redacted secrets fail preflight |
| R13 | Existing host-scoped candidate intent causes a visible refusal before directory planning can write | Candidate-empty/nonempty tests across shell, scriptable, and library paths |
| R14 | Rebase is explicit and produces a new visible plan; the original approval cannot authorize changed intent silently | Concurrent-change test asserts new plan identity and confirmation |
| R15 | One transaction covers every declared change in dependency order and uses the existing journal/recovery engine | Multi-kind apply test plus injected failure/rollback |
| R16 | Shared write-target collisions are reported before the first write with target and every conflicting ref | #69 entry-point parity and structured-output tests |
| R17 | Human and JSON output contain the same named outcome, completeness, scope, per-ref operation, evidence, blockers, and errors | Golden schema tests coordinated with #109 |
| R18 | Plan/output never contains plaintext secret values | Seeded sentinel tests across YAML, diff, plan, errors, JSON, and journal export |
| R19 | Every writable kind states its declarative capability and evidence: present-only, present+absent, or unsupported | Registry coverage test and evidence-ledger consistency test |
| R20 | No write-capable behavior is advertised beyond its live evidence level | Coverage/docs/ledger assertions plus versioned live evidence |
| R21 | Successful re-application of unchanged declarations is idempotent and produces no writes | Second-apply test asserts no mutation/save job |
| R22 | Partial, unknown, stale, denied, unreachable, timeout, and malformed results are never collapsed into no-change or success | Per-state tests derived from Constitution Principle II |
| R23 | Shell, scriptable CLI, and library entry points resolve the same declarations and produce equivalent plans/outcomes where exposed | Paired fixture tests; any intentionally unavailable surface must fail with an exact supported replacement |

## Outcome model

The machine contract uses named outcomes. #109 owns the shared numeric mapping.

| Outcome | Meaning |
|---|---|
| `no_change` | Every declared reference is safely known and already matches |
| `changes` | At least one create/update/delete is safely applicable |
| `blocked` | Intent is valid but a safety guard refuses it |
| `invalid` | Directory, declaration, schema, reference, or command is invalid |
| `incomplete` | Required live evidence is partial, unavailable, stale, denied, timed out, or malformed |
| `applied` | Every planned write and persistence step verified |
| `reverted` | Apply failed and every restoration verified |
| `revert_failed` | Restoration is failed, incomplete, or unverified and requires attention |

A top-level result includes declaration-set identity/digest, target identity,
scope, outcome, completeness, plan identity, transaction ID when applicable,
and per-reference results. It does not imply that undeclared live objects were
evaluated as desired state.

## Safety gates

Ratification of this specification does not waive implementation blockers.
Directory apply must not leave draft or be documented as shipped until:

- #20 has fail-closed ownership for every enabled kind;
- #24 quiesces timed-out async jobs before rollback;
- #63 prevents candidate lost updates and cross-target state collision;
- #64 requires exact and complete async outcomes;
- #69 covers every shared/full-object write target with structured collision
  output;
- #100 enforces transaction lifecycle state in the engine;
- #103 handles verification exceptions and verifies rollback;
- #106 protects secrets in persisted and exported state;
- #110 makes journal recovery fail closed on corrupt history;
- #112 makes confirmation crash-consistent.

A resource with a narrower unresolved safety/evidence issue remains
declaratively unsupported even if the command ships for other kinds.

## Open questions

No product question is intentionally left for the implementation to guess.
Merge of #115 recorded owner approval of D1–D16. New questions
that materially change authority, deletion, safety, or evidence reopen this
spec rather than being absorbed in code review.

## Acceptance criteria

- [x] The owner approved D1–D16 through merge of #115; the package is recorded as 1.0.0 ratified
- [ ] Partial/additive authority is stated consistently in CLI help, README, roadmap, JSON schema, and coverage output
- [ ] Drift, dry-run, and apply share one intent universe and preflight
- [ ] Desired-only creates are visible before apply
- [ ] Undeclared live objects are visibly out of scope, never called clean or deleted
- [ ] Empty input is invalid and no implicit prune/delete path exists
- [ ] Explicit deletion is schema-valid, capability-gated, previewed, and rollback-verified
- [ ] Unknown, unmodeled, redacted, and write-only fields cannot be erased by partial replacement
- [ ] Every P0/safety gate above is closed or the affected capability remains unavailable
- [ ] Named outcomes and numeric exits agree with #109 across interfaces
- [ ] Installed-artifact tests exercise the schemas, loader, drift, dry-run, and apply paths
- [ ] Evidence and documentation state what is mock-only and what is live-write verified

## Evidence expected

**At ratification:** design evidence only. The package records decisions and
testable requirements; it does not prove implementation.

**Before merging implementation:** unit, property, multiprocessing,
fault-injection, exact-request, mock integration, and installed-wheel evidence.
Mutation testing removes each safety guard and proves its acceptance test fails.

**Before enabling a resource for declarative writes:** versioned live evidence
for read, materialization, write, persistence, verification, deletion where
supported, and rollback. BGP, OSPF, static routes, and any secret-bearing or
full-object-replacement resource remain explicitly unverified/unsupported until
their open resource issues are resolved.

**Not claimed by v1:** authoritative reconciliation, pruning, full-fabric
ownership, or live-write support for every registered kind.
