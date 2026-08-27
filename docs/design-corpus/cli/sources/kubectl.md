# kubectl

**Primary source:** [kubectl command reference](https://kubernetes.io/docs/reference/kubectl/generated/)
**Accessed:** 2026-08-27

## What the source establishes

Distinct verbs for distinct intents, rather than one overloaded `show`:

* `kubectl get` — "Display one or many resources".
* `kubectl describe` — "Show details of a specific resource or group of resources".
* `kubectl diff` — "Show differences between current and desired configuration".
* `kubectl explain` — "Explain resources (view fields and their descriptions)".

Objects are addressed as `TYPE NAME` or `TYPE/NAME`, with namespace scope as a
flag (`-n/--namespace`). Output format is selected with `-o/--output`, with
structured formats available.

## Transferable

1. **`diff` as a top-level verb for current-versus-desired.** kubectl treats
   the comparison as a first-class operation rather than a rendering of
   something else. Corroborates D-NSO-3.
2. **`explain` as the discoverability primitive.** A dedicated way to ask "what
   fields exist here?" is exactly what #70 wants at every nonterminal, and what
   #77 needs so operators stop having to know `appliance/nat-maps`. The lesson
   is that discoverability deserves a *designed* surface, not just tab
   completion.
3. **Format as a flag, orthogonal to the noun.** Same conclusion as Junos's
   `| display`, reached in a different tradition — which is the strongest form
   of corroboration available in this corpus.
4. **List versus detail as separate verbs** is a real alternative to the
   optional-key drill-down of D-IOS-2, and is recorded as the road not taken.

## Incompatible assumptions

* **`TYPE/NAME` slash addressing.** Superficially it resembles pyecsdwan's
  `appliance/nat-maps` — but kubectl's slash separates *type from instance*
  (`pod/my-pod`), whereas pyecsdwan's separates *scope from kind*, duplicating
  scope the command already established. The resemblance is misleading and is
  the trap #77 describes; it should not be cited as precedent for keeping the
  current form.
* **A uniform, cheap, indexed API.** kubectl talks to one API server with
  consistent list semantics. pyecsdwan spans an Orchestrator plus per-appliance
  proxied calls of wildly varying cost and reliability. `get` being uniformly
  cheap is why kubectl needs no cost vocabulary; pyecsdwan does.
* **Verb-first grammar.** `kubectl get pods` is verb-first; the network-CLI
  tradition this project's operators come from is `show`-then-scope. Adopting
  kubectl's verb set wholesale would fight the muscle memory the Junos
  flavoring was chosen to serve.

## Decisions

* **D-K8S-1.** Keep `show` as the operational/config read verb rather than
  adopting `get`/`describe`. The audience is network operators. *(Reject
  verb-first.)*
* **D-K8S-2.** Adopt drill-down by optional key (D-IOS-2) rather than a
  separate `describe` verb — one fewer verb, and it matches the network-CLI
  tradition. *(Reject `describe`, with the alternative recorded.)*
* **D-K8S-3.** Discoverability gets a designed surface: a bare nonterminal
  lists valid next tokens, and there is an offline command reference view
  (#77's "domain, scope, instances, mutability, support status"). This is
  `explain`'s lesson without `explain`'s verb. *(Adopt.)*
* **D-K8S-4.** `--format` is the output selector, matching `-o`'s
  orthogonality. Native vendor text is one of its values. *(Adopt; agrees
  with D-JUN-3.)*
