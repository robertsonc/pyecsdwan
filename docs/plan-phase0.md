# Phase 0 plan — framework foundations

The four decisions the mission asked for before coding, plus the Phase-1 endpoint
inventory extracted from `EC_SD-WAN_Expert` and the vendored `pyedgeconnect` SDK.
Full endpoint detail lives in `docs/research/`.

## 1. Resource contract (`src/pyecsdwan/contract.py`)

One base class every configurable object implements:

```python
class Resource:
    kind: str                    # "interface-labels", "template-group", ...
    scope: Scope                 # ORCHESTRATOR | APPLIANCE
    reversibility: Reversibility # REVERSIBLE | COMPENSABLE | IRREVERSIBLE
    tier: Tier                   # RAW(0) | GENERATED(1) | CURATED(2)
    dependencies: tuple[str, ...]  # kinds applied before this kind

    def fetch(ctx, ref) -> RawState
    def normalize(raw) -> CanonicalState          # idempotency lives here
    def diff(ref, current, desired) -> Diff       # default structural impl
    def apply(ctx, diff) -> ApplyResult           # async-job aware
    def verify(ctx, ref, desired) -> bool         # default: re-fetch + empty diff
    def rollback(ctx, ref, snapshot) -> ApplyResult
    def managed_by(ctx, ref) -> str | None        # template-ownership hook
```

Deviations from the mission's sketch, deliberate:
- `diff()`/`verify()` get default implementations driven by a dumb structural
  differ (`diffing.py`) — semantic knowledge (IDs to strip, list sort keys,
  name↔ID resolution) lives only in `normalize()`, so most plugins write two
  methods, not six.
- `Tier` is on the contract because tier gates transaction participation
  (Tier-0/1 never silently join `commit confirm`).
- `canonicalize_desired()` shapes user intent through the same normalize path so
  both diff sides are identically canonical (no phantom drift).

## 2. Journal format (`~/.pyecsdwan/journal/<txn-id>/`)

- `meta.json` — small state index, atomically rewritten (tmp + rename + dir
  fsync): txn id, target (`orch_origin`, the canonical
  `scheme://host[:port][/path]`, plus `orch_host` for display), state machine
  (PENDING → APPLYING →
  APPLIED_UNCONFIRMED|CONFIRMED → REVERTING → REVERTED/REVERT_FAILED; AUDIT_ONLY
  for Tier-0 calls), confirm deadline, item ref-keys.
- `events.jsonl` — append-only, fsync per record; the source of truth.
  SNAPSHOT events embed full pre-change raw server state per resource
  (snapshot-before-write happens at commit time, re-fetched fresh). APPLY_START /
  APPLY_RESULT / VERIFIED / REVERT_* / CONFIRM / WATCHDOG_* events give a
  complete audit trail; a torn final line after a crash is tolerated on read.
- `confirm.marker` — written by bare `commit` inside a confirm window (fsynced).
- `watchdog.pid` — liveness probe for the orphan scan.
- History: last 10 terminal transactions kept (configurable); non-terminal
  journals are never pruned. `rollback <n>` restores the nth prior CONFIRMED
  txn's snapshots as a *new* journaled transaction (rollbacks are revertible).
- Meta/events disagreement resolves in favor of events.

## 3. Watchdog mechanism (target: Linux server over SSH)

Default backend: **detached daemon via classic double-fork/setsid**
(`watchdog.py`, spawned as `python -m pyecsdwan.watchdog <txn-dir>`). First fork
lets the arming CLI reap the intermediate; setsid + second fork drop the
controlling terminal, so SSH teardown/SIGHUP cannot kill it; fds redirect to
`watchdog.log`, cwd to `/`. Loop: marker seen → exit; txn reaches a terminal
state some other way → exit; deadline passes → revert from journal snapshots.

- Credentials: watchdog re-reads `ECSDWAN_*` env (inherited at arm time) or OS
  keyring. Interactive session logins can't be replayed by a daemon, so the
  engine **refuses `commit confirm` without an API key** — no fake safety.
- If the watchdog fails to arm, the engine auto-reverts the just-applied
  changeset rather than leaving an unprotected unconfirmed commit.
- On any CLI startup: orphan scan (non-terminal txn + no live watchdog pid) →
  offer `rollback --pending`.
- systemd user-timer backend: documented alternative only, NOT default — it
  requires `loginctl enable-linger` for SSH-only users or the user manager dies
  with the last session.

## 4. Phase-1 endpoint inventory (mined, not guessed)

Sources: vendored `pyedgeconnect/orch/*.py` docstrings, `EC_SD-WAN_Expert`
(core client, learnings.json field notes, and the two vendored **OpenAPI 3.0
specs** — Orchestrator 870 paths, appliance 437 paths — now copied to `specs/`
as the Tier-1 ingestion baseline). 9.3+ query-param path style.

**Templates / template groups** (`docs/research/templates-overlays-security.md`):
`GET/POST/DELETE /template/templateGroups?templateGroup=`, `POST
/template/templateCreate`, `GET/POST /template/templateSelection?templateGroup=`
(section names), `GET/POST /template/applianceAssociation?nePk=` (POST is a
COMPLETE replacement; triggers async push → action log), `GET /template/history
?nePk=&latestOnly=`, `GET /template/history/groupList?nePk=`, `GET/POST
/template/templateGroupsPriorities`.

**BIOs / overlays**: `GET/POST /gms/overlays/config`, `GET/PUT/DELETE
/gms/overlays/config?overlayId=` (modify auto-pushes to fabric), regional
variants, `GET/POST /gms/overlays/priority`, association `GET/POST
/gms/overlays/association`, `POST /gms/overlays/association/remove`,
`DELETE ?overlayId=&nePk=` (returns 204).

**Security policy**: read `GET /securityMaps?nePk=&cached=`; write via
**`POST /vrf/config/securityPolicies?map={srcSeg}_{dstSeg}`** (orchestrator
scope, `options.templateApply=false`) or per-appliance proxy `POST
/appliance/rest?nePk=&url=/securityMaps`; nested map→zonePair→prio schema from
the appliance OpenAPI spec; rules carry `gms_marked`.

**Shared machinery**: appliance inventory `GET /appliance` (nePk/hostName/...,
33 fields); async polling `GET /action/status?key=` (ARRAY response, one record
per appliance under one guid; `taskStatus` is the reliable signal — 
`completionStatus` can be false even on success); save-changes
`POST /appliance/saveChanges` `{"nePks": [...]}` → `clientKey` after any
appliance-proxy write; `POST /action/cancel?key=`.

**Template ownership detection** (first-class feature): no per-section
"managed-by" field exists. Detection = join of `GET /template/applianceAssociation
?nePk=` (groups on the appliance) × `GET /template/templateSelection
?templateGroup=` (sections each group carries), mapping resource kind → template
section name; plus per-object `gms_marked` where the API exposes it (routes,
security-map rules, port forwarding, tunnels).

## 5. Phase-0 trivial resource

`interface-labels` (`GET/POST /gms/interfaceLabels`, orchestrator-scoped
singleton, full-replace semantics, field-verified as working in
learnings.json) — exact snapshot/restore, no async, proves
set → compare → commit confirm → auto-revert → commit end to end.
