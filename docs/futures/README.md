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

## Legacy MCP server: rebuild or archive? (issue #62)

`contrib/mcp_server_legacy/` is quarantined — disabled by default, read-only
when enabled, direct-to-appliance tools removed, TLS on, credentials out of
tool arguments, and now covered by ruff/mypy/tests. What has *not* been
decided is what it should become:

* **Rebuild** it as a curated front end over `pyecsdwan.Resource`/`txn`, so an
  agent gets the same plan/journal/ownership/rollback guarantees an operator
  gets. Note this is not a port: the existing server wraps the vendored
  `pyedgeconnect` reference SDK, not this product, so a rebuild is new code
  against a different library. The natural shape is one tool per verb of the
  existing contract (`plan`, `commit`, `compare`, `rollback`, `show`) rather
  than one tool per endpoint — a few tools instead of 641.
* **Archive** it as a separate raw-SDK project, out of this repository
  entirely, and let this repo carry only the transactional surface.

Either way the quarantine stands in the meantime. The thing that should *not*
happen is the middle path it was on: a second product surface in the same
repository with none of the same guarantees.

## CONTRIBUTING.md is the upstream project's, not this one's

`CONTRIBUTING.md` still describes `black`, `flake8` and PyCharm — it was
inherited from the vendored `pyedgeconnect` project and describes neither this
repository's tooling (ruff, mypy `--strict`, pytest) nor its workflow. Noticed
while wiring up CI for #65 and deliberately left alone there: it is
documentation reconciliation, which is issue #68's subject.

## The mock cannot exercise per-appliance-distinct instance names

Every appliance-scoped kind in the bundled mock is a per-appliance singleton
named `global`. That makes the mock unable to reach the case appliance-scoped
instance discovery actually exists for — a kind whose instance *names differ
per appliance* — because deduplication alone resolves a singleton and the
appliance filter never has to do any work.

Found while fixing #76/#78: the first three tests written against the mock
passed with the filter deleted. The three that catch it use a synthetic
`list_refs` (`tests/test_show_scoping.py`), which is honest but isolates the
logic from the fixtures.

Worth giving one appliance-scoped kind genuinely per-appliance instances in
`mock/server.py` so the real path is covered end to end. Same class of gap as
the `/gms/versions` fixture in epic #54, where `installed[0] == current` let
the wrong implementation pass every assertion.

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
- **A shared write target has no `--json` surface, because `compare` has
  none.** #69 detects the collision at plan time, prints it in `compare`, and
  refuses at commit. The issue also asked for it in JSON output — but `diff`/
  `compare` takes no `--format`/`--json` today, so there is nothing to add the
  field to. It goes in when the plan gets a machine-readable surface (epic #8's
  bulk apply will want one anyway); the shape is `txn.Collision`, already a
  frozen dataclass carrying `target` and `refs`.
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

Since #66 that no-op round trip has a name (`no-op-round-trip`), a place to be
recorded (`src/pyecsdwan/_evidence/ledger.json`) and a protocol around it
(`docs/live-validation.md`). The specific questions below are *what to look at*
while running it; the ladder is *how the answer gets written down so it counts
next time*. The 2026-08-26 live reads are the cautionary case: real
observations, made against real gear, and unusable as support evidence because
nobody recorded the Orchestrator version.

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
  more than one artifact in `src/pyecsdwan/_specs/` — but the selection wants a
  version-aware
  sort before 9.10 ships. Found while building #51; not fixed here because
  `specs.py` was owned by parallel work.
- **Bulk loopback reclaim is documented but unexposed, and "reclaim all" is
  unresolved.** Fixing #60 settled the by-id call (`id` is a query parameter;
  `/reclaim/{id}` is not a route) but not the other half of the vendor's own
  summary, "Reclaim all deleted ip addresses **or** Reclaim deleted ip address
  by id" — that operation's only parameter is `id`, marked required, so one
  half of the sentence has no route behind it in anything vendored here.
  `reclaim_deleted_ips()` therefore requires an id and the all-mode is not
  offered. Two questions for whoever has a fabric: does an id-less
  `DELETE /loopbackOrch/pool/reclaim` reclaim everything or 400, and are
  `DELETE /loopbackOrch/pool/reclaimBySeg?segId=` and
  `.../reclaimBySegRegSubnet?seg=&reg=&subnet=` (both unambiguous, neither
  exposed) the intended bulk surface? See
  `resources/loopback.RECLAIM_ALL_HAS_NO_KNOWN_ROUTE`.
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

