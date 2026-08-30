# Live ownership findings — 2026-08-30

Orchestrator **9.7.0.43282**, ECOS **9.7.0.0_109184**, 6-appliance lab fabric.
All read-only except one BGP write that was applied and reverted (see
`job-shapes.md`). Every claim here is backed by data captured in-session.

This exists because `ownership.py` models template authority **per resource
kind**, and live evidence says authority is actually **per field** and **per
entry**. Nothing has shipped, so the model can change.

## 1. The section-name vocabulary is knowable, and mostly guessed wrong

`GET /template/templateGroups` returns every template name the Orchestrator
knows — 46 of them on this build — with an `isSelected` flag per group. The
tool did not have to guess any of this.

Five kinds name sections that **do not exist**, so they can never detect an
owner. This is fail-**open**: the tool permits a write and a template push
takes it back.

| Kind | Guessed | Real |
|---|---|---|
| `appliance/optimization-map` | `optimizationMaps` | **`optmap`** — selected, and proven to push (below) |
| `appliance/deployment` | `deployment`, `interfaces` | neither exists |
| `appliance/dhcp` | `dhcpd`, `dhcpFailover` | neither exists |
| `appliance/nat-pools` | `natPools` | not a section (`natMaps` is) |
| `appliance/zones` | `zones` | not a section |

Only `optimization-map` is *proven* harmful here; the rest are unproven because
nothing selects a matching section on this fabric. Extra-but-fake names that do
no harm because a real sibling is listed: `inboundShapers` (real: `shaper`),
`subnets` (real: `routes`).

Confirmed correct: `bgp`, `ospf`, `qosMaps`, `acls`, `securityMaps`, `natMaps`,
`routeMaps`, `routes`, `vrrp`, `banners`, `dns`, `snmp`, `mgmtServices`,
`logging`, `shaper`.

**`optmap` really does push.** Its four entries appear byte-identical on the
appliance, matched by comment:

```
prio 1000  'Disable All Else'                            template == live
prio 1490  'WANOp Exclusion'                             template == live
prio 1500  'Max Dedupe Apps for DC to DC Traffic'        template == live
prio 1510  'Balanced Dedupe Apps for DC to DC Traffic'   template == live
```

## 2. Authority is per field, not per kind

The right question is not "is a section with this name selected" but **"does
the template body cover the fields this resource writes"**. Both sides are
fetchable, so this is computable rather than declarable.

| Section | What the resource writes | Template covers it? |
|---|---|---|
| `bgp` | neighbors (peers) | **NO** — body is `ka`/`hold`/`next_hop_self`/`route_target_template` |
| `ospf` | interfaces, areas | **NO** — body is `helloInterval`/`deadInterval`/auth |
| `vrrp` | vrid, virtual IP | **NO** — body is `advTimer`/`priority`/`preempt` |
| `routes` | subnet entries | **NO** — body is `auto_subnet`/`ecmp_en` |
| `banners` | motd / issue text | YES — body *is* the banner text |
| `dns` | nameservers | YES — body carries `8.8.8.8`/`8.8.4.4` |
| `snmp` | communities | YES |

The first four over-refuse: the guard blocks changes no template would revert.
BGP peers are the worked example — peers are locally significant and the `bgp`
template has no peer key at any depth.

## 3. Authority is per entry, by priority band

For map-type resources, Orchestrator-pushed entries occupy **1000–9999**;
entries outside that band are locally significant. Confirmed with **no
exceptions** across every instance-defining section:

| Section | Template prio range | Live appliance also has |
|---|---|---|
| `natMaps` | 1000 | — |
| `qosMaps` | 1000–1040 | 20000, 20001, 65535 |
| `optmap` | 1000–1510 | 10020, 10070, 65535 |
| `routesRedistributeMaps` | 1000–1100 | — |

So a change to prio 20000 is local and must be permitted; a change to prio 1000
is template-owned and must be refused. The current model cannot express either.

### `gms_marked` is not the provenance signal

It is `False` on exactly the entries the template pushes (qosMaps 1000–1040)
and `True` on local 20000-band entries. `ownership.py` currently says
"`acls.py` prefers the per-rule `gms_marked` flag" — that preference does not
hold on 9.7. The `acls` ECOS path also returns overlay `qmap`/`rmap` structures
carrying no `gms_marked` at all.

### `options.merge` / `templateApply` — observed, not understood

Templates for `optmap`, `qosMaps`, `routesRedistributeMaps` carry
`options.merge: false` (whole-object replace), yet local out-of-band entries
survive on the appliance. The live objects carry `merge: true,
templateApply: false` while demonstrably holding template content.

**Do not build ownership on these flags.** Their semantics are inferred, not
documented, and an ownership model resting on a misread flag is the failure
this whole file is about. The priority band is the convention that is both
operator-confirmed and data-confirmed.

## 4. `redistributionMaps` is unmodelled

