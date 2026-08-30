# Handoff — 2026-08-30

Written for a session restarting on a **test host with live Orchestrator
access**, which this session did not have. Everything below was verified
against the bundled mock only.

## Where things stand

Branch `claude/review-issues-epics-roadmap-toi9yt`, pushed, clean, gate green
(`ruff` + `mypy` + `pytest`, plus `make smoke`).

Merged tonight:

| PR | What |
|---|---|
| #119 | candidate acknowledgement (no longer wipes another shell's staging) + T7 declaration envelope |
| #120 | #63's cross-target identity collision — one canonical origin per Orchestrator, everywhere state is keyed |
| #122 | grammar Decisions 9 and 10 |

Unmerged, on the branch, no PR yet — **two commits of T6 work** (`b36040f`,
`f75d054`). Open a PR for these or fold them into the next one.

## The one thing that changes with live access

`docs/live-validation.md` is the protocol. The standing claim in
`docs/ROADMAP.md`'s parity map is still literally true:

> Today **no resource in this tool has live change-and-rollback evidence** —
> not one write path has been executed and rolled back on a fabric at a
> recorded version.

Every ✅ in that table means `mock-verified`. All 41 curated kinds are in that
state, including everything shipped tonight. `src/pyecsdwan/_evidence/ledger.json`
is the machine-readable record; `ec-cli show coverage --evidence` reads it
offline.

### Suggested order for a first live run

1. **Reads only, first.** `ec-cli show ...` and `ec-cli drift`. Non-mutating,
   and they exercise the resolver, the retry policy, and every `normalize()`
   against real payload shapes. That is where I would expect the first
   surprises: every normalizer was written against the mock and the vendored
   `specs/orchestrator-openapi-7.2.0.json`, and the mock is my model of the
   API, not the API.
2. **`ec-cli show coverage --evidence`** to see what the ledger claims before
   you change it.
3. **Only then** consider a write, on something reversible and unimportant.
   `interface-labels` is the simplest curated kind. Commit-confirm (`commit
   --confirm-minutes N`) is the safety net: it arms a detached watchdog that
   reverts if you do not confirm.

### Credentials

Do **not** paste a key into a chat transcript. Use either:

```bash
export ECSDWAN_ORCH_URL=https://<orchestrator>     # scheme matters — see below
export ECSDWAN_API_KEY=...
```

or the OS keyring, service `pyecsdwan`, username = the **canonical origin**
(e.g. `https://orch.example.com`). A key stored under the bare hostname by an
older build is still read, as a fallback, and #106 owns prompting you to
re-store it.

`ECSDWAN_INSECURE=1` disables TLS verification. Only for a lab box with a
self-signed cert, and it warns.

### Things that will now behave differently from the mock

- **`http://` is a different Orchestrator from `https://`** as of #120, and so
  is a different port or base path. State is keyed by canonical origin. If you
  point at the same box two ways you get two candidate stores and two journals,
  by design.
- The client appends `/gms/rest` if your URL lacks it, and identity is derived
  through the same function, so `https://x` and `https://x/gms/rest` are one
  target.
- **`apply --from` cannot write.** It previews and refuses, pending T8. This is
  deliberate — see below.

## In-flight work: T6 (#69)

Spec `specs/003-declarative-apply/tasks.md`. The owner's 2026-08-29 comment on
#69 is the work order — four bullets, two done in the commits above:

- [x] declare every full-object replacement target, not only deployment/DHCP
- [x] stable structured conflict object with target + all refs
      (`Collision.as_json()`, and `CommitError.collisions`)
- [x] entry-point parity for candidate commit and declarative apply
- [ ] **coverage test that every write-capable curated resource declares or
      explicitly exempts a target** — partially done

### What is actually left

14 of 41 write-capable curated kinds declare a target. The remaining ~27 need a
per-kind decision: does this resource replace a whole server object, and which?

`tests/test_write_collisions.py` holds `FULL_OBJECT_REPLACEMENT`, the list I
was able to establish. Extending coverage to *all* write-capable kinds means
tracing each `apply()` to the object it writes.

**Do not try to derive this mechanically.** I tried; the data disproves it:

- `appliance/bgp` writes two genuinely different objects
  (`/bgp/config/system`, `/bgp/config/neighbor`)
- `zones` declares an ID allocator (`/zones/nextId`) beside its object
- `template-group`'s two paths have the common prefix `/template/template`, a
  nonsense string

A target asserted mechanically and wrongly is worse than an absent one: the
absent one at least does not refuse a legitimate changeset. There is a test,
`test_only_the_known_pair_collides_today`, pinning the shared set to exactly the
real deployment/dhcp overlap — it exists to catch false refusals, which is the
failure mode a wave of new declarations introduces.

## Why T8 is blocked

I proposed T8 and then found it blocked; T6 is what I did instead.

T8 (per-resource declarative capability and safe materialization) depends on
**T5, T6, T7**. T7 is done. T6 is in progress above. **T5 (#106, separating
rollback-private secrets from public plan/audit state) is unstarted.** The spec
is explicit that T1–T6 "are not optional polish."

T8 is what gates re-enabling `apply --from` writes, and T13 after it.

## Open decisions — owner's, not mine

| # | Question |
|---|---|
| spec 001 **Q4** | does `show fabric <domain>` warrant existing where the Orchestrator has a single-call answer? |
| spec 001 **Q5** | where the orchestrator registry's *mutations* live. My recommendation is recorded in `specs/001-cli-command-taxonomy/spec.md`: a file, `show orchestrators`, no mutation verbs in v1 |
| #63 | closed, but I flagged two things rather than hide them behind checked boxes — see the closing comment |

## Gotchas that cost me time

- **`ruff format` is not part of the gate and must never be run.** `make check`
  is `ruff check` + bare `mypy` + `pytest`.
- **The mutation harness restores `src/` with `git checkout -- src`**, so it
  destroys uncommitted work under `src/`. It now refuses to start against a
  dirty `src/` and reports a stale-anchor mutation as a no-op instead of a
  silent MISSED — both lessons from this session, where it ate a change and
  twice reported MISSED for mutations that never applied. Script lives in the
  session scratchpad, not the repo; recreate it if you want it.
- **Run the full suite green *before* any mutation sweep.** A sweep against a
  red suite tells you nothing.
- `pytest-timeout` is not installed; `--timeout` is not a valid flag.
- The suite takes ~2 minutes. Use `-p no:randomly` for reproducible ordering.

## The habit that kept finding real bugs

Every guard verified by deleting it. Of the ~45 mutations run this session,
eight were MISSED, and **every single miss was a real coverage gap**, not a
false alarm — including one in a test written minutes earlier (completeness
asserted "overrides `write_target`", which a base returning `None` satisfies
while detecting nothing). Two more "misses" were the harness failing to apply a
stale-anchored mutation, which is why it now reports that distinctly.

The recurring defect shape in this codebase, three times tonight: **a guard
that exists but is not on the path.** `clear()` surviving on the shell path in
#100; `_guard_unadopted_staging` tested directly while nothing tested that
`commit_candidate` calls it; the collision check reachable only for the one
pair someone had already thought about. Test the wiring, not just the guard.
