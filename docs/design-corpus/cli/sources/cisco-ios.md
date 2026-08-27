# Cisco IOS / NX-OS

**Primary source:** [Cisco IOS IP Routing: BGP Command Reference — BGP Commands: show ip through Z](https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/command/irg-cr-book/bgp-s1.html)
**Accessed:** 2026-08-27

> **Source-quality note.** Issue #73 lists a BGP troubleshooting PDF as a
> starting source. That PDF's content streams are compressed and could not be
> read, and an attempt to fetch it returned a *general-knowledge* command list
> explicitly flagged as not drawn from the document. Under this corpus's own
> rule that only primary sources support normative claims, that result was
> discarded and the IOS BGP command reference used instead.

## What the source establishes

The route-table view is the bare noun, with everything else a qualifier:

```
show ip bgp [ip-address [mask [longer-prefixes [injected] | shorter-prefixes
  [length] | best-path-reason | bestpath | multipaths | subnets] | ...] | all
  | oer-paths | prefix-list name | pending-prefixes | route-map name
  | version {version-number | recent offset-value}]
```

described as displaying "the contents of the BGP routing table". Alongside it,
`show ip bgp summary` and `show ip bgp neighbors` are separate documented
commands for session-level overview and per-neighbor detail.

The reference page consulted did not carry directly quotable one-line
descriptions for `summary` and `neighbors`; their existence and syntax are
attested, their prose descriptions are not quoted here.

## Transferable

1. **summary / neighbors / routes is the operator's mental model for BGP.**
   It is stable across vendors and decades, and #72 should use those three
   nouns because operators already have them — not because they are elegant.
2. **Detail is a drill-down under the same noun**, reached by adding a key
   (a neighbor address, a prefix), not by a different verb. That supports
   `show appliance X bgp neighbors [<ip>]` over a separate `describe`-style
   verb.
3. **Configuration is a different command entirely** (`show running-config`),
   never a mode of the operational command. Independent corroboration of
   Principle I from the most widely used network CLI in existence.

## Incompatible assumptions

* **The command's subject is always "this device".** Every form above is
  implicitly local. pyecsdwan's subject must be named, and may be a fabric or
  a set of appliances, so the IOS forms cannot be adopted verbatim — they are
  the *tail* of a pyecsdwan command, after scope.
* **Reads are free.** `show ip bgp` against a local RIB costs nothing.
  pyecsdwan's equivalent is a proxied call per appliance, so a fabric-wide
  routes view is a fan-out whose cost the grammar must expose.
* **The flat `show ip bgp ...` option soup.** Twelve mutually-qualifying
  optional tokens in one command is precisely the discoverability problem #70
  is trying to avoid. Adopt the three nouns, not the option surface.

## Decisions

* **D-IOS-1.** `summary`, `neighbors`, `routes` are the BGP operational leaves
  for #72. *(Adopt.)*
* **D-IOS-2.** Instance drill-down is an optional key after the collection
  noun, not a new verb. *(Adopt.)*
* **D-IOS-3.** IOS forms attach *after* scope; they never become top-level.
  *(Adopt as constraint.)*
* **D-IOS-4.** Do not reproduce the flat optional-token surface. Qualifiers
  that change cost class become flags, so they are visible. *(Reject.)*
