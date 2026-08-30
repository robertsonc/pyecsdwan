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

## 5. Other live findings

* `natPools` and `snatMaps` 404 as bare ECOS paths; `nat/natPools` works. Two
  `test_live_*_read_only` tests fail on a real 400 under 9.7 — unchased.
* Three `test_live_*_read_only` tests called `config.load_settings()`, which
  does not exist. Gated on `ECSDWAN_ORCH_URL`, so they had never run anywhere.

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
