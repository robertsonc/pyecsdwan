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
from pyecsdwan.reports.bgpstate import BgpState

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


def render_bgp_summary(console: Console, state: BgpState) -> None:
    """``show appliance <name> bgp summary`` (#72).

    `bgp_state` 0 ("not enabled") is a successful answer, not a failure, and
    it gets a sentence rather than a table of zeros: eighteen counters that
    are all zero because the protocol is switched off is a worse answer than
    saying the protocol is switched off.
    """
    summary = state.summary
    freshness = " (cached)" if state.cached else ""
    console.print(
        Text(f"# {state.appliance} ({state.ne_pk}) — BGP{freshness}", style="bold")
    )
    if not summary.enabled:
        console.print("BGP is not enabled on this appliance.")
        return

    table = Table(show_header=False, box=None)
    table.add_column("field", style="dim")
    table.add_column("value")
    rows: list[tuple[str, object]] = [
        ("state", f"{summary.state_name} ({summary.bgp_state})"),
        ("local ASN", summary.local_asn),
        ("router id", summary.rtr_id),
        ("local ip", summary.local_ip),
        ("peers", f"{summary.num_peers_active} active of {summary.num_peers}"),
        ("routes received", summary.num_bgp_rtes_rcvd),
        ("  from eBGP", summary.num_ebgp_rtes),
        ("  from iBGP", summary.num_ibgp_rtes),
        ("routes in RIB", summary.num_rib_rtes),
        ("subnets advertised", summary.num_subs_installed),
        ("rejected (scope)", summary.reject_mismatches),
        ("rejected (unpreferred)", summary.reject_unpreferred),
    ]
    for label, value in rows:
        table.add_row(label, "-" if value is None else str(value))
    console.print(table)

    # Error counters are surfaced only when non-zero: a row of zeros trains
    # the reader to skip the section where the one that matters will appear.
    for label, total, last in (
        ("routerd", summary.mgmt_stub_tot_errors, summary.mgmt_stub_last_err_str),
        ("tunneld-bgp", summary.tunbgp_tot_errors, summary.tunbgp_last_err_str),
    ):
        if total:
            detail = f": {last}" if last else ""
            console.print(Text(f"! {label}: {total} error(s){detail}", style="yellow"))


def render_bgp_neighbors(console: Console, state: BgpState, peer: str | None = None) -> None:
    """``show appliance <name> bgp neighbors [<ip>]`` (#72).

    A peer in the configuration but absent from `/bgp/state` is shown as
    *configured, not observed* rather than omitted or assumed established —
    #72's first guardrail, and the reason this view cannot be built on the
    config object.
    """
    rows = [n for n in state.neighbors if peer is None or n.peer_ip == peer]
    freshness = " (cached)" if state.cached else ""
    console.print(
        Text(f"# {state.appliance} ({state.ne_pk}) — BGP neighbors{freshness}", style="bold")
    )
    if not rows:
        # An answer, and a different one from "no such peer" — which the
        # caller reports separately, with its own exit code.
        console.print(
            f"(no BGP neighbors {'matching ' + peer if peer else 'on this appliance'})"
        )
        return

    table = Table("peer", "ASN", "state", "rcvd", "sent", "updates rx/tx", "caps")
    for row in rows:
        style = "dim" if row.configured_only else None
        table.add_row(
            row.peer_ip,
            "-" if row.asn is None else str(row.asn),
            row.peer_state_str or "-",
            "-" if row.rcvd_pfxs is None else str(row.rcvd_pfxs),
            "-" if row.sent_pfxs is None else str(row.sent_pfxs),
            f"{row.rcvd_updates if row.rcvd_updates is not None else '-'}/"
            f"{row.sent_updates if row.sent_updates is not None else '-'}",
            row.peer_caps or "-",
            style=style,
        )
    console.print(table)
    if any(r.configured_only for r in rows):
        console.print(
            Text(
                "dimmed rows are configured but not observed in BGP state — "
                "they are not established, and are not reported as such",
                style="dim",
            )
        )
