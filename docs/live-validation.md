# Live validation protocol (#66)

How a resource earns an evidence level above `mock-verified`, and what to write
down so the claim means something to the next operator.

`docs/plugin-promotion.md` is the other half of this: it covers **tier**, which
is how carefully a resource was written and is decided in a code review. This
covers **evidence**, which is what someone has watched it do on real gear.
Neither substitutes for the other. A resource can be immaculately curated and
have never touched a fabric — three of the 42 still have not, and only three
have been changed and rolled back on one.

`ec-cli show coverage --evidence` reports where every kind stands, offline.

## The ladder

| level | means |
|---|---|
| 1 `implemented` | code exists and type-checks |
| 2 `mock-verified` | green against the bundled mock — the ceiling of what this repository can establish on its own |
| 3 `live-read-verified` | `fetch()` + `normalize()` run against a real fabric |
| 4 `live-no-op-write-verified` | a no-op round trip: read, commit it back unchanged, re-plan is empty |
| 5 `live-change-and-rollback-verified` | a real change, verified, rolled back, and persisted |
| 6 `production-supported` | plus the four failure paths below |

**Level 5 is the floor for calling a write path supported.** Below it,
"shipped" means the code exists.

## Why versions are not optional above level 2

An observation without a version is not evidence about a fabric anyone else
has. "It worked" on an unrecorded Orchestrator tells the next operator nothing
about theirs, and it cannot be re-checked when a release changes a payload.

This is not hypothetical here. This repository already has live-read history:
Phase-2/3 plugins carry payload shapes captured against a real lab Orchestrator
on 2026-08-26, and six template-section names were confirmed against a real
Default Template Group (`docs/sitrep/2026-08-26-fanout.md`). **None of it
promotes a single resource**, because nobody wrote down the version. That is
why `evidence.Record.validate()` refuses a level-3-or-above entry without the
Orchestrator version, the ECOS version, the auth mode, the date, and a source
to re-read.

Record it at the time. Reconstructing it later is guessing.

## What to capture, once, before you start

```
ec-cli show fabric version --json > evidence-versions.json
```

That gives the Orchestrator's running version and every appliance's active
partition version. Note also:

- **auth mode** — `api-key` (sessionless; `*.silverpeak.cloud` and
  `*.silverpeaksystems.net`) or `session`. They are different code paths in
  `client.py`, and cloud and on-prem Orchestrators do not agree about which
  they accept, so evidence gathered under one does not transfer to the other.
- **the appliance model and role** you tested against, in the notes.

Never capture credentials, tokens, hostnames of production fabrics, or
customer data. Payload shapes, field names and version strings are the point;
values are not. `ec-cli api` output and journal entries both go through the
client's redaction, but a hand-pasted response does not — read what you paste.

## The behaviors

Each is a string the ledger records, and each is checked cumulatively by
`evidence.required_behaviors()`. Run them in this order; each one assumes the
previous passed.

### `live-read` — level 3

```
ec-cli show configuration appliance <name> <noun>
```

Passes when `fetch()` returns and `normalize()` does not raise. Watch for
fields the vendored 7.2.0 spec does not carry: they ride unknown-key
passthrough, and a resource that drops one silently produces phantom drift
forever after. Compare against the mock fixture and record any divergence in
the resource's module docstring.

### `no-op-round-trip` — level 4

Read current state, stage it back unchanged, commit, then re-plan.

```
ec-cli show configuration appliance <name> <noun> --json > current.json
ec-cli load current.json
ec-cli diff                 # must be empty; if not, normalize() is not canonical
ec-cli commit
ec-cli diff                 # must still be empty
```

**A non-empty first diff is a failure, not a surprise to work around.** It
means the resource's canonical form does not round-trip through the server,
which is exactly the phantom-drift bug `normalize()` exists to prevent — and
it will re-write configuration on every unrelated commit.

This is the cheapest test with real teeth, and the first one to run against
any fabric you are nervous about.

### `real-change` + `post-apply-verification` + `rollback` + `save-persistence` — level 5

One sequence, all four:

```
ec-cli set appliance <name> <noun> <path> <value>
ec-cli commit confirm 10
ec-cli show configuration appliance <name> <noun>   # the change is really there
ec-cli rollback
ec-cli show configuration appliance <name> <noun>   # and really gone
```

