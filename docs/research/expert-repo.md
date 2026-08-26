# EC_SD-WAN_Expert repo — mined knowledge

Source: /home/user/EC_SD-WAN_Expert (Claude skill + MCP server for EdgeConnect).
Headline finds and what pyecsdwan does with them.

## OpenAPI specs (now vendored here under `specs/`)

- `docs/orchestrator-apiDocs.json` → `specs/orchestrator-openapi-7.2.0.json` —
  OpenAPI 3.0.0, Orchestrator REST 7.2.0, **870 paths**.
- `docs/appliance-apiDocs.json` → `specs/appliance-openapi-7.2.0.json` —
  OpenAPI 3.0.0, ECOS REST 7.2.0, **437 paths**.
These are the Tier-1 spec-ingestion baseline (tools/spec_sync.py diffs future
specs against them). Base paths: Orchestrator `/gms/rest`, appliance `/rest/json`
(proxy `url=` param carries the part after `rest/json/`).

## Auth / transport (core/orchestrator.py)

- Base `https://{host}/gms/rest/`; API key header `X-Auth-Token`.
- auth modes: sessionless (cloud: *.silverpeak.cloud, *.silverpeaksystems.net) vs
  session (`POST authentication/login` w/ X-Auth-Token header; legacy fallback body
  `{user:'',password:'',token:key}`); never key in query strings.
- nePk regex `^\d{1,10}\.\w{1,10}$`; ECOS path regex `^[a-zA-Z0-9/_\-\.]+$`;
  SSRF host validation; secret redaction on Authorization/X-Auth-Token/password.
- Timeout clamp 5–300s; GET expects 200, POST/PUT 200/201/204, DELETE 200/204.
- No retry/pagination/caching implemented anywhere; vendor guidance: Orchestrator
  is a low-QPS control plane — don't poll stats through it.
- Pagination idiom = time windows (`startTime`/`endTime` epoch **ms**) + `limit`;
  `cached` query flag on ~88 endpoints (server default = cached; repo prefers live).

## Async patterns (field-verified learnings)

- Canonical: POST → guid/clientKey → `GET /action/status?key=` → **response is an
  ARRAY** (repo indexes [0]; group pushes have one record per appliance) → poll
  until `taskStatus.upper()` in (COMPLETED, FAILED).
- **`completionStatus` is unreliable**: for ECOS upgrades it stays `false` even on
  success; `logLevel` is always ERROR. Success test used in the field:
  `taskStatus == "COMPLETED" and result.startswith("Success")`. → pyecsdwan's
  poller keys on taskStatus first, uses completionStatus only as a tiebreaker.
- `POST /action/cancel?key=` exists. `GET /action/inProgress` returns 400 in
  practice — don't use.
- `POST /broadcastCli` returns the GUID as **plain text**, not JSON.
- Preconfig apply polls a different endpoint with numeric taskStatus 0/1/2
  (see appliance-jobs.md).

## Security policy writes (closes the pyedgeconnect gap)

- **Orchestrator-level**: `POST /vrf/config/securityPolicies?map={srcSeg}_{dstSeg}`
  body `{data: SecurityMaps, options: {merge: bool, templateApply: bool (false for
  orchestration)}, settings: {mapName: logging}}`. GET same path. This is the
  fabric-scope write the UI uses for Security Policies orchestration.
- **Per-appliance**: `POST /appliance/rest?nePk=&url=/securityMaps` with
  `{data: {mapName: {zoneFrom_zoneTo: {prio: {N: {match, set:{action:allow|deny,
  logging, tag}, misc:{rule, logging, logging_priority 0-8, tag}, comment,
  gms_marked}}}}}, options:{merge, templateApply}}`. Priority 65535 = implicit
  terminal deny. Match fields: src/dst_ip (cidr), src/dst_port, protocol,
  application, app_group, dscp, src/dst/either_dns (wildcards), _geo, _service,
  _vrf, overlay, internet, acl.
- Zones: ECOS `POST /zones` body `{zoneId: {name}}` with REQUIRED query
  `deleteDependencies`; orch `POST /zones` (+ `/zones/nextId`, `/zones/eeEnable`).
- Segment-zone map: `POST /vrfZonesMap` `{vrfId: {zoneIndex: {id, name}}}`.

## Endpoint reliability (learnings.json, 35 working / 10 failing)

Working (subset): `gms/interfaceLabels`, `gms/overlays/config`,
`gms/overlays/association` (GET/DELETE — DELETE returns **204**), `appliance`,
`deployment`, `broadcastCli`, `action/status`, `gms/appliance/preconfiguration`
(+validate), `tunnels2/bonded`, `tunnels2/physical`, `regions`, `gms/group`.
Failing (404/400): `tunnels2/{nePk}`, `gms/tunnels/state`, `tunnels/health`,
`routes`, `routes3`, `routeMap`, `configuration`, `appliance/preconfiguration`
(no gms/ prefix), `action/inProgress`.
Param gotchas: tunnel-exception `interface_label` wants numeric label id as string
("13") not name; appliance refs must be nePk not hostname.

Overlay decommission cascade (learnings): DELETE overlay association →
OrchestratorManager deletes tunnels/IPSLA/QoS then **re-applies templates**
(30–90s); never delete tunnels individually.

## Notes

- The repo's modules/routing/api.py + interfaces/api.py use endpoints that DO NOT
  exist (`appliance/{id}/bgp/config` etc.); the fix (enhancements/routing_fixes.py)
  is the passthrough form (`appliance/rest?nePk=&url=/bgpConfig`, `/ospfNeighbors`,
  `/interfaces`, `/subnets3/all`, `/networkRoutes`).
- The skill's claim "template groups have no API" is folklore — the spec carries
  the full `/template/*` surface (incl. `/template/applianceAssociation2` bulk and
  `/template/deleteTemplateGroup`).
- ECOS upgrade safety: hub-first, max 5 appliances per batch, validate first
  (`POST /validateApplianceUpgrade`), refuse `upgradable:false`.
