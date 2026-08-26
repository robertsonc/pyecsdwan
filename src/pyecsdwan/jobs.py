"""Async job (action key) polling.

Template pushes, overlay changes, and several appliance operations return an
action key (a GUID). Progress is polled via ``GET /action/status?key=<guid>``
which returns either one task record or a list of related records (group
pushes fan out one record per appliance, tied together by ``guid``).

Task record fields (from the actionLog Swagger section):
``taskStatus`` (str), ``percentComplete`` (int), ``completionStatus`` (bool),
``result`` (str — the Orchestrator's error/summary text), ``endTime`` (ms
epoch; 0 while running), ``nepk`` (per-appliance records).

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


def _record_finished(rec: dict[str, Any]) -> bool:
    if rec.get("endTime"):
        return True
    if isinstance(rec.get("percentComplete"), (int, float)) and rec["percentComplete"] >= 100:
        return True
    status = str(rec.get("taskStatus", "")).lower()
    return any(marker in status for marker in _DONE_STATUSES)


def _record_succeeded(rec: dict[str, Any]) -> bool:
    # Field experience (see docs/research/expert-repo.md): taskStatus is the
    # reliable signal; completionStatus can stay false even on success (e.g.
    # ECOS upgrades). Key on taskStatus text first, completionStatus only as a
    # tiebreaker when taskStatus says nothing either way.
    status = str(rec.get("taskStatus", "")).lower()
    if any(bad in status for bad in ("fail", "error", "cancel")):
        return False
    if any(good in status for good in ("done", "complete", "finish", "success")):
        return True
    completion = rec.get("completionStatus")
    if completion is not None:
        return bool(completion)
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
    records: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        raw = client.get("/action/status", params={"key": action_key})
        if isinstance(raw, dict):
            records = [raw]
        elif isinstance(raw, list):
            records = [r for r in raw if isinstance(r, dict)]
        else:
            records = []
        if records and all(_record_finished(r) for r in records):
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
                log.debug("job_failed", key=action_key, detail=detail)
                return JobOutcome(
                    key=action_key, state="FAILED", detail=detail, per_appliance=per_appliance
                )
            detail = str(records[0].get("result") or "") if len(records) == 1 else ""
            log.debug("job_success", key=action_key)
            return JobOutcome(
                key=action_key, state="SUCCESS", detail=detail, per_appliance=per_appliance
            )
        time.sleep(delay)
        delay = min(delay * 2, settings.job_poll_max)
    detail = f"job did not finish within {settings.job_timeout}s"
    if description:
        detail = f"{description}: {detail}"
    log.debug("job_timeout", key=action_key)
    return JobOutcome(key=action_key, state="TIMEOUT", detail=detail)
