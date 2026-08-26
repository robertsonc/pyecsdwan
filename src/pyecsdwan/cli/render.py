"""Rich rendering helpers shared by the ``ec-cli`` subcommands and the shell.

Everything here is presentation-only: functions take a ``rich`` Console plus
core data structures (:class:`pyecsdwan.txn.Plan`,
:class:`pyecsdwan.txn.CommitReport`, :class:`pyecsdwan.journal.TxnJournal`)
and never touch the network or the journal themselves.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from pyecsdwan import txn
from pyecsdwan.diffing import render_diff_lines
from pyecsdwan.journal import TxnJournal

#: Junos-style diff markers -> rich styles.
_MARKER_STYLES: dict[str, str] = {"-": "red", "+": "green", "~": "yellow"}

_STATE_STYLES: dict[str, str] = {
    "CONFIRMED": "green",
    "APPLIED_UNCONFIRMED": "bold yellow",
    "REVERTED": "yellow",
    "REVERTING": "yellow",
    "REVERT_FAILED": "bold red",
    "FAILED": "red",
    "AUDIT_ONLY": "dim",
}


def render_plan(console: Console, plan: txn.Plan) -> None:
    """Junos-flavored plan rendering: per-item edit headers with +/- lines."""
    for item in plan.changed_items:
        console.print(Text(f"[edit {item.ref.key()}]", style="bold"))
        for marker, text in render_diff_lines(item.diff):
            console.print(Text(f"{marker} {text}", style=_MARKER_STYLES.get(marker, "")))
        if item.owner:
            console.print(Text(f"  managed-by: {item.owner}", style="bold yellow"))
    for warning in plan.warnings:
        console.print(Text(warning, style="dim"))
    if plan.empty:
        console.print("no changes")


def render_report(console: Console, report: txn.CommitReport) -> None:
    """Commit/rollback report: messages green on success, red on failure.

    Group pushes (e.g. a keyless template push fanning out to every
    appliance in a template group) can fail on some appliances and succeed
    on others; any job that carries a per-appliance breakdown gets one
    printed here so the operator sees exactly which nePk(s) failed, not just
    that the overall push did (issue #22).
    """
    style = "green" if report.ok else "red"
    for message in report.messages:
        console.print(Text(message, style=style))
    for job in report.jobs:
        if not job.per_appliance:
            continue
        job_style = "red" if job.state != "SUCCESS" else "dim"
        console.print(Text(f"  per-appliance results ({job.state}):", style=job_style))
        for ne_pk, detail in sorted(job.per_appliance.items()):
            console.print(Text(f"    {ne_pk}: {detail}", style=job_style))
    if report.confirm_deadline:
        console.print(Text(f"confirm deadline: {report.confirm_deadline}", style="yellow"))
    if report.txn_id:
        console.print(Text(f"transaction: {report.txn_id} [{report.state}]", style="dim"))


def render_journal_table(console: Console, txns: list[TxnJournal]) -> None:
    """Tabular transaction listing (``show journal`` / ``show pending``)."""
    table = Table(title=f"transactions ({len(txns)})")
    table.add_column("txn id", no_wrap=True)
    table.add_column("state")
    table.add_column("created")
    table.add_column("items", justify="right")
    table.add_column("confirm deadline")
    for journal in txns:
        meta = journal.meta
        table.add_row(
            meta.txn_id,
            Text(meta.state, style=_STATE_STYLES.get(meta.state, "")),
            meta.created_at,
            str(len(meta.items)),
            meta.confirm_deadline or "-",
        )
    console.print(table)


def tier0_banner(console: Console) -> None:
    """Red warning panel printed before every Tier-0 raw API passthrough."""
    console.print(
        Panel(
            Text(
                "RAW API PASSTHROUGH — journaled for audit only. No candidate config, "
                "no commit-confirm, NO ROLLBACK GUARANTEES.",
                style="bold red",
            ),
            border_style="red",
        )
    )
