# OpenConfig / gNMI

**Primary source:** [gNMI specification](https://openconfig.net/docs/gnmi/gnmi-specification/)
**Accessed:** 2026-08-27

## What the source establishes

gNMI draws the intent separation at the **schema** level, where it is not a
CLI convention but a typed filter on a request. `GetRequest.type` selects:

* **CONFIG** — "data that the target considers to be read/write" (YANG
  `config true`).
* **STATE** — "the read-only data on the target" (YANG `config false`).
* **OPERATIONAL** — "read-only data on the target that is related to software
  processes operating on the device, or external interactions of the device."
* Unspecified — the target returns CONFIG, STATE and OPERATIONAL together.

Paths are an ordered list of `PathElem` messages, each a node name plus
optional key/value pairs for list indexing (`/a/e[key=k1]/f/g`), rather than
opaque strings. The root is a zero-length element array.

Errors are canonical and distinguish *kinds* of absence:

* **NOT_FOUND** — the path is syntactically correct but does not exist and has
  no YANG default.
* **UNIMPLEMENTED** — the path is valid but not implemented by this server.
* **INVALID_ARGUMENT** — the path is syntactically incorrect.

The target emits one `Notification` per requested path and must never collapse
several paths into one response.

## Transferable

1. **The three-way split is the intellectual basis for #70.** CONFIG / STATE /
   OPERATIONAL is precisely the distinction the current `show` root collapses.
   That a wire protocol found it necessary to type this — rather than leaving
   it to naming — is the strongest argument that pyecsdwan's grammar should
   make it explicit rather than implicit.
2. **The error taxonomy is directly reusable.** "Syntactically wrong" /
   "valid but absent" / "valid but this target cannot do it" is exactly the
   distinction #78 says is missing, and #71 needs for its exit-code table.
   Notably these are three *different* answers where pyecsdwan currently has
   one silence.
3. **Structured paths with keyed list elements.** An instance is addressed by
   key within its collection, not by a flattened string — which is the
   underlying reason `appliance/nat-maps` reads as leakage (#77): it is a
   flattened path fragment surfacing where a scoped noun belongs.
4. **Never collapse distinct requests into one response.** A fan-out over
   appliances must keep per-appliance outcomes distinguishable — which
   `reports/fanout.py` already does, and this is the principled statement of
   why.

## Incompatible assumptions

* **A schema exists and is authoritative.** gNMI presumes YANG. pyecsdwan has
  OpenAPI baselines of varying fidelity and 1709 endpoints still raw-only, so
  "is this leaf config or state?" is frequently not answerable from a model.
  The taxonomy must let a command be honest about an unclassifiable surface
  rather than forcing it into CONFIG or STATE.
* **STATE vs OPERATIONAL is finer than this CLI needs.** The gNMI distinction —
  read-only data generally, versus read-only data about running software —
  serves subscription semantics. Collapsing both into one operational intent
  is the right call for an operator CLI, and is recorded here as a deliberate
  simplification rather than an oversight.

## Decisions

* **D-GNMI-1.** Adopt a three-way intent split, collapsing STATE and
  OPERATIONAL into a single *operational* intent, and keeping *candidate* as a
  fourth that gNMI has no equivalent for. *(Adopt with stated simplification.)*
* **D-GNMI-2.** Adopt the NOT_FOUND / UNIMPLEMENTED / INVALID_ARGUMENT
  distinction as the basis for #71's outcome table, extended with the
  distributed cases gNMI does not cover: unreachable, timeout, permission
  denied, partial, stale. *(Adopt and extend.)*
* **D-GNMI-3.** Do not adopt gNMI path syntax at the CLI. `/a/e[key=k1]/f`
  is a machine grammar; the CLI's equivalent is scoped tokens. *(Reject the
  syntax, keep the addressing model.)*