ECOS `routeMaps` is **route policy** (traffic steering:
`match: {acl: Overlay_DEFAULT}`, `set: {auto_mod: overlay, auto_overlay: 2}`).
Route *redistribution* lives at ECOS `redistributionMaps`, which no curated
resource reads. It holds `OSPF_PRIMARY`/`OSPF_SECONDARY` **and** the per-peer
BGP rtmaps (`S1-ecv-01_to_bd-01`, …) that `bgp` neighbours reference by name in
`rtmap_inbound`/`rtmap_outbound` — so BGP config points at objects the tool
cannot see or diff. Its template section is `routesRedistributeMaps`.

## 5. What the tool got right, verified live

`drift` degrades correctly on an incomplete fabric. Two appliances were
unreachable (state `2`, in maintenance); `drift --kind appliance/bgp` returned:

```
drift: 0  in-sync: 0  undeclared: 4  unreadable: 2
incomplete: 2 instance(s)/scope(s) could not be compared, so "no drift" is not
a claim this run can make                                          exit 8
```

It enumerated all six, marked the two as `unreadable`, and refused to claim
"no drift". That is T9's "list failure is incomplete" criterion satisfied, now
with live evidence rather than mock evidence. `list_refs` returning unreachable
appliances is correct for this reason and should not be "fixed" to filter them.

## 6. Other live findings

* **`drift --kind` takes registry keys, not user-facing nouns.** `set bgp
  config --appliance X` works; `drift --kind bgp` is rejected and demands
  `appliance/bgp`. Decision 6 (#77) says kinds are addressed "by user-facing
  nouns scoped by the command, never by internal registry keys", so this flag
  contradicts the ratified grammar. `test_grammar_parity.py` covers the two
  *parsers* but not this flag's vocabulary.
* Live read-only smoke probes hard-failed on unreachable appliances because
  they fetched every ref `list_refs` returned. They now skip what cannot
  answer (`readable_refs` fixture) and assert they probed something, so an
  all-unreachable fabric fails loudly rather than passing vacuously.
* The 400 those probes hit was the two maintenance-mode appliances, not a bad
  path: every resource fetch succeeds on all four reachable ones. `natPools`
  and `snatMaps` 404 only as *bare* ECOS paths — the real ones are
  `nat/natPools` and `vrf/config/snatMaps`, which the resources already use and
  the vendored payload examples already document.
* Three `test_live_*_read_only` tests called `config.load_settings()`, which
  does not exist. Gated on `ECSDWAN_ORCH_URL`, so they had never run anywhere.

## 7. Live read sweep — all 41 curated kinds

Run after the ownership rework, read-only, against the four reachable
appliances (S3 was in maintenance).

**41 kinds, 0 problems.** Every kind that could be read is idempotent
(`normalize(normalize(x)) == normalize(x)`) and produces no phantom drift
(a canonical state diffed against itself is empty) — on *real* payloads, which
is the property the promotion checklist asks for and which the mock could only
ever suggest.

Ownership after the rework, on a fabric whose Default Template Group selects 17
sections:

| Verdict | Kinds |
|---|---|
| OWNED | acl, banners, bgp, inbound-shaper, loopback, mgmt-services, optimization-map, ospf, qos-map, route-map, security-maps, shaper, snmp |
| UNOWNED | logging, routes |
| UNKNOWN (fails closed) | deployment, dhcp, nat-maps, nat-pools, zones |

The five UNKNOWNs are the mappings whose section names the fabric does not
have. That is the correct answer and the one stage 1 introduced; before it they
were a confident "unowned".

Four kinds read nothing. Three are genuinely empty on this fabric — `vrrp`
returns `[]` from the appliance, and `security-policy`/`ip-service-group` have
nothing configured. The fourth was a bug, below.

### The sweep found a regression the rework had introduced

`template-group` read 0 refs and raised
`InvalidURL: URL component 'query' too long`. `ownership._template_groups`
cached the full group bodies under the key `template_groups` — which
`Resolver.template_groups()` already owned for a list of group *names*.
Whichever ran first won, so `template-group.list_refs` built refs whose name
was an entire group object.

Nothing in the type system stops this: both values are `list`. No mock test hit
it either, because it needs both callers in one process against one cache. The
sweep is where the orderings met. Fixed by namespacing the key, with a test on
the constant *and* on the behaviour in the order that broke.

## Suggested shape for the rework

Ownership becomes a question about **the fields being changed**, answered from
data already fetchable:

1. Resolve the appliance's template groups and their *selected* sections
   (already done).
2. Fetch the selected sections' bodies (new, one call, cacheable).
3. A change is owned only where the changed path — or, for prio-keyed objects,
   the changed entry's band — is covered by a selected template body.

That keeps #20's fail-closed posture where it is warranted, removes the
over-refusal on locally significant config, and closes the fail-open cases by
deriving names instead of guessing them. It also deletes `SECTION_MAP`'s
guess/verified distinction: nothing needs guessing once the vocabulary is read
from the fabric.
