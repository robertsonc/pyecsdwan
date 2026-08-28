"""Async job (action key) polling and the save-changes persistence primitive.

Template pushes, overlay changes, and several appliance operations return an
action key (a GUID). Progress is polled via ``GET /action/status?key=<guid>``
which returns either one task record or a list of related records (group
pushes fan out one record per appliance, tied together by ``guid``).

Some writes are keyless (the template-association POST is fire-and-204): their
per-appliance records land only in the action log. Those are polled via the
listing endpoint ``GET /action?startTime=&endTime=&logLevel=&appliance=``
(epoch **milliseconds** — the vendored SDK docstring wrongly says seconds),
correlating a single ``guid`` inside the window (``wait_for_recent_action``),
which refuses rather than choosing when several share it.

Task record fields (from the actionLog Swagger section):
``taskStatus`` (str), ``percentComplete`` (int), ``completionStatus`` (bool),
``result`` (str — the Orchestrator's error/summary text), ``endTime`` (ms
epoch; 0 while running), ``startTime`` (ms epoch), ``nepk`` (per-appliance
records).

Terminal detection is deliberately tolerant of field variations across
Orchestrator releases: a record is finished when ``endTime`` is non-zero, or
``percentComplete`` >= 100, or ``taskStatus`` names a done-state.

``save_changes`` composes this poller with ``POST /appliance/saveChanges``:
the one save-changes operation every appliance-proxy write must be followed
by (docs/research/appliance-jobs.md §save-changes). Resources normally reach
it through ``Ctx.save_changes``.
"""

from __future__ import annotations

import time
from collections.abc import Collection, Sequence
from typing import Any

import structlog

from pyecsdwan.client import OrchClient, validate_ne_pk
from pyecsdwan.config import Settings
from pyecsdwan.contract import JobOutcome

log = structlog.get_logger("pyecsdwan.jobs")

_DONE_STATUSES = frozenset(
    s.lower() for s in ("done", "completed", "complete", "finished", "failed", "error", "cancelled")
)
#: Statuses that are explicitly still in flight — a record wearing one of
#: these is NOT finished, even at percentComplete 100 (the bar can hit 100
#: before the task is stamped terminal).
_IN_FLIGHT = frozenset(
    s.lower() for s in ("in progress", "progress", "queued", "running", "pending")
)


#: Terminal `taskStatus` values that mean the task finished successfully.
_SUCCESS_STATUS_TOKENS = frozenset(("done", "complete", "finish", "success"))
#: Terminal `taskStatus` values that mean it did not.
_FAILURE_STATUS_TOKENS = frozenset(("fail", "error", "cancel", "abort", "reject"))
#: Explicit failure text in `result`. Kept as a *supplement* to the success
#: allowlist below, not as the primary test: this list is what used to decide
#: success by absence, and no list of failure words is ever complete.
_FAILURE_RESULT_TOKENS = frozenset(
    ("fail", "error", "denied", "unable", "cannot", "invalid", "reject", "refused")
)

#: Allowlisted `result` prefixes for a *successful* terminal record, lowercase.
#:
#: Provenance matters here, so each entry names its evidence. Adding one on a
#: hunch reopens exactly the hole this closes — an unrecognised shape must
#: fail closed until someone has seen it succeed.
#:
#: * ``success`` — field-verified: ``docs/research/expert-repo.md`` records the
#:   working test as ``taskStatus == "COMPLETED" and
#:   result.startswith("Success")``, against Orchestrator 9.3/9.4.
#:
#: Unrecognised shapes are reported as UNKNOWN with their exact text, so this
#: list grows from observation rather than from guessing.
#: ``docs/research/job-shapes.md`` is the table of what has been observed, and
#: the procedure for adding to it.
SUCCESS_RESULT_SHAPES: tuple[str, ...] = ("success",)


def _is_in_flight(status: str) -> bool:
    return any(marker in status for marker in _IN_FLIGHT)


def _record_finished(rec: dict[str, Any]) -> bool:
    status = str(rec.get("taskStatus", "")).lower()
    if _is_in_flight(status):
        return False
    if rec.get("endTime"):
        return True
    if isinstance(rec.get("percentComplete"), (int, float)) and rec["percentComplete"] >= 100:
        return True
    return any(marker in status for marker in _DONE_STATUSES)


