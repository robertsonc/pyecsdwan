# Plugin promotion checklist (Tier 0 → 1 → 2)

Transactional safety is never auto-granted: an OpenAPI spec cannot express
reversibility, async-job semantics, or template ownership. Coverage grows
through three tiers; `ec-cli show coverage` reports where every kind stands.

**Tier is not evidence (#66).** This document is about how carefully a resource
was *written*, which is decided in a code review and tops out at Tier 2. What
anyone has *seen it do* on real gear is a separate, independent axis — the
evidence ladder in `docs/live-validation.md`, recorded per resource in
`src/pyecsdwan/_evidence/ledger.json` and reported by
`ec-cli show coverage --evidence`.

A resource can be immaculately curated and have never touched a fabric. Today
all 41 of them are: every curated kind sits at `mock-verified`, and no write
path anywhere in this tool has been run and rolled back on real gear at a
recorded version. Completing every box below does not change that, and
`ec-cli plugin promote` now says so rather than letting a green checklist read
as production readiness.

## Tier 0 — raw passthrough (day-0 coverage of everything)

`ec-cli api get|post|put|delete <path> [--body file] [--appliance NAME]`

- Journaled for audit (state `AUDIT_ONLY`), prints the no-rollback-guarantees
  banner, never enters candidate config or `commit confirm`.
- Nothing to promote — this tier is the floor, not a plugin.

## Tier 1 — generated from spec

Three tools, in order (`tools/README.md` has the invocations):

1. `tools/spec_sync.py --diff` detects an endpoint the vendored baseline does
   not carry; `--update` vendors it.
2. `tools/gen_models.py` emits pydantic models + a typed binding for one
   operation.
3. `tools/gen_plugin.py` turns that binding into a registered plugin stub in
   `src/pyecsdwan/resources/generated/`. `--from-diff` drives it straight off
   the report from step 1, one stub per path.

The emitted stub has:

- `fetch`/`apply`/`rollback` wired to the generated bindings, plus
  `ctx.save_changes([nePk])` on appliance scope
- a best-effort GET-before-write snapshot where the spec exposes a `GET` on
  the write's path — and `reversibility = COMPENSABLE` only then. With no
  `GET` there is nothing to snapshot and the stub declares `IRREVERSIBLE`
  instead, because a compensator `rollback()` cannot run is worse than the
  truth. A paired `DELETE` on its own does not change that: without a `GET`
  the stub cannot tell a create from an update, and deleting to "compensate"
  an update would destroy live configuration. It never claims `REVERSIBLE` —
  nothing in a spec promises the `GET`'s response is accepted verbatim by the
  write.
- `normalize()` raising `NotCurated` until a human/agent finishes it
- `tier = Tier.GENERATED`

Unlike `pyecsdwan.generated`, a stub is meant to be **hand-edited** — it is
the starting point for curation, and `gen_plugin.py` refuses to overwrite one
without `--force`.

Tier-1 resources refuse `commit confirm` unless the operator passes
`--allow-untransactional`, and the plan output flags the downgrade. That guard
is enforced at runtime by `txn.py`; the `NotCurated` raise above is enforced
statically by `make check` (see *What the gate actually checks*), so a stub
that was wired up far enough to return from `normalize()` fails in the repo
rather than looking curated on a fabric. That gate parametrizes over the live
registry, so it applies to every stub anyone generates — not only the ones
committed here — with nothing to opt into.

Note what the `NotCurated` raise costs, deliberately: `txn.build_plan()` calls
`normalize()` on every candidate, so a Tier-1 stub cannot take part in a plan
or a commit **at all** until someone curates it. The wiring below `normalize()`
is there so that curation is a small edit against working code, not so the
stub can be pointed at a fabric today.

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

Since #27 this half is no longer vacuous over the live registry — the two
committed samples in `pyecsdwan.resources.generated` are real Tier-1 kinds
that it bites on. It also covers stubs that are not in this repo: the check
is parametrized off `default_registry.kinds()`, so generating a stub is all
it takes to be held to the rule.

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

**The gate is proved to bite.** Passing input is not evidence a gate works, so
the same functions are run over constructed resources that must FAIL: one that
returns from `normalize()` instead of raising, one that raises the wrong error,
one whose `normalize()` is not a fixed point, one that canonicalizes everything
to an empty shell, and one whose re-plan is dirty. (That proof was written when
the registry held no Tier-0/1 kind at all and the un-curated half passed
vacuously; #27's committed stubs mean it now has real subjects too, but the
constructed failures stay — a *passing* Tier-1 kind still cannot show that the
check would fail a broken one.)

## Running the checklist against your own fabric

```
ec-cli plugin promote <noun> [--name INSTANCE] [--appliance NAME] [--json]
```

`<noun>` is what an operator types — `banners`, not `appliance/banners`;
registry keys are internal and not accepted (#74). Where a noun names two
different objects, `--appliance` picks: `plugin promote zones` checks the
Orchestrator's zone table, `plugin promote zones --appliance BR1-EC` checks the
appliance's. The `--json` output reports the *resolved* kind, so it is never
ambiguous about which one was checked.

Same checks, pointed at a real Orchestrator rather than the mock — worth
running before promoting, because your fabric's state exercises shapes the
fixtures do not. It is read-only (one `fetch()` for the sampled instance) and
exits non-zero when a box fails. It also lists the human-judgment boxes above,
so they stay visible.

**On a Tier-1 stub it reports "not ready" and exits non-zero, by design.** The
only box it can evaluate there is the `NotCurated` refusal, and that one passes
*because* the stub is still a stub. The Tier-2 boxes cannot run at all while
`normalize()` raises, so there is nothing to be green about — implement
`normalize()` first, set `tier = Tier.CURATED`, then re-run to have the Tier-2
boxes actually evaluated. `--json` reports this as `green: false` with
`tier2_evaluated: false`. An un-curated kind needs no sample instance, so the
command answers without one (generated stubs implement no `list_refs()`).

It does **not** flip the tier. `tier = Tier.CURATED` is a source declaration a
reviewer signs off on, not a runtime toggle; when every machine-checkable box
is green the command prints the change to make and leaves it to you.

It also prints the resource's **evidence level**, and says plainly when a green
checklist is a statement about the code and not about any fabric. Those are the
two things a reader is most likely to conflate, so they are printed together.

**Caveat on Tier-1 kinds** (see `docs/futures/README.md`): the command gates
the idempotency checks behind `tier >= CURATED`, so for a generated stub the
only box it evaluates is the `NotCurated` refusal — which passes, and it then
prints the promote-now message. Ignore that on a stub: implement `normalize()`
first, and treat the command as useful from the moment the raise is gone.
`make check` is not fooled either way — a Tier-2 kind whose `normalize()`
raises fails the gate immediately.
