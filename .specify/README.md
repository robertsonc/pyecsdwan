# Spec-driven workflow (brownfield adoption)

Issue #75. This directory holds the project constitution and the templates the
feature flow uses. It follows [GitHub Spec Kit](https://github.com/github/spec-kit)
in shape, adopted as a *brownfield* workflow: it applies to new and changed
work, and does not demand retroactive conversion of what already exists.

```
.specify/
  memory/constitution.md    the governing rules (ratified by the owner)
  templates/spec-template.md
  templates/plan-template.md
  templates/tasks-template.md
specs/<NNN>-<slug>/         one directory per feature
  spec.md                   what and why, with acceptance criteria
  plan.md                   how, with the constitution check
  tasks.md                  ordered work items, each mappable to an issue
```

> `specs/` at the repository root is free for this because the vendored
> OpenAPI baselines moved into the package as `src/pyecsdwan/_specs/` in #65.
> The name now means feature specifications, and only that.

## The flow

```
constitution → specification → clarification → plan → tasks → implementation → evidence
```

1. **Specification** (`spec.md`) — what and why. No implementation detail. Every
   requirement is testable. Unknowns are recorded as explicit open questions,
   not resolved by guessing.
2. **Clarification** — the open questions go to the owner. A question whose
   answer changes the design blocks the plan; one that does not is recorded
   with a stated assumption and proceeds. This step is where a *decision* is
   made rather than a default absorbed.
3. **Plan** (`plan.md`) — how, including the **constitution check**: each
   principle, whether it applies, and how the design satisfies it. A principle
   marked non-negotiable and unsatisfied stops the plan.
4. **Tasks** (`tasks.md`) — ordered, independently reviewable work items with
   explicit dependencies, each carrying its own acceptance check.
5. **Implementation** — tasks become GitHub issues (below). Code lands per task
   or per coherent group, gated by `make check`.
6. **Evidence** — what was actually verified, at what level (Principle V), and
   what was *not*. This repository already does this per session in
   `docs/sitrep/`; a feature's evidence section is the same discipline scoped
   to one feature.

## Tasks and GitHub issues

**Issue state remains the live tracker.** `tasks.md` is the plan of record for
a feature; it is not a second tracker to keep in sync.

* An approved task becomes a GitHub issue when work is ready to start on it —
  not at planning time. Planning a task is cheap; an issue nobody is going to
  pick up for two months is noise in the tracker.
* The task line records its issue number once one exists. That is the only
  synchronization required, and it is one-directional.
* Status lives in the issue, never in `tasks.md`. If the two disagree, the
  issue wins.
* A task that turns out to be an epic is re-sharded in the issue tracker, and
  `tasks.md` records the split. Epic #54 did exactly this and it worked.

## When this flow does *not* apply

Deliberately narrow. Skip it for:

* **P0 and security fixes.** They ship first. Record the constitution check in
  the follow-up, not the fix.
* **Bug fixes with an obvious correct behavior** — a wrong path, a crash, an
  off-by-one. The issue is the spec.
* **Mechanical changes** — dependency bumps, formatting, generated-code
  refreshes.

Use it for anything that changes a public interface, adds a resource kind,
alters a safety guarantee, or would be hard to reverse.

## Ratification status

The constitution is at **0.1.0, drafted and not yet ratified**. Until the owner
ratifies it:

* The templates and flow are usable, and this feature (`001-cli-command-taxonomy`)
  is the pilot #75 asks for.
* Nothing is rejected *because of* the constitution — a proposed rule is not a
  rule. The constitution check in a plan records findings rather than blocking.

Ratification means the owner accepts the six principles and their
non-negotiable/convention split, at which point the checks become binding and
the version moves to 1.0.0.
