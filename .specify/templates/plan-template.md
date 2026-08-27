# Implementation plan: <NAME>

**Feature:** `<NNN>-<slug>` · **Spec:** `./spec.md` · **Date:** <YYYY-MM-DD>

## Approach

How, in enough detail to disagree with. Name the modules touched and the shape
of the change, not the diff.

## Constitution check

Required. Work every principle; "n/a" is a valid answer with a reason.

| Principle | Applies? | How this design satisfies it | Risk |
|---|---|---|---|
| I. Intent-separated interfaces | | | |
| II. Safety truth over convenience | | | |
| III. Model-first, labelled native | | | |
| IV. One grammar across interfaces | | | |
| V. Evidence-gated support | | | |
| VI. Reversible evolution | | | |

**A non-negotiable principle (I, II, V) that this design does not satisfy stops
the plan.** Redesign, or propose an amendment — do not proceed and note it.

### Exceptions claimed

Conventions (III, IV, VI) departed from, per the constitution's *Exceptions*
section. Each needs a reason and an expiry.

| Principle | Reason | Expires |
|---|---|---|

## Alternatives rejected

What else was considered and why it lost. A plan with no rejected alternative
usually means only one was thought of.

## Risks

What could make this wrong, and what would surface it early.

## Verification strategy

How the acceptance criteria become tests. For anything guarding against a
silent-wrong-answer class of bug, state how you will confirm the test *fails*
without the fix — this repository has found three guards that passed with the
fix removed.
