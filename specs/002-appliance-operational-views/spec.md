# Feature specification: appliance operational views, starting with BGP

**Feature:** `002-appliance-operational-views` · **Version:** 0.1.0 · **Status:** draft
**Issues:** #72, under epic #70. Consumes `001-cli-command-taxonomy` (grammar
approved 2026-08-28). **Blocks #74**, which states it depends on this spec.
**Date:** 2026-08-28

> Second use of the `.specify/` flow. #72's first instruction is *"Define the
> BGP operational subtree only after validating the available Orchestrator/ECOS
> read sources"* — so this spec starts from what the vendored baselines
> actually contain, and one of the three proposed views does not survive it.

## Problem

The generic resource view answers "what normalized configuration exists?".
Reusing it behind `show appliance <name> bgp` would label configuration as
protocol status — the exact conflation epic #70 exists to remove. BGP
operational state needs its own source, its own fields, and its own honesty
about what cannot be answered.

## Source validation

Searched both vendored baselines (`src/pyecsdwan/_specs/`, 7.2.0, 1833
operations) for BGP and route-table read sources.

### What exists

| Scope | Endpoint | Carries |
|---|---|---|
| appliance | `GET /bgp/state` | `summary` block **and** `neighbor.neighborState` |
| orchestrator | `GET /bgp/state` | same shape; params `nePk` (**required**), `cached`, `vrfId` |
| orchestrator | `GET /bgp/state/allVrfs` | all segments |
| appliance | `GET /bgp/config/*` | configuration — **not** status |

**The API draws the CONFIG/STATE line itself.** `/bgp/state` and
`/bgp/config/*` are separate endpoints, which independently corroborates the
taxonomy's central distinction (D-GNMI-1) rather than it being a CLI
convention imposed on a flat API.

### Finding 1 — `summary` and `neighbors` are one call, not two

Both views come from a single `/bgp/state` response: the `summary` object and
the `neighbor.neighborState` object arrive together. So the two commands share
one fetch and one cost class — a fact the grammar's cost column must reflect,
and a reason not to model them as independent reads.

### Finding 2 — `routes` has no source, and must be reported unsupported

There is **no BGP route-table endpoint in either baseline.** Searched every GET
whose path mentions route/rib/prefix/advertis (21 endpoints):

* `/networkRoutes` — management/LAN routes and WAN next-hops, not the BGP RIB
* `/routeMaps`, `/routeMap/dependencies/{name}`, `/redistributionMaps` —
  route *policy*, i.e. configuration
* `/multicast/state/routes` — multicast
* `/routeLabels`, Azure `route-server/*` — unrelated

What *does* exist is **counts, not rows**: `num_bgp_rtes_rcvd`,
`num_ebgp_rtes`, `num_ibgp_rtes`, `num_rib_rtes`, `num_subs_installed` in the
summary, and `rcvd_pfxs` / `sent_pfxs` per neighbor.

Per #72's own acceptance criterion — *"each have verified semantics and
fixtures, **or are explicitly reported unsupported**"* — `routes` is
**unsupported**. `show appliance <name> bgp routes` must answer `unsupported`
(exit 5), naming why, and must not be silently dropped from the help listing
as though it had never been proposed.

This is the fourth brief this session that endpoint verification has
corrected, after epic #4's `/route_policy`, epic #54's `/flow/{neId}/q`, and
#78's premise.

### Finding 3 — the API distinguishes cached from live, natively

Orchestrator `GET /bgp/state` takes a `cached` query parameter. Q2's answer
made `--stale-ok` opt-in; this maps straight onto it, so freshness is a
property the source supports rather than something the CLI has to infer.

### Finding 4 — three schema traps

1. **`neighborState` is an object keyed `"0"`, `"1"`, … not an array.** The
   schema documents exactly two keys, described as "state information of the
   first/second neighbor". That is a documentation artifact, not a limit —
   an implementation must iterate every numeric key. Assuming an array, or a
   maximum of two, is the same class of mistake as epic #54's `installed[0]`.
2. **`bgp_state_str` is typed `integer`** while described as "String
   representation of the bgp state". Type and description contradict, exactly
   like `ipEitherFlag` in #59. **Unverified** — do not rely on either until
   observed live.
3. **`GET /bgp/vrfs/{vrfId}/state` is summarised "Delete specific/all segment
   BGP state".** A read-shaped verb whose own summary says delete. Either the
   summary is wrong or the endpoint mutates; both are possible in this API
   (`GET /oro/debug/closeGrpcConnection` really does mutate). **Do not use
   this endpoint** until verified — use the orchestrator form's `vrfId`
   parameter instead. Feeds #67.

## Goal

`show appliance <name> bgp summary|neighbors` answer with real protocol state,
never with configuration; `routes` says clearly that no source exists; and a
bare `... bgp` lists the leaves rather than fetching anything.

## Non-goals

* Not implementing the views — #74 does that, and depends on this spec.
* Not parsing native CLI text to synthesise a route table. #72's guardrail
  forbids it without versioned fixtures and an explicit maturity status, and
  Principle V would make that a Tier-0 claim at best.
* Not adding OSPF/interfaces/tunnels. The pattern here must be reusable for
  them; instantiating it is separate work.