- **`real-change`** — the write reached the appliance. For an appliance-scope
  resource, confirm the action-log record names its nePk; a control-plane
  record alone is not evidence the push landed (#64).
- **`post-apply-verification`** — `verify()` returns true against freshly
  fetched state, not against what was staged.
- **`rollback`** — the snapshot restores exactly. For a COMPENSABLE resource,
  check what the compensator leaves behind, not just that it ran.
- **`save-persistence`** — `hasUnsavedChanges` is false on every appliance
  written to. This is the one people skip: a change in running config that was
  never saved to flash looks identical until the next reboot.

Use `commit confirm` rather than a bare commit. If the change cuts your own
management path, the confirm window reverts it without you.

### `reboot-persistence` — level 6

Reload the appliance and re-read. Save-changes writing to flash and the
appliance booting with the change are two claims; only the second one survives
a maintenance window.

### `template-owned-refusal` — level 6

Associate a template group that selects this resource's section, then attempt a
direct change. `managed_by()` must refuse it, naming the owning group. Then
re-run with `--override-template` and confirm it proceeds.

Worth doing carefully: this test is the only thing that turns a guessed
section name into a known one, and since #20 the difference is load-bearing
rather than advisory. `ownership.SECTION_MAP` marks each kind `verified` or
not, and the flag decides what a *non-match* means:

* **verified** — the name came back from a live `GET /template/templateSelection`.
  A group that does not select it genuinely does not own the section: `unowned`,
  and the write proceeds.
* **unverified** — the name is spelled after the ECOS path and has never been
  observed. A group that does not select it has established nothing, because
  "not selected" and "wrong name" are indistinguishable: `unknown`, and the
  commit is refused without `--override-template`.

A *match* is conclusive either way — a guess that matched was a correct guess —
so verification only ever affects the negative answer.

Seven kinds are verified today, from the eleven names one real Default Template
Group returned on 2026-08-26 (`adminDistance`, `cli`, `dns`, `datetime`,
`logging`, `mgmtServices`, `routes`, `secureWebServicesConfig`, `shaper`,
`snmp`, `webconfig`). Sixteen kinds are still guesses. Three of those were
previously commented "CONFIRMED real (matches the ECOS path itself)" — that is
the guess restated, not confirmation, and #20 recorded them as what they are.

**How to promote one.** Find or build a template group that selects the section
in question, associate it with a lab appliance, and run
`ec-cli show configuration appliance <name> <noun>`: the header must read
`managed-by: template-group <group>`. Then add the observed name to
`ownership.LIVE_CONFIRMED_SECTIONS`, switch the kind's entry from `_guess` to
`_verified`, and record the Orchestrator version. `tests/test_ownership_fail_closed.py`
refuses a `verified` entry that names nothing in that list, so the two cannot
drift apart.

This is issue #20's remaining half, and it closes by evidence — one live group
per section name — not by anyone re-reading the code.

### `injected-job-failure` — level 6

Make a write fail server-side — a payload the appliance rejects, or an
appliance that is unreachable mid-commit — and confirm the transaction fails
and auto-reverts rather than reporting CONFIRMED. Since #64 an unrecognised
terminal job shape is `UNKNOWN` and fails closed; if you hit one, the failure
detail quotes it, and it belongs in `docs/research/job-shapes.md`.

### `permission-denied` — level 6

Run against an account without write permission on this resource. The failure
must name the permission problem and revert cleanly. A 403 that surfaces as a
generic error, or a transaction that half-applies before hitting one, is a
finding.

## Recording it

Add or update the record in `src/pyecsdwan/_evidence/ledger.json`:

```json
{
  "kind": "appliance/banners",
  "level": "live-change-and-rollback-verified",
  "orchestrator": "9.4.2",
  "ecos": "9.3.1.40100",
  "auth_mode": "api-key",
  "observed": "2026-09-14",
  "behaviors": [
    "live-read", "no-op-round-trip", "real-change",
    "post-apply-verification", "rollback", "save-persistence"
  ],
  "source": "docs/sitrep/2026-09-14-live.md",
  "notes": "EC-V spoke; the banner object carries a `motd` key absent from the 7.2.0 spec"
}
```

`source` must point at something re-readable — a sitrep, a PR, an issue
comment. "I remember doing it" is how the 2026-08-26 observations became
unusable.

Then add the versions to the ledger's `support` block, which is what
`show fabric version` warns against. An empty `support` block warns about every
fabric, which is correct while it is empty.

The ledger is validated on load, so an entry that claims more than it carries
fails immediately — including on an operator's machine, not only in CI.

## What this cannot do

Nothing here can be automated into `make check`, and it should not be. The
whole point of the ladder is that levels 3–6 require a fabric that this
repository does not have and must never assume. `tests/test_evidence.py`
checks that the *bookkeeping* is honest — every curated kind has a record,
no record over-claims, mock evidence cannot reach the top. It cannot check
whether anyone actually ran the protocol; only the `source` field can, and
only by being re-read.
