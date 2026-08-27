# Sitrep — 2026-08-27 — Epic #70 design phase (#75, #73, #71)

Follows `docs/sitrep/2026-08-27-hardening.md`. Second tranche this session,
after #62/#63/#65 merged as PR #79.

## TL;DR

- Epic **#70** was chosen over finishing epic #9, on the owner's call that it
  is the more foundational thread. It is also newer than the work picked at the
  session's start — it landed at 14:49, mid-session.
- Shipped the three **design** issues — #75 constitution, #73 design corpus,
  #71 grammar — and stopped at the epic's own approval gate. No parser change.
- **Two decisions escalated rather than defaulted**, and they block the plan.
- **1313 tests**, unchanged: this tranche is documents. One test *did* change,
  and it was one of mine from #65 that turned out to be over-broad.

## The gate is real, and it is the point

Epic #70 says: *"No command migration should begin until the governing
decisions and grammar acceptance table are approved."* That is not a formality
to route around. #71 lists eight required decisions; six are answerable from
the design corpus, and two are judgement calls about operator experience:

- **Q1.** Is the datastore token mandatory (`show configuration running
  appliance X`) or does bare `show configuration appliance X` default to
  running? Mandatory is unambiguous and verbose; defaulting is shorter and
  matches IOS's `show running-config`, but leaves the most common read with an
  unstated datastore.
- **Q2.** Does a fan-out command confirm, warn, or just run? Is `--stale-ok`
  opt-in, or is cached-with-annotation the default?

Both change the grammar table and the tests derived from it. Picking a default
and calling it a decision is precisely what `.specify/README.md` names as the
failure mode of the clarification step, so they are recorded as blocking open
questions instead.

## Two things the live registry said that assumption would not have

The taxonomy rests on facts read out of the running registry, not inferred:

**43 kinds — 21 appliance-scope, 22 orchestrator-scope — and exactly one
bare-name collision.** `zones` exists as both `appliance/zones` and an
orchestrator-scope `zones`, and they are genuinely different objects
(appliance firewall zones versus the Orchestrator's zone definitions and
segment↔zone map). #77 says aliases must be "unique within each scope"; this
proves that qualifier is load-bearing rather than decorative. A flat alias
namespace would collide on day one.

**Two prefixes leak, and they are not the same problem.** `appliance/<kind>`
duplicates scope the command has already established. `generated/<operation-id>`
leaks the Tier-1 *generator*, with names like
`generated/appliance_post_virtualif_vti_by_vti_name` underneath — arguably
worse. Note one prefix encodes **scope** and the other encodes **tier**, which
is itself an argument for surfacing neither.

## The migration's one dangerous row

Most of the compatibility table is renames — same data, new spelling, warned
alias, safe. One row is not:

```
show appliance BR1-EC bgp
```

Today it returns modeled **configuration**. Under the new grammar,
`show appliance <name> <domain>` is the **operational** form. Same tokens,
different kind of data, no error.

An alias with a warning does not fix this. A warning that scrolls past still
leaves the operator reading session state believing it is configuration. So
that form **hard-fails** during the migration window, naming both
replacements, and resumes with the operational meaning only at the declared
boundary. Being unavailable for a window is a real cost, and it is the only
option that cannot mislead.

## A source I could not read, and did not paper over

#73 lists a Cisco BGP troubleshooting PDF among its starting sources. Its
content streams are compressed; the fetch returned a plausible list of BGP
commands **explicitly flagged as general knowledge, not from the document**.

Under this corpus's own first rule — only primary sources support normative
claims — that result was discarded and the Cisco IOS BGP command reference used
instead. The corpus records the substitution rather than quietly citing the
PDF. Recorded here because the tempting move was to keep the answer, since it
happened to be correct.

## A test of mine that was testing the wrong thing

#65 added:

```python
def test_the_baselines_are_no_longer_at_the_repository_root():
    assert not (REPO_ROOT / "specs").exists()
```

True when written. Wrong the moment #75 adopted Spec Kit, because `specs/` now
means *feature specifications* and has nothing to do with OpenAPI. The
invariant it meant was "no API baseline sits outside the package, where no
wheel would ship it" — so it now asserts that, by globbing for
`*-openapi-*.json` and `payload-examples-*.json`.

Verified by planting a stray baseline and watching it fail, then removing it.

The general lesson is worth keeping: a test that asserts a *proxy* for the
invariant (a directory name) instead of the invariant itself (where the
baselines are) goes wrong silently the moment the proxy stops tracking.

## The corpus's own disagreements

Recorded in `precedent-matrix.md` because they were judgement, not reading:

1. **Verb-first vs show-first.** kubectl separates intent by verb; the network
   CLIs separate by mode and noun. Resolved on audience, not merit — a
   different audience justifies the opposite answer.
2. **List/detail as verbs vs drill-down by key.** Resolved toward the network
   tradition, at the cost of `... bgp neighbors` and `... bgp neighbors
   10.0.0.1` differing only by a trailing token.
3. **NVUE's columns** are the one central technique rejected outright: showing
   operational and applied side by side is the same conflation #70 exists to
   fix, merely rendered rather than hidden. Its *vocabulary* (applied vs
   startup) is adopted, because EdgeConnect has the same
   applied-but-not-persisted gap — which is #64.

The one dimension with **no precedent in any source** is fan-out cost. Every
source assumes cheap reads against one target or one indexed API. That decision
is flagged as invented rather than borrowed, so a later reader does not go
looking for a citation that does not exist.

## What is NOT done

- **No code.** No parser change, no alias layer, no outcome classifier. #72,
  #74, #77 and #78 are specified and unstarted, by design.
- **The constitution is drafted at 0.1.0 and not ratified.** Until the owner
  accepts it, the constitution check records findings rather than blocking, and
  a proposed rule is not a rule.
- **`unsupported` vs `not_found` cannot be verified without gear.** The bundled
  mock cannot faithfully distinguish them, so that pair of outcomes will be
  asserted against the mock and must stay labelled unverified — stated in the
  spec's evidence section before implementation rather than discovered in
  review.

## Final state

`make check`: ruff clean, mypy `--strict` clean (94 source files), **1313
passed, 7 skipped**. Three commits, documents only plus the one test fix.

## Next

1. **Answer Q1 and Q2**, and approve the grammar. Everything in #70 is blocked
   on that and nothing else.
2. **#78 can go first regardless.** The silent appliance command is live today,
   and its fix does not depend on the new grammar — the one part of the epic
   worth doing out of order.
3. **#77 before #74**, or the parser change gets made twice — once against
   registry keys, once against aliases.
4. Epic **#9** still has #64 (async jobs failing closed), #66, #67, #68; **#69**
   remains unblocked by #63.
