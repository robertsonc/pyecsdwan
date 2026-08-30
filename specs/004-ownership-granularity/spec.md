# Spec: ownership granularity

**Feature:** `004-ownership-granularity` · **Status:** draft, for owner review
**Depends on:** #20 (fail-closed ownership), #63 (origin identity), spec 003 T6

Stage 1 shipped in `75c9eb2` and closed the fail-open half. This spec is the
other half, and it is written first because the naive version of it is wrong in
a way that only live data reveals.

## Problem

`ownership.py` answers **"does a template govern this resource kind"**. The
question that matters is **"would a template push revert *this change*"**.
Those differ, and the gap is not academic — it is wrong in both directions on a
real fabric.

*Over-refusal.* BGP peers are locally significant. The `bgp` template governs
timers and per-peer defaults and contains no peer key at any depth, so no push
can revert adding a peer. The guard refuses it anyway, because the `bgp`
section is selected and ownership is keyed by kind.

*Fail-open.* Closed in stage 1: a section name the Orchestrator has never heard
of produced the same "unowned" as a resource no template governs.

## Source validation

Orchestrator **9.7.0.43282**, ECOS **9.7.0.0_109184**, six appliances, one
group with meaningful selections. Full capture in
`docs/research/live-ownership-2026-08-30.md`.

**Templates govern fields, not resources.** Whether the template body covers
what the resource writes:

| Section | Resource writes | Template body | Covered |
|---|---|---|---|
| `bgp` | neighbours (peers) | `ka`, `hold`, `next_hop_self`, `route_target_template` | **no** |
| `ospf` | interfaces, areas | `helloInterval`, `deadInterval`, auth | **no** |
| `vrrp` | vrid, virtual IP | `advTimer`, `priority`, `preempt` | **no** |
| `routes` | subnet entries | `auto_subnet`, `ecmp_en` | **no** |
| `banners` | motd / issue | the banner text itself | yes |
| `dns` | nameservers | `8.8.8.8`, `8.8.4.4` | yes |
| `snmp` | communities | communities | yes |

**Templates govern entries by priority band.** Orchestrator-pushed entries
occupy 1000–9999; entries outside it are locally significant. No exceptions
observed:

| Section | Template prios | Live appliance also carries |
|---|---|---|
| `natMaps` | 1000 | — |
| `qosMaps` | 1000–1040 | 20000, 20001, 65535 |
| `optmap` | 1000–1510 | 10020, 10070, 65535 |
| `routesRedistributeMaps` | 1000–1100 | — |

The `optmap` entries match the appliance byte-for-byte by comment, so this is a
push, not a coincidence.

**What we deliberately do not know.** One Orchestrator, one version, one
populated group. `options.merge` and `templateApply` contradict observed
behaviour and their semantics are inferred, not documented. `gms_marked` is
`False` on exactly the pushed entries, so it is not a provenance signal
whatever its name suggests. `acls` and `securityMaps` bodies were empty here,
so their entry-level behaviour is unproven.

## Goal

Ownership answers a question about **the change**, so that a locally
significant change is permitted and a template-governed one is refused, with
#20's fail-closed posture intact wherever the answer is not known.

## Non-goals

* **Not** modelling template semantics we have not established. Nothing may
  depend on `merge`, `templateApply` or `gms_marked` until documented.
* **Not** deriving governed subtrees automatically. This was attempted and
  fails: the `bgp` template's `vrf.0` block carries `ka`, `hold`,
  `next_hop_self`, `as_override` — *the same field names a new peer carries* —
  so path-overlap marks a new peer as governed. The template says how peers
  behave, not which exist, and no structural comparison sees that.
* **Not** relaxing #20. Unknown stays refused.
* **Not** modelling `redistributionMaps`, which is unmodelled and separate
  (it also holds the rtmaps BGP neighbours reference by name).

## The model

Three layers, applied in order. Each is independently useful and testable.