- **The mock's `GET /flow?overlays=` filter disagrees with the spec, and the
  Orchestrator overlay inventory cannot supply what the spec wants.** The
  vendored spec documents `overlays` as *overlay IDs* joined by `|`
  (`"1|2"`); `mock/server.py` matches *overlay names* split on `,`. Sending
  the spec's form returns nothing from the mock, and vice versa. Nothing
  today uses the filter — `show fabric flows summary` (#58) counts rows from one
  read per appliance instead, for reasons documented in
  `reports/flows.py` — so this is latent rather than broken. Whoever adds an
  `--overlay` filter must resolve it against live gear first, and will also
  hit the second half of the problem: `/gms/overlays/config` enumerates one
  overlay (`CorpFabric`, id 1) while flow rows name three (`RealTime`,
  `CriticalApps`, `Passthrough`), so there is no ID for most of what appears
  in the data. Left alone deliberately — changing the mock's filter
  semantics would move ground under parallel workers for a feature no
  command asks for.
- **The CLI passthrough allowlist has no `debug` read verbs, and that gap is a
  judgement call worth revisiting against a live appliance** (issue #56).
  `pyecsdwan.reports.applianceconfig.ALLOWED_VERBS` is `{show, display}`. The
  brief asked for "the `debug`-style read commands the vendor tool permits",
  but the vendored 9.6 payload examples show the appliance's `debug` namespace
  is not read-only — `DELETE /debug/generic/{}` deletes a module's data, and
  `GET /oro/debug/closeGrpcConnection` mutates behind a read-shaped verb
  (since #67 that endpoint is in `retry.MUTATING_GETS`, so it is never
  replayed even if a caller asks) — and
  on ECOS the `debug` CLI verb arms debug logging, which is appliance state
  and a load hazard on a busy box. Deny-by-default therefore refused all of
  them rather than guessing which are inert. If someone enumerates the exact
  read-only `debug` subcommands against a live appliance (or reads them out of
  `EC_SD-WAN_Expert` directly), they can be added as *exact two-token heads*
  (`debug <subcommand>`), never as a bare `debug` verb — the verb set is
  matched with `==` precisely so a namespace cannot be opened wholesale.
- **`POST /broadcastCli` cannot render config text, only confirm execution**
  (issue #56). It answers with a bare GUID; neither the vendored Orchestrator
  OpenAPI (`/broadcastCli` -> `text/plain` string) nor the payload examples
  document any endpoint that returns the per-appliance command output, and
  `docs/research/appliance-config.md` records the same ("Text response, no
  per-appliance status from broadcastCli"). So `show configuration appliance A B
  --format native` reads
  text per appliance through the proxy `cli` path via the bounded fan-out, and
  `--broadcast` is the opt-in "run this read across these appliances and
  confirm it ran" form. If a later Orchestrator release exposes broadcast
  output retrieval, `broadcast_read_command` is the one function that changes.
- **`show configuration appliance --format native`'s security section derives its
  segment pairs instead of listing
  them, and reads deployment through the appliance proxy rather than the
  Orchestrator-scope endpoint** (issue #55). Two endpoint findings, both
  recorded here because a later Orchestrator release could make either moot:
  (1) `GET /vrf/config/securityPolicies` *requires* a `map` query parameter
  (`<srcSegment>_<dstSegment>`), so there is no "list every orchestrated
  policy" read. `reports/fabric.py` therefore derives the pairs from
  `resources/zones.py`'s `segment_zone_map()` view (`GET /zones/vrfZonesMap`)
  and bounds the cross product at `MAX_POLICY_READS`, falling back to
  intra-segment pairs — stated in-band — past that. The vendored spec does
  carry `GET /vrf/config/securityPoliciesSegments` ("Gets all security maps",
  an array of `<src>_<dst>` strings), which would replace the derivation with
  one call; it is not implemented by `mock/server.py`, so adopting it needs a
  mock endpoint (and ideally a live capture) first. (2) The spec's
  Orchestrator-scope `GET /deployment` requires **both** `nePk` and `cached`
  (an omitted `cached` is a 422, exactly as with
  `/appliancesSoftwareVersions`) and is likewise absent from the mock;
  `reports/fabric.py` reads the same object over the appliance proxy path
  `resources/deployment.py` already owns. If the orchestrator-scope form is
  ever preferred (it is one call per appliance either way), the change is
  confined to `fabric.appliance_deployment`.

## Epic #54 (operational show commands) — deferred work

### Move the report renderers into `cli/render.py`

`render_version_report`, `render_flows_summary`, `render_flow_search` and
`render_fabric_config` live in `cli/main.py`; `render.py`'s own docstring says
it holds the presentation-only helpers shared by the subcommands and the
shell, which is exactly what these are. The shell currently imports them back
out of `main.py`, which works but reads backwards.

**It is a refactor, not a move.** I attempted it and reverted. The four public
functions are 178 lines, but they pull 13 more symbols with them —
`_render_overlays`, `_render_templates`, `_render_security`,
`_render_inventory`, `_render_deployment`, `_version_table`, `_bounded_note`,
`_human_bytes`, `_human_uptime`, `_DEGRADED_STYLE`, `_NEXT_BOOT_STYLE`,
`_SKEW_STYLE`, and `err_console` (a `main.py` object). `render.py` would also
have to import `pyecsdwan.reports`, which `main.py` already imports at module
load, so the import graph needs checking for a cycle before anyone starts.

Worth doing when someone next touches that area with room to do it properly.
Not worth doing halfway.

### `GET /vrf/config/securityPoliciesSegments` — adopted, with a caveat

Now used by `reports/fabric.py` to read the configured segment pairs in one
call instead of deriving an O(n²) cross product. The bounded derivation
remains as the fallback because the endpoint is not on every Orchestrator
release. **Neither path has been exercised against real gear** — the mock
implements the endpoint as this repo reads the spec, which is not the same as
the vendor implementing it that way.

### `ipEitherFlag` semantics are assumed, not verified

`reports/flows.py` and the mock both take `ipEitherFlag=true` to mean "match
the address at either end", which is what the parameter name says and what
makes `show flow <ip>` a server-side query. The spec's own description says
the opposite — *"If true, ip1 will be treated as the source IP, and ip2 will
be treated as the destination IP"* — which describes directional matching.

One of the two is wrong and nobody has tested it live. If the description
turns out to be right, `show flow <ip>` silently misses every flow where the
queried address is the destination. **Confirm this before trusting the
command on a production fabric**: run `show flow <ip>` for an address you know
appears as a destination, and check it comes back.

### Orchestrator-scope `GET /deployment` is unused

Both `nePk` and `cached` are required (the same trap
`/appliancesSoftwareVersions` carries), and the mock implements no
orchestrator-scope `/deployment` at all. `reports/fabric.py` reads the same
object through the appliance proxy, as `resources/deployment.py` does. If the
orchestrator-scope path is ever wanted, the mock needs the route first.

### Smaller items

- `show fabric flows summary` counts rows because `active` carries no per-overlay
  breakdown. A cell bounded by `--max-flows` renders as `2+` and the footer
  says so; if the API ever grows a per-overlay summary, the counting can go.
- The mock's `overlays` filter on `GET /flow` splits names on `,` while the
  spec says IDs split on `|`. Nothing uses it. Reconcile if anything starts to.
- **Resolved by #94: `ipEitherFlag` is directional when true.** This file
  carried it as "name versus description, unverified live". The live run in
  #94 settled it in favour of the description — `true` matches `ip1` as the
  *source only*, so `show fabric flow <ip>` was searching one direction and
  answering "no flows found" for any host that mostly receives traffic. The
  client now sends `false`, and the mock implements the documented semantics
  rather than the name it had been agreeing with.
- **Still unexplained from #94: two appliances answer `GET /flow` with a hard
  500.** `S3-ecv-01` and `S3-ecv-02` returned 500 in 42-48ms on every attempt
  while four siblings answered 200 in 300-1500ms. A fast deterministic 500
  looks like rejection rather than a failure during work, so a parameter those
  two do not accept is one candidate and an appliance-side fault is another.
  Worth checking whether `show fabric flows summary` — same endpoint, no
  address parameters — succeeds on the same two appliances: if it does, the
  filter arguments are implicated; if it does not, they are unhealthy.
- The flow report labels any failed fan-out item "unreachable", including an
  API error like that 500. The appliance may be perfectly reachable and the
  *query* at fault, so the word sends an operator to check the wrong thing.
  Renaming it touches the `--json` payload's `unreachable` key, so it wants
  its own change rather than riding along with a P0 fix.
- Zone and VRF ids in flow rows are rendered as integers; no name lookup.
- `--section` on `show configuration appliance --format native` takes one name,
  not a repeatable list.
