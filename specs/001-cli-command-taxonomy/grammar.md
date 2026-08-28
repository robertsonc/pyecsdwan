# Command grammar

**Feature:** `001-cli-command-taxonomy` · **Version:** 0.2.0 · **Status:** draft
**Changed in 0.2.0:** Q1 and Q2 answered by the owner — the datastore token is
optional and defaults to `running`; fan-out confirms then warns; `--stale-ok`
is opt-in.
Decisions cited as `D-*` are defined in `docs/design-corpus/cli/`.

## 1. The four intents

Every read command resolves to exactly one. This is Principle I, and no token
sequence may resolve to two.

| Intent | Means | Source | Freshness |
|---|---|---|---|
| **operational** | Running state of the network — sessions, routes, flows, versions | live device/Orchestrator read | live |
| **running configuration** | Configuration as it exists on the target now, normalized | live read via proxy | live |
| **candidate** | Staged intent, not yet committed | local `~/.pyecsdwan/candidate/` | local |
| **native** | Vendor's own configuration text | live read via proxy | live |

`native` is a **format of running configuration**, not a fifth intent — see
Decision 5. It appears in this table because operators think of it as a
distinct thing, and the grammar must answer them where they are.

## 2. Canonical shape

```
show [<scope-noun> <scope-key>] <domain> [<collection>] [<instance>] [flags]
```

Scope is outermost-first and mandatory (D-NSO-1, D-EC-1). There is no implicit
"this device" — EdgeConnect always has a subject.

### Operational state

```
show appliance <name> <domain> [<collection>] [<instance>]
show fabric <domain> [<collection>] [<instance>]
```

```
show appliance BR1-EC bgp summary
show appliance BR1-EC bgp neighbors
show appliance BR1-EC bgp neighbors 10.0.0.1
show appliance BR1-EC bgp routes
show fabric flows summary
show fabric flow 10.1.2.3
```

`show appliance BR1-EC bgp` is a **nonterminal**: it lists `summary`,
`neighbors`, `routes` and exits 0. It never picks one (D-NSO-2).

### Configuration

```
show configuration [running] appliance <name> [<kind> [<instance>]]
show configuration [running] fabric [<section>]
show configuration [running] <kind> [<instance>]        # orchestrator-scope
show configuration candidate [<kind> [<instance>]]
show configuration appliance <name> --format native
```

**The datastore token is optional and defaults to `running`** (Decision 1).
`running` may always be written explicitly and means exactly the same thing;
`candidate` is never a default and must be named.

```
show configuration appliance BR1-EC bgp              # running (default)
show configuration running appliance BR1-EC bgp      # running (explicit, identical)
show configuration candidate appliance BR1-EC bgp    # candidate
```

This is shorter for the common read and matches IOS's `show running-config`,
at the cost that the most frequent command does not name its datastore. The
mitigation is that **`candidate` is never implicit** — the only unnamed
datastore is the live one, so an operator can never be shown staged intent
while believing they are looking at the device.

### Reserved words

Making the token optional means `show configuration <token>` must decide what
`<token>` is: a datastore, a scope noun, or an orchestrator-scope kind. So
these are **reserved and may not be used as a kind alias**:

```
running · candidate · appliance · fabric · configuration
```

