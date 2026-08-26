# tools

Phase-3 home of the Tier-1 spec-ingestion pipeline against `specs/`.
See docs/plugin-promotion.md and docs/futures/README.md.

## spec_sync.py — fetch + diff/refresh the OpenAPI baselines (#25)

```
python tools/spec_sync.py --diff                       # both specs, sources from env
python tools/spec_sync.py --diff --spec orchestrator --source https://.../apiDocs.json
python tools/spec_sync.py --update --spec appliance --source ./apiDocs.json
```

Sources are never guessed: pass `--source` (URL or local file) or set
`ECSDWAN_SPEC_SOURCE_ORCHESTRATOR` / `ECSDWAN_SPEC_SOURCE_APPLIANCE`;
unconfigured targets are skipped. URL fetches honor `ECSDWAN_API_KEY`
(sent as `X-Auth-Token`) and `ECSDWAN_INSECURE=1` / `--insecure`.

`--diff` reports added / removed / changed endpoints (path + method,
fingerprinting the operation object plus shared path parameters), component
schema drift, and metadata changes; exit 1 on drift, 0 in sync, 2 on error.
`--update` rewrites the baseline in the existing convention: compact
single-line JSON with deployment hostnames in `servers`/`basePath`/`host`
sanitized to the `*.example.com` placeholders. Diffs sanitize both sides in
memory first, so a real hostname never leaks and never counts as drift.

Model/stub codegen from a detected endpoint is later work (#26/#27), as are
`show coverage` (#28) and promotion gating (#29).