def _record_state(rec: dict[str, Any]) -> str:
    """Classify one terminal task record: SUCCESS, FAILED or UNKNOWN (#64).

    **This used to infer success from the absence of failure.** A record whose
    ``taskStatus`` was terminal passed unless its ``result`` contained one of
    five English tokens, so "Completed" + "Invalid configuration" was a
    success, and so was "Completed" + "Rejected" — neither string contains
    fail/error/denied/unable/cannot. A localized result passed for the same
    reason. That is a transaction confirmed against a push that did not
    happen, which is the failure Principle II exists to prevent.

    Success is now *allowlisted* (:data:`SUCCESS_RESULT_SHAPES`), and anything
    terminal that matches neither list is ``UNKNOWN`` — which every caller
    treats as failure, because they all branch on ``state != "SUCCESS"``.
    Failing closed on an unrecognised shape is the point: the alternative is
    guessing, and the thing being guessed about is whether a change reached
    the fabric.

    The cost is real and deliberate: a genuinely successful shape this list
    does not know yet will fail its transaction and auto-revert. The detail
    carries the exact status and result text so the shape can be reported and
    added, which is why `UNKNOWN` is a distinct state rather than folded into
    FAILED.
    """
    status = str(rec.get("taskStatus", "")).strip().lower()
    result = str(rec.get("result", "")).strip().lower()

    if any(bad in status for bad in _FAILURE_STATUS_TOKENS):
        return "FAILED"
    if any(bad in result for bad in _FAILURE_RESULT_TOKENS):
        return "FAILED"
    if not any(good in status for good in _SUCCESS_STATUS_TOKENS):
        # Terminal by endTime/percentComplete but wearing a status nothing
        # recognises. `completionStatus` is not a tiebreaker here: the field
        # is documented unreliable (false even on success for ECOS upgrades),
        # so trusting it would reintroduce the guess this removes.
        return "UNKNOWN"
    if not result:
        # Empty result on a terminal success status. Accepted, because many
        # records carry none and rejecting them would fail closed on the
        # ordinary case rather than the ambiguous one.
        return "SUCCESS"
    if any(result.startswith(shape) for shape in SUCCESS_RESULT_SHAPES):
        return "SUCCESS"
    return "UNKNOWN"


def _terminal_outcome(key: str, records: list[dict[str, Any]]) -> JobOutcome | None:
    """SUCCESS/FAILED/UNKNOWN once *every* record is finished, else None."""
    if not records or not all(_record_finished(r) for r in records):
        return None
    per_appliance = {
        str(r.get("nepk") or r.get("nePk") or ""):
            str(r.get("result") or r.get("taskStatus") or "")
        for r in records
        if r.get("nepk") or r.get("nePk")
    }
    states = [(_record_state(r), r) for r in records]

    failures = [r for state, r in states if state == "FAILED"]
    if failures:
        detail = "; ".join(
            str(f.get("result") or f.get("taskStatus") or "failed") for f in failures
        )
        log.debug("job_failed", key=key, detail=detail)
        return JobOutcome(key=key, state="FAILED", detail=detail, per_appliance=per_appliance)

    unknown = [r for state, r in states if state == "UNKNOWN"]
    if unknown:
        # Named precisely, because the operator's next move is to report the
        # shape so the allowlist can grow — and because "it failed" would be a
        # claim this code cannot support.
        shapes = "; ".join(
            f"taskStatus={r.get('taskStatus')!r} result={r.get('result')!r}" for r in unknown[:3]
        )
        detail = (
            f"{len(unknown)} record(s) finished in a shape this poller does not "
            f"recognise, so the push cannot be confirmed: {shapes}"
        )
        log.debug("job_unknown", key=key, detail=detail)
        return JobOutcome(key=key, state="UNKNOWN", detail=detail, per_appliance=per_appliance)

    detail = str(records[0].get("result") or "") if len(records) == 1 else ""
    log.debug("job_success", key=key)
    return JobOutcome(key=key, state="SUCCESS", detail=detail, per_appliance=per_appliance)


def extract_action_key(response: Any) -> str | None:
    """Pull an action/client key out of a write response, if one is present.

    Many Orchestrator writes are fire-and-204 (the template push is one: its
    per-appliance results land in the action log under a guid, not in the
    response). Others return ``{"clientKey": ...}`` / ``{"actionKey": ...}``.
    Returns the key when the response carries one, else None so the caller
    proceeds without polling."""
    if isinstance(response, str) and response.strip():
        # appliance_resync / delete_ecos_image return a bare-string key.
        candidate = response.strip().strip('"')
        return candidate or None
    if isinstance(response, dict):
        for field in ("clientKey", "actionKey", "action_key", "guid", "key"):
            val = response.get(field)
            if isinstance(val, str) and val:
                return val
    return None


