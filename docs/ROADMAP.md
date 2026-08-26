# pyecsdwan roadmap — to Orchestrator feature parity

Every operation a user performs in the EdgeConnect Orchestrator UI should be
expressible through this CLI, with transactional safety the Orchestrator API
never had. This page indexes the epics that get us there; the GitHub Issues are
the live tracker.

## Shipped (on `main`)

**Phase 0 — framework.** Resource-plugin contract (Scope / Reversibility /
Tier), candidate config, crash-safe journal, transaction engine with
commit-confirm + detached watchdog, httpx client, resolver cache, action-key
poller, Junos-flavored shell + scriptable subcommands, Tier-0 raw `api`
passthrough, bundled mock Orchestrator. Trivial resource: `interface-labels`.

**Phase 1 — first vertical slice.** `template-group` + `template-association`,
`bio` + `bio-association`, `security-policy`, and template-ownership detection
(`ownership.py`). Plus an adversarial hardening pass (host-scoped recovery,
URL-escape guard, confirm-vs-revert atomicity, default-fill idempotency, …).

**Phase 2 — appliance-scope config, first pass (#3).** Save-changes primitive
(#11); `appliance/deployment` (#12, interfaces/IP/VLANs, validate-then-apply);
`appliance/dhcp` (#13, composes over the deployment object); `appliance/vrrp`
(#14); `appliance/routes` (#15, add/delete-delta `COMPENSABLE` rollback);
`appliance/bgp` and `appliance/ospf` (#16, #17 — read+write, code-complete
against a spec-confirmed appliance-proxy endpoint but not yet live-write-
tested); `appliance/loopback` (#18, read+diff) + `loopback-orch`
(#18, fabric-wide pool, full read-modify-write); `appliance/zones` +
`appliance/security-maps` (#19). Template-ownership detection (#20) partially
grounded — `dns`/`routes`/`shaper` section names confirmed live, the rest
stay unverified placeholders.

**Phase 3 — orchestrator-scope breadth, first item.** Orchestrator firewall
zones (#30).

**Tier-1 spec pipeline, first tool.** `tools/spec_sync.py` — fetch + diff the
published OpenAPI spec against the `specs/` baseline (#25). Codegen
(pydantic models, plugin stubs, `show coverage`) is still ahead (#26–29).

`make check` gate: ruff + mypy `--strict` + tests, all green.

## Coverage model (why parity is incremental but safe from day one)

| Tier | What | In transactions? |
|---|---|---|
| **0** raw | `ec-cli api get\|post\|put\|delete <path>` — any endpoint, today | never; audit-journaled, no rollback |
| **1** generated | spec-ingested pydantic models + plugin stub | plain commit; confirm only with `--allow-untransactional` |
| **2** curated | real `normalize()`, true reversibility class, ownership detection | full commit-confirm |

So *anything and everything* is reachable now via Tier 0; curated plugins
(Tier 2) grow coverage against a stable contract, and the Tier-1 pipeline
(Epic below) generates stubs for the long tail.

## Epics

| Epic | Scope | Issues |
|---|---|---|
| **Phase 2 — appliance-scope config** (#3) | interfaces/IP, DHCP, VRRP, routes, BGP, OSPF, loopbacks, zones — via the appliance proxy + save-changes + ownership detection | #11–#20 |
| **Phase 3 — orchestrator-scope breadth** (#4) | zones, ACLs/IP objects/app-defs, NAT, route/opt/QoS policy, service orchestration, regions, priorities, internal subnets, common settings | #30–#39 |
| **Async job handling** (#5) | no silent success on keyless pushes; per-appliance fan-out; preconfig channel; cancellation | #21–#24 |
| **Tier-1 spec pipeline** (#6) | `tools/spec_sync.py`, model/binding/stub codegen, `show coverage`, promotion gating | #25–#29 |
| **Fleet lifecycle** (#7) | discovery/approval, decommission cascade, preconfig, backup/restore, upgrades, licensing — IRREVERSIBLE class | in-epic checklist |
| **Fabric ops & observability** (#8) | `drift`, declarative bulk apply, JSON Schema, dashboard-parity views | in-epic checklist |
| **Production hardening** (#9) | live session-login, systemd watchdog backend, keyring, audit export | in-epic checklist |
| **(v2) RBAC broker** (#10) | direct-to-appliance access, gated — explicitly out of v1 scope | in-epic checklist |

The near-term epics (#3, #4, #5, #6) are sharded into child sub-issues now;
the operational/v2 epics (#7–#10) carry their breakdown as checklists and get
sharded into issues when their phase starts.

## Parity map (Orchestrator UI area → status)

| Orchestrator UI area | Status |
|---|---|
| Templates & template groups (content + association) | ✅ shipped (Phase 1) |
| Business Intent Overlays (config + appliance association) | ✅ shipped (Phase 1) |
| Firewall / security policy (orchestrator scope) | ✅ shipped (Phase 1) |
| Interface labels | ✅ shipped (Phase 0; advanced constraints #39) |
| Appliance interfaces / IP / DHCP / VRRP / routes | ✅ shipped (Phase 2, #12–#15) |
| BGP / OSPF (read+diff → write) | ✅ shipped (#16, #17); write path is code-complete against a spec-confirmed endpoint but not yet live-write-tested (see docs/futures/README.md) |
| Loopbacks / loopback orchestration | ✅ shipped (Phase 2, #18; per-appliance loopback is read+diff only, no write endpoint documented) |
| Firewall zones (orchestrator scope + segment↔zone map) | ✅ shipped (Phase 3, #30) |
| Firewall zones + security policy (appliance scope) | ✅ shipped (Phase 2, #19) |
| ACLs / NAT / route-opt-QoS policy | 🔶 Phase 3 (#31–#33) |
| Service orchestration associations | 🔶 Phase 3 (#34) |
| Regions / regional overlays / priorities | 🔶 Phase 3 (#35, #36) |
| Common settings (DNS/NTP/SNMP/logging) | 🔶 Phase 3 (#38) |
| Appliance lifecycle (discovery/upgrade/backup/preconfig) | ⚙️ Epic #7 |
| Fabric drift / declarative apply / dashboards | ⚙️ Epic #8 |
| Any endpoint not yet curated | 🟢 reachable today via Tier-0 `ec-cli api` |

Legend: ✅ done · 🔷 Phase 2 · 🔶 Phase 3 · ⚙️ operational epic · 🟢 Tier-0 now.

_Regenerate the parity/coverage view once `ec-cli show coverage` (Epic #6, #28)
reads the `specs/` baseline._
