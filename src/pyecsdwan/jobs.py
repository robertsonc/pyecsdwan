"""Async job (action key) polling.

Template pushes, overlay changes, and several appliance operations return an
action key (a GUID). Progress is polled via ``GET /action/status?key=<guid>``
which returns either one task record or a list of related records (group
pushes fan out one record per appliance, tied together by ``guid``).

Some writes are keyless (the template-association POST is fire-and-204): their
per-appliance records land only in the action log. Those are polled via the
listing endpoint ``GET /action?startTime=&endTime=&logLevel=&appliance=``
(epoch **milliseconds** — the vendored SDK docstring wrongly says seconds),
picking the newest ``guid`` inside the window (``wait_for_recent_action``).

Task record fields (from the actionLog Swagger section):
``taskStatus`` (str), ``percentComplete`` (int), ``completionStatus`` (bool),
``result`` (str — the Orchestrator's error/summary text), ``endTime`` (ms
epoch; 0 while running), ``startTime`` (ms epoch), ``nepk`` (per-appliance
records).

Terminal detection is deliberately tolerant of field variations across
Orchestrator releases: a record is finished when ``endTime`` is non-zero, or
``percentComplete`` >= 100, or ``taskStatus`` names a done-state.
"""

from __future__ import annotations

import time
from typing import Any

import structlog

from pyecsdwan.client import OrchClient
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


def _record_succeeded(rec: dict[str, Any]) -> bool:
    # Field experience (see docs/research/expert-repo.md): taskStatus is the
    # reliable terminal signal, but a "Completed" record can still carry an
    # error in `result` — the verified success test is
    # taskStatus == COMPLETED AND result startswith "Success". completionStatus
    # is unreliable (false even on success for ECOS upgrades) and used only as
    # a last-resort tiebreaker.
    status = str(rec.get("taskStatus", "")).lower()
    if any(bad in status for bad in ("fail", "error", "cancel")):
        return False
    result = str(rec.get("result", "")).strip().lower()
    if any(good in status for good in ("done", "complete", "finish", "success")):
        # A terminal record can still carry an error in `result` (a "Completed"
        # push that failed on some appliances). Reject only on an explicit
        # error token; a plain success message ("template pushed") passes.
        if any(bad in result for bad in ("fail", "error", "denied", "unable", "cannot")):
            return False
        return True
    completion = rec.get("completionStatus")
    if completion is not None:
        return bool(completion)
    return True


def _terminal_outcome(key: str, records: list[dict[str, Any]]) -> JobOutcome | None:
    """SUCCESS/FAILED outcome once *every* record is finished, else None."""
    if not records or not all(_record_finished(r) for r in records):
        return None
    per_appliance = {
        str(r.get("nepk") or r.get("nePk") or ""):
            str(r.get("result") or r.get("taskStatus") or "")
        for r in records
        if r.get("nepk") or r.get("nePk")
    }
    failures = [r for r in records if not _record_succeeded(r)]
    if failures:
        detail = "; ".join(
            str(f.get("result") or f.get("taskStatus") or "failed") for f in failures
        )
        log.debug("job_failed", key=key, detail=detail)
        return JobOutcome(key=key, state="FAILED", detail=detail, per_appliance=per_appliance)
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


#: Backdating applied to the window start so a server clock slightly behind
#: the client's still lists the record stamped just before our POST.
_WINDOW_SLACK_MS = 1000
#: 0=Debug, 1=Info, 2=Error. Push records are logged at Info.
_ACTION_LOG_LEVEL = 1
#: Rows per listing call — generous for one appliance's recent window.
_ACTION_LOG_LIMIT = 100


def wait_for_recent_action(
    client: OrchClient,
    settings: Settings,
    ne_pk: str,
    since_ms: int,
    description: str = "",
) -> JobOutcome:
    """Poll the action log for a keyless push against one appliance.

    For writes that return 204 without an action key (template association),
    the per-appliance results exist only as action-log records under a guid.
    This polls ``GET /action`` filtered by ``appliance=ne_pk`` and a time
    window opening just before the write (``since_ms``, epoch milliseconds),
    takes the newest guid in the window, and applies the same terminal /
    success detection as ``wait_for_action``. An empty list keeps polling —
    the log can lag the 204 — so "no records by the deadline" is a TIMEOUT,
    as is a record set still in flight at the deadline.
    """
    deadline = time.monotonic() + settings.job_timeout
    delay = settings.job_poll_initial
    guid = ""
    while time.monotonic() < deadline:
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
        rows = [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
        groups: dict[str, list[dict[str, Any]]] = {}
        for rec in rows:
            groups.setdefault(str(rec.get("guid") or ""), []).append(rec)
        if groups:
            # Several guids can share the window (e.g. an operator push moments
            # earlier); ours is the newest. `>=` so an exact-ms tie resolves to
            # the record listed last, which the mock emits in creation order.
            best_started = -1
            for candidate_guid, recs in groups.items():
                started = max(int(r.get("startTime") or 0) for r in recs)
                if started >= best_started:
                    guid, best_started = candidate_guid, started
            outcome = _terminal_outcome(guid, groups[guid])
            if outcome is not None:
                return outcome
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)
    if guid:
        detail = f"job did not finish within {settings.job_timeout}s"
    else:
        detail = f"no action-log records appeared within {settings.job_timeout}s"
    if description:
        detail = f"{description}: {detail}"
    log.debug("job_timeout", key=guid, appliance=ne_pk)
    return JobOutcome(key=guid, state="TIMEOUT", detail=detail)