**L1 — vocabulary from the fabric.** *(shipped)* Section names are read from
`/template/templateGroups`, and an entirely unknown mapping answers UNKNOWN.

**L2 — priority band, structural.** For any change addressed to a `prio`-keyed
entry, the entry is template-governed iff its priority is in the governed band.
This needs no per-resource work: `prio` is a structural feature of the payload,
so one rule covers qos-map, optimization-map, route-map, nat-maps, acl and
security-maps at once.

**L3 — declared governed subtrees.** For resources the template partitions by
field rather than by entry, the resource declares which subtrees the template
governs:

```python
class Bgp(Resource):
    #: The `bgp` template governs system-level settings and per-peer defaults.
    #: It contains no peer key at any depth, so peer existence is local.
    template_governs = ("system",)
```

A change under a governed subtree is owned; a change outside one is not. Four
resources need this: `bgp`, `ospf`, `vrrp`, `routes`. Everything else keeps
today's whole-resource behaviour, which the source validation shows is correct
for them.

## Required decisions

These are the owner's. Each changes the shape of the work.

| # | Decision | Why it is escalated |
|---|---|---|
| D1 | **Does ownership become change-aware?** Today `managed_by(ctx, ref)` cannot see what is changing, so L2 and L3 are impossible without passing the diff. This is a contract change touching every resource that overrides it. | Architectural, and the alternative — a second method alongside `managed_by` — trades a smaller blast radius for two ways to ask one question. |
| D2 | **Is 1000–9999 hard-coded, configurable, or derived?** Derived would mean taking the band from the selected template's own prios, which is self-describing and survives a release that moves the range — but it makes ownership depend on a body we may fail to read. | It is a vendor convention this project cannot verify across releases, and hard-coding an unverifiable magic range is the kind of guess `SECTION_MAP` already taught us about. |
| D3 | **What happens to a change spanning both?** e.g. one commit editing `system.ka` (governed) and adding a peer (local). | Refusing the whole change is safe and may be annoying; splitting it is neither obviously safe nor obviously expressible. |
| D4 | **Does `SECTION_MAP` survive at all?** With the vocabulary readable and authority per-field, the kind→section mapping is the last hand-maintained guess in the path. It could instead be derived by matching the template body against the resource's live config. | Deleting it removes a whole class of the defect found live; deriving it is more machinery and a new way to be wrong. |

## Requirements

| # | Requirement | Verified by |
|---|---|---|
| R1 | Adding or removing a BGP peer on a template-managed appliance is permitted without `--override-template` | live: the case that started this |
| R2 | Changing a field the template sets (`system.ka`) is still refused | live + mock |
| R3 | A change to a prio outside the governed band is permitted; inside it is refused | mock, from the captured 9.7 bodies |
| R4 | A kind with no L3 declaration keeps whole-resource behaviour | mock, all remaining kinds |
| R5 | Every unknown stays refused: unreadable selection, unreadable vocabulary, absent declaration | mock |
| R6 | No decision reads `merge`, `templateApply` or `gms_marked` | source test, like `test_origin_identity` |
| R7 | The band and the subtree declarations are stated once and consumed everywhere | source test |

## Open questions

| # | Question | Blocks? |
|---|---|---|
| Q1 | Does the 1000–9999 band hold on releases other than 9.7? | No — D2 can defer it by deriving |
| Q2 | Do `acls`/`securityMaps` govern entries when non-empty? Both bodies were empty on this fabric. | No — L2 covers them if they do |
| Q3 | Should `redistributionMaps` become a curated resource? BGP references its maps by name, so BGP config points at objects the tool cannot diff. | No — separate issue |

## Evidence expected

Live re-verification of R1–R3 on the same fabric, plus the mock corpus built
from the captured 9.7 template bodies so the model is testable without a
fabric. No kind is advertised as correctly scoped until its behaviour is shown
against a real template group that selects it.
