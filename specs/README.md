# specs/

Vendored, offline API knowledge. Nothing here is fetched at runtime —
`pyecsdwan.specs` is the only reader, and it degrades to an empty universe
when a file is missing.

| file | what it is | regenerate with |
|---|---|---|
| `orchestrator-openapi-7.2.0.json` | Orchestrator OpenAPI/Swagger baseline, hostnames sanitized to `orchestrator.example.com` | `tools/spec_sync.py --update --spec orchestrator --source URL_OR_FILE` |
| `appliance-openapi-7.2.0.json` | Appliance (ECOS) baseline, same sanitization | `tools/spec_sync.py --update --spec appliance --source URL_OR_FILE` |
| `payload-examples-9.6.json` | request/response **shape** examples distilled from the vendor's public Postman collections for releases 9.3–9.6 | `tools/postman_sync.py --update` |

Both tools take `--diff` (exit 1 on drift, 0 in sync, 2 on error) so a refresh
is always a reviewed change, never a silent one.

## The placeholder caveat — read this before trusting an example

The Postman examples are **schema skeletons, not captures**. The vendor fills
every scalar with `"string"`, `0` or `true`:

```json
GET {{applianceBaseUrl}}/lte/config
{"cell0": {"admin": "string", "apn": "string", "generation": "string"}}
```

They document field names and nesting, and nothing else. No test or default
may treat one as a real value.

## Why the Postman collections were incorporated

Issue #51 framed them as broadening endpoint coverage. **That premise is
wrong** and the artifact is not built toward it. Measured against these
baselines, with every path-parameter dialect normalized:

| | count |
|---|---|
| operations in the two baselines | 1833 |
| endpoints in the 9.6 collections | 1776 |
| in the collections but **not** in the baselines | **1** (`GET /stats/infrastructure/reportLoaderUrl`) |
| in the baselines but not in the collections | 58 |

The baselines are already a superset. What the collections genuinely add is
**payload shape**, and one half of that is far stronger than the other:

* **Request bodies — the real win.** 169 baseline write operations
  (POST/PUT/PATCH) carry no typed request body at all. The collections supply a
  request example for **128** of them (76%).
* **Response bodies — thin.** 667 baseline operations carry no typed success
  response. The collections supply an example for only **21** of them (3%);
  their 1162 response examples land overwhelmingly on GETs that the baselines
  already type, where they serve as a cross-check rather than as new
  information.
* **Provenance.** `since` records the oldest of 9.3/9.4/9.5/9.6 carrying an
  endpoint (1544 date to 9.3, 136 first appear in 9.6); 18 endpoints present in
  an older release are gone from 9.6 and are kept with `removed_after` set.
* **Taxonomy.** Every entry carries the vendor's two-level folder path, e.g.
  `["Orchestrator Level", "alarm"]`.

The raw collections are ~5 MB each and are **not** vendored; only the ~1 MB
distilled artifact is.

## Artifact shape

Keys come from `pyecsdwan.specs.endpoint_key(scope, method, path)`, so an entry
joins directly onto a spec operation:

```json
{
  "meta": {"source": "...", "retrieved": "YYYY-MM-DD",
           "releases": ["9.3", "9.4", "9.5", "9.6"], "caveat": "shape only — ..."},
  "endpoints": {
    "orchestrator POST /alarm/alarmConfig": {
      "name": "Update alarms configuration",
      "folder": ["Orchestrator Level", "alarm"],
      "path": "/alarm/alarmConfig",
      "request": {"...": "..."},
      "response": {"...": "..."},
      "since": "9.3"
    }
  }
}
```

`path` is the vendor's raw path, kept because the key normalizes parameter
names away and because query strings there document real query parameters. A
body that is not JSON (a bare `string`, or prose) is preserved under
`request_raw` / `response_raw` rather than dropped. `meta.retrieved` is a
provenance stamp and never counts as drift.

Read it through `pyecsdwan.specs.payload_example(scope, method, path)` — never
by opening the file directly.