No kind collides with them today (42 bare names checked). The alias validator
(#77, R6) enforces it going forward, alongside per-scope uniqueness — this
constraint did not exist while the token was mandatory, because the token
position was then unambiguous.

### Configuration mode

Mode carries the intent, so bare `show` needs no qualifier (D-JUN-1):

```
show                 # the candidate, at the current level
show compare         # candidate vs running
show | compare       # alias, retained (D-NSO-3)
```

### Transaction and audit state

Unchanged by this spec, listed for completeness of the intent map:

```
show journal · show pending · show locks · show candidate · show coverage
```

These are neither network-operational nor configuration — they are *CLI state*.
That is a fifth category the corpus has no precedent for, and it is deliberately
left under bare `show` because the subject is the tool itself, not the fabric.

## 3. Scope nouns

| Noun | Subject | Cost class |
|---|---|---|
| `appliance <name>` | one appliance, via the Orchestrator proxy | `single` |
| `fabric` | every appliance, bounded fan-out | `fanout` |
| *(none)* | the Orchestrator itself | `single` |

## 4. Domains and kinds

Resource kinds are addressed by **user-facing nouns scoped by the command**,
never by internal registry keys (Decision 6, #77).

The live registry today holds 43 kinds: 21 appliance-scope (all carrying an
`appliance/` prefix), 22 orchestrator-scope (2 carrying a `generated/` prefix).

Two forms of leakage exist, and they are different problems:

* **`appliance/<kind>`** duplicates scope the command has already established.
  Once `show appliance BR1-EC` is parsed, `appliance/` is noise.
* **`generated/<operation-id>`** leaks the Tier-1 *generator*, and the names
  beneath it are raw operation ids such as
  `generated/appliance_post_virtualif_vti_by_vti_name`. Note this prefix
  encodes **tier**, while `appliance/` encodes **scope** — the two prefixes do
  not mean the same kind of thing, which is itself a reason not to surface
  either.

### The collision, and why the alias namespace is scoped

Stripping prefixes into one flat namespace produces exactly one collision
today, and it is a real one:

| Bare noun | Kinds |
|---|---|
| `zones` | `appliance/zones` **and** orchestrator-scope `zones` |

They are genuinely different objects — appliance firewall zones versus the
Orchestrator's zone definitions and segment↔zone map. So the alias namespace is
**per scope**, not global (#77's "unique within each scope" is load-bearing,
not decorative):

```
show appliance BR1-EC zones        -> appliance/zones
show configuration running zones   -> zones   (orchestrator-scope)
```

Uniqueness is checked at startup and in tests, so a future collision fails for
a developer rather than for an operator (#77 acceptance).

## 5. Outcomes

Derived from gNMI's taxonomy (D-GNMI-2), extended with the distributed cases it
does not cover because it addresses one target and pyecsdwan fans out.

| Outcome | Meaning | Exit | Human | JSON `status` |
|---|---|---|---|---|
| `ok` | Result, non-empty | 0 | the result | `ok` |
| `empty` | Target answered, configuration genuinely empty | 0 | explicit "no configuration" line | `empty` |
| `not_found` | Path valid, object does not exist | 4 | names what was looked for | `not_found` |
| `unsupported` | Valid, this software/API does not implement it | 5 | names version/endpoint | `unsupported` |
| `invalid` | Syntactically wrong command | 2 | usage + valid next tokens | `invalid` |
| `denied` | Permission refused | 6 | names the operation | `denied` |
| `unreachable` | Appliance/Orchestrator not reachable | 7 | names the target | `unreachable` |
| `timeout` | Bounded wait elapsed | 7 | names target and budget | `timeout` |
| `partial` | Fan-out: some targets failed | 8 | per-target rows, failures marked | `partial` |
| `stale` | Served from cache past its freshness bound | 0 | age + source annotation | `stale` |
| `error` | Malformed/unexpected response | 1 | appliance, kind, operation, cause | `error` |

**Principle II governs this table.** `empty` and `stale` exit 0 because they
are answers; every other non-`ok` state is distinguishable and none may be
rendered as success. A renderer may never reduce a valid response to zero
visible characters (#78).

`stale` exiting 0 was flagged as a judgement call in `plan.md`. Decision 7
settles it: cached data is served **only** when the operator passes
`--stale-ok`, so `stale` is never reached by default and exiting 0 is simply
honouring what was asked for. It still carries its age and source annotation.

`partial` is the one with no precedent — gNMI's rule that a target must never
collapse several paths into one response (D-GNMI-3) is the principle, but the
exit code and per-row marking are EdgeConnect-specific (D-EC-3).

## 6. Flags

| Flag | Applies to | Default | Notes |
|---|---|---|---|
| `--format {yaml,json,native}` | reads | `yaml` | `native` valid only for running configuration (D-K8S-4, D-JUN-3) |
| `--appliance <name>` | scriptable `set`/`delete` | — | scriptable spelling of the `appliance` scope noun; same ordering (Principle IV) |
| `--max-concurrency <n>` | `fanout` commands | existing default | |
| `--timeout <s>` | reads | existing default | bounded, always (#78) |
| `--stale-ok` | cached reads | **off** | opt-in; without it a read is live or it fails (Decision 7) |
| `--yes` / `-y` | `fanout` commands | off | skip the confirmation prompt (Decision 7) |

### Fan-out cost behavior (Decision 7)

A `fanout` command **confirms, then warns**:

* **Interactive** (a TTY on stdin and stderr) — prompt before running, naming
  the target count and the per-appliance call it will make. `--yes` skips it.
* **Non-interactive** (piped, scripted, CI) — a prompt would hang a pipeline,
  which is the failure class #78 exists to remove. So it does not prompt: it
  **warns on stderr**, names the same cost, and proceeds.

> **Interpretation flagged.** The owner's answer was "confirm, then warn". This
> reads it as *confirm where a prompt is possible, warn where it is not* — the
> only arrangement in which both halves can be true, since a prompt in a
> pipeline cannot be answered. If the intent was instead "prompt **and** also
> warn", or "prompt the first time then warn thereafter", say so and this
> section changes; nothing else depends on it.

Warnings go to stderr so piped output stays machine-parseable.

## 7. Worked examples

Each maps to exactly one intent, source, scope, cost class and schema.

| Command | Intent | Source | Scope | Cost |
|---|---|---|---|---|
| `show appliance BR1-EC bgp summary` | operational | appliance proxy | single | single |
| `show appliance BR1-EC bgp neighbors 10.0.0.1` | operational | appliance proxy | single | single |
| `show fabric flows summary` | operational | all appliances | fabric | fanout |
| `show configuration appliance BR1-EC bgp` | running config | appliance proxy | single | single |
| `show configuration running appliance BR1-EC bgp` | running config | appliance proxy | single | single |
| `show configuration appliance BR1-EC --format native` | running config (native) | appliance CLI, allowlisted | single | single |
| `show configuration fabric security` | running config | Orchestrator + fan-out | fabric | fanout (confirms) |
| `show configuration candidate` | candidate | local | — | free |
| `show compare` *(config mode)* | candidate vs running | local + live | per staged item | varies |
| `show appliance BR1-EC zones` | operational | appliance proxy | single | single |
| `show configuration zones` | running config | Orchestrator | — | single |

The last two are the collision pair, disambiguated by scope alone.