def cancel_action(client: OrchClient, action_key: str) -> bool:
    """Cancel an in-flight action key: ``POST /action/cancel?key=<action_key>``
    (issue #24).

    The vendored SDK's ``cancel_audit_log_task`` is buggy — it issues a GET
    on the status endpoint instead of calling cancel at all (see
    docs/research/appliance-jobs.md's SDK-defects note) — so this calls the
    real endpoint directly rather than going through it. Returns ``True``
    when the Orchestrator accepts the cancel request. A cancel on an
    already-terminal or unknown key is not treated as an error here (the
    Orchestrator's own response governs); callers that need to distinguish
    "cancelled" from "was already done" should re-poll the key afterward.
    """
    log.debug("action_cancel", key=action_key)
    response = client.post("/action/cancel", params={"key": action_key})
    if isinstance(response, dict):
        for field in ("success", "ok", "cancelled"):
            val = response.get(field)
            if isinstance(val, bool):
                return val
    return True


def wait_for_action(
    client: OrchClient,
    action_key: str,
    settings: Settings,
    description: str = "",
) -> JobOutcome:
    """Poll one action key to a terminal state.

    Returns SUCCESS / FAILED / TIMEOUT; FAILED and TIMEOUT carry the
    Orchestrator's error text and per-appliance results where available.
    A TIMEOUT inside a commit-confirm window counts as failure upstream.
    """
    deadline = time.monotonic() + settings.job_timeout
    delay = settings.job_poll_initial
    while time.monotonic() < deadline:
        raw = client.get("/action/status", params={"key": action_key})
        if isinstance(raw, dict):
            records = [raw]
        elif isinstance(raw, list):
            records = [r for r in raw if isinstance(r, dict)]
        else:
            records = []
        outcome = _terminal_outcome(action_key, records)
        if outcome is not None:
            return outcome
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)
    detail = f"job did not finish within {settings.job_timeout}s"
    if description:
        detail = f"{description}: {detail}"
    log.debug("job_timeout", key=action_key)
    return JobOutcome(key=action_key, state="TIMEOUT", detail=detail)


#: Preconfig apply's own numeric taskStatus values (docs/research/
#: appliance-jobs.md §Preconfig apply) — distinct from the string taskStatus
#: (e.g. "Completed", "In Progress") the action-log channel above uses.
_PRECONFIG_NOT_STARTED = 0
_PRECONFIG_IN_PROGRESS = 1
_PRECONFIG_FINISHED = 2

_PRECONFIG_APPLY_PATH = "/gms/appliance/preconfiguration/apply"


def wait_for_preconfig_apply(
    client: OrchClient,
    preconfig_id: str,
    settings: Settings,
    description: str = "",
) -> JobOutcome:
    """Poll a preconfig apply to terminal state on its own numeric-taskStatus
    channel (issue #23) — ``GET /gms/appliance/preconfiguration/apply?
    preconfigId=`` returns ``{taskStatus: 0 NotStarted | 1 InProgress |
    2 Finished, completionStatus (valid only once taskStatus==2), guid
    (actionlog bridge, unused here), result: [{taskStatus, completionStatus,
    name, result, nePk, data}]}`` — a completely separate shape and terminal
    signal from the string-``taskStatus`` action-log channel
    ``wait_for_action``/``wait_for_recent_action`` poll above. Only
    ``taskStatus == 2`` is terminal; 0 and 1 keep polling regardless of what
    ``completionStatus`` happens to hold (per the API's own "valid only when
    taskStatus==2" caveat — checking it early would misread a still-running
    apply as failed).

    A per-appliance breakdown is built from the ``result[]`` list when
    present, matching :class:`JobOutcome`'s ``per_appliance`` shape used
    elsewhere. No resource calls this yet — the preconfiguration lifecycle
    resource that will (epic #7) doesn't exist yet; this is the polling
    primitive it will build on.
    """
    deadline = time.monotonic() + settings.job_timeout
    delay = settings.job_poll_initial
    while time.monotonic() < deadline:
        raw = client.get(_PRECONFIG_APPLY_PATH, params={"preconfigId": preconfig_id})
        if isinstance(raw, dict) and raw.get("taskStatus") == _PRECONFIG_FINISHED:
            return _preconfig_terminal_outcome(preconfig_id, raw)
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)
    detail = f"preconfig apply did not finish within {settings.job_timeout}s"
    if description:
        detail = f"{description}: {detail}"
    log.debug("preconfig_apply_timeout", preconfig_id=preconfig_id)
    return JobOutcome(key=preconfig_id, state="TIMEOUT", detail=detail)


