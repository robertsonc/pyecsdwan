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

- **Tier-1 codegen** (`tools/spec_sync.py` emitting pydantic models + plugin
  stubs): Phase 3. `specs/` baseline (OpenAPI 7.2.0, orchestrator + appliance)
  is already vendored.
- **Fabric-wide drift report** (`ec-cli drift`): every curated kind × every
  appliance, diff against declared desired-state files; CI-friendly exit codes.
  Phase 3.
- **BGP/OSPF write path**: no modeled write endpoint exists in either API
  surface (see docs/research/appliance-config.md). Candidates: appliance REST
  raw paths (verify against a live appliance) or broadcastCli rendering.
  Until verified, Phase-2 plugins for these stay read+diff with
  NotImplementedError on apply.
- **Configurable rollback-history depth per resource kind** (large snapshots,
  e.g. full deployment objects, may deserve shorter history).
- **`commit confirm` via systemd user timer** (docs/watchdog-backends.md).
- **Shell niceties**: `show | compare` paging, `set` path completion from
  resource schemas, multi-line YAML edit (`edit <kind> <name>` in $EDITOR).
- **Response-cache with TTL for read-heavy show commands** (the Orchestrator is
  a low-QPS control plane; see docs/research/expert-repo.md).
- **Live-Orchestrator session-login test.** The CSRF/logout-verb fix
  (`X-XSRF-TOKEN` echo, GET logout, loginType) is unit-shaped only; add a
  cassette/integration test against a real login flow when one is available.
- **Resolver cache projection.** `/appliance` is cached whole; project it to
  the fields actually consumed (hostName/nePk/site/model) to avoid persisting
  inventory/recon data. File mode is already 0o600.
