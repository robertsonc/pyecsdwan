# pyecsdwan

A transactional CLI abstraction layer for HPE Aruba EdgeConnect SD-WAN.
Everything you do in the Orchestrator UI — orchestrator-level (Business Intent
Overlays, security policy, templates and template groups, associations) and
appliance-level (interfaces, BGP, OSPF, DHCP, VRRP, routes) — expressible from
a Junos-flavored CLI, with the transactional semantics the Orchestrator API
never had:

- **Candidate config**: `set`/`delete` accumulate locally; nothing touches the
  Orchestrator until `commit`.
- **`show | compare`**: a canonical diff of exactly what commit will send.
- **`commit confirm <minutes>`**: auto-rollback by a detached watchdog that
  survives SSH death, unless you confirm in time.
- **`rollback <n>`**: Junos-style history from crash-safe journaled snapshots.
- **Template-ownership detection, fail-closed**: direct appliance changes on
  template-managed sections are refused without `--override-template`, because
  the next template push would silently revert them — and so are changes whose
  ownership could not be *established* (an unreadable template selection, a
  section name nobody has confirmed against a live Orchestrator). Ownership is
  `owned` / `unowned` / `unknown`, and only `unowned` writes freely; it is
  re-read immediately before the write, so associating a template group between
  `compare` and `commit` cannot slip past the guard.
- **Tier-0 raw passthrough**: `ec-cli api get|post|put|delete <path>` reaches
  *any* Orchestrator or appliance-proxy endpoint from day one — journaled for
  audit, loudly outside the transaction guarantees.

The repo also vendors the upstream
[pyedgeconnect SDK](docs/pyedgeconnect-README.md) as the endpoint reference the
plugins are built from, and OpenAPI 7.2.0 specs under `src/pyecsdwan/_specs/` as the
spec-ingestion baseline.

## Install

Target: a Linux server you SSH into. No sudo needed.

```bash
git clone https://github.com/robertsonc/pyecsdwan
cd pyecsdwan
make install          # creates .venv, installs, symlinks ./ec-cli
./ec-cli --help
# or as a uv tool:
uv tool install .     # installs `ec-cli` and the `sase-cli` alias
```

## Connect

```bash
export ECSDWAN_ORCH_URL=orchestrator.example.com
export ECSDWAN_API_KEY=<api key>        # or store it in the OS keyring
./ec-cli show appliances
```

Credentials are never taken on argv. TLS verification is on by default
(`--insecure` exists, and nags). `commit confirm` requires API-key auth — a
background watchdog cannot replay an interactive login.

No Orchestrator handy? `python -m pyecsdwan.mock --port 8442` starts the
bundled fake Orchestrator; then `ec-cli --mock 8442 shell`.

## Junos-mode cheat sheet

```
$ ./ec-cli                       # or: ec-cli shell
pyecsdwan> show appliances
pyecsdwan> show journal
pyecsdwan> configure
pyecsdwan(config)# set interface-labels global wan 3 name LTE
pyecsdwan(config)# set interface-labels global wan 3 topology 2
pyecsdwan(config)# show | compare        # colorized +/- canonical diff
pyecsdwan(config)# commit confirm 10     # auto-reverts in 10 min unless...
pyecsdwan(config)# commit                # ...confirmed inside the window
pyecsdwan(config)# rollback 1            # restore previous confirmed state
pyecsdwan(config)# discard               # drop candidate changes
pyecsdwan(config)# exit
pyecsdwan> exit
```

