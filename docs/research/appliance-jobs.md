# Appliance inventory, async jobs, save-changes — pyedgeconnect SDK reference

Source of truth: vendored SDK at `pyedgeconnect/` in this repo. Field names verbatim
from docstrings. Paths shown in the Orchestrator >= 9.3 query-param form (pre-9.3 used
path segments).

## Appliance inventory (`GET /appliance`)

Returns `list[dict]`; 33 top-level keys per appliance. The load-bearing ones:

| Field | Type | Meaning |
|---|---|---|
| `id` / `nePk` | str | Primary key, same value, e.g. `"3.NE"` |
| `uuid` | str | Appliance self-assigned unique id |
| `hostName` | str | Appliance hostname (resolver key) |
| `site` | str | Site tag |
| `sitePriority` | int | Priority within site |
| `networkRole` | str | spoke=0, hub=1, mesh=2 |
| `groupId` | str | Orchestrator group primary key |
| `IP` / `ip` | str | Appliance IP (duplicated field) |
| `serial` | str | Serial number |
| `model` | str | e.g. `EC-XS` |
| `softwareVersion` | str | ECOS version |
| `hasUnsavedChanges` | bool | True → needs `saveChanges` |
| `rebootRequired` | bool | True → needs reboot |
| `state` | int | 0 Unknown, 1 Normal, 2 Unreachable, 3 Unsupported version, 4 Out of sync, 5 Sync in progress |
| `reachabilityChannel` | int | 0 unknown, 1 HTTP(S) user/pass (not SD-WAN), 2 appliance→orch websocket, 4 via Cloud Portal |
| `discoveredFrom` | int | 1 MANUAL, 2 PORTAL, 3 APPLIANCE |
| `zoneList` | dict | `{"zones": [str]}` in use |
| `interfaceList` | dict | `{"interfaceLabels": [str]}` label ids in use |
| `tagsList` | list | — |
| `portalObjectId` | str | Cloud Portal hash id |

`GET /appliance?nePk=X` filters to one. `GET /appliance/networkRoleAndSite?nePk=X`
returns the same set plus `platform`, `haPeer`, `preconfigStatus`;
`POST` there accepts `{id, networkRole, site, sitePriority}`.

## Appliance proxy

- `GET/POST/DELETE /appliance/rest?nePk={nePk}&url={ecosPath}` — pass-through to the
  appliance's own REST API. Body passed verbatim.
- Proxied writes mutate the appliance **running config only**. Persist with
  save-changes (below) or the change is lost on reboot (`hasUnsavedChanges: true`).
  The SDK does NOT do this automatically; nor does its own
  `examples/upload_security_policy/upload_security_policy.py` (posts
  `url="securityMaps"` then never saves). Treat save-after-proxy-write as the
  CLI's responsibility.

## Save changes (Swagger `saveChanges`)

| Call | Path | Body | Returns |
|---|---|---|---|
| batch | `POST /appliance/saveChanges` | `{"nePks": [..]}` (9.3+; pre: `{"ids": [..]}`) | `{"clientKey": str}` |
| single | `POST /appliance/saveChanges?nePk=X` | `{}` | `{"clientKey": str}` |

`clientKey` is polled via the action log (below).

## Async jobs / action keys

> Which terminal shapes the poller treats as success, failure or unknown — and
> the evidence behind each — is `docs/research/job-shapes.md` (#64).

Poll: `GET /action/status?key={key}`. Record fields (also used by `GET /action` audit
listing): `id`, `user`, `ipAddress`, `nepk` (lowercase!), `name`, `description`,
`taskStatus` (str), `startTime`/`endTime`/`queuedTime` (ms epoch; endTime 0 while
running), `percentComplete` (int), `completionStatus` (bool), `result` (str — error
or result text), `intTaskStatus` (int), `guid` (groups related actions — group pushes
fan out one record per appliance under one guid).

Operations returning keys: save-changes (`clientKey`), appliance backup/restore
(`clientKey`), `upgrade_appliances` (`clientKey`), `appliance_resync`
(**bare string** body, not JSON), `delete_ecos_image` (bare string).

Keyless pushes (template association's fire-and-204) are confirmed via the
listing `GET /action?startTime=&endTime=&logLevel=&appliance={nePk}` —
startTime/endTime are epoch **milliseconds** (the SDK docstring wrongly says
seconds), logLevel 0=Debug/1=Info/2=Error; `jobs.wait_for_recent_action` polls
it and picks the newest `guid` in the window.

Preconfig apply is async on a **separate channel**:
`GET /gms/appliance/preconfiguration/apply?preconfigId=` → `{taskStatus: 0|1|2,
completionStatus (valid only when taskStatus==2), guid (actionlog bridge),
starttime, endtime, result: [{taskStatus, completionStatus, name, result, nePk,
data}]}`.

Async-with-no-key operations (verify by re-reading state): hostname update,
`PUT /appliance/discovered/update`, direct appliance reboot.

`POST /action/cancel` exists in Swagger; the SDK's `cancel_audit_log_task` is buggy
(does a GET on status instead) — call the cancel endpoint directly if needed.

## Hostname update

`POST /hostname?nePk={nePk}` body `{"hostname": "<name>"}` → 204. No action key;
"will take a few seconds to run" — verify by re-fetching `GET /appliance` and
comparing `hostName`. (The SDK docstring table says GET; the code POSTs. Trust POST.)

## Preconfig (for later phases)

`/gms/appliance/preconfiguration` CRUD (`?preconfigId=`, `?filter=names|metadata`);
`configData` is base64 YAML (pass plain YAML to SDK helpers, they encode);
`POST .../validate` returns full response with per-line YAML errors;
`POST .../apply?preconfigId=&nePk=` applies to existing appliance;
`POST .../apply/discovered?preconfigId=&discoveredId=` approves+applies;
`autoApply` provisions on discovery. Matching by `serialNum` then `tag`.

## SDK defects noted while mining (do not replicate)

- `get_all_approved` hits `/appliance/discovered` (wrong path).
- `add_*_discovered_appliances` transpose latitude/longitude in the payload.
- `modify_appliance` and `change_appliance_group` lack 9.3 query-param branches.
- `cancel_audit_log_task` never cancels (GETs status instead).
- Preconfig pre-9.3 branches assign a tuple to `path` (trailing comma).
- Assorted return-type annotation mismatches (`-> dict` vs actual bool/list).
