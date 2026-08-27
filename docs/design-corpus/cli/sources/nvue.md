# NVIDIA NVUE

**Primary source:** [NVUE show commands — system config](https://docs.nvidia.com/networking-ethernet-software/nvue-reference/Show-Commands/System-Config/)
**Accessed:** 2026-08-27 · **Version:** NVUE reference (Cumulus Linux 5.9+ behavior noted)

## What the source establishes

NVUE distinguishes configuration states **within one view**, as columns rather
than as separate commands:

```
cumulus@switch:~$ nv show system config
             operational  applied
-----------  -----------  ---------
             apply        apply
```

`operational` is the current running state; `applied` is the last successfully
committed configuration. There is a third state — the startup configuration in
`/etc/nvue.d/startup.yaml` — reached through `nv config save`, and `nv config
apply` promotes pending to applied without necessarily updating startup unless
auto-save is on.

No `pending` or revision keyword appears in these show commands; the states are
differentiated by column comparison only.

## Transferable

1. **Naming the third state.** applied / startup / operational is a real
   distinction that Junos and NSO's two-state framing hides. pyecsdwan has an
   exact analogue in appliance `saveChanges` semantics: a write can be applied
   and not persisted to flash, which is #64's open concern about `save_changes()`
   returning SUCCESS when no action key comes back. The corpus records the
   vocabulary even though the display technique is rejected below.
2. **Config-vs-state adjacency is genuinely useful to operators** — the
   question "is what is running what I configured?" is common enough that
   NVUE made it the default view. pyecsdwan's answer to that question is
   `drift` (epic #8), and it should be a named command rather than a column.

## Incompatible assumptions — and the rejected pattern

* **Columns conflate intents in one view.** This is the one corpus source whose
  central technique pyecsdwan should *not* adopt. Principle I requires that a
  command name a single intent; a view whose columns are operational and
  applied is exactly the ambiguity #70 was filed about, merely rendered
  side-by-side instead of hidden behind different verbs. It reads well on a
  single switch and would be actively misleading across a fabric, where each
  column would need its own per-appliance reachability and freshness state.
* **Implicit "no news is good news".** Column comparison makes *equality* the
  visible default and difference something the reader must spot. Principle II
  points the other way: the interesting state should be the loud one.

## Decisions

* **D-NVUE-1.** Adopt the applied / startup / operational vocabulary where the
  EdgeConnect data model actually distinguishes them — specifically, an applied
  change that is not yet persisted must be sayable. *(Adopt vocabulary; feeds
  #64.)*
* **D-NVUE-2.** Reject side-by-side config/state columns as a default view.
  Difference is a first-class command (`compare`, and epic #8's `drift`), not a
  column the reader has to diff by eye. *(Reject, with reason recorded.)*