def _preconfig_terminal_outcome(preconfig_id: str, raw: dict[str, Any]) -> JobOutcome:
    per_appliance: dict[str, str] = {}
    results = raw.get("result")
    if isinstance(results, list):
        for entry in results:
            if not isinstance(entry, dict):
                continue
            ne_pk = str(entry.get("nePk") or "")
            if ne_pk:
                per_appliance[ne_pk] = str(entry.get("result") or entry.get("taskStatus") or "")
    if not raw.get("completionStatus"):
        detail = str(raw.get("result") or "preconfig apply failed")
        log.debug("preconfig_apply_failed", preconfig_id=preconfig_id, detail=detail)
        return JobOutcome(
            key=preconfig_id, state="FAILED", detail=detail, per_appliance=per_appliance
        )
    log.debug("preconfig_apply_success", preconfig_id=preconfig_id)
    return JobOutcome(key=preconfig_id, state="SUCCESS", per_appliance=per_appliance)


#: Backdating applied to the window start so a server clock slightly behind
#: the client's still lists the record stamped just before our POST.
_WINDOW_SLACK_MS = 1000
#: 0=Debug, 1=Info, 2=Error. Push records are logged at Info.
_ACTION_LOG_LEVEL = 1
#: Rows per listing call — generous for one appliance's recent window.
_ACTION_LOG_LIMIT = 100


def _action_log_window(
    client: OrchClient, ne_pk: str, since_ms: int
) -> list[dict[str, Any]]:
    """One ``GET /action`` over the correlation window for one appliance."""
    raw = client.get(
        "/action",
        params={
            "startTime": since_ms - _WINDOW_SLACK_MS,
            "endTime": int(time.time() * 1000),
            "logLevel": _ACTION_LOG_LEVEL,
            "limit": _ACTION_LOG_LIMIT,
            "appliance": ne_pk,
        },
    )
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def action_log_guids(client: OrchClient, ne_pk: str, since_ms: int) -> frozenset[str]:
    """Guids already in the correlation window *before* a keyless write (#64).

    Pass the result to :func:`wait_for_recent_action` as ``ignore_guids`` and
    the correlation stops depending on clocks entirely: a guid that was
    already there is not the one this write is about, whatever its timestamp
    says and whichever way the two clocks disagree.

    This is what makes an apply-then-revert sequence work. The revert's window
    opens :data:`_WINDOW_SLACK_MS` before its own POST, which reaches back over
    the apply's push — so the revert would otherwise find two guids and refuse
    to correlate either, failing every auto-revert of a keyless push. Comparing
    identity rather than time removes that without weakening the refusal: an
    operator's *concurrent* push still shows up as a second new guid.

    Cost is one GET before the write. Cheap, and the alternative is a
    timestamp comparison between two machines' clocks.
    """
    return frozenset(
        str(rec.get("guid") or "") for rec in _action_log_window(client, ne_pk, since_ms)
    )


