# Legacy MCP server — quarantined (issue #62)

**This is not part of the pyecsdwan product.** It lives under `contrib/`, is
not packaged into the wheel, and is disabled unless you explicitly turn it on.

## Why it was quarantined

It wraps the **vendored `pyedgeconnect` reference SDK**, not `pyecsdwan`. That
is the whole problem in one sentence: it was never a front end over this
product's safety model, so it shares none of it. It reflectively exposed every
public method of that SDK as an MCP tool — 641 on `Orchestrator` alone, of
which roughly 250 write or destroy — as a second product surface in the same
repository, with:

- no candidate/plan step, no journal, no snapshot, no rollback,
- no template-ownership check, so a template push silently reverts anything
  it writes,
- `verify_ssl=False` by default on both Orchestrator and appliance sessions,
- API keys and passwords accepted as **tool arguments**, which puts them in
  front of the model, into the transcript, and into anything logging the call,
- direct-to-appliance authentication, which #10 explicitly defers until an
  RBAC broker exists,
- and no packaging, dependency declaration, lint, type check, or test — which
  is how all of the above survived as long as it did.

## What changed

| | Before | Now |
|---|---|---|
| Runs by default | yes | **no** — needs `ECSDWAN_MCP_LEGACY_ENABLE=1` |
| Direct-to-appliance tools | ~105 | **removed entirely** (#10) |
| TLS verification | off | **on**; insecure is a separate lab-only opt-in |
| Credentials | tool arguments | environment or OS keyring only |
| Write/destructive tools | exposed | withheld unless `ECSDWAN_MCP_LEGACY_ALLOW_WRITES=1`, and labelled Tier 0 |
| Lint / types / tests | none | `ruff`, `mypy --strict`, and a test module in the normal suite |

Read/write/destructive classification is in `policy.py`, which imports neither
`mcp` nor `pyedgeconnect` so that the security-relevant half of this component
is covered by the ordinary `make check`.

**Classification is not a prefix match**, deliberately. 53 `get_*` methods in
the vendored SDK issue a POST, and this repository has already found endpoints
that mutate behind a read-shaped verb — `GET /oro/debug/closeGrpcConnection`
closes a live gRPC link (see `reports/applianceconfig.py`, and issue #67). A
method counts as a read only when its name looks like one **and** its body
issues nothing but GETs. Anything we cannot read the verbs for is a write.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `ECSDWAN_MCP_LEGACY_ENABLE` | unset | Required. Without it the server refuses to start. |
| `ECSDWAN_MCP_LEGACY_ALLOW_WRITES` | unset | Also expose write/destructive tools. Requires the above too. |
| `ECSDWAN_MCP_LEGACY_INSECURE` | unset | Disable TLS verification. Lab only. |
| `ECSDWAN_ORCH_URL` | — | Orchestrator URL. |
| `ECSDWAN_API_KEY` | — | API key; falls back to the OS keyring (service `pyecsdwan`). |

There is no username/password path. Interactive login would mean accepting a
password as a tool argument.

## Running it

```bash
pip install -e '.[mcp-legacy]'
cd contrib
ECSDWAN_MCP_LEGACY_ENABLE=1 \
ECSDWAN_ORCH_URL=https://orchestrator.example.com \
python3 -m mcp_server_legacy
```

`claude_desktop_config.json` in this directory is a starting point.

## Use `ec-cli` instead

For anything you would want to undo, the CLI is the supported surface: it
plans, journals, snapshots before writing, verifies after, detects
template-owned sections, and can roll back. Tier-0 raw access is available
there too — `ec-cli api get|post|put|delete <path>` — with the difference that
it is audit-journaled and labelled as what it is.

## Still open

Whether this component is **rebuilt** as a curated front end over
`pyecsdwan.Resource`/`txn`, or **archived** as a separate raw-SDK project, is
a product decision that has not been made — see `docs/futures/README.md`.
Rebuilding is not a port: it would mean writing it against a different library
than the one it currently wraps.