## The views

### `show appliance <name> bgp summary`

| | |
|---|---|
| Source | orchestrator `GET /bgp/state?nePk=<pk>` (`vrfId` optional) |
| Cost | `single` — one call, shared with `neighbors` |
| Freshness | live by default; `--stale-ok` sets `cached=true` and annotates |

Fields (all from the `summary` block, names as the API gives them):
`bgp_state` + `bgp_state_str` (trap 2), `local_asn`, `local_ip`, `rtr_id`,
`num_peers`, `num_peers_active`, `num_bgp_rtes_rcvd`, `num_ebgp_rtes`,
`num_ibgp_rtes`, `num_subs_installed`, `reject_mismatches`,
`reject_unpreferred`, plus the `mgmt_stub_*` / `tunbgp_*` error counters.

`bgp_state` is an integer enum the schema documents inline (0 = Not Enabled,
1 = Down, … 9 = Active, 10 = Unknown). **0 and 1 are different answers** and
neither is an error: "BGP is not enabled here" is a successful `ok`, not
`unsupported`.

### `show appliance <name> bgp neighbors [<ip>]`

| | |
|---|---|
| Source | the same `/bgp/state` response, `neighbor.neighborState` |
| Cost | `single`, shared with `summary` |
| Drill-down | optional `<ip>` filters to one peer (D-IOS-2) |

Per-peer fields: `peer_ip`, `asn`, `peer_state` + `peer_state_str`,
`local_ip`, `rtr_id`, `rcvd_pfxs`, `sent_pfxs`, `rcvd_updates`,
`sent_updates`, `time_established`, `time_last_update`, `peer_caps`, and the
`{rcvd,sent,}last_err*` triples.

`neighbor.neighborCount` is authoritative for how many peers exist; the row
count must be reconciled against it, and a mismatch reported as `partial`
rather than silently trusted.

**Correlation with configuration** (#72 requires this): a neighbor present in
`/bgp/config/neighbor` but absent from `neighborState` is *configured but not
observed* — a real and interesting state. It must be shown as such, never
inferred to be established. That is #72's first guardrail, and the reason this
view cannot be built on the config object.

### `show appliance <name> bgp routes` — **unsupported**

No source exists. The command must:

* exit `5` (`unsupported`), per `grammar.md` §5;
* say that no BGP route-table endpoint exists in the supported API, not that
  the appliance failed;
* point at what *is* available — the route counts in `summary`, and Tier-0
  `ec-cli api` for anyone who finds a source this spec missed;
* remain listed by a bare `... bgp`, marked unsupported. Hiding it would make
  the CLI look like it never considered the question.

### Bare `show appliance <name> bgp`

Lists `summary`, `neighbors`, `routes (unsupported)` and exits 0. It makes
**no API call** — #72's guardrail that a bare nonterminal is contextual help,
not an implicit expensive fetch, and #74's criterion that static help never
costs a request.

## Requirements

| # | Requirement | How it is verified |
|---|---|---|
| R1 | `summary` and `neighbors` never render the normalized config object | Test asserts the source endpoint and that config-only fields are absent |
| R2 | One `/bgp/state` call serves both views | Request-count test |
| R3 | `routes` answers `unsupported` with exit 5 and an explanation | Test on exit code and message |
| R4 | A bare `... bgp` lists leaves and makes no API call | Request-count test asserting zero calls |
| R5 | Every numeric key of `neighborState` is read, not just `0`/`1` | Fixture with 5 neighbours |
| R6 | Configured-but-not-observed neighbours are shown as such | Fixture: config has 3, state has 2 |
| R7 | `neighborCount` disagreeing with row count reports `partial` | Fixture with a deliberate mismatch |
| R8 | `--stale-ok` sets `cached=true` and annotates; default is live | Request-param test both ways |
| R9 | JSON carries appliance, source, observed-at, support state, partial/error detail | Schema test |
| R10 | denied / unsupported / stale / partial / unreachable are distinguishable | One test per state, human and JSON |
| R11 | `GET /bgp/vrfs/{vrfId}/state` is not called anywhere | Grep test over the source |

## Open questions

| # | Question | Blocks? | Owner |
|---|---|---|---|
| Q1 | `peer_state`'s integer enum is not documented in the schema the way `bgp_state`'s is. Map it from observed values, or render `peer_state_str` and keep the integer raw? | No — R10 works either way; affects table polish | implementer, pending live data |
| Q2 | `time_established` / `time_last_update` are integers with no unit given. Epoch seconds is the obvious reading and the obvious way to be wrong by 1000×. | No — render raw until confirmed | pending live data |

## Evidence expected

* **This spec:** source-verified against the vendored 7.2.0 baselines. The
  endpoint set, parameters and field names are read from the specs, not
  assumed.
* **At #74's merge:** mock-verified, once the mock grows a `/bgp/state` route.
* **Not obtainable without gear:** `bgp_state_str`'s real type, `peer_state`'s
  enum, the time units, whether `neighborState` ever exceeds two keys in
  practice, and whether `/bgp/vrfs/{vrfId}/state` deletes. All labelled
  unverified until someone runs them against hardware (Principle V).
