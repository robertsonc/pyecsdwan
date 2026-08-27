# Compatibility and migration

**Feature:** `001-cli-command-taxonomy` · **Version:** 0.1.0 · **Status:** draft

Principle VI: an old command form either keeps working, or fails loudly naming
its replacement. It never silently changes meaning.

## The one dangerous case

Most rows below are renames — the same data, spelled differently, so an alias
with a warning is safe. **One row is not.**

```
show appliance BR1-EC bgp
```

* **Today:** fetches the `appliance/bgp` resource, normalizes it, and prints
  *modeled configuration*.
* **Under this grammar:** `show appliance <name> <domain>` is the *operational*
  form, so the same tokens would return BGP session state.

Same input, different kind of data, no error. That is precisely the failure
Principle VI exists to prevent, and an alias-with-warning does not prevent it —
a warning that scrolls past still leaves the operator reading session state
believing it is configuration.

**Therefore this form must fail during the migration window**, not warn:

```
$ show appliance BR1-EC bgp
error: `show appliance <name> <kind>` has split into two commands, because it
       previously returned configuration while the new grammar reads this
       position as operational state.

  configuration:  show configuration running appliance BR1-EC bgp
  operational:    show appliance BR1-EC bgp summary | neighbors | routes

  This form will become the operational one in <BOUNDARY>.
```

Exit 2 (`invalid`). Refusing to guess is the whole point: the two answers are
both plausible and only the operator knows which they meant.

At the declared boundary the form starts working again with the operational
meaning. Between now and then it is unavailable — a deliberate cost, and the
only option that cannot mislead.

## Migration table

| Existing form | Status | Replacement | Mechanism |
|---|---|---|---|
| `show run` | rename | `show configuration running fabric` | alias + warning |
| `show run <section>` | rename | `show configuration running fabric <section>` | alias + warning |
| `show run appliance <name>` | rename | `show configuration running appliance <name> --format native` | alias + warning |
| `show appliance <name> <kind>` *(shell)* | **meaning change** | split — see above | **hard fail** |
| `show appliance <name> <kind> <instance>` *(shell)* | **meaning change** | split — see above | **hard fail** |
| `show <kind> [<instance>]` *(shell, orch-scope)* | rename | `show configuration running <kind> [<instance>]` | alias + warning |
| `show appliance <name> appliance/<kind>` | leakage (#77) | `show ... <kind>` (prefix dropped) | alias + warning |
| `show <kind>` where kind is `generated/<op-id>` | leakage (#77) | taxonomy-approved noun, or Tier-0 `api` | alias + warning |
| `show appliances` | unchanged | — | — |
| `show version` | unchanged | — | — |
| `show flows summary` | scope noun added | `show fabric flows summary` | alias + warning |
| `show flow <ip>` | scope noun added | `show fabric flow <ip>` | alias + warning |
| `show journal` / `pending` / `locks` / `candidate` / `coverage` | unchanged | — | CLI state, not fabric (grammar §2) |
| `show` *(config mode)* | unchanged | — | candidate, per D-JUN-1 |
| `show compare` / `show \| compare` | both kept | — | D-NSO-3 |
| `set` / `delete --appliance <name>` | unchanged spelling | — | already matches scope ordering |
| `diff` / `compare` / `commit` / `confirm` / `discard` / `rollback` / `api` | unchanged | — | not read commands |

## Rules

1. **Warnings go to stderr**, so piped output stays machine-parseable.
2. **An alias warning names the exact replacement**, never a doc link alone.
3. **Aliases are tested for behavior, not acceptance** — a test asserting the
   old form is accepted proves nothing; it must assert the old form returns
   what it always returned.
4. **The hard-fail row is tested for refusal**, including that its message
   names both replacements.
5. **`--format native` inherits the existing allowlist unchanged.** `show run
   appliance` is the one surface reaching an appliance's command interpreter,
   and its deny-by-default validator (40 refusal cases) is not relaxed by being
   renamed.

## Removal boundary

**Open question — the owner's call.** The table above says `<BOUNDARY>`
because the project has no release cadence yet to anchor it to. Options:

* the next MINOR release after ratification, or
* a fixed date, or
* "when `show coverage` reports the taxonomy applied to every curated kind".

Whichever is chosen goes in the constitution's amendment record and in
`--help`. Until it is chosen, aliases have no expiry, which by Principle VI's
own logic makes them permanent by default — so this is worth settling before
#74 begins rather than after.
