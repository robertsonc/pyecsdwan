# Appliance-level config surface (Phase-2 groundwork)

Source: vendored `pyedgeconnect/` (orch/ + ecos/). Paths in 9.3+ query-param form.
Key structural finding: **the Orchestrator API has almost no appliance-config WRITE
endpoints** — writes go to the appliance's own REST API through the proxy
(`/appliance/rest?nePk=&url=<ecosPath>`), then must be persisted with
save-changes (see appliance-jobs.md).

## Read endpoints (Orchestrator-side)

| Section | Endpoints (GET) | Notes |
|---|---|---|
| BGP | `/bgp/config/system?nePk=`, `/bgp/config/neighbor?nePk=`, `/bgp/config/allVrfs/{system,neighbor}?nePk=`, `/bgp/state[...]` | response fields undocumented in SDK |
| OSPF | `/ospf/config/{system,interfaces}?nePk=`, `/ospf/state/{system,interfaces,neighbors}?nePk=` | ditto |
| Deployment | `/deployment?nePk=` (live call to appliance!), `/tunnelsConfiguration/deployment[?nePk=]` (orch view) | richest documented payload; see below |
| VRRP | `/vrrp?nePk=&cached=` | config+state merged per entry |
| Loopback (per-appliance) | `/virtualif/loopback?nePk=&cached=` | fields undocumented |
| Loopback orchestration | `/loopbackOrch`, `/loopbackOrch/pool`, `/loopbackOrch/pool/history/{seg}` | orchestrator-native; POST `/loopbackOrch` IS a write (full overwrite — GET first) |
| Static routes | `/subnets?nePk=&cached=[&subnet=]` | large; `subnets.entries[].state.*` incl. advert_bgp/ospf, learned_*, adminDistance |
| Admin distance | `/appliance/adminDistance/{neId}?cached=` | read-only |

`cached=True` = Orchestrator's last-known value; `cached=False` = live pull from the
appliance. That flag IS the implicit proxy switch on these reads.

## Write paths (via appliance proxy: `POST /appliance/rest?nePk=&url=<path>`)

| Section | ECOS path | Semantics |
|---|---|---|
| Deployment (interfaces/IP/DHCP) | `deployment` (POST) | FULL-OBJECT replace. GET current, modify, POST whole. `deployment/validate` returns `{err, rebootRequired}` — call before POST |
| VRRP | `vrrp` (POST) | list of entries: pkt_trace, adv_timer, preempt, holddown, auth, desc, enable(Up/Down), priority, vipaddr, interface, groupId |
| Static routes | `subnets3/configured` (POST, destructive full replace), `subnets3/configured/addMultiple`, `subnets3/configured/deleteMultiple` | nested {prefix: {cidr: {..., nhop: {...}}}}; per-entry `gms_marked` flag; zone_id 65534 = none |
| Security policy | `securityMaps` (POST) | see security research + examples/upload_security_policy |
| Port forwarding | `portForwarding2` (POST) | `set_gms_marked` variant exists |
| Network interfaces | `networkInterfaces` (POST body {"ifInfo": ...}) | |
| DNS | `resolver` (POST) | |
| CLI fallback | `cli` {"command"}, `cliMultiple` {"commands"} per appliance; orch-wide `POST /broadcastCli` {"nePks": [...], "cmdList": [...]} (9.3+; pre-9.3 "neList") | only channel for BGP/OSPF/per-appliance-loopback writes — the SDK models none. Text response, no per-appliance status from broadcastCli |

**BGP and OSPF have NO modeled write endpoint on either side.** v1 resource plugins for
them must either drive the appliance's own bgp/ospf config REST paths raw (verify
against a live appliance before curation) or stay NotImplementedError stubs.

## DHCP

No `/dhcp*` endpoint exists. DHCP server/relay is a subtree of the deployment object:
`modeIfs[].applianceIPs[].dhcpd` = {type: server|relay|none, server:{prefix, ipStart,
ipEnd, gw[], dns[], ntpd[], netbios[], netbiosNodeType, maxLease, defaultLease,
options{}, host{name:{mac,ip}}, failover}, relay:{dhcpserver[], option82,
option82_policy}}; plus top-level `dhcpFailover` keyed by interface. Changing DHCP =
POST the whole deployment.

## Deployment object shape (from get_appliance_deployment docstring)

Top-level: `scalars` (platform limits, ~60 read-only fields incl. vrrpCompatible,
maxVLANs), `sysConfig` (mode bridge|router, maxBW, ifLabels{lan[],wan[]},
license, zones[], vrfs[], vrfZonesMap), `mgmtIfData` {iface:{dhcp,nexthop,ip,mask}},
`modeIfs` [{ifName, applianceIPs:[{ip, mask, subif, vlan, wanNexthop, label, lanSide,
wanSide, dhcp, harden(0-3), behindNAT, maxBW{inbound,outbound}, dhcpd, zone, vrf,
comment, failover}]}], `dpRoutes` [{prefix, nexthop, intf, metric, type:"gw"}],
`vifs` {pppoe[], bondedIfs[]}, `dhcpFailover`.

## Template ownership detection (design input)

- `templateApplied`/`appliedTemplates` fields DO NOT EXIST anywhere.
- Appliance-level signals: `GET /template/applianceAssociation?nePk=` (associated
  groups), `GET /template/history/groupList?nePk=` (applied groups),
  `GET /template/templateSelection?templateGroup=` (which template *sections* a
  group carries), `GET /template/templateGroupPriorities` (precedence).
  → Ownership check = map resource kind -> template section name(s); a section is
  owned iff an associated group has that template selected.
- Object-level signal: `gms_marked` boolean ("configured by Orchestrator") on:
  static routes (ecos subnets3), security-map rules, port-forwarding rules,
  tunnels/bonded/3rd-party tunnels, VRF security policies (orch _vrf.py). NOT
  present on deployment/interfaces/DHCP/VRRP/BGP/OSPF.
- `templateApply` appears once as a request option (`options: {merge, templateApply}`)
  on `POST /vrf/config/securityPolicies/{map}` — request flag, not state.
- Tunnels have `POST /applyTunnelTemplate` (ecos).

## SDK gotchas (do not replicate)

- 9.3 migration is usually path→query-param, but `broadcastCli` and saveChanges
  change a BODY key (`neList`/`ids` → `nePks`) with the path unchanged.
- `cached` bool interpolated into URLs as Python `True`/`False` (capitalized).
- `set_loopback_orchestration` writes `mgmtIp` while docs/GET say `mgmtIP`.
- `set_appliance_subnet_sharing_options` docstring says GET; code POSTs
  (`{"auto_subnet": {"self", "add_local", "add_local_lan", "add_local_wan"}}`).
- Preconfig pre-9.3 branch tuple bug; `get_appliance_applied_template_goups` typo.
- ECOS base URL is `https://<ip>:443/rest/json`; proxy `url=` param takes the path
  after `rest/json/`.
