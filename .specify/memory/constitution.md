# pyecsdwan constitution

**Version:** 0.1.0 (draft — not yet ratified)
**Status:** proposed for ratification by the repository owner
**Last amended:** 2026-08-27

This document governs how pyecsdwan is designed and what "done" means. It
exists because the project's real rules were already strong but scattered
across code comments, the roadmap, `docs/futures/`, plugin-promotion guidance,
and individual issues — where each new feature had to rediscover them. Issue
#75 asked for one place that the spec, plan and task templates can consume.

Nothing here is aspirational. Every principle below carries a **review
question** that a reviewer can answer yes or no about a specific diff, and
names how it is checked. A principle that cannot be checked is a preference,
and preferences belong in `CONTRIBUTING.md`, not here.

## How to read this

Each principle is marked:

* **Non-negotiable** — a change violating it is rejected, not discussed. These
  encode the safety guarantees the product is *for*. Amending one is a MAJOR
  version bump and needs an explicit, recorded decision by the owner.
* **Convention** — a strong default. Departures are allowed with a recorded
  reason in the feature's spec, and the exception expires (see *Exceptions*).

---

## I. Intent-separated interfaces

**Non-negotiable.**

Operational state, desired/candidate configuration, committed running
configuration, and native vendor output are four different things. A command
names which one it means, and no token sequence may mean two of them.

