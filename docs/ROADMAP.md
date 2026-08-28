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

**Phase 3 — orchestrator-scope breadth (#4), 8 of 9 issues.** Orchestrator
firewall zones (#30); ACLs + IP objects + AppExpress groups (#31); NAT maps,
pools and SNAT maps (#32); QoS/optimization/route maps + shapers (#33);
regions, region associations and regional overlays (#35); overlay and
template-group priorities (#36); internal subnets (#37); SNMP, logging,
mgmt-services, banners and schedule-timezone (#38); interface-label advanced
constraints (#39). Service orchestration (#34) is deferred — see the scope
comment on that issue.

**The epic's headline finding: "orchestrator-scope breadth" was a
misnomer.** Most of these surfaces are **GET-only on the Orchestrator API**;
the real write path is the appliance (ECOS) API through the
`/appliance/rest` proxy. So #31's ACLs, all of #32, all five of #33, and
four of #38's five landed appliance-scope, not orchestrator-scope. Several
issue-texts also named paths that exist in neither vendored spec
(`/route_policy`, `/optimization_policy`, `/nat_policy`, `/vrf_snat_maps`,
`/vrf_dnat_maps`, `/acls/{name}`, `/third_party_services`, `/services`) —
they are pyedgeconnect SDK module names, not REST paths. Corrected paths are
recorded in each resource's module docstring.

Every epic-#4 write path is spec-confirmed but **not live-write-tested** —
session access was read-only throughout. See `docs/futures/README.md`
§"Write semantics needing live confirmation" before trusting one.

**Tier-1 spec pipeline (#6) — complete.** `tools/spec_sync.py` fetches and
diffs the published OpenAPI spec against the `src/pyecsdwan/_specs/`
baseline (#25).
`pyecsdwan/specs.py` is the one read-only view over the two vendored
baselines — 1833 operations, bounded cycle-safe `$ref` resolution, and the
path normalization every source joins on. `tools/gen_models.py` emits
pydantic models + typed bindings per operation (#26); `tools/gen_plugin.py`
wraps those in a Tier-1 `Resource` stub whose `normalize()` raises
`NotCurated` until curated (#27). `show coverage` reports the whole endpoint
universe by tier (#28), and the promotion checklist is machine-enforced by
`make check` rather than advisory (#29).

`tools/postman_sync.py` distils the vendor's published Postman collections
for 9.3–9.6 into `src/pyecsdwan/_specs/payload-examples-9.6.json` (#51). It adds no endpoint
breadth — the 7.2.0 baselines are already a superset — but supplies request
payload shape for 128 of the 169 write operations the specs leave untyped.

Coverage today: **119 of 1833 endpoints curated, 5 generated, 1709 raw-only**
(`ec-cli show coverage`).

**Operational reporting (#54, first tranche of epic #8).** Read-only fabric
reports in `pyecsdwan/reports/`, deliberately outside `resources/` — they have
no normalize/diff/apply/rollback contract and no reversibility class, and
modeling them as resources would imply guarantees they cannot honor.
the fabric config breakdown by section (#55), an appliance's own CLI
running-config through a deny-by-default read-only allowlist (#56),
Orchestrator + per-appliance active/backup/next-boot partitions with skew
detection (#57), the flow-count matrix (#58) and a fabric-wide deduped
address search (#59). All five were respelled by #74's grammar migration —
`show configuration fabric`, `show configuration appliance <name> --format
native`, `show fabric version`, `show fabric flows summary`, `show fabric
flow <ip>` — and the old spellings are gone, not aliased. All bounded by a shared
concurrency-capped, failure-isolating fan-out: one unreachable appliance is a
marked row, never a lost report.

**Production hardening, first tranche (epic #9).** The two P0s from the
audit batch, plus the gate that keeps them true.

*Concurrency (#63).* The transaction guarantees assumed this CLI was the sole
writer. Atomic replacement stops a *torn* candidate, not a *lost* one: two
shells that both loaded at T0 and saved at T1 and T2 left only the second
one's work. `locking.py` adds host-scoped advisory locks — `flock`, so the
kernel drops them on SIGKILL and a stale lock is structurally impossible
rather than merely detected, with an `O_EXCL` + start-token fallback for
platforms without it. Candidate mutation is now a locked *read-modify-write*
cycle: serializing the writes alone does not fix a lost update, and there is a
test pinned to that distinction. Commit, confirm, revert and rollback share
one commit lock; the detached watchdog's revert takes it too but waits far
longer, because a watchdog that gave up would leave an unconfirmed change
applied. Commit-time drift now **fails closed** — the engine used to notice
that server state had moved since compare, recompute the diff and carry on,
folding another operator's change into this one's changeset; `--rebase` opts
in and re-merges intent over current state. `show locks` reports holders.

*MCP trust boundary (#62).* `mcp_server/` reflectively exposed every public
method of the vendored `pyedgeconnect` SDK — 641 on `Orchestrator`, ~250 of
them writes — with no transaction, TLS verification off, and credentials as
tool arguments. It was never a front end over this product: it wraps the
reference SDK, not `pyecsdwan`. Quarantined to `contrib/mcp_server_legacy/`:
disabled unless explicitly enabled, direct-to-appliance tools removed outright
(#10), TLS on, credentials from environment/keyring only, reads by default and
writes behind a second opt-in labelled Tier 0. Classification is by the verbs
each method actually issues, not its name — 53 `get_*` methods issue POSTs.
Rebuild-over-`txn` versus archive-as-a-separate-project is still open; see
`docs/futures/README.md`.

*CI and packaging (#65).* The gate ran locally and nowhere else.
`.github/workflows/ci.yml` runs ruff + mypy `--strict` + pytest on 3.10/3.11/
3.12 for every push and pull request, then builds an sdist and wheel and
installs it into a clean environment to exercise the CLI from outside the
source tree. That second job exists because of a bug the first cannot see:
the OpenAPI baselines lived at the repository root and never reached the
wheel, so an installed `show coverage` reported an empty endpoint universe —
not an error, a confident wrong answer. They now live in
`src/pyecsdwan/_specs/` and ship as declared package data, with
`include-package-data` turned **off** so what lands in a wheel is a reviewed
declaration rather than a side effect of what happens to be tracked by git.
`make smoke` runs the same wheel check locally.

**CLI information architecture (epic #70).** The `show` root meant four
different things — operational state, normalized configuration, staged
candidate, native vendor text — and the command did not say which. Designed
first: a ratified constitution and brownfield Spec Kit workflow (#75,
`.specify/`), a primary-source design corpus resolving nine dimensions across
Junos, Cisco NSO, gNMI, IOS, NVUE, kubectl and EdgeConnect's own constraints
(#73, `docs/design-corpus/cli/`), and the versioned grammar (#71,
`specs/001-cli-command-taxonomy/`). Then implemented against it:

*One grammar, three intents.* `show <cli-state>`, `show <scope> <domain>` and
`show configuration [running|candidate] ...` — no token sequence resolves to
two, which makes Principle I structural rather than documented. Nonterminals
list their continuations and exit 0 (#74). The datastore token is optional and
means `running`; `candidate` is never implicit, so the only unnamed datastore
is the live one.

*One resolution path.* `registry.scoped_instances()` is the single
implementation every surface uses to discover instances (#76), user-facing
nouns replace registry keys on both surfaces with the keys withdrawn entirely
(#77), and instance names complete (#49). `tests/test_grammar_parity.py` runs
each worked example through both parsers and requires the same answer, because
nothing structural keeps a shell function and a Typer registry in step.

*Operational state, honestly.* `show appliance <name> bgp summary|neighbors`
from one `/bgp/state` call; `routes` reports `unsupported` because no BGP
route-table endpoint exists in either baseline, and stays listed rather than
hidden (#72). A peer configured but absent from state is shown as exactly
that, never inferred established.

*Terminal states are distinguishable.* All eleven outcomes of `grammar.md` §5
with their exit codes, classified at both dispatch boundaries — `denied` is
not `unreachable`, `unsupported` is not `error`, `empty` is not `not_found`
(#78). The table is asserted against the spec by parsing it, so the two cannot
drift. `show commands` renders the whole surface offline, generated from the
parser's own tables and round-tripped through the dispatcher.

Outstanding: whether the shipped `--json` boolean becomes the grammar's
`--format {yaml,json,native}`. It is a flag break across a dozen commands and
the migration table covers command forms rather than flags, so it is an owner
decision rather than an implementation gap.

`make check` gate: ruff + mypy `--strict` + tests, all green — now enforced by
CI on every push and pull request, not only locally.

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
| **Tier-1 spec pipeline** (#6) ✅ | `tools/spec_sync.py`, model/binding/stub codegen, `show coverage`, promotion gating | #25–#29 |
| **Fleet lifecycle** (#7) | discovery/approval, decommission cascade, preconfig, backup/restore, upgrades, licensing — IRREVERSIBLE class | in-epic checklist |
| **Fabric ops & observability** (#8) | `drift`, declarative bulk apply, JSON Schema, dashboard-parity views | #54 (✅ shipped) + in-epic checklist |
| **Production hardening** (#9) | concurrency, MCP trust boundary, CI/packaging, async-job fail-closed, evidence ladder, retry policy | #62–#68 (#62, #63, #65 ✅ shipped; #64, #66, #67, #68 open) |
| **CLI information architecture** (#70) ✅ | intent-separated command taxonomy, spec-driven design; constitution + design corpus + grammar, then the migration itself | #49, #71–#78 (all shipped; one flag decision open) |
| **(v2) RBAC broker** (#10) | direct-to-appliance access, gated — explicitly out of v1 scope | in-epic checklist |

The near-term epics (#3, #4, #5, #6) are sharded into child sub-issues; #5 and
#6 are complete. #8's first tranche shipped as #54 (operational `show`
commands, sub-issues #55–#59); the rest of #8 stays an in-epic checklist.
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
| ACLs / IP objects / app-defs | ✅ shipped (#31; ACLs are appliance-scope — orchestrator `/acls` is GET-only) |
| NAT policy / SNAT / DNAT maps | ✅ shipped (#32; appliance-scope. D-NAT has no write endpoint anywhere — read-only view) |
| Route / optimization / QoS policy + shapers | ✅ shipped (#33; all five appliance-scope — orchestrator exposes them GET-only) |
| Service orchestration associations | ⛔ deferred (#34 — ~100 endpoints across 8+ vendor integrations; an epic, not an issue. See the scope comment on the issue.) |
| Regions / regional overlays / priorities | ✅ shipped (#35, #36) |
| Internal subnets | ✅ shipped (#37; subnet-sharing options not modelable — write-only endpoint, no read path) |
| Common settings (SNMP/logging/mgmt-services/banners/timezone) | ✅ shipped (#38; appliance-scope. DNS proxy/cache, `logging/remote`, NTP deferred) |
| Appliance lifecycle (discovery/upgrade/backup/preconfig) | ⚙️ Epic #7 |
| Fabric drift / declarative apply / dashboards | ⚙️ Epic #8 |
| Operational reporting (`show configuration fabric` / `show fabric version` / `flows`) | ✅ shipped (#54: #55–#59), respelled by #74. `show fabric flow <ip>`'s server-side matching rests on `ipEitherFlag`, whose spec description contradicts its name — unverified live |
| Any endpoint not yet curated | 🟢 reachable today via Tier-0 `ec-cli api` |

Legend: ✅ done · 🔷 Phase 2 · 🔶 Phase 3 · ⚙️ operational epic · 🟢 Tier-0 now.

`ec-cli show coverage --endpoints` now reads the vendored baseline directly
(#28), so this table is a narrative summary — the command is the source of
truth for what is covered at which tier.