def wait_for_recent_action(
    client: OrchClient,
    settings: Settings,
    ne_pk: str,
    since_ms: int,
    description: str = "",
    action_name: str = "",
    ignore_guids: Collection[str] = (),
) -> JobOutcome:
    """Poll the action log for a keyless push against one appliance.

    For writes that return 204 without an action key (template association),
    the per-appliance results exist only as action-log records under a guid.
    This polls ``GET /action`` filtered by ``appliance=ne_pk`` and a time
    window opening just before the write (``since_ms``, epoch milliseconds),
    correlates *one* guid to that write, and applies the same terminal /
    success detection as ``wait_for_action``. An empty list keeps polling —
    the log can lag the 204 — so "no records by the deadline" is a TIMEOUT,
    as is a record set still in flight at the deadline.

    **Correlation refuses rather than guesses (#64).** This used to take the
    guid with the newest ``startTime``, on the reasoning that ours is the most
    recent thing to have happened. Its own comment named the counterexample —
    "an operator push moments earlier" shares the window — and a concurrent
    push is not merely possible but likely on exactly the fabrics where it
    matters most, because a change window is when everyone is pushing. If more
    than one guid is in the window this now returns ``UNKNOWN`` naming them,
    rather than attributing someone else's outcome to this transaction.
    Waiting cannot resolve it: more time admits more activity, not less.

    Correlation uses what the API permits, in this order:

    * **appliance** — server-side ``appliance=ne_pk`` filter.
    * **novelty** — ``ignore_guids``, the guids :func:`action_log_guids` saw in
      the same window just before the write. A guid that was already there is
      not this write's, and unlike a timestamp that conclusion does not depend
      on two machines agreeing about the time.
    * **start time** — the window opens at the write, less
      :data:`_WINDOW_SLACK_MS` of clock-skew slack.
    * **operation type** — ``action_name``, matched case-insensitively against
      the record's ``name``, when a caller can supply one. No caller does yet:
      the ``name`` string the Orchestrator writes for a template-association
      push has not been observed in the field, and guessing it would filter
      out the very record being waited on. ``docs/research/job-shapes.md`` is
      where an observed one gets recorded.
    * **initiator** — not used. Records carry ``user``/``ipAddress``, but the
      CLI authenticates with an API key and has no verified answer for "which
      user am I", so any comparison would be invented.

    Residual risk, stated because it cannot be removed at this layer: a *lone*
    candidate is accepted, and if the log has not yet caught up with our own
    204 while an operator's push landed too late to appear in ``ignore_guids``,
    that lone candidate is theirs. The API offers nothing that distinguishes
    the two, so the choice is between this and having no keyless path at all.
    """
    deadline = time.monotonic() + settings.job_timeout
    delay = settings.job_poll_initial
    known = frozenset(ignore_guids)
    seen: list[str] = []
    while time.monotonic() < deadline:
        groups: dict[str, list[dict[str, Any]]] = {}
        for rec in _action_log_window(client, ne_pk, since_ms):
            guid = str(rec.get("guid") or "")
            if guid in known:
                continue
            if action_name and action_name.lower() not in str(rec.get("name", "")).lower():
                continue
            # Re-checked client-side: the window is a server-side filter and a
            # server that ignores or rounds it would silently widen the set
            # this correlates over.
            if int(rec.get("startTime") or 0) < since_ms - _WINDOW_SLACK_MS:
                continue
            groups.setdefault(guid, []).append(rec)
        if len(groups) > 1:
            named = ", ".join(sorted(groups))
            detail = (
                f"{len(groups)} unrelated action-log records share the window on {ne_pk} "
                f"({named}), so this push cannot be told apart from concurrent activity"
            )
            if description:
                detail = f"{description}: {detail}"
            log.debug("job_ambiguous", appliance=ne_pk, guids=sorted(groups))
            return JobOutcome(key="", state="UNKNOWN", detail=detail)
        if groups:
            guid, records = next(iter(groups.items()))
            seen = [guid]
            outcome = _terminal_outcome(guid, records)
            if outcome is not None:
                return outcome
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)
    guid = seen[0] if seen else ""
    if guid:
        detail = f"job did not finish within {settings.job_timeout}s"
    else:
        detail = f"no action-log records appeared within {settings.job_timeout}s"
    if description:
        detail = f"{description}: {detail}"
    log.debug("job_timeout", key=guid, appliance=ne_pk)
    return JobOutcome(key=guid, state="TIMEOUT", detail=detail)


#: The appliance-inventory field that answers "is this appliance's running
#: config persisted?" — ``True`` means it is not (docs/research/
#: appliance-jobs.md §Appliance table). This is the independent check #64 asks
#: for: it is the fabric's own answer, not a restatement of the save response.
UNSAVED_FIELD = "hasUnsavedChanges"


def _unsaved_appliances(client: OrchClient, ne_pks: Sequence[str]) -> set[str] | None:
    """Which of ``ne_pks`` still report unsaved changes, or None if unknowable.

    None is the important return: the inventory came back in an unexpected
    shape, or an appliance is missing from it, or it carries no
    :data:`UNSAVED_FIELD`. "Cannot tell" must never collapse into "clean" —
    that is the same absence-means-success inference #64 removed from
    :func:`_record_state`, and it would be worse here, because this *is* the
    persistence check.

    Fetched live (``GET /appliance``) rather than through ``Resolver``, whose
    cache is minutes old by design: a stale answer to "did my write persist?"
    is not an answer.
    """
    raw = client.get("/appliance")
    if not isinstance(raw, list):
        return None
    by_pk = {
        str(a.get("nePk") or a.get("id")): a
        for a in raw
        if isinstance(a, dict) and (a.get("nePk") or a.get("id"))
    }
    pending: set[str] = set()
    for ne_pk in ne_pks:
        appliance = by_pk.get(ne_pk)
        if appliance is None or UNSAVED_FIELD not in appliance:
            return None
        if appliance[UNSAVED_FIELD]:
            pending.add(ne_pk)
    return pending


