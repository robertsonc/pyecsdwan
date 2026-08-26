# Futures / deferred roadmap

Reconcile at the end of each session: anything implemented moves out of here
(note the session in `docs/sitrep/`).

## Direct-to-appliance access via RBAC broker (explicitly out of scope for v1)

v1 talks to appliances only through the Orchestrator proxy
(`/appliance/rest?nePk=&url=`), because the appliance's own REST API
(`https://<appliance>/rest/json`, admin/password auth) has no RBAC — anyone
with the credential owns the box. The future design is a separate gateway
project: a broker that holds appliance credentials, enforces per-operation
policy (read / write / destructive scopes, per-user, per-appliance), audits
every call, and exposes a narrow API the CLI can target when the Orchestrator
proxy is down or too slow. Until that exists, direct mode stays out.

## Other deferred items

- **Tier-1 codegen** (pydantic models + plugin stubs from `specs/`, `ec-cli
  show coverage` over generated stubs, promotion-checklist gating): epic #6,
  issues #26–29. `tools/spec_sync.py` itself (fetch + diff the OpenAPI spec
  against the `specs/` baseline) shipped and closed as #25 — this item is
  now just the codegen/gating layer built on top of it.
- **Fabric-wide drift report** (`ec-cli drift`): every curated kind × every
  appliance, diff against declared desired-state files; CI-friendly exit codes.
  Phase 3 / epic #8, not yet sharded into an issue.
- **BGP/OSPF write path Stage 2 — live-write verification** (issues #16,
  #17): `apply()`/`rollback()` are now implemented (full-object POST via the
  appliance proxy, `reversibility = REVERSIBLE`) against endpoints confirmed
  to exist in `specs/appliance-openapi-7.2.0.json`, following the exact
  proxy pattern five other appliance-scope resources already use live. **Not
  yet live-write-tested** — this environment's own safety tooling blocked
  the in-session verification attempt before it could run a single POST.
  Whoever tests this live should start with a true no-op round-trip (GET
  current config, `commit` it back unchanged, confirm the replan diffs
  empty) before trusting it with a real change — see each resource's module
  docstring for the same recommendation.
- **`appliance/loopback` write path** (issue #18): no documented write
  endpoint; candidate is `POST virtualif/loopback` via the appliance proxy,
  unconfirmed. `loopbackOrch`'s structure and the `mgmtIp`/`mgmtIP` casing
  fold are implemented defensively (issue text + vendored SDK docstrings) but
  not yet verified against a live *populated* fabric — this session's lab
  Orchestrator returned an empty `loopbackOrch`.
- **`subnets3/configured/{addMultiple,deleteMultiple}` write shapes**
  (`resources/routes.py`, issue #15): only the `GET subnets3/configured` read
  shape was captured live this session; the write bodies are inferred from
  SDK convention. Worth a live-write confirmation before production use.
- **Template-ownership section names, the rest of them** (issue #20): this
  session confirmed `dns`/`routes`/`shaper` are real section names against a
  live Default Template Group; `bgp`/`ospf`/`vrrp`/`dhcpd`+`dhcpFailover`/
  `natMaps`/`securityMaps`/`deployment`+`interfaces`/`zones` remain
  `UNVERIFIED` placeholders in `ownership.KIND_TO_TEMPLATE_SECTIONS` (that
  group simply didn't select those sections — neither confirmed nor
  contradicted). Needs a live Orchestrator with a template group that
  actually selects them.
- **Configurable rollback-history depth per resource kind** (large snapshots,
  e.g. full deployment objects, may deserve shorter history).
- **`commit confirm` via systemd user timer** (docs/watchdog-backends.md).
- **Shell niceties**: `show | compare` paging, `set` path completion *from
  resource schemas* (i.e. completing field paths within a resource's
  `desired_state_doc`, not the kind/appliance-name completion issue #49
  already fixed), multi-line YAML edit (`edit <kind> <name>` in $EDITOR).
- **Response-cache with TTL for read-heavy show commands** (the Orchestrator is
  a low-QPS control plane; see docs/research/expert-repo.md).
- **Live-Orchestrator session-login test.** The CSRF/logout-verb fix
  (`X-XSRF-TOKEN` echo, GET logout, loginType) is unit-shaped only; add a
  cassette/integration test against a real login flow when one is available.
  (Distinct from API-key auth, which this session validated works fine
  against live gear — this item is specifically about the interactive
  username/password session-login path.)
- **Resolver cache projection.** `/appliance` is cached whole; project it to
  the fields actually consumed (hostName/nePk/site/model) to avoid persisting
  inventory/recon data. File mode is already 0o600.
- **`OrchClient.appliance_request()` has no query-param passthrough.**
  `deleteDependencies` and similar flags aren't body fields, so
  `resources/appliance_zones.py` (#19) had to bypass the helper and call
  `ctx.client.post("/appliance/rest", ...)` directly (with an explicit
  `validate_ne_pk` call to keep the same safety net). An optional `params`
  kwarg on `appliance_request` would let future resources needing
  proxy-level query params stay DRY.
- **`ownership.py` does 2 API round-trips per `managed_by()` call, uncached.**
  `associated_groups`/`selected_sections` hit the Orchestrator fresh every
  time; a resource that also reads `gms_marked` from its own `fetch()` can
  cost 3 calls per planned item. Now that 8 appliance-scope resources call
  `managed_by()` routinely, a short-TTL per-plan cache (keyed by nePk/group)
  would cut this down across a multi-resource changeset touching one
  appliance.
- **Per-changeset "object lock" for resources sharing one server object.**
  `appliance/deployment` (#12) and `appliance/dhcp` (#13) can both stage
  changes to the *same* underlying `deployment` object in one commit; today
  each does a correct but order-dependent read-modify-write with no
  detection or reporting of the overlap (documented as a sharp edge in
  `dhcp.py`'s module docstring). Worth a real fix — e.g. an optional
  `Resource.write_target(ctx, ref) -> str | None` hook so `txn.py` can warn
  at plan time — if a third resource starts sharing an object; two is still
  proportionate to a docstring.
- **`Resource.list_refs()` has no cross-appliance enumeration support.**
  Its signature (`list_refs(self, ctx)`) can't express "every appliance ×
  this kind" without each resource re-deriving it from
  `ctx.resolver.appliances()` individually (as `#12`–`#19` all now do).
  Worth a registry-side helper once fabric-wide `show`/drift (epic #8) needs
  to enumerate appliance-scope kinds consistently.
- **`resources/__init__.py`'s append-only convention fights ruff's
  `I001`/`RUF022`.** Multiple Phase-2 workers this session had to insert
  their new import/`__all__` entry in true alphabetical order (not append at
  the end) to keep `make check` green, and two independently proposed the
  same fix: either a `per-file-ignores` entry for `I001`/`RUF022` on this one
  file in `pyproject.toml`, or just instructing future parallel workers to
  run `ruff --fix` rather than hand-place entries. Low risk, mechanical —
  worth doing before the next fanout session touches this file again.
- **Mock deployment GET is seed-but-not-persist on first read.**
  `mock/server.py`'s `deployment` handler returns a fresh `_seed_deployment()`
  on every unwritten GET rather than storing it, which surprised a test
  writer reaching into `state.appliance_ecos[...]["deployment"]` before any
  write had happened. Worth a one-line comment (or seeding it into
  `appliance_ecos` up front, matching the `vrrp`/`loopback`/`zones` pattern)
  so the next Phase-2 worker doesn't hit the same `KeyError` surprise.
