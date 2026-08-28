# Observed async-job shapes

The terminal shapes `pyecsdwan.jobs` recognises, and where each one comes from.

This file exists because #64 made success an **allowlist**. Before that, a
record was a success unless its `result` contained one of five English failure
words, so every unseen shape — a new release's wording, a localized
Orchestrator, "Rejected" — passed. Now an unrecognised shape is `UNKNOWN`, and
`UNKNOWN` fails its transaction. That makes this list load-bearing: adding an
entry lets a shape confirm a change on a live fabric, so nothing goes in
without evidence, and every entry names its own.

## How the classifier uses this

`jobs._record_state()` classifies one **already-terminal** record
(`jobs._record_finished()` decides terminal, on `endTime` / `percentComplete` /
a done-state `taskStatus`, and is deliberately tolerant — being finished is a
weaker claim than having worked):

| Test | Result |
|---|---|
| `taskStatus` contains a failure token | `FAILED` |
| `result` contains a failure token | `FAILED` |
| `taskStatus` matches no success token | `UNKNOWN` |
| `result` is empty | `SUCCESS` |
| `result` starts with an allowlisted shape | `SUCCESS` |
| anything else | `UNKNOWN` |

Failure is tested first and stays token-based *on purpose*: it is a
supplement, not the decision. No list of failure words is ever complete, which
is exactly why success can no longer be inferred from their absence.

An empty `result` on a success `taskStatus` is accepted. Many records carry
none; rejecting them would fail closed on the ordinary case rather than the
ambiguous one, and the status is then the only signal there is.

## `taskStatus` (string channel: `GET /action/status`, `GET /action`)

| Value | Class | Provenance |
|---|---|---|
| `COMPLETED` | success | `docs/research/expert-repo.md` §Async patterns — field-verified, poll until `taskStatus.upper()` in (COMPLETED, FAILED) |
| `FAILED` | failure | same |
| `Completed` | success | vendored SDK / Swagger `actionLog` section, mixed case |
| `In Progress`, `Queued`, `Running`, `Pending` | in flight | `jobs._IN_FLIGHT`; a record wearing one is not terminal even at `percentComplete` 100 |
| `Done`, `Finished` | success | tolerance carried from the original poller; token match, not a verified string |
| `Cancelled`, `Error`, `Aborted`, `Rejected` | failure | token match |

Matched as case-insensitive **substrings**, so `COMPLETED`, `Completed` and
`Task completed` all land the same way.

## `result` (success allowlist)

`jobs.SUCCESS_RESULT_SHAPES`, matched as a lowercase prefix:

| Prefix | Orchestrator / ECOS | Provenance |
|---|---|---|
| `Success` | version unrecorded | `docs/research/expert-repo.md` §Async patterns: "Success test used in the field: `taskStatus == "COMPLETED" and result.startswith("Success")`" |

The version column is honest rather than helpful: the observation is
field-sourced but arrived without a version stamp, and the surrounding SDK
reference is written against Orchestrator >= 9.3 query-param paths. Recording
"9.3" here would be a guess dressed as evidence — the same move this whole
issue is about.

**To add a shape:** run the operation on a real fabric, capture the record
(`taskStatus` and `result` verbatim), and add a row with the Orchestrator and
ECOS versions it came from. An operator hitting an unknown shape sees it
quoted back at them in the failure detail —

```
1 record(s) finished in a shape this poller does not recognise, so the push
cannot be confirmed: taskStatus='Completed' result='Configuración aplicada'
```

— which is the report this table grows from.

## `completionStatus`

**Not used as a tiebreaker.** For ECOS upgrades it stays `false` on success and
`logLevel` is always ERROR (`docs/research/expert-repo.md`). A field that is
wrong in the success direction cannot break a tie about success.

## Preconfig apply (numeric channel)

`GET /gms/appliance/preconfiguration/apply?preconfigId=` is a different
protocol on a different endpoint (`docs/research/appliance-jobs.md`
§Preconfig apply), and `jobs.wait_for_preconfig_apply` handles it separately:

| `taskStatus` | Meaning |
|---|---|
| `0` | NotStarted |
| `1` | InProgress |
| `2` | Finished — and only now is `completionStatus` valid |

The string-channel allowlist above does not apply here; `2` plus
`completionStatus` is the terminal test, because on this endpoint
`completionStatus` is documented as meaningful once `taskStatus == 2`.

## Keyless writes

Two write shapes return no action key, and each is confirmed by evidence rather
than by the absence of an error:

* **`POST /appliance/saveChanges` with no `clientKey`** (off-spec for 9.3+) —
  `jobs._verify_persisted` polls `hasUnsavedChanges` on `GET /appliance` until
  every saved appliance reports its changes persisted. Unreadable flag →
  `UNKNOWN`; still set at the deadline → `FAILED`.
* **Template association's fire-and-204** — per-appliance results exist only as
  action-log records under a guid. `jobs.wait_for_recent_action` correlates by
  appliance (server-side filter), start-time window, and optionally operation
  `name`, and returns `UNKNOWN` naming the guids when more than one shares the
  window.

No `name` string has been observed for the template-association push, so no
caller passes `action_name` yet. Record one here and the correlation tightens
by one dimension; guessing one would filter out the very record being awaited.
