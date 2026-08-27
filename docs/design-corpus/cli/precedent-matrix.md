# CLI precedent matrix

Issue #73. Each dimension is compared across the corpus, then resolved into a
decision for #70/#71. The decision column is the point of this document — a
row that ends in "interesting" rather than a decision has not been finished.

Source detail and citations are in `sources/`. Decision IDs (`D-XXX-n`) are
defined there.

---

## 1. Operator intent and mode

| Source | How intent is separated |
|---|---|
| Junos | By **mode**. Config-mode `show` = candidate; operational-mode `show configuration` = last committed. |
| NSO | By **mode**, with explicit crossings (`show running-config`, `do show ...`). |
| gNMI | By **typed request field** — CONFIG / STATE / OPERATIONAL / all. |
| IOS | By **separate command** — `show ip bgp ...` vs `show running-config`. |
| NVUE | **Not separated** — operational and applied are columns of one view. |
| kubectl | By **verb** — `get`/`describe` vs `diff`. |
| pyecsdwan today | **Not separated** — `show` hosts operational reports, normalized config, and native text. |

**Decision.** Four intents, named in the command: *operational*, *running
configuration*, *candidate*, *native*. Mode carries intent where it can
(config-mode bare `show` stays the candidate, D-JUN-1); operational mode names
the datastore explicitly (D-JUN-2). NVUE's column approach is rejected
(D-NVUE-2). This is Principle I, and it is the whole of #70.

## 2. Scope ordering

| Source | Order |
|---|---|
| NSO | Outermost-first: `devices device NAME config ...`; a truncated path lists. |
| kubectl | `TYPE NAME`, namespace as a flag. |
| IOS / Junos | Implicit — subject is always this device. |
| gNMI | Structured path, keyed list elements. |
| pyecsdwan today | **Inconsistent** — shell is appliance-first, scriptable uses `--appliance NAME`. |

**Decision.** Outermost-first and uniform across interfaces: scope → kind →
instance (D-NSO-1, Principle IV). Scope is mandatory — EdgeConnect has no
implicit "this device" (D-EC-1). This resolves #71's canonical-ordering
decision and is the precondition for #77: once `show appliance NAME` has
established scope, repeating `appliance/` in the kind is duplication.

## 3. Operational summary / detail / drill-down

| Source | Shape |
|---|---|
| IOS | `show ip bgp summary` / `neighbors` / `show ip bgp` (table); detail by optional key. |
| kubectl | Separate verbs — `get` for list, `describe` for detail. |
| NSO | Path truncation lists; extending it drills down. |

**Decision.** Adopt IOS's three nouns for BGP (D-IOS-1) and NSO/IOS's optional-
key drill-down rather than kubectl's second verb (D-K8S-2, D-IOS-2). Rationale
is audience, not elegance: operators already have summary/neighbors/routes.

## 4. Candidate / running / startup / native

| Source | States distinguished |
|---|---|
| Junos | candidate, committed. |
| NSO | copy-of-active, running; native by namespace prefix. |
| NVUE | pending, applied, startup, operational. |
| gNMI | CONFIG vs STATE (no candidate concept). |

**Decision.** pyecsdwan has *candidate* (client-side staged intent — not a
materialized tree, per Junos's incompatible assumption), *running* (read live
through the proxy; no local mirror, D-EC-4/#63), and *native* (a format, not a
verb: D-JUN-3, D-NSO-4, D-K8S-4 all agree). NVUE's *startup* vocabulary is
adopted where EdgeConnect actually distinguishes applied-vs-persisted
(D-NVUE-1) — which is #64's open `save_changes()` problem.

## 5. Output format and machine contract

| Source | Mechanism |
|---|---|
| Junos | Pipe modifier — `\| display set\|xml\|detail`. |
| kubectl | Flag — `-o/--output`. |
| gNMI | Typed protobuf notifications, one per requested path. |

**Decision.** `--format` as an orthogonal flag (D-K8S-4), with `native` among
its values. Junos's pipe grammar is not adopted wholesale (D-JUN-4), though
`| compare` stays. Every command declares an output schema; multi-appliance
results keep per-appliance outcomes distinguishable (D-GNMI-3, D-EC-3).

## 6. Contextual help and completion

| Source | Mechanism |
|---|---|
| NSO | Truncated path lists what is under it. |
| kubectl | `explain` as a dedicated discoverability verb. |
| Junos | `?` completion; re-displays missing-statement messages on every `show`. |

**Decision.** A bare nonterminal lists valid next tokens and never guesses
(D-NSO-2) — the #70 governing direction, and the fix for #49/#77/#78. Adopt
kubectl's lesson that discoverability deserves a designed surface, without
adding its verb (D-K8S-3): an offline reference view listing domain, scope,
instances, mutability and support status. Junos's repeat-every-time messaging
is explicitly not copied.

## 7. Partial, cached, stale, unsupported, unreachable

| Source | Vocabulary |
|---|---|
| gNMI | NOT_FOUND / UNIMPLEMENTED / INVALID_ARGUMENT; never collapses paths. |
| kubectl | HTTP-derived status; partial-list warnings. |
| IOS / Junos / NVUE | Effectively none — single local device, so absent is absent. |

**Decision.** gNMI's three-way split is the basis (D-GNMI-2), extended with the
distributed cases it does not cover — unreachable, timeout, permission denied,
partial, stale — because pyecsdwan fans out across appliances and they do not.
This becomes #71's outcome/exit-code table and is the direct fix for #78's
silent command. Principle II: none of these may read as success.

## 8. Compatibility and deprecation

| Source | Behavior |
|---|---|
| kubectl | Deprecation warnings on stderr; versioned API groups. |
| NSO / Junos | Release-noted; long tails. |
| pyecsdwan today | No policy — this is the first breaking grammar change. |

**Decision.** Principle VI. Every altered form either keeps working with a
warning or fails naming its exact replacement; none silently changes meaning.
`show | compare` is kept as an alias rather than deprecated (D-NSO-3) — it is
already in operators' fingers and costs nothing. #71 owes a full compatibility
table; #74 implements it.

## 9. Fan-out and query cost

| Source | Treatment |
|---|---|
| all of them | **None.** Every source assumes cheap reads against one target or one indexed API. |

**Decision.** This is the dimension with no precedent to borrow, and it is
EdgeConnect's most distinctive constraint (D-EC-2). Every command carries a
declared cost class; a fabric-wide fan-out says so before it runs; one
unreachable appliance is a marked row, never a lost report (D-EC-3, as
`reports/fanout.py` already does). Invented, not borrowed — and flagged as
such so a later reader does not go looking for the source.

---

## Where the corpus disagrees with itself

Worth stating plainly, because these are the places a decision was a judgement
rather than a reading:

1. **Verb-first vs show-first.** kubectl separates intent by verb; the network
   CLIs separate it by mode and noun. Resolved on audience (D-K8S-1), not on
   merit — a different audience would justify the opposite.
2. **List/detail as verbs vs drill-down by key.** kubectl says two verbs, IOS
   and NSO say one path. Resolved toward the network tradition (D-K8S-2), at
   the cost of `show appliance X bgp neighbors` and `... neighbors 10.0.0.1`
   differing only by a trailing token.
3. **NVUE's columns.** The only source whose central technique is rejected
   outright (D-NVUE-2). It answers a real question — "is running what I
   configured?" — and pyecsdwan answers it with a named command instead.
