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

- ~~**Tier-1 codegen**~~ — shipped as epic #6 (#25 `spec_sync.py`, #26
  `gen_models.py`, #27 `gen_plugin.py`, #28 `show coverage`, #29 the
  promotion gate). What remains deferred from it: curating the generated
  stubs (each is a Tier-1 → Tier-2 promotion of its own), and the two
  emitter limits in the list below (the map-key heuristic, the `__all__`
  ordering divergence).
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

## Epic #4 (Phase 3, orchestrator-scope breadth) — deferred work

Filed during the 2026-08-27 epic-#4 fanout; see
`docs/sitrep/2026-08-27-epic4.md`. Grouped by theme rather than by issue,
since several workers independently proposed overlapping items.

### Not modelable as written (evidence recorded, don't retry blindly)

- **Per-appliance subnet-sharing options** (#37's second half). `POST
  /subnets/setSubnetSharingOptions?nePk=` has **no read path anywhere**:
  POST-only in the orchestrator spec, no ECOS counterpart returning
  `auto_subnet`, and live `GET /subnets?nePk=` is the learned-route table
  (the only `auto`-ish key is a per-route `state.automatic` boolean). A
  write-only endpoint cannot be Tier 2 — no read means no `fetch()`, hence
  no snapshot, no diff, no rollback, and fabricating a current state is what
  the promotion checklist forbids. Returns if either (1) a read endpoint
  surfaces, or (2) it's modeled as a Tier-0-style fire-and-forget action
  outside the transaction engine. Evidence in
  `resources/internal_subnets.py`'s docstring.
- **Inter-segment D-NAT** (#32). `/dnatMaps` is orchestrator-GET-only and
  absent from the appliance spec entirely. Ships as the read-only view
  `nat.inter_segment_dnat_maps()` rather than a resource that couldn't
  honor its own contract.
- **Removing a single regional-overlay entry** (#35). No endpoint removes
  one `[overlayId][regionId]` pair, so `regional-overlay` is
  `deletable=False` and a rollback that would need to remove a
  newly-created entry fails loudly rather than reaching for the
  under-specified exhaustive `POST /gms/overlays/config/regions`. Closing
  this needs a live-verified body shape for that POST's array-of-map (is
  the overlay id the array index, or the config's own `id`?).

### Write semantics needing live confirmation

Every epic-#4 write path is spec-confirmed but **not live-write-tested** —
this session's live access was read-only throughout. Before trusting any of
them with a real change, do a no-op round trip first (GET current, commit it
back unchanged, confirm the re-plan diffs empty).

- **`PUT /gms/overlays/config/regions` merge-vs-replace** (#35). `apply()`
  does a defensive read-modify-write that is correct either way; if PUT
  provably merges, it can send the single entry and drop one GET per apply.
- **`POST nat/natPools` replace-vs-merge** (#32). Unlike `natMaps` it has no
  `merge` option and the spec is silent, so removed ids are `DELETE`d
  individually before the plural POST. Confirming semantics would drop that
  pre-pass.
- **`POST acls` with `options.merge=false`** (#31) — confirm it really
  deletes ACLs absent from the body; the REVERSIBLE guarantee rests on it.
  Also capture a real `GET dependency/acl/{name}` response so the
  fails-closed parser can stop being shape-tolerant.
- **The live `natMaps`/policy-map `options` key set** (#32, #33). This pass
  confirmed the block exists and that `activeMap` is in it; the rest of the
  handling is spec-driven.
- **`nonDefaultRoutes` / `segmentedIpv6Enabled` on write** (#37) — the
  latter is live-present but **absent from the vendored 7.2.0 spec**, so it
  rides unknown-key passthrough. Confirm the server accepts and preserves it
  on POST, and that CIDRs echo back in the same canonical spelling
  `ipaddress` produces (a mismatch would be permanent phantom drift).

### Template-section names still UNVERIFIED (feeds #20)

This session confirmed a real Default Template Group's selected-section list:
`adminDistance, cli, dns, datetime, logging, mgmtServices, routes,
secureWebServicesConfig, shaper, snmp, webconfig`. So `snmp`, `logging`,
`mgmtServices`, `dns`, `shaper`, `routes` are now **live-confirmed** in
`ownership.KIND_TO_TEMPLATE_SECTIONS`, and `datetime` is unclaimed — it's the
section a future NTP/time resource should take.

Still UNVERIFIED and worth one pass against a group that selects them:
`appliance/deployment`, `appliance/zones`, `appliance/acl`,
`appliance/nat-maps`, `appliance/nat-pools`, `appliance/qos-map`,
`appliance/optimization-map`, `appliance/route-map`,
`appliance/inbound-shaper`, `appliance/banners`, plus the long-standing
`bgp`/`ospf`/`vrrp`/`dhcp` placeholders.

### Follow-on resources (surfaces identified but not built)

- **#34 service orchestration** — deferred entirely; see the scope comment on
  the issue. `/thirdPartyServices/*` is ~100 endpoints across 8+ vendor
  integrations and is an epic, not an issue. Start with the vendor-neutral
  `serviceOrchestration/*` subtree.
- **Branch NAT** (`nat/maps`, with per-rule `prio` granularity) — a second
  appliance NAT write surface supporting true COMPENSABLE per-rule deltas
  instead of full-table replace. Plausibly the claimant of the pre-seeded
  bare `"appliance/nat"` ownership key.
- **`qosMaps/dscpOverride`**, **a curated `trafficclass` resource** (the
  traffic-class name table both shapers and QoS maps reference; exposed
  read-only today as `shapers.traffic_classes()`), and **per-map delta
  writes** (`qosMaps/{mapName}` DELETE, `deleteMultiple`, `shapers/{id}`)
  — all #33.
- **`routeMap/dependencies/{name}`** as an `apply()` pre-flight, mirroring
  what #31 built for ACLs.
- **DNS proxy / DNS cache / `logging/remote` / per-appliance NTP** (#38's
  deferred half). ⚠️ `logging/remote`'s `self` key is a **nested settings
  object** (`fac`, `min_severity`, `port`, `protocol`, `sslCert`…), *not* an
  id echo — applying `_strip_meta` to it would destroy the receiver config.
- **`/ipObjects/*/bulkUpload` and `/merge`** (#31) — a batching path for
  large address/service-group loads.
- **`/applicationDefinition/appExpressAppConfig`** (#31) — sibling of the
  group config, natural follow-on.

### Framework / contract friction surfaced by this epic

- **`security_policy._inject_self` is security-maps-specific, and three
  workers independently discovered it the hard way.** It hardcodes the
  map → zonePair → `prio` → rule nesting, so on any shallower shape it
  writes the echo into the wrong container (`self: "prio"` into a NAT
  priority table; `self: "entry"` into an ACL entry container). Each of #31,
  #32 and #33 wrote its own correct depth variant and pinned the divergence
  with a regression test. `_strip_meta` by contrast is generic and was reused
  verbatim everywhere. Worth either generalizing `_inject_self` to take a
  depth/path spec, or renaming it to advertise that it is not generic.
- **No plugin-level channel for per-commit operator intent.** `force` /
  `override_template` / `allow_untransactional` stop at `txn._guard()` and
  never reach `apply()`. Resources that need an out-of-band "yes, cascade"
  signal now improvise three different ways: `zones.py` derives it from the
  diff, `interface_labels.py` (#39) and `acls.py` (#31) stage a directive in
  desired state. An `options: Mapping[str, Any]` on `Ctx` or `Diff`,
  populated from commit flags, would remove the improvisation — and would
  let #39's `deleteDependencies` become the real `--delete-dependencies`
  flag its issue asked for.
- **`canonicalize_desired()` isn't given `current`.** `txn.build_plan` has it
  in hand one line earlier. Passing it would let plan-time constraint checks
  (#39's overlay-usage refusal, #31's ACL dependency pre-flight) run without
  a redundant `fetch()` per plan.
- **`normalize()` has no `ctx`**, so name↔id resolution and resolver-enriched
  error messages can only happen in `canonicalize_desired()`. Plugins that
  want friendly errors about *server* state duplicate validation or degrade
  to raw ids.
- **No "list is a set" vs "list is an order" marker on the contract.** #36's
  template-group priority list is the first place where sorting in
  `normalize()` would be a *bug* — order is the configuration. A declared
  property (or a line in `docs/plugin-promotion.md`) would stop the next
  reader from "fixing" it. Relatedly, `diffing.structural_diff`'s comment
  says canonical lists "are stably sorted by normalize()", which is now only
  half true.
- **A `merge_desired = True` class flag** would make the full-object-replace
  contract explicit instead of every such resource restating it in prose
  (`common_settings`, `internal_subnets`, and most of this epic).
- **`txn.build_plan`'s `deletable=False` error hardcodes an
  interface-labels-shaped hint** (`delete <kind> <name> wan <label-id>`),
  which reads as nonsense for a settings singleton like `appliance/snmp`. A
  per-resource `delete_hint` attribute would fix it.
- **`Ctx.resolver` is typed non-optional but constructed as `None` across
  `tests/`**, forcing local `Resolver | None` widening to keep
  `mypy --strict` from calling guards unreachable.
- **`resources/overlays.py`'s `Bio.apply` does `body.setdefault("name",
  ref.name)` while its `normalize()` doesn't strip `name`** — if a desired
  state ever lacks `name`, post-apply `verify()` would see the injected key
  as drift. Not hit today because intent always carries a name.
- **`specs.payload_examples()` picks the payload-example artifact by
  lexicographic glob order** (`sorted(...)[-1]` over `payload-examples-*.json`),
  which is version order only while releases stay single-digit: a future
  `payload-examples-9.10.json` would sort *before* `payload-examples-9.6.json`
  and silently lose. Harmless today — `tools/postman_sync.py` refuses to leave
  more than one artifact in `specs/` — but the selection wants a version-aware
  sort before 9.10 ships. Found while building #51; not fixed here because
  `specs.py` was owned by parallel work.
- **`loopback.reclaim_deleted_ips()` builds a path that does not exist.** It
  calls `DELETE /loopbackOrch/pool/reclaim/{id}` when given a `loopback_id`,
  but neither vendored baseline has that path — the Orchestrator spec's
  `DELETE /loopbackOrch/pool/reclaim` takes `id` as a **required query
  parameter** ("Reclaim all deleted ip addresses or Reclaim deleted ip address
  by id"). Both call forms are therefore wrong: the by-id one 404s, and the
  "all" one omits a parameter the spec marks required. Found by #28's
  declared-endpoints-exist-in-spec check; the function is a maintenance
  helper no `apply()` path reaches, so it was left alone rather than fixed
  in a coverage change. `Resource.endpoints` for `loopback-orch` declares only
  the real `DELETE /loopbackOrch/pool/reclaim`.
- **`appliance POST /virtualif/loopback` exists but `appliance/loopback` still
  refuses to write.** The module says "no documented endpoint" for the write
  path and `apply()` raises; the appliance baseline does list POST (and
  per-interface POST/DELETE on `/virtualif/loopback/{loopbackName}`). Worth
  re-checking against live gear — it may be promotable from IRREVERSIBLE
  read-only to a full curated resource.
- **`security-policy` and `appliance/vrrp` implement no `list_refs()`**, so
  `ec-cli show <kind>` and any registry-wide sweep (drift reports, the #29
  promotion gate) cannot enumerate them and must be handed a ref. For
  `security-policy` that is arguably unavoidable — the Orchestrator exposes no
  endpoint listing the configured `srcSeg_dstSeg` pairs, so enumeration would
  mean guessing from the segment table. For `appliance/vrrp` it is a cost
  decision: enumerating means one proxy GET per appliance. Both are declared
  with their reasons in `PROBE_REFS` in `tests/test_promotion.py`; if either
  grows a `list_refs()`, that entry must be deleted (the suite asserts it).
- **The `gen_models.py` map-key heuristic is name-shaped, and a few vendor
  spellings slip past it** (issue #26). A `properties` block is read as a
  mapping when *every* key is all digits, angle-bracketed, an appliance
  primary key (`1.NE`) or an IPv4 literal — which covers the ~485 map-shaped
  blocks in the two baselines. It still reads three families as records:
  ASCII placeholder keys the vendor wrote without brackets (`x.x.x.x`,
  `x.x.x.x/x`, `a number from 1 to 1000`), interface-name keys (`lan0`/`lan1`
  in `poe/config`, arguably real fields), and the 32 *mixed* blocks like
  `{"<nePk>": ..., "header": ...}`, where the map-shaped names are dropped
  and absorbed by `extra="allow"` rather than typed. All three still produce
  valid, drift-tolerant models; they just under-type. A value-shaped rule
  (all sibling values share one schema, key names vary) would catch the rest
  and is the obvious next iteration.
- **`gen_models.dunder_all_order` and ruff disagree on one family of names.**
  The helper reproduces `RUF022`'s isort-style order by splitting digit runs
  and comparing them as integers; ruff uses `strnatcmp` semantics, where a
  digit run with a *leading zero* is compared as a fraction and sorts before
  the whole numbers. So `..._081e94` (helper: 81) lands after `..._1e304d`
  where ruff wants it before. Invisible to `gen_models.py` — its committed
  samples carry no such slug — and invisible to a normal `gen_plugin.py` tree
  of a handful of stubs. It only surfaces if hundreds of hash-disambiguated
  stubs share `resources/generated/__init__.py`, and `gen_plugin.write()`'s
  ruff pass fixes the file on disk. Found by generating all 837 write
  operations into the tree at once (#27); not fixed here because
  `tools/gen_models.py` was owned by parallel work.
