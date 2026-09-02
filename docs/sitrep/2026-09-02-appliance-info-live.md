# Sitrep — appliance extra info, live change and rollback, 2026-09-02

The source of record for the `appliance-info` entry in
`src/pyecsdwan/_evidence/ledger.json`. Everything here was observed.

## Environment

| | |
|---|---|
| Orchestrator | **9.7.0.43282** (a temporary lab, 18 × EC-V) |
| ECOS | n/a — the object lives on the Orchestrator, not the appliance |
| Auth | **api-key**, from `ECSDWAN_API_KEY` |
| Date | 2026-09-02 |

## Why

The lab's preconfiguration had stamped a city into every appliance's
location and left `country` at its default: Frankfurt, London, Madrid,
Singapore, Sydney and Tokyo, all `US`. Correcting eighteen appliances through
the UI was the alternative. `appliance-info` is the kind built for it.

## Level 3 — read sweep

`list_refs()` found all 18 appliances. Per appliance: `fetch()`,
`normalize()`, `normalize(normalize(x)) == normalize(x)`, and a canonical
state diffed against itself asserted empty.

**Result: 18 of 18 read, 0 idempotency failures, 0 phantom drift.**

Every object had the three sections the vendored 7.2 spec describes —
`contact`, `location`, `overlaySettings` — and nothing else. Fields nobody set
came back as `null` (`address2`, `state`, `zipCode`, `email`, `phoneNumber`);
`overlaySettings.ipsecUdpPort` carried a real port and
`isUserDefinedIPSecUDPPort` was `false`. The canonical form treats `null` and
`""` alike, so neither can read as drift against the other.

## Level 4 — no-op round trip

On `S2-EC-01` (London, `US`): `set appliance-info S2-EC-01 location country US`
planned as **empty**. No transaction was journaled and nothing was written.

## Level 5 — real change, verification, rollback

On the same appliance, through the real transaction engine:

1. `set appliance-info S2-EC-01 location country "United Kingdom"`; `commit`
   → `CONFIRMED`, one change applied, post-apply verification passed.
2. Re-read: `country` was `United Kingdom`. With that one field set back in
   the comparison, the object was **identical to the baseline** — `city`,
   `address`, the `null` fields, the contact and the overlay port all
   untouched. The write goes out as the complete object, composed over a
   fresh raw read.
3. `rollback 1` → `CONFIRMED`, one resource restored. Re-read: the object was
   **byte-identical to the baseline** (`json.dumps(..., sort_keys=True)`
   equal).
4. A fresh client session re-read the same object, identical again.

Persistence here is the Orchestrator's own database; there is no appliance
flash and no `saveChanges` in the path. The fresh-session re-read after both
the change and the rollback is the persistence evidence this object admits.

## Not observed

* An appliance the Orchestrator returns no object for (the resource maps a
  404 to "absent" and refuses to guess on rollback; every lab appliance had
  an object).
* `DELETE /appliance/extraInfo` — deliberately unused by the resource.
* Whether the UI validates `country` against a list. The API accepted a free
  string; the lab's own preconfiguration wrote `US`.
