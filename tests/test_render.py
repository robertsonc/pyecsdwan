"""Unit tests for cli/render.py's per-appliance fan-out breakdown (#22).

render_report() takes plain data structures and never touches the network,
so these are pure unit tests — no mock Orchestrator needed.
"""

from __future__ import annotations

from rich.console import Console

from pyecsdwan.cli.render import render_report
from pyecsdwan.contract import JobOutcome
from pyecsdwan.txn import CommitReport


def _rendered(report: CommitReport) -> str:
    console = Console(record=True, width=100)
    render_report(console, report)
    return console.export_text()


def test_failed_group_push_reports_every_appliance() -> None:
    """The exact scenario issue #22 asks for: a push to several appliances,
    one of them failing, reports the failing nePk explicitly."""
    report = CommitReport(
        ok=False,
        state="REVERTED",
        messages=["FAILED: template-association:BR1-EC: push failed"],
        jobs=[
            JobOutcome(
                key="guid-1",
                state="FAILED",
                detail="boom: policy rejected",
                per_appliance={
                    "1.NE": "Success",
                    "2.NE": "Success",
                    "3.NE": "boom: policy rejected",
                },
            )
        ],
    )
    out = _rendered(report)
    assert "3.NE: boom: policy rejected" in out
    assert "1.NE: Success" in out
    assert "2.NE: Success" in out
    assert "per-appliance results (FAILED)" in out


def test_successful_push_still_shows_breakdown_but_dim() -> None:
    report = CommitReport(
        ok=True,
        state="CONFIRMED",
        messages=["commit complete: 1 change(s) applied"],
        jobs=[JobOutcome(key="guid-2", state="SUCCESS", per_appliance={"1.NE": "Success"})],
    )
    out = _rendered(report)
    assert "1.NE: Success" in out
    assert "per-appliance results (SUCCESS)" in out


def test_job_without_per_appliance_data_prints_nothing_extra() -> None:
    """A job with no fan-out (e.g. a single save-changes call) must not
    print an empty breakdown block."""
    report = CommitReport(
        ok=True,
        state="CONFIRMED",
        messages=["commit complete: 1 change(s) applied"],
        jobs=[JobOutcome(key="", state="SUCCESS", detail="no appliances to save")],
    )
    out = _rendered(report)
    assert "per-appliance" not in out


def test_no_jobs_at_all_renders_like_before() -> None:
    report = CommitReport(ok=True, state="NO_CHANGES", messages=["no changes"])
    out = _rendered(report)
    assert out.strip() == "no changes"