def _verify_persisted(
    client: OrchClient, ne_pks: Sequence[str], settings: Settings
) -> JobOutcome:
    """Confirm a keyless save actually persisted, by asking the fabric (#64).

    A keyless save is accepted-and-unawaited: there is no action key to poll
    and, because a batch save spans appliances, no single-appliance action-log
    window either. What there *is* is :data:`UNSAVED_FIELD`, which the
    appliance inventory reports independently of anything the save response
    said — so this polls it to the same deadline a keyed save would have used.

    Three outcomes, and only the first is success:

    * every appliance reports its changes saved -> SUCCESS
    * at least one still reports unsaved changes at the deadline -> FAILED,
      naming them
    * the flag cannot be read at all -> UNKNOWN, which fails the transaction
      just the same but does not claim the save failed

    The check is deliberately conservative in one known way: a *concurrent*
    write by another operator re-sets the flag, and this would then report
    FAILED for a save that did persist. That direction is the safe one — the
    transaction reverts and the operator retries — and the alternative is
    trusting a response that carried no confirmation at all.
    """
    deadline = time.monotonic() + settings.job_timeout
    delay = settings.job_poll_initial
    while True:
        pending = _unsaved_appliances(client, ne_pks)
        if pending is None:
            detail = (
                "save returned no client key, and the appliance inventory does not report "
                f"{UNSAVED_FIELD} for every appliance saved, so persistence cannot be "
                "confirmed"
            )
            log.debug("save_changes_unverifiable", ne_pks=list(ne_pks))
            return JobOutcome(key="", state="UNKNOWN", detail=detail)
        if not pending:
            log.debug("save_changes_verified", ne_pks=list(ne_pks))
            return JobOutcome(
                key="",
                state="SUCCESS",
                detail=(
                    "save returned no client key; persistence confirmed via "
                    f"{UNSAVED_FIELD}"
                ),
            )
        if time.monotonic() >= deadline:
            names = ", ".join(sorted(pending))
            detail = (
                f"save returned no client key and {names} still report unsaved changes "
                f"after {settings.job_timeout}s, so the running-config change is not "
                "persisted"
            )
            log.debug("save_changes_unpersisted", ne_pks=sorted(pending))
            return JobOutcome(key="", state="FAILED", detail=detail)
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)


def save_changes(
    client: OrchClient,
    ne_pks: Sequence[str],
    settings: Settings,
    description: str = "",
) -> JobOutcome:
    """Persist appliance running config to flash (the save-changes primitive).

    Proxied appliance writes (``/appliance/rest?nePk=&url=``) mutate the
    running config only: without this call the change is lost on reboot and
    the appliance reports ``hasUnsavedChanges``. One batched
    ``POST /appliance/saveChanges {"nePks": [...]}`` (9.3+ form) covers every
    appliance in ``ne_pks`` — callers pass all appliances an operation wrote
    to, so a multi-appliance changeset costs one save, not one per write.
    The returned ``clientKey`` is polled to terminal via ``wait_for_action``;
    a FAILED or TIMEOUT outcome means the change is NOT persisted and must
    fail the calling ``apply()``/``rollback()``.

    An empty ``ne_pks`` is a successful no-op (no API call), so callers can
    invoke this unconditionally. Duplicates collapse; nePks are validated
    before any request.

    Should the Orchestrator answer without a client key (off-spec for 9.3+),
    the save is **verified against the fabric** rather than assumed
    (:func:`_verify_persisted`) — see #64. It used to return SUCCESS with an
    explanatory detail, on the reasoning that a batch save spans appliances so
    there is no single-appliance window for ``wait_for_recent_action`` to fall
    back on. That reasoning is sound and the conclusion still did not follow:
    "we could not check" is not "it worked", and this outcome is what a
    transaction confirms against.
    """
    unique = sorted({validate_ne_pk(ne_pk) for ne_pk in ne_pks})
    if not unique:
        return JobOutcome(key="", state="SUCCESS", detail="no appliances to save")
    label = description or f"save changes ({', '.join(unique)})"
    response = client.post("/appliance/saveChanges", {"nePks": unique})
    key = extract_action_key(response)
    if key is None:
        log.debug("save_changes_keyless", ne_pks=unique)
        return _verify_persisted(client, unique, settings)
    log.debug("save_changes_started", key=key, ne_pks=unique)
    return wait_for_action(client, key, settings, label)
