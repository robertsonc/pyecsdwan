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

## gen_models.py — pydantic models + a typed binding for one operation (#26)

```
python tools/gen_models.py --scope appliance --method POST --path bgp/config/system
python tools/gen_models.py --scope orchestrator --method GET --path /gms/grNodes --dry-run
```

Writes two modules per operation, `src/pyecsdwan/generated/models/<slug>.py`
and `.../bindings/<slug>.py`: request/response models (`extra="allow"`, so
live drift from the 7.2.0 baseline round-trips instead of failing validation)
and one function calling `OrchClient` — `request` for orchestrator paths,
`appliance_request` for appliance (ECOS) paths, which take the path
*relative*. Output is deterministic and already ruff-clean; generation is
per-operation on purpose, because the baselines carry 1833 of them.

Importable rather than shell-only: `generate(endpoint)` returns the whole
result in memory (both sources, model class names, the binding's callable
name, path/query parameters). `tools/gen_plugin.py` uses exactly that, so the
two tools cannot disagree about a field name, a type, or a slug.

Exit codes: 0 generated, 1 no such operation, 2 error.

## gen_plugin.py — a Tier-1 plugin stub for one write operation (#27)

```
python tools/gen_plugin.py --scope appliance --method POST --path '/virtualif/vti/{vtiName}'
python tools/gen_plugin.py --scope orchestrator --method POST --path /alarm/correlationSettings
python tools/gen_plugin.py --from-diff drift.json          # see "end to end" below
```

Writes `src/pyecsdwan/resources/generated/<slug>.py` plus every model/binding
it imports, and rewrites that package's `__init__.py` so the stub actually
registers. The emitted `Resource` has `fetch`/`apply`/`rollback` wired to the
generated bindings, `tier = Tier.GENERATED`, and a `normalize()` that raises
`NotCurated`. The last part is the point of the tier, not an omission:
idempotency needs a human decision about which fields are server-generated
echoes, and `tests/test_promotion.py` parametrizes over the whole registry, so
**every** stub anyone generates is held to that raise by `make check` — there
is no opt-in and no way to register a stub that quietly returns.

Unlike `pyecsdwan.generated`, these files are meant to be **hand-edited** —
they are the starting point for curation — so the tool refuses to overwrite
one without `--force`.

Reversibility is derived, never guessed upward:

- **COMPENSABLE** — a `POST`/`PUT`/`PATCH` with a `GET` on the same path.
  `fetch()` snapshots, `rollback()` replays the snapshot through the same
  write, and where the spec also exposes a `DELETE` and the snapshot shows the
  object was absent, `rollback()` deletes instead. Never REVERSIBLE: nothing
  in a spec promises the GET's response is accepted verbatim by the write.
- **IRREVERSIBLE** — a write with no paired `GET` (nothing to snapshot, so
  nothing to put back), and every `DELETE`-primary stub. These refuse `commit`
  without `--force`, which is the right answer: claiming a compensator that
  `rollback()` cannot run is worse than declaring the truth. A paired `DELETE`
  alone never buys COMPENSABLE — without a `GET` the stub cannot tell a create
  from an update, and "compensating" an update by deleting would destroy live
  configuration.

Appliance-scope stubs resolve `nePk` through the resolver and end every proxy
write with one batched `ctx.save_changes([nePk])`, without which the change is
lost on the appliance's next reboot.

An instance ref carries the endpoint's path and required query parameters in
`Ref.name` — a bare value for a single parameter (`vti1`), `name=value` pairs
otherwise (`vrfId=0,IP=10.0.0.1`). Nothing is defaulted: a guessed parameter
would address a different instance than the operator named.

Two families of operation are refused, with the reason: a path containing
whitespace (two 7.2.0 paths have a trailing space, and `Resource.endpoints` is
a space-separated key), and a `DELETE` whose spec makes a request body
mandatory (a delete's desired state is "absent"; there is nothing to build the
payload from). Read-only operations get no plugin at all — a `GET` is a view,
not a configurable object; use `gen_models.py` for a typed read binding.

Exit codes: 0 generated, 1 nothing to generate, 2 error.

## End to end: a new endpoint in the published spec becomes a stub

```
python tools/spec_sync.py --diff   --spec appliance --source new.json --json > drift.json
python tools/spec_sync.py --update --spec appliance --source new.json
python tools/gen_plugin.py --from-diff drift.json
```

`--update` in the middle is not optional: `gen_plugin.py` generates from the
vendored baseline, so an operation `specs/` does not carry yet has nothing to
generate from (it says so, naming the endpoint, rather than failing quietly).
`--from-diff` collapses each path to one stub, generated from the strongest
write method added on it — a new resource arrives in a diff as `GET` + `POST`
+ `DELETE`, and that is one resource, not three.

`tests/test_gen_plugin.py::test_spec_sync_diff_produces_a_registered_stub`
drives exactly this sequence against a fixture spec and then imports the
result: the epic's definition of done.

## Where a stub goes next

`ec-cli show coverage --endpoints --tier 1` lists what is generated but not
curated. `docs/plugin-promotion.md` is the checklist for promoting one, and
`ec-cli plugin promote <kind>` runs the machine-decidable half of it against
your own fabric.