*Why.* This is the concrete failure #70 was filed about: `show appliance X
<kind>` returns normalized configuration while `show run appliance X` returns
native device text, and both live under the same `show` root beside
operational reports like `show version` and `show flows`. An operator cannot
tell from the command which of the four they are getting. gNMI draws exactly
this line at the schema level — `CONFIG` is "data that the target considers to
be read/write", `STATE` is "the read-only data on the target", and
`OPERATIONAL` is read-only data about running software processes — and Junos
draws it at the mode level, where configuration-mode `show` is the candidate
and operational-mode `show configuration` is the last committed configuration.

**Review question.** For every command this change adds or alters: can you
name its single intent (operational / candidate / running / native), its
datastore or source, and its scope — from the command tokens alone, without
reading the implementation?

**Checked by.** The command-taxonomy spec's grammar table (`specs/`), and the
acceptance tests derived from it. A command absent from that table is not
shippable.

---

## II. Safety truth over convenience

**Non-negotiable.**

Unknown, partial, stale, timed-out, or unverified outcomes never become
success by omission. Where the truth cannot be established, the command says
so; it does not pick the reassuring interpretation.

*Why.* The project has now hit this four separate times, and every instance
was a silent wrong answer rather than an error:

* `commit` noticed that server state had moved since compare, recomputed the
  diff and **carried on**, folding another operator's change into this
  operator's changeset (#63).
* An installed `show coverage` reported "0 of 0 endpoints" because the API
  baselines never shipped in the wheel — not an error, a confident wrong
  answer in the command the roadmap names as the source of truth (#65).
* `fan_out`'s first draft keyed success off a non-None value, silently
  dropping rows for endpoints that legitimately answer `null` (#54).
* `_record_succeeded()` treats a completed job as success unless the result
  contains one of a short list of English failure tokens, so "Completed +
  Invalid configuration" reads as success (#64, still open).

**Review question.** For each way this change can fail to establish the truth
— empty, absent, unsupported, denied, unreachable, timed out, stale, partial,
malformed — does the operator get a *distinct* and visible outcome? Can any of
them be mistaken for success?

**Checked by.** Per-state tests, not a single happy-path test. #78 is the
current worked example: `{}`, `None`, `""`, HTTP 204 and `[]` must have
intentional, separately-tested meanings, and a renderer may never reduce a
valid response to zero visible characters.

---

## III. Model-first, with a labelled native escape hatch

**Convention.**

The normalized, model-driven view is the primary interface. Native vendor
output remains available, is always explicitly requested, and is always
labelled as native — it is never what you get by accident.

*Why.* Native text is the honest answer when the model cannot represent
something, and pretending otherwise would be worse. But it is unstable across
releases, unparseable by contract, and outside every guarantee the resource
contract makes. NSO takes the same position: the model-driven view is
primary, and device-native configuration is reachable but namespaced
explicitly (`devices device <name> config ios:*`) rather than being a
different top-level command.

**Review question.** Can an operator reach native vendor output without asking
for it by name? If a model cannot represent the data, does the command say so
rather than silently degrading to native?

**Checked by.** Grammar review against the taxonomy spec; native must be a
declared format or source token, never a default.

---

## IV. One grammar across interfaces

**Convention.**

The interactive shell, the scriptable CLI, and machine-readable output share
nouns, scope ordering, and error semantics. A command learned in one is
transferable to the others.

*Why.* Today the shell uses appliance-first scope while scriptable `set` and
`delete` use `--appliance NAME`, so the same operation reads differently
depending on how it is invoked (#71). Divergence here is not a cosmetic
problem: it means documentation, help text, and muscle memory are each only
half-right.

**Review question.** Does every command added here have a one-to-one canonical
mapping between shell and scriptable forms, with the same nouns in the same
order? Do both resolve resource names identically?

**Checked by.** A mapping table in the taxonomy spec, and tests that exercise
both surfaces against the same fixture.

---

## V. Evidence-gated support

**Non-negotiable.**

"Implemented", "green against the mock", "live-read verified", and
"live-write verified" are different claims. Documentation, `show coverage`,
and issue status state which one applies. They are never conflated, and the
stronger claim is never implied by silence.

*Why.* The roadmap currently says "shipped" for BGP/OSPF and every epic-#4
write path while also stating those paths have never been live-write-tested.
Both statements are in the same document. #66 exists to fix this with an
explicit evidence ladder; this principle is what makes that ladder binding
rather than advisory.

**Review question.** What is the strongest evidence that actually exists for
this change — spec, mock, live read, live write? Does every place that
describes it to a user say that, and not more?

**Checked by.** #66's evidence ladder once ratified; until then, an explicit
"what is not verified" section in the feature's spec and in the PR, which this
repository already does per-sitrep.

---

## VI. Reversible evolution

**Convention.**

Renames and grammar changes ship with compatibility aliases that are explicit,
warned, tested, and removed only at a declared boundary. An old command form
either keeps working or fails loudly — it never silently changes meaning.

*Why.* The taxonomy work in #70 is a breaking change to a public grammar. The
danger is not that an old command stops working; it is that an old command
keeps working and means something *else* — e.g. `show appliance X bgp`
returning configuration before the change and operational state after it.

**Review question.** For every command form this change alters: does the old
form still work with a warning, or fail with a message naming its exact
replacement? Is there any input for which the old form now returns a different
kind of data without saying so?

**Checked by.** The compatibility table required by #71, plus tests that
assert the old form's *behavior*, not merely that it is accepted.

---

## Exceptions

A feature may depart from a **Convention** by recording, in its spec:

1. the principle departed from,
2. why the principle's goal is met another way or does not apply,
3. an expiry — a release, a date, or a named blocking issue.

An exception without an expiry is not an exception; it is an undocumented
amendment. Exceptions are reviewed when their expiry passes: renew with a new
reason, or remove the departure.

**Non-negotiable** principles have no exception path. Changing one requires an
amendment.

## Amendment

* **Who.** The repository owner ratifies and amends. Contributors — human or
  agent — propose.
* **How.** A pull request that changes this file alone, stating the principle
  affected, the rationale, and the migration impact on work already in flight.
* **Versioning.** Semantic. MAJOR: a non-negotiable principle is added,
  removed, or weakened. MINOR: a convention is added or materially changed.
  PATCH: wording, examples, or clarification that does not change what passes
  review.
* **Record.** Every amendment appends to *Amendment history* below with
  version, date, rationale, and what it invalidates.

## Brownfield scope

This constitution governs **new and changed** work from ratification onward.
It is explicitly *not* a mandate to retroactively convert existing issues,
rewrite shipped resources, or block urgent fixes:

* A P0 or a security fix is never blocked on producing a spec artifact. It
  ships, and the constitution check is recorded in the follow-up.
* Existing commands come into compliance through #74's migration, not through
  a flag day.
* An existing issue is not rewritten to Spec Kit format. New feature work uses
  the flow in `.specify/README.md`; old issues stay as they are and remain the
  live tracker.

## Amendment history

| Version | Date | Change | Invalidates |
|---|---|---|---|
| 0.1.0 | 2026-08-27 | Initial draft for ratification (#75). Six principles, three non-negotiable. | — |