Operational mode splits by *intent* (#70), and no token sequence resolves to
two:

```
pyecsdwan> show appliances | journal | locks | coverage | commands
pyecsdwan> show configuration [running|candidate] ...   # configuration
pyecsdwan> show appliance <name> bgp summary            # live protocol state
pyecsdwan> show fabric version                          # live, every appliance
```

`show commands` lists the whole surface — intent, scope, mutability and
support status — and needs no Orchestrator connection at all.

## Scriptable subcommands (automation / CI)

```bash
ec-cli set interface-labels global wan 3 name LTE
ec-cli diff                     # exit 1 if changes pending -> CI drift check
ec-cli drift                    # every instance, not just staged ones; 0/1/8
ec-cli commit --confirm-minutes 10
ec-cli commit                   # confirm within the window
ec-cli rollback 1
ec-cli rollback --pending       # recover orphaned unconfirmed transactions
ec-cli load interface-labels global labels.yaml   # declarative desired state
ec-cli api get /appliance       # Tier-0 raw passthrough (audit-journaled)
ec-cli api post /gms/interfaceLabels --body labels.json
ec-cli api get /systemInfo --appliance BR1-EC     # via appliance proxy
ec-cli show commands            # every command: intent / scope / support (offline)
ec-cli show coverage            # every kind: scope / reversibility / tier
ec-cli show coverage --endpoints --tier 2         # every spec endpoint x tier
ec-cli plugin promote bgp --appliance BR1-EC      # run the Tier-2 checklist
```

Reads, split by intent — configuration and operational state are different
commands over different sources, and the exit code says which terminal state
was reached (`grammar.md` §5):

```bash
ec-cli show configuration appliance BR1-EC banners     # normalized config
ec-cli show configuration appliance BR1-EC --format native   # vendor text
ec-cli show configuration fabric security              # fabric config, by section
ec-cli show appliance BR1-EC bgp neighbors --json      # live protocol state
ec-cli show fabric version                             # live, every appliance
```

`drift` compares against the local candidate by default. For a CI check, point
it at a desired-state directory in git instead — reviewed, versioned, the same
answer on every run:

```text
desired/
  fabric/
    interface-labels/global.yaml          # fabric/<noun>/<instance>.yaml
    bio/CorpFabric.yaml
  appliances/
    BR1-EC/
      banners/global.yaml                 # appliances/<name>/<noun>/<instance>.yaml
      bgp/config.yaml
```

Then `ec-cli drift --from desired/` to see what differs, and
`ec-cli apply --from desired/` to commit it as one transaction —
`--dry-run` writes nothing and exits 1 if it would change anything, which is
the CI gate. Apply is the same planner, guards and journal as `commit`; only
the intent comes from git. It refuses if you have staged work in the candidate,
because one transaction carrying both intents would commit changes the
directory never declared.

The directories are the same user-facing
nouns the commands take — never registry kinds, which is not cosmetic: the kind
behind a per-appliance banner is `appliance/banners`, and a path separator in a
directory name would silently become two levels. Each file *is* that instance's
desired state, so a value the appliance holds that no file declares is drift.
It reads and never writes; declarative apply is separate work.

## Tier-1 spec pipeline (`tools/`)

```bash
python tools/spec_sync.py --diff                  # spec drift vs specs/
python tools/postman_sync.py --diff               # vendor payload examples
python tools/gen_models.py  --scope appliance --method POST --path /bgp/config/system
python tools/gen_plugin.py  --scope appliance --method POST --path /bgp/config/system
```

`gen_models` emits pydantic models + a typed client binding for one spec
operation; `gen_plugin` wraps those in a Tier-1 `Resource` stub whose
`normalize()` raises `NotCurated` until a human curates it. See
`docs/plugin-promotion.md`.

## Safety model, in one table

| Class | Meaning | commit confirm |
|---|---|---|
| REVERSIBLE | exact snapshot/restore | yes |
| COMPENSABLE | compensating action (create→delete) | yes |
| IRREVERSIBLE | no undo (deletes, upgrades, licenses) | **refused** — needs `--force`, no fake safety |

| Tier | Meaning | In transactions? |
|---|---|---|
| 0 | raw `api` passthrough | never — audit journal only |
| 1 | generated from spec | never — `normalize()` raises `NotCurated`, so a stub cannot be planned |
| 2 | curated plugin | full commit-confirm |

**Tier 1 is developer scaffolding, not operator coverage (#68).** This table
used to say "plain commit; confirm only with `--allow-untransactional`", which
was wrong in the direction that matters: `txn.build_plan()` calls `normalize()`
on every candidate, and a generated stub's raises — so a Tier-1 kind is stopped
before a plan exists, and never reaches the `--allow-untransactional` guard at
all. A stub is the *starting point for curation*, wired far enough that
finishing it is a small edit against working code. It is not something to point
at a fabric today.

That is deliberate, and the alternative was considered: if best-effort writes
are ever wanted they get their own explicit surface, rather than being bought
by weakening the normalization contract every curated resource depends on.

Partial failure mid-changeset auto-reverts the already-applied steps from the
journal and reports exactly what state the fabric is in. Orphaned unconfirmed
transactions (CLI or host died) are detected on every start; recover with
`rollback --pending`.

State lives under `~/.pyecsdwan/` (journal doubles as the audit log), and the
journal comes back out as one:

```bash
ec-cli show journal                     # transactions, newest first
ec-cli show journal --json              # the same, machine-readable
ec-cli show journal --events            # every event as NDJSON -> a SIEM
```

Add `--txn <id>` to any of those to select one transaction; an id that is not
in the journal is an error rather than an empty export, because a pipeline
reading zero lines would record that nothing happened.

`--events` is one self-contained record per line — stamped with its transaction
and Orchestrator, oldest first, so a line still means something after a log
shipper has torn it out of its file. Snapshot bodies are **redacted by
default**: the journal is `0600` precisely because a snapshot holds a whole
appliance object, and exporting is distribution. What survives redaction is a
SHA-256 of the canonical body and its size, so an auditor can still prove two
exports describe the same state — or that a restore matched what was captured —
without ever seeing the config. `--include-snapshots` opts in.

A journal directory too corrupt to open is named rather than skipped, on every
one of these surfaces (and on stderr for `--events`, where a warning in the
stream would break the consumer). An audit trail that quietly described a
smaller history than the one on disk would be worse than none.

## Development

```bash
make check     # ruff + mypy + pytest — the local gate
pytest -m "not slow"   # skip the detached-watchdog e2e tests
```

Repo conventions: `docs/sitrep/` session handoffs, `docs/futures/` roadmap,
`docs/research/` mined API knowledge, `docs/plugin-promotion.md` for how a
resource earns commit-confirm.
