# EdgeConnect Orchestrator / ECOS — the constraining reality

**Primary sources:** this repository. The vendored OpenAPI baselines
(`src/pyecsdwan/_specs/orchestrator-openapi-7.2.0.json`,
`appliance-openapi-7.2.0.json`), the resource module docstrings, and the
findings recorded in `docs/futures/README.md` and `docs/sitrep/`.
**Accessed:** 2026-08-27 · **Baseline version:** 7.2.0 (1833 operations)

This entry exists because #73 requires that "EdgeConnect realities override
aesthetic mimicry when the data source cannot support a command honestly." A
grammar borrowed from Junos or NSO is worthless if the API underneath cannot
answer it. These are the constraints every decision above must survive.

## Constraints

1. **Most Orchestrator-scope surfaces are GET-only; the write path is the
   appliance API through `/appliance/rest`.** Epic #4's headline finding was
   that "orchestrator-scope breadth" was a misnomer — ACLs, all of NAT, all
   five QoS/route/optimization surfaces and four of five common settings
   landed appliance-scope. A grammar implying fabric-level writes to those
   would be describing an API that does not exist.

2. **Reads have wildly different costs.** An Orchestrator-scope GET is one
   call. The equivalent "for every appliance" view is a bounded, concurrency-
   capped fan-out (`reports/fanout.py`), and one unreachable appliance must
   become a marked row rather than a lost report. Cost is therefore a property
   the grammar has to expose, which no source in this corpus needed to do.

3. **Some read-shaped endpoints mutate.** `GET /oro/debug/closeGrpcConnection`
   closes a live gRPC link; `DELETE /debug/generic/{type}` deletes a module's
   data. 53 `get_*` methods in the vendored SDK issue POSTs. "It starts with
   show, so it is safe" is not true here, which is why the `show run appliance`
   allowlist is deny-by-default and why #67 exists.

4. **The CLI is not the only writer.** The Orchestrator UI, template pushes and
   other automation write concurrently. #63 made commit-time drift fail closed
   because of this. Any command implying a stable local mirror of running
   config — NSO's model — would be lying.

5. **Applied is not persisted.** A write can be accepted without being saved to
   flash, and `save_changes()` currently returns SUCCESS when no action key
   comes back (#64). NVUE's applied/startup vocabulary (D-NVUE-1) is needed
   precisely because this distinction is real here.

6. **Template ownership can silently revert an operator's change.** A
   template-managed section written directly is reverted by the next template
   push. `ownership.py` detects this, and ~10 kinds still carry UNVERIFIED
   section names (#20). Any configuration view should be able to say who owns
   what it is showing.

7. **Coverage is uneven and honest about it.** 119 of 1833 endpoints curated,
   5 generated, 1709 raw-only. The grammar must accommodate a kind that exists
   at Tier 0 only, rather than implying uniform support.

## Decisions

* **D-EC-1.** Scope is mandatory and explicit; there is no implicit "this
  device". *(Constrains D-IOS-3.)*
* **D-EC-2.** Cost class is part of every command's contract, and a fan-out
  says so before running. No source in this corpus supplies this — it is
  EdgeConnect-specific and must be invented rather than borrowed.
* **D-EC-3.** Per-appliance outcomes stay distinguishable in every multi-
  appliance view; a partial result is labelled partial, never quietly trimmed.
  *(Agrees with D-GNMI-3's one-notification-per-path rule.)*
* **D-EC-4.** A configuration view surfaces template ownership where known,
  and says "unverified" where the section mapping is a placeholder rather than
  implying it is unowned. *(Principle V.)*
* **D-EC-5.** Where a kind exists only at Tier 0, the grammar exposes it as
  such rather than pretending to a curated view. *(Principle V.)*
