# Plugin promotion checklist (Tier 0 → 1 → 2)

Transactional safety is never auto-granted: an OpenAPI spec cannot express
reversibility, async-job semantics, or template ownership. Coverage grows
through three tiers; `ec-cli show coverage` reports where every kind stands.

## Tier 0 — raw passthrough (day-0 coverage of everything)

`ec-cli api get|post|put|delete <path> [--body file] [--appliance NAME]`

- Journaled for audit (state `AUDIT_ONLY`), prints the no-rollback-guarantees
  banner, never enters candidate config or `commit confirm`.
- Nothing to promote — this tier is the floor, not a plugin.

## Tier 1 — generated from spec

Produced by `tools/spec_sync.py` from `specs/*.json` for new endpoints:
pydantic models + typed bindings + a plugin stub with:

- `fetch`/`apply` wired to generated bindings
- best-effort GET-before-write snapshot; `reversibility = COMPENSABLE` at most
- `normalize()` raising `NotCurated` until a human/agent finishes it
- `tier = Tier.GENERATED`

Tier-1 resources refuse `commit confirm` unless the operator passes
`--allow-untransactional`, and the plan output flags the downgrade. That guard
is enforced at runtime by `txn.py`; the `NotCurated` raise above is enforced
statically by `make check` (see *What the gate actually checks*), so a stub
that was wired up far enough to return from `normalize()` fails in the repo
rather than looking curated on a fabric.

## Tier 2 — curated (earns full commit-confirm)

Promotion checklist — every box, no exceptions. Boxes marked **[gate]** are
machine-enforced by `make check` (`tests/test_promotion.py`, over
`pyecsdwan.registry`); the rest are human judgment and are *reported*, never
decided, by the tooling. Nothing below is optional because a machine cannot
check it.

- [ ] **[gate]** `normalize()` implemented: server-generated IDs and injected
      defaults stripped, lists sorted by stable keys, names↔IDs resolved via
      the resolver. Proved directly: `normalize(normalize(x)) == normalize(x)`
      on a sample fetched through the resource's own `fetch()`.
- [ ] **[gate]** Idempotency: canonical state fed back through
      `canonicalize_desired()` re-plans to an empty diff — applying the same
      intent twice writes once.
- [ ] Reversibility class truly set — REVERSIBLE only when snapshot/restore is
      exact; COMPENSABLE when only a compensator exists (rollback() implements
      it, tolerating an absent object); IRREVERSIBLE when neither (refuses
      confirm windows, requires --force).
- [ ] Async jobs: every action key polled via `jobs.wait_for_action`;
      `apply()` returns only after terminal state; TIMEOUT == failure.
- [ ] Appliance-scope resources: `managed_by()` implemented (template
      association × selection join; `gms_marked` where the API has it), and
      appliance-proxy writes persisted via `ctx.save_changes([...])` — one
      batched call at the end of `apply()`/`rollback()` covering every
      appliance the operation wrote to; a non-SUCCESS outcome fails the
      operation.
- [ ] `dependencies` declared for ordering (e.g. group before association).
- [ ] Docstring notes any spec-vs-live divergence; unknown fields pass through.
- [ ] **[gate]** `tier = Tier.CURATED`, registered in `resources/__init__.py`,
      `make check` green. Until it is, `normalize()` must keep raising
      `NotCurated` — a stub that quietly returns is what the gate exists to
      catch.

Where the source schemas are missing (EC_SD-WAN_Expert has no payload and the
spec is silent), the resource stays a stub with `NotImplementedError` and a
TODO naming the missing data. Do not invent payloads.

## What the gate actually checks (issue #29)

`tests/test_promotion.py` runs two obligations over the live plugin registry,
so a kind is covered the moment it is registered — there is no list to keep in
step and no test-name convention to break.

**Un-curated kinds must refuse.** Every resource below `Tier.CURATED` must
raise `NotCurated` from `normalize()`. Raising `NotImplementedError`, or
returning anything at all, fails. This is the static half of the guard
`txn.py` enforces at runtime (a confirm changeset containing a Tier-0/1
resource is refused without `--allow-untransactional`): the point is that a
generated stub left un-curated fails in `make check`, not on a fabric.

**Curated kinds must be idempotent, provably.** One parametrized test per
curated kind runs four checks against a sample raw state:

| check | meaning |
| --- | --- |
| `normalize-runs` | `normalize()` does not raise on real server state |
| `sample-non-trivial` | the sample canonicalizes to something with leaf values |
| `normalize-idempotent` | `normalize(normalize(x)) == normalize(x)` |
| `replan-empty` | re-planning canonical state produces no diff |

`sample-non-trivial` is the one that keeps the rest honest. `{"templates": {}}`
is a truthy dict on which idempotency holds for free; container truthiness is
not evidence, so only leaf values count.

**Where the samples come from.** Per kind, in order: `list_refs()` against the
bundled mock Orchestrator, taking the first instance whose state carries
content (39 of 41 kinds today); or a declared probe ref, for the two kinds that
implement no listing because the API exposes none (`appliance/vrrp`,
`security-policy`). Both rungs go through the resource's own `fetch()`. There
is no opt-out list — the table exists, is empty, and is asserted empty, so a
kind cannot drop out of coverage silently.

Two mock tables ship empty and are seeded by the gate. The security-policy seed
is the vendor's published Postman payload example (`specs.payload_example`,
#51): **shape only** — the vendor writes `"string"` / `0` / `true` into every
scalar, so it proves field names and nesting round-trip and proves nothing
about values.

**The gate is proved to bite.** The registry holds no Tier-0/1 kind today, so
the un-curated half would pass vacuously on the registry alone. The same
functions are therefore run over constructed stubs that must FAIL: one that
returns from `normalize()` instead of raising, one that raises the wrong error,
one whose `normalize()` is not a fixed point, one that canonicalizes everything
to an empty shell, and one whose re-plan is dirty.

## Running the checklist against your own fabric

```
ec-cli plugin promote <kind> [--name INSTANCE] [--appliance NAME] [--json]
```

Same checks, pointed at a real Orchestrator rather than the mock — worth
running before promoting, because your fabric's state exercises shapes the
fixtures do not. It is read-only (one `fetch()` for the sampled instance) and
exits non-zero when a box fails. It also lists the human-judgment boxes above,
so they stay visible.

It does **not** flip the tier. `tier = Tier.CURATED` is a source declaration a
reviewer signs off on, not a runtime toggle; when every machine-checkable box
is green the command prints the change to make and leaves it to you.
