# Cisco NSO CLI

**Primary source:** [The NSO CLI (NSO 5.7 guides)](https://developer.cisco.com/docs/nso-guides-5.7/the-nso-cli/)
**Accessed:** 2026-08-27 · **Version:** NSO 5.7

## What the source establishes

NSO is the closest structural analogue to pyecsdwan in this corpus: a
model-driven controller that manages many devices through one northbound
grammar, with transactional commits.

* **Two modes**, distinguished in the prompt: operational (`admin@ncs#`) shows
  live values from devices plus operational data held in the CDB;
  configuration (`admin@ncs(config)#`), entered with `config`, works on
  configuration data.
* **Crossing modes is explicit**: `show running-config` reads config from
  operational mode; `do show running-config` reaches back the other way.
* **Scope ordering is outermost-first**:
  `devices device <DEVICE-NAME> config <device-specific-config>`. Listing is
  the same path truncated — `show devices device`.
* **Transactions**: changes are made to a copy of the active configuration and
  do not take effect until `commit` or `commit confirm`. `commit check`
  validates without applying. `show configuration diff` shows uncommitted
  changes with +/- notation. `rollback configuration [number]` reverts.
* **Native config is namespaced, not separately verbed**: device-native
  configuration lives under `devices device <name> config ios:*`, reached by
  the same path with a vendor prefix. `devices sync-from` populates it.

## Transferable

1. **Scope ordering outermost-first, with listing as a truncation.**
   `devices device NAME config ...` is exactly the shape #71 proposes as
   `show configuration running appliance NAME ...`, and it means a bare prefix
   is a *list*, not an error — which is the behavior #70 asks for at every
   nonterminal.
2. **Commit-confirm as a first-class verb.** pyecsdwan already has this; NSO
   confirms it is the expected shape for a multi-device controller rather than
   a Junos-only idiom.
3. **Diff is a noun under configuration, not a pipe.** `show configuration diff`
   is a legitimate precedent for `show compare` as a first-class form, which is
   one of #71's required decisions.
4. **Native as a namespace inside the model path.** NSO does not give native
   config its own top-level command. It is the same address with a vendor
   prefix. This independently corroborates D-JUN-3 from a model-driven system
   rather than a device CLI.

## Incompatible assumptions

* **NSO owns the devices' configuration.** It syncs from them and treats the
  CDB as authority, so "running config" is a thing it holds. pyecsdwan reads
  through the Orchestrator proxy per request and holds no such mirror; every
  running-config read is a live call with real cost and real failure modes.
  A grammar that implies a local mirror would be lying.
* **Single running datastore, no candidate/running split at the device.** NSO's
  model works because the controller is the only writer. pyecsdwan explicitly
  is not (#63) — the Orchestrator UI, template pushes and other automation
  write concurrently, which is why commit-time drift must fail closed.
* **`do` as a mode-crossing prefix.** Terse and learnable for NSO's audience,
  but it is a second grammar bolted on. pyecsdwan's shell should not grow one.

## Decisions

* **D-NSO-1.** Adopt outermost-first scope ordering, uniformly, for both
  interactive and scriptable forms. *(Adopt; settles #71's "canonical scope
  ordering" decision.)*
* **D-NSO-2.** A bare nonterminal lists valid next tokens rather than erroring
  or guessing. *(Adopt; this is the #70 governing direction and #77/#78's fix.)*
* **D-NSO-3.** `show compare` becomes a first-class form; `show | compare`
  remains as an alias, not a synonym to be deprecated — it is already in
  operators' fingers and costs nothing to keep. *(Adopt.)*
* **D-NSO-4.** No `do`-style mode-crossing prefix. Configuration mode gets the
  operational commands it needs by name, or the operator exits the mode.
  *(Reject.)*
