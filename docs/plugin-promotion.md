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
`--allow-untransactional`, and the plan output flags the downgrade.

## Tier 2 — curated (earns full commit-confirm)

Promotion checklist — every box, no exceptions:

- [ ] `normalize()` implemented: server-generated IDs and injected defaults
      stripped, lists sorted by stable keys, names↔IDs resolved via the
      resolver. Prove it: `normalize(normalize(x)) == normalize(x)`.
- [ ] Idempotency test: apply desired state, re-plan, diff is empty
      (`tests/` has per-resource round-trip tests to copy from).
- [ ] True reversibility class set — REVERSIBLE only when snapshot/restore is
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
- [ ] `tier = Tier.CURATED`, registered in `resources/__init__.py`,
      `make check` green.

Where the source schemas are missing (EC_SD-WAN_Expert has no payload and the
spec is silent), the resource stays a stub with `NotImplementedError` and a
TODO naming the missing data. Do not invent payloads.
