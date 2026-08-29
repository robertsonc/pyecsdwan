# pyecsdwan roadmap — to Orchestrator feature parity

Every operation a user performs in the EdgeConnect Orchestrator UI should be
expressible through this CLI, with transactional safety the Orchestrator API
never had. This page indexes the epics that get us there; the GitHub Issues are
the live tracker.

## Shipped (on `main`)

**Phase 0 — framework.** Resource-plugin contract (Scope / Reversibility /
Tier), candidate config, crash-safe journal, transaction engine with
commit-confirm + detached watchdog, httpx client, resolver cache, action-key
poller, Junos-flavored shell + scriptable subcommands, Tier-0 raw `api`
passthrough, bundled mock Orchestrator. Trivial resource: `interface-labels`.

**Phase 1 — first vertical slice.** `template-group` + `template-association`,
`bio` + `bio-association`, `security-policy`, and template-ownership detection
(`ownership.py`). Plus an adversarial hardening pass (host-scoped recovery,
URL-escape guard, confirm-vs-revert atomicity, default-fill idempotency, …).

**Phase 2 — appliance-scope config, first pass (#3).** Save-changes primitive
(#11); `appliance/deployment` (#12, interfaces/IP/VLANs, validate-then-apply);
`appliance/dhcp` (#13, composes over the deployment object); `appliance/vrrp`
(#14); `appliance/routes` (#15, add/delete-delta `COMPENSABLE` rollback);
`appliance/bgp` and `appliance/ospf` (#16, #17 — read+write, code-complete
against a spec-confirmed appliance-proxy endpoint but not yet live-write-
tested); `appliance/loopback` (#18, read+diff) + `loopback-orch`
(#18, fabric-wide pool, full read-modify-write); `appliance/zones` +
`appliance/security-maps` (#19). Template-ownership detection (#20) now
fails closed: ownership is `owned`/`unowned`/`unknown`, anything unreadable or
unverified refuses the write, and the check is repeated immediately before the
write phase. Seven kinds carry live-confirmed section names; the remaining
sixteen answer `unknown` on a non-match until someone verifies them against a
real template group (`docs/live-validation.md`).

**Phase 3 — orchestrator-scope breadth (#4), 8 of 9 issues.** Orchestrator
firewall zones (#30); ACLs + IP objects + AppExpress groups (#31); NAT maps,
pools and SNAT maps (#32); QoS/optimization/route maps + shapers (#33);
regions, region associations and regional overlays (#35); overlay and
template-group priorities (#36); internal subnets (#37); SNMP, logging,
mgmt-services, banners and schedule-timezone (#38); interface-label advanced
constraints (#39). Service orchestration (#34) is deferred — see the scope
comment on that issue.

**The epic's headline finding: "orchestrator-scope breadth" was a
misnomer.** Most of these surfaces are **GET-only on the Orchestrator API**;
the real write path is the appliance (ECOS) API through the
`/appliance/rest` proxy. So #31's ACLs, all of #32, all five of #33, and
four of #38's five landed appliance-scope, not orchestrator-scope. Several
issue-texts also named paths that exist in neither vendored spec
(`/route_policy`, `/optimization_policy`, `/nat_policy`, `/vrf_snat_maps`,
`/vrf_dnat_maps`, `/acls/{name}`, `/third_party_services`, `/services`) —
they are pyedgeconnect SDK module names, not REST paths. Corrected paths are
recorded in each resource's module docstring.

Every epic-#4 write path is spec-confirmed but **not live-write-tested** —
session access was read-only throughout. See `docs/futures/README.md`
§"Write semantics needing live confirmation" before trusting one. Since #66
this is recorded per resource rather than only in prose: `ec-cli show coverage
--evidence` reports it, and the ledger it reads is validated on load.

**Tier-1 spec pipeline (#6) — complete.** `tools/spec_sync.py` fetches and
diffs the published OpenAPI spec against the `src/pyecsdwan/_specs/`
baseline (#25).
`pyecsdwan/specs.py` is the one read-only view over the two vendored
baselines — 1833 operations, bounded cycle-safe `$ref` resolution, and the
path normalization every source joins on. `tools/gen_models.py` emits
pydantic models + typed bindings per operation (#26); `tools/gen_plugin.py`
wraps those in a Tier-1 `Resource` stub whose `normalize()` raises
`NotCurated` until curated (#27). `show coverage` reports the whole endpoint
universe by tier (#28), and the promotion checklist is machine-enforced by
`make check` rather than advisory (#29).

`tools/postman_sync.py` distils the vendor's published Postman collections
for 9.3–9.6 into `src/pyecsdwan/_specs/payload-examples-9.6.json` (#51). It adds no endpoint
breadth — the 7.2.0 baselines are already a superset — but supplies request
payload shape for 128 of the 169 write operations the specs leave untyped.

Coverage today: **119 of 1833 endpoints curated, 5 generated, 1709 raw-only**
(`ec-cli show coverage`).

**Operational reporting (#54, first tranche of epic #8).** Read-only fabric
reports in `pyecsdwan/reports/`, deliberately outside `resources/` — they have
no normalize/diff/apply/rollback contract and no reversibility class, and
modeling them as resources would imply guarantees they cannot honor.
the fabric config breakdown by section (#55), an appliance's own CLI
running-config through a deny-by-default read-only allowlist (#56),
Orchestrator + per-appliance active/backup/next-boot partitions with skew
detection (#57), the flow-count matrix (#58) and a fabric-wide deduped
address search (#59). All five were respelled by #74's grammar migration —
`show configuration fabric`, `show configuration appliance <name> --format
native`, `show fabric version`, `show fabric flows summary`, `show fabric
flow <ip>` — and the old spellings are gone, not aliased. All bounded by a shared
concurrency-capped, failure-isolating fan-out: one unreachable appliance is a
marked row, never a lost report.

**Production hardening, first tranche (epic #9).** The two P0s from the
audit batch, plus the gate that keeps them true.

*Concurrency (#63).* The transaction guarantees assumed this CLI was the sole
writer. Atomic replacement stops a *torn* candidate, not a *lost* one: two
shells that both loaded at T0 and saved at T1 and T2 left only the second
one's work. `locking.py` adds host-scoped advisory locks — `flock`, so the
kernel drops them on SIGKILL and a stale lock is structurally impossible
rather than merely detected, with an `O_EXCL` + start-token fallback for
platforms without it. Candidate mutation is now a locked *read-modify-write*
cycle: serializing the writes alone does not fix a lost update, and there is a
test pinned to that distinction. Commit, confirm, revert and rollback share
one commit lock; the detached watchdog's revert takes it too but waits far
longer, because a watchdog that gave up would leave an unconfirmed change
applied. Commit-time drift now **fails closed** — the engine used to notice
that server state had moved since compare, recompute the diff and carry on,
folding another operator's change into this one's changeset; `--rebase` opts
in and re-merges intent over current state. `show locks` reports holders.

*One identity per Orchestrator (#63, second half).* Scoping is only as good as
the key it scopes by, and the key was the *display host* — the URL with its
scheme and everything after the first slash thrown away. So
`https://orch/tenant-a` and `https://orch/tenant-b` were one identity, as were
a plaintext and a TLS endpoint on one name; and the file names were that
already-lossy string run through a sanitizer, which mapped `orch:443` and
`orch_443` together too. Distinct targets then shared one candidate store, one
lock, one resolver cache and one rollback history — and the guard against
restoring one Orchestrator's snapshot into another compared display hosts, so
for exactly these targets it compared equal and waved the restore through.
Everything that persists or compares identity now keys by
`Settings.origin`, a canonical `scheme://host[:port][/path]`, with file names
carrying a digest of the *unsanitized* origin. `host` remains, for display
only. Journals record both and are matched on the origin; ones written before
it existed are still matched on their hostname, because losing an operator's
way back is worse than the ambiguity that inherits — and a restore from one
says so in the journal rather than implying the target was verified. The rule
that production never keys state by the lossy field is not a type — both are
`str` — so it is a test over the source with a reasoned allowlist, covering
the test tree as well: a test that stages by the display host while the code
keys by the origin passes while asserting nothing, which is how nineteen of
them stayed green against two different files.


*Fabric-wide drift (#8).* `ec-cli drift` enumerates every instance of every
kind and compares it against staged intent. The rows worth having are the ones
`diff` never had a reason to print: an instance nobody has declared is
**undeclared**, not in sync — reporting it as clean is how an entirely
unmanaged fabric passes a drift check. An instance that could not be read is
**unreadable**, and a Tier-1 stub is **unsupported** (and is never fetched;
there is no canonical form to compare, so the round trip is pure load on a
control plane). Exit 0 / 1 / 8, and **8 outranks 1**: a run that skipped part
of the fabric has not earned the word "clean", so incompleteness is reported
ahead of the drift it did find. Unsaved running-config changes are a note, not
a row — "differs from declared intent" and "differs from what is on flash" are
two axes, and folding them together is the collapsing the whole command exists
to avoid.

`--from <dir>` swaps the intent source for a desired-state directory in git —
`fabric/<noun>/<instance>.yaml` and `appliances/<name>/<noun>/<instance>.yaml`,
keyed on user-facing nouns because a registry kind like `appliance/banners`
contains a path separator and would silently split into two directory levels.
That is the GitOps *read* half, and only that: it reads, it never writes.
Landing it before declarative apply is deliberate — a layout mistake here costs
a rename, and the same mistake underneath a write path costs a fabric. Both
intent sources materialize through one `materialize_desired`, so `drift` can
never report something `commit` would not do.

`ec-cli apply --from <dir>` is the write half, and it needed no second
transaction engine: `candidate.IntentSource` is the one interface both the
candidate store and a desired-state directory implement, so `build_plan` takes
either and everything downstream — ownership, shared write targets,
drift-since-compare, reversibility, the journal — is literally the same code.
`--dry-run` writes nothing and exits 1 if applying would change anything.
A non-empty candidate refuses the apply rather than merging two intents into
one transaction.

*MCP trust boundary (#62).* `mcp_server/` reflectively exposed every public
method of the vendored `pyedgeconnect` SDK — 641 on `Orchestrator`, ~250 of
them writes — with no transaction, TLS verification off, and credentials as
tool arguments. It was never a front end over this product: it wraps the
reference SDK, not `pyecsdwan`. Quarantined to `contrib/mcp_server_legacy/`:
disabled unless explicitly enabled, direct-to-appliance tools removed outright
(#10), TLS on, credentials from environment/keyring only, reads by default and
writes behind a second opt-in labelled Tier 0. Classification is by the verbs
each method actually issues, not its name — 53 `get_*` methods issue POSTs.
Rebuild-over-`txn` versus archive-as-a-separate-project is still open; see
`docs/futures/README.md`.

*CI and packaging (#65).* The gate ran locally and nowhere else.
`.github/workflows/ci.yml` runs ruff + mypy `--strict` + pytest on 3.10/3.11/
3.12 for every push and pull request, then builds an sdist and wheel and
installs it into a clean environment to exercise the CLI from outside the
source tree. That second job exists because of a bug the first cannot see:
the OpenAPI baselines lived at the repository root and never reached the
wheel, so an installed `show coverage` reported an empty endpoint universe —
not an error, a confident wrong answer. They now live in
`src/pyecsdwan/_specs/` and ship as declared package data, with
`include-package-data` turned **off** so what lands in a wheel is a reviewed
declaration rather than a side effect of what happens to be tracked by git.
`make smoke` runs the same wheel check locally.

**CLI information architecture (epic #70).** The `show` root meant four
different things — operational state, normalized configuration, staged
candidate, native vendor text — and the command did not say which. Designed
first: a ratified constitution and brownfield Spec Kit workflow (#75,
`.specify/`), a primary-source design corpus resolving nine dimensions across
Junos, Cisco NSO, gNMI, IOS, NVUE, kubectl and EdgeConnect's own constraints
(#73, `docs/design-corpus/cli/`), and the versioned grammar (#71,
`specs/001-cli-command-taxonomy/`). Then implemented against it:

*One grammar, three intents.* `show <cli-state>`, `show <scope> <domain>` and
`show configuration [running|candidate] ...` — no token sequence resolves to
two, which makes Principle I structural rather than documented. Nonterminals
list their continuations and exit 0 (#74). The datastore token is optional and
means `running`; `candidate` is never implicit, so the only unnamed datastore
is the live one.

*One resolution path.* `registry.scoped_instances()` is the single
implementation every surface uses to discover instances (#76), user-facing
nouns replace registry keys on both surfaces with the keys withdrawn entirely
(#77), and instance names complete (#49). `tests/test_grammar_parity.py` runs
each worked example through both parsers and requires the same answer, because
nothing structural keeps a shell function and a Typer registry in step.

*Operational state, honestly.* `show appliance <name> bgp summary|neighbors`
from one `/bgp/state` call; `routes` reports `unsupported` because no BGP
route-table endpoint exists in either baseline, and stays listed rather than
hidden (#72). A peer configured but absent from state is shown as exactly
that, never inferred established.

*Terminal states are distinguishable.* All eleven outcomes of `grammar.md` §5
with their exit codes, classified at both dispatch boundaries — `denied` is
not `unreachable`, `unsupported` is not `error`, `empty` is not `not_found`
(#78). The table is asserted against the spec by parsing it, so the two cannot
drift. `show commands` renders the whole surface offline, generated from the
parser's own tables and round-tripped through the dispatcher.

Outstanding: whether the shipped `--json` boolean becomes the grammar's
`--format {yaml,json,native}`. It is a flag break across a dozen commands and
the migration table covers command forms rather than flags, so it is an owner
decision rather than an implementation gap.

`make check` gate: ruff + mypy `--strict` + tests, all green — now enforced by
CI on every push and pull request, not only locally.

## Coverage model (why parity is incremental but safe from day one)

| Tier | What | In transactions? |
|---|---|---|
| **0** raw | `ec-cli api get\|post\|put\|delete <path>` — any endpoint, today | never; audit-journaled, no rollback |
| **1** generated | spec-ingested pydantic models + plugin stub | never — `normalize()` raises `NotCurated`, so a stub cannot be planned (#68) |
| **2** curated | real `normalize()`, true reversibility class, ownership detection | full commit-confirm |

So *anything and everything* is reachable now via Tier 0; curated plugins
(Tier 2) grow coverage against a stable contract, and the Tier-1 pipeline
(Epic below) generates stubs for the long tail.

Tier 1 is **developer scaffolding, not operator coverage**: a stub is where
curation starts, not something an operator can commit through. This table said
otherwise until #68, contradicting both the code and
`docs/plugin-promotion.md`; `tests/test_tier_claims.py` now derives the claim
from the registry so it cannot drift again.

## Epics

| Epic | Scope | Issues |
|---|---|---|
| **Phase 2 — appliance-scope config** (#3) | interfaces/IP, DHCP, VRRP, routes, BGP, OSPF, loopbacks, zones — via the appliance proxy + save-changes + ownership detection | #11–#20 |
| **Phase 3 — orchestrator-scope breadth** (#4) | zones, ACLs/IP objects/app-defs, NAT, route/opt/QoS policy, service orchestration, regions, priorities, internal subnets, common settings | #30–#39 |
| **Async job handling** (#5) | no silent success on keyless pushes; per-appliance fan-out; preconfig channel; cancellation | #21–#24 |
| **Tier-1 spec pipeline** (#6) ✅ | `tools/spec_sync.py`, model/binding/stub codegen, `show coverage`, promotion gating | #25–#29 |
| **Fleet lifecycle** (#7) | discovery/approval, decommission cascade, preconfig, backup/restore, upgrades, licensing — IRREVERSIBLE class | in-epic checklist |
| **Fabric ops & observability** (#8) | `drift` (✅ shipped), declarative bulk apply, JSON Schema, dashboard-parity views | #54 (✅ shipped) + in-epic checklist |
| **Production hardening** (#9) | concurrency, MCP trust boundary, CI/packaging, async-job fail-closed, evidence ladder, retry policy | #62–#68 (#62–#67 ✅ shipped; #68 open) |
| **CLI information architecture** (#70) ✅ | intent-separated command taxonomy, spec-driven design; constitution + design corpus + grammar, then the migration itself | #49, #71–#78 (all shipped; one flag decision open) |
| **(v2) RBAC broker** (#10) | direct-to-appliance access, gated — explicitly out of v1 scope | in-epic checklist |
| **(future) More than one Orchestrator** (#121) | named-target registry, a top-level selection noun, per-target fan-out with no cross-target atomicity — see the section below | in-epic checklist |

The near-term epics (#3, #4, #5, #6) are sharded into child sub-issues; #5 and
#6 are complete. #8's first tranche shipped as #54 (operational `show`
commands, sub-issues #55–#59); the rest of #8 stays an in-epic checklist.
the operational/v2 epics (#7–#10) carry their breakdown as checklists and get
sharded into issues when their phase starts.

## Planned — operating across more than one Orchestrator (#121)

Unstarted. It is on this page because #63 changed what it would cost, and
because the shape of the command surface has to be decided before any of it is
built.

**What #63 already settled.** Every piece of persisted state is keyed by a
canonical origin — candidate store, advisory locks, journals and rollback
history, resolver cache, keyring entry — and a candidate or a snapshot carries
the origin it was staged against and is refused against any other. Two
Orchestrators already cannot share state, and a snapshot from one cannot be
restored into another. So this is no longer a data-model problem. What is
missing is a way to *name* a target and a way to *choose* one.

**A name.** A target is identified today only by its URL, from
`ECSDWAN_ORCH_URL`. Operators need short names — `prod-emea`, `lab` — so a
registry maps name to origin. It must *reference* credentials, never hold them:
the keyring is already keyed per origin (#63), and "credentials never live in
argv or in files" is the existing rule, not a new one.

**The noun is already taken.** `fabric` is a ratified scope noun in
`specs/001-cli-command-taxonomy/grammar.md` — "every appliance, bounded
fan-out" — sitting beside `appliance <name>` and the bare no-scope form, which
means *the Orchestrator itself*. `show fabric version` asks every appliance; it
does not name a fabric. Spelling the new selector `--fabric` would give one
word two meanings one space apart (`ec-cli --fabric prod show fabric version`),
which is the operator surprise Principle VI exists to prevent. The precise noun
for the new concept is the one the grammar already uses for that subject:
`orchestrator`. Whichever wins, it is a grammar change and belongs in a spec
amendment under #75's workflow, not in a flag someone adds.

**Ambient selection is the failure mode.** The registry is the easy half. The
dangerous half is a *sticky* selection: `kubectl config use-context` and
`AWS_PROFILE` are the canonical demonstrations, where a persisted current
target means a command typed in one terminal acts on whatever was last selected
in another, and the operator's model and the tool's disagree at precisely the
moment a write goes out. Junos has no analogue, because a Junos session *is*
the device — the design corpus (#73) has no precedent to copy here, which is
itself the finding. So: the shell may carry a **session-scoped** selection,
because it shows it — the banner already prints the origin and the prompt can
carry the name, and a selection you can see is not ambient. The scriptable CLI
must **not infer one for anything that writes**: `commit`, `apply` and
`rollback` with no explicit target refuse rather than pick. Failing closed
costs one flag; guessing costs a change on the wrong fabric. No global mutable
"current target" file shared by every terminal on the host.

**Fan-out across targets.** The capability worth having is `drift` across every
target, and one declaration applied to several. Three constraints, in the order
they bite. There is **no cross-target atomicity and there will not be**: each
target's commit is its own transaction with its own journal and confirm window,
so partial success across targets is the *normal* outcome and the report has to
make per-target state legible rather than collapse it into one exit code —
promising two-phase commit across independent Orchestrators would be a claim
the API cannot support. Fan-out is **explicit or it does not happen**, never
inferred from a selection; Decision 7's cost-class treatment of appliance
fan-out (confirm, then warn) is the precedent. And the confirm window is **per
target and runs on wall-clock**, so arming ten from one command means ten
independent watchdogs — stated before the first write, not discovered at the
first revert.

**Ordering.** After spec 003's T8 (per-resource safe materialization), because
multi-target apply is only meaningful once single-target apply writes at all;
alongside #106, which owns the credential half.

## Parity map (Orchestrator UI area → code status)

**What ✅ means here, precisely (#66).** It means the code shipped and is green
against the bundled mock Orchestrator — `mock-verified` on the evidence ladder.
It does **not** mean anyone has run a write against real gear. Today **no
resource in this tool has live change-and-rollback evidence**: not one write
path has been executed and rolled back on a fabric at a recorded version.

That is not a caveat on a few rows, it is the state of all 41 of them, which is
why it is stated once here rather than repeated in every cell — a per-row claim
maintained by hand is a claim that drifts. The machine-readable answer is
`src/pyecsdwan/_evidence/ledger.json`, checked on load and by
`tests/test_evidence.py`, and readable offline:

```
ec-cli show coverage --evidence
```

`docs/live-validation.md` is the protocol for producing evidence that counts.
Until a row's resource reaches `live-change-and-rollback-verified`, treat its
write path as code that has never met a fabric.

| Orchestrator UI area | Code status |
|---|---|
| Templates & template groups (content + association) | ✅ shipped (Phase 1) |
| Business Intent Overlays (config + appliance association) | ✅ shipped (Phase 1) |
| Firewall / security policy (orchestrator scope) | ✅ shipped (Phase 1) |
| Interface labels | ✅ shipped (Phase 0; advanced constraints #39) |
| Appliance interfaces / IP / DHCP / VRRP / routes | ✅ shipped (Phase 2, #12–#15) |
| BGP / OSPF (read+diff → write) | ✅ shipped (#16, #17); write path is code-complete against a spec-confirmed endpoint but not yet live-write-tested (see docs/futures/README.md) |
| Loopbacks / loopback orchestration | ✅ shipped (Phase 2, #18; per-appliance loopback is read+diff only, no write endpoint documented) |
| Firewall zones (orchestrator scope + segment↔zone map) | ✅ shipped (Phase 3, #30) |
| Firewall zones + security policy (appliance scope) | ✅ shipped (Phase 2, #19) |
| ACLs / IP objects / app-defs | ✅ shipped (#31; ACLs are appliance-scope — orchestrator `/acls` is GET-only) |
| NAT policy / SNAT / DNAT maps | ✅ shipped (#32; appliance-scope. D-NAT has no write endpoint anywhere — read-only view) |
| Route / optimization / QoS policy + shapers | ✅ shipped (#33; all five appliance-scope — orchestrator exposes them GET-only) |
| Service orchestration associations | ⛔ deferred (#34 — ~100 endpoints across 8+ vendor integrations; an epic, not an issue. See the scope comment on the issue.) |
| Regions / regional overlays / priorities | ✅ shipped (#35, #36) |
| Internal subnets | ✅ shipped (#37; subnet-sharing options not modelable — write-only endpoint, no read path) |
| Common settings (SNMP/logging/mgmt-services/banners/timezone) | ✅ shipped (#38; appliance-scope. DNS proxy/cache, `logging/remote`, NTP deferred) |
| Appliance lifecycle (discovery/upgrade/backup/preconfig) | ⚙️ Epic #7 |
| Fabric drift / declarative apply / dashboards | ⚙️ Epic #8 |
| Operational reporting (`show configuration fabric` / `show fabric version` / `flows`) | ✅ shipped (#54: #55–#59), respelled by #74. `show fabric flow <ip>`'s server-side matching rests on `ipEitherFlag`, whose spec description contradicts its name — unverified live |
| Any endpoint not yet curated | 🟢 reachable today via Tier-0 `ec-cli api` |

Legend: ✅ done · 🔷 Phase 2 · 🔶 Phase 3 · ⚙️ operational epic · 🟢 Tier-0 now.

`ec-cli show coverage --endpoints` now reads the vendored baseline directly
(#28), so this table is a narrative summary — the command is the source of
truth for what is covered at which tier.
