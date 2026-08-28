# Migration

**Feature:** `001-cli-command-taxonomy` · **Version:** 0.3.0 · **Status:** draft
**Changed in 0.3.0:** the owner confirmed pyecsdwan has **not shipped to
production**, so there are no old commands to maintain. The compatibility-alias
design in 0.1.0/0.2.0 is withdrawn; old forms are **removed**, not aliased.

## The decision, and what it deletes

Principle VI ("reversible evolution") exists to protect people who are already
running the thing. Nobody is. There are no scripts to break, no muscle memory
to respect, and no removal boundary to negotiate — Q3 is answered by not
needing an answer.

Keeping aliases anyway would have cost real work and left the CLI carrying two
spellings for every renamed command, forever by default, to protect users who
do not exist. So:

| Was planned (0.2.0) | Now |
|---|---|
| Alias + deprecation warning on every renamed form | **Removed.** The old form is not accepted. |
| Hard-fail window for the one meaning-change | **Not needed.** The new meaning simply applies. |
| Removal boundary (Q3) | **Not needed.** Nothing to remove later. |
| Behavior tests for every legacy form | **Not needed.** Replaced by tests that the old form is *gone*. |

**Principle VI is not being violated — it does not apply.** Its subject is an
operator who would be surprised, and there is no such operator. Recorded here
rather than in the constitution's exception register for that reason: this is
"out of scope", not "departing with an expiry".

## What changes for the operator

| Old form | New form |
|---|---|
| `show run` | `show configuration fabric` |
| `show run <section>` | `show configuration fabric <section>` |
| `show run appliance <name>` | `show configuration appliance <name> --format native` |
| `show appliance <name> <kind> [<instance>]` | **split by intent** — `show appliance <name> <domain> ...` for state, `show configuration appliance <name> <kind>` for config |
| `show <kind> [<instance>]` *(orch-scope)* | `show configuration <kind> [<instance>]` |
| `show appliance <name> appliance/<kind>` | `show appliance <name> <kind>` — the key is no longer accepted (rule 3, shipped) |
| `ec-cli set\|delete\|load\|plugin promote appliance/<kind>` | same commands with `<kind>` — the scriptable CLI resolves nouns too (rule 6, shipped) |
| `show flows summary` | `show fabric flows summary` |
| `show flow <ip>` | `show fabric flow <ip>` |

Unchanged: `show appliances`, `show version`, `show journal`, `show pending`,
`show locks`, `show candidate`, `show coverage`, config-mode `show`,
`show compare` / `show | compare`, and every non-read command (`set`,
`delete`, `commit`, `confirm`, `discard`, `rollback`, `api`, `diff`).

## The one that was dangerous is now simply gone

`show appliance BR1-EC bgp` returned modeled configuration and now means
operational state — same tokens, different data. 0.2.0 handled that with a
hard-fail window, because a warning that scrolls past still leaves an operator
reading session state believing it is configuration.

With no existing users, there is nobody holding the old meaning, so the new one
just applies. **The hard-fail is withdrawn** — it was protection for a
population that does not exist, and keeping it would refuse a command that
ought to work.

## Rules

1. **Removed means removed.** An old form produces `invalid` (exit 2) with the
   normal "unknown command / valid next tokens" message — not a special
   deprecation path, because there is no deprecation.
2. **Tests assert absence, not aliasing.** For each row above, a test that the
   old spelling is *not* accepted, so the removal cannot silently regress into
   a half-supported form.
3. **The `#77` legacy `appliance/<kind>` acceptance is withdrawn too.** It
   shipped in #82 as a warned alias under the old plan; #74 removes it and the
   warning with it. Registry keys stay internal, full stop.
4. **Nouns are one rule, not one per surface.** The shell resolved nouns and
   the scriptable CLI did not, so `banners` worked at the prompt while
   `ec-cli plugin promote banners` answered "unknown resource kind" — and
   listed the registry keys. Both surfaces now resolve through
   `Registry.resolve_cli`, with the same scope rule and the same retry in the
   other scope; only the unknown-token error differs, and deliberately (the
   shell lists the position's nouns, since position is scope there; the
   scriptable CLI lists all of them, since scope is a flag).
5. **`--format native` inherits the existing allowlist unchanged.** `show run
   appliance` is the one surface reaching an appliance's command interpreter;
   its deny-by-default validator (40 refusal cases) is not relaxed by being
   renamed.
6. **Documentation carries before/after examples anyway** (#74 asks for them).
   Not for compatibility — for anyone reading an older sitrep or issue and
   wondering why the command in it no longer exists.

## If this turns out to be wrong

The moment there is a real user, this decision inverts and Principle VI applies
in full. That is a one-line trigger worth naming: **the first external
installation is the point at which command removal stops being free.**
