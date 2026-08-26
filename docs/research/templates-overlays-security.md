# Templates, BIOs/overlays, security policy — Phase-1 endpoint reference

Mined inline from `pyedgeconnect/orch/` (_template.py, _overlays.py,
_overlay_association.py, _security_maps.py) and EC_SD-WAN_Expert. Paths in 9.3+
query-param form.

## Template groups (Swagger `template`)

| Operation | Method + path | Payload / response |
|---|---|---|
| List all groups | GET `/template/templateGroups` | group objects incl. templates |
| Get group | GET `/template/templateGroups?templateGroup=NAME` | `{name, templates: [{name, valObject}]}` |
| Update group content | POST `/template/templateGroups?templateGroup=NAME` | body `{name, templates:[{name, valObject}]}` |
| Create group | POST `/template/templateCreate` | `{name, templates?}`; HTTP 204 when created without templates, 200 with |
| Delete group | DELETE `/template/templateGroups?templateGroup=NAME` | 204 |
| Selected templates | GET/POST `/template/templateSelection?templateGroup=NAME` | list[str] of template (section) names; POST replaces |
| Association (all) | GET `/template/applianceAssociation` | `{nePk: [groupName,...]}` |
| Association (one) | GET `/template/applianceAssociation?nePk=X` | `{"templateIds": [groupName,...]}` |
| Associate | POST `/template/applianceAssociation?nePk=X` | body `{"templateIds": [...]}` — **COMPLETE replacement**: to add, include existing groups. 204. Triggers template push (async → action log) |
| Applied history | GET `/template/history?nePk=X&latestOnly=True|False` | list of applied templates; 204 when none |
| Applied groups | GET `/template/history/groupList?nePk=X` | list of applied group names |
| Priorities | GET/POST `/template/templateGroupsPriorities` | `{priorities: [...]}` / `{"templateIds": [ordered names]}` (note: GET path uses `templateGroupsPriorities`; docstring header says `templateGroupPriorities` — trust the code string) |

Template names within a group = config *section* names (the same names the
Orchestrator UI shows: e.g. `securityMaps`, `bgp`, `ospf`, ...). The `valObject`
carries the section's config payload. The Default Template Group enumerates all
template names.

Association POST is the push trigger; per-appliance results land in the action log
(grouped by guid). Ownership detection joins association x selection (see
appliance-config.md).

## Business Intent Overlays (Swagger `overlays`, `overlayAssociation`)

| Operation | Method + path | Payload / response |
|---|---|---|
| List overlays | GET `/gms/overlays/config` | list of overlay objects `{id, name, ...}` |
| Create | POST `/gms/overlays/config` | full overlay config object; response carries new overlay ID |
| Get one | GET `/gms/overlays/config?overlayId=N` | overlay object |
| Modify | PUT `/gms/overlays/config?overlayId=N` | full object; "changes will be pushed to the appliances automatically" (async fabric push) — overrides regional customization |
| Delete | DELETE `/gms/overlays/config?overlayId=N` | 204; removes appliances from overlay + overlay reports |
| Regional | GET/POST/PUT `/gms/overlays/config/regions[...overlayId&regionId]` | regionId 0 = global |
| Priorities | GET/POST `/gms/overlays/priority` | `{"1": overlayId, "2": ...}` — priority→id map, full overwrite, each id needs unique priority |
| Association (all) | GET `/gms/overlays/association` | `{overlayId: [nePk,...]}` (not in Swagger as of 9.0.3) |
| Association add | POST `/gms/overlays/association` | `{overlayId: [nePks]}` — ADDS (union), 204 |
| Association remove | POST `/gms/overlays/association/remove` | same shape, removes, 204 |
| Remove single | DELETE `/gms/overlays/association?overlayId=N&nePk=X` | 204 |

Overlay id is server-assigned int; name is user-facing → resolver maps. Overlay
config/modify pushes ripple to the fabric asynchronously (watch action log).

## Security policy (Swagger `securityMaps`)

- Orchestrator-side READ: GET `/securityMaps?nePk=X&cached=bool` → per-appliance
  security policy (`mapsByVrf`/zone-pair rule structure).
- **No orchestrator-side WRITE endpoint.** Writes go via appliance proxy:
  `POST /appliance/rest?nePk=X&url=securityMaps` with the map payload — this is what
  the SDK's own `examples/upload_security_policy/upload_security_policy.py` does
  (builds map from CSV: zone-pair keys `"<fromZoneId>_<toZoneId>"`, rules with
  priority keys, match criteria incl. src/dst ip, ports, protocol, application,
  action allow/deny, logging). ECOS side also exposes
  GET/POST `/securityMaps`, GET `/securityMaps/{map}`, `/securityMaps/{map}/{zonePair}`,
  DELETE `/securityMaps/{map}/{zonePair}[/{rulePriority}]`, `/securityMapsSettings`.
- Security-map rules carry `gms_marked` (Orchestrator-configured flag).
- Zones: orch `_zones.py` manages zone definitions (`/zones` endpoints); firewall
  zones also appear in deployment `sysConfig.zones` and appliance `zoneList`.
- After proxy writes: save-changes required (see appliance-jobs.md).
- In the UI, security policy is normally pushed via the `securityMaps` template
  section — direct proxy writes on template-managed appliances is exactly the
  footgun our ownership detection must catch.
