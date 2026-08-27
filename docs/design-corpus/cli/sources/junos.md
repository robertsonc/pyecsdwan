# Junos CLI

**Primary source:** [Junos configuration viewing](https://www.juniper.net/documentation/us/en/software/junos/cli/topics/topic-map/junos-configuration-viewing.html)
**Accessed:** 2026-08-27 · **Version:** undated topic page (Junos CLI User Guide)

## What the source establishes

Junos separates intent by **mode**, and the same verb means different things
in each:

* In **configuration mode**, `show` displays the *candidate* configuration at
  the current hierarchy level.
* In **operational mode**, `show configuration` displays the last committed
  configuration — the one currently running.

Output form is a **pipe modifier**, not a different command:
`| display set` renders the configuration as the `set` commands that would
recreate it, `| display set relative` does so from the current level,
`| display set explicit` includes implicitly-created statements, `| display xml`
gives XML, `| display detail` adds help text and permission requirements, and
`| match` filters by regular expression.

On incomplete configuration, the source notes the CLI keeps re-displaying its
message about the missing required statement on every `show` rather than
reporting once and moving on.

## Transferable

1. **Mode carries intent, so the verb does not have to.** `show` needs no
   qualifier inside configuration mode because the mode already says
   "candidate". This is why pyecsdwan can keep bare `show` in configuration
   mode while requiring `show configuration ...` in operational mode, without
   the two being inconsistent — they are the same rule applied in two modes.
2. **Format is a modifier, orthogonal to the noun.** `| display set` does not
   change *what* is shown, only its rendering. That separation is what lets
   pyecsdwan treat native vendor text as a `--format` rather than as a
   different command (`show run`), which is the specific mistake #70 names.
3. **Naming the datastore in operational mode.** `show configuration` is
   explicit that you are crossing from state into config.

## Incompatible assumptions

* **A candidate is cheap and local.** Junos edits a candidate on the device
  itself. pyecsdwan's candidate is a client-side changeset in
  `~/.pyecsdwan/candidate/`, materialized against server state at compare and
  commit time, and — since #63 — under a host-scoped lock. "The candidate" is
  therefore *staged intent*, not a fully materialized configuration tree, and
  the grammar must not imply otherwise.
* **One device, one datastore.** Junos `show` has an unambiguous subject.
  pyecsdwan's subject may be the fabric, one appliance, or many, and the read
  cost differs by orders of magnitude between them. Junos offers no precedent
  for the cost problem.
* **The re-display-on-every-show behavior is not a model to copy.** Repeating
  the same message on every invocation is noise; report once, clearly.

## Decisions

* **D-JUN-1.** Keep configuration-mode bare `show` = candidate. *(Adopt.)*
* **D-JUN-2.** Operational-mode configuration reads must carry the
  `configuration` noun. *(Adopt.)*
* **D-JUN-3.** Native vendor text becomes a format modifier, not a verb.
  Retire `show run appliance X` in favour of
  `show configuration running appliance X --format native`. *(Adopt; drives
  the #74 migration.)*
* **D-JUN-4.** Do not copy the pipe-modifier syntax wholesale. `| compare`
  already exists and stays; a general `| display <x>` grammar is a larger
  surface than this project needs, and `--format` is the same idea in the
  shape the scriptable CLI already uses. *(Reject, with the principle kept.)*
