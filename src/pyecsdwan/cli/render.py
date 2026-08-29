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
from pyecsdwan.cli import reference
from pyecsdwan.cli.reference import CommandRow
from pyecsdwan.diffing import render_diff_lines
from pyecsdwan.journal import TxnJournal
from pyecsdwan.reports import drift as drift_report
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
        # Both blocking states are shown, and in the same place: an operator
        # who sees nothing under a diff should be able to read that as "nothing
        # owns this", which is only true if UNKNOWN prints too (#20).
        if item.ownership.blocks_write:
            console.print(Text(f"  {item.ownership.label}", style="bold yellow"))
    # Ahead of the dim warnings and in red: a collision is not advice, it is a
    # commit that will be refused, and burying it among tier notes would let an
    # operator write the whole changeset before finding out (#69).
    for collision in plan.collisions:
        console.print(
            Text(
                f"shared write target {collision.target} — claimed by "
                f"{', '.join(collision.refs)}",
                style="bold red",
            )
        )
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
    # Peers are counted from the neighbours the response actually listed, not
    # from the summary block's own tally (#87). A live appliance sent neither
    # `num_peers` nor `num_peers_active`, so this row read "None active of
    # None" — a Python repr in operator output — directly above a table of two
    # established sessions parsed from the same response. What we can verify is
    # the rows; the summary's claim is shown too, and only when it disagrees.
    peers = f"{state.observed_established} established of {state.observed_peers} observed"
    if state.summary_peer_counts_missing:
        peers += "  (the summary block reported no counts)"
    elif state.summary_peer_counts_disagree:
        peers += (
            f"  (the summary block says {summary.num_peers_active} active "
            f"of {summary.num_peers} — same response, disagreeing)"
        )
    rows: list[tuple[str, object]] = [
        ("state", summary.state_label),
        ("local ASN", summary.local_asn),
        ("router id", summary.rtr_id),
        ("local ip", summary.local_ip),
        ("peers", peers),
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


def render_command_reference(console: Console, rows: list[CommandRow]) -> None:
    """The offline command reference (#77, T4).

    Grouped by intent, because the intent split is the thing the taxonomy
    exists to make visible — a flat alphabetical list would hide exactly what
    an operator needs to see, which is that configuration and operational
    state are different commands over different sources.
    """
    for intent in (reference.CLI_STATE, reference.OPERATIONAL, reference.CONFIGURATION):
        group = [r for r in rows if r.intent == intent]
        if not group:
            continue
        table = Table(title=f"{intent} ({len(group)})")
        table.add_column("command", overflow="fold")
        table.add_column("scope")
        table.add_column("address")
        table.add_column("mutability")
        table.add_column("support", overflow="fold")
        for row in group:
            unsupported = row.support.startswith("unsupported")
            table.add_row(
                row.command,
                row.scope,
                row.address,
                row.mutability,
                Text(row.support, style="yellow") if unsupported else row.support,
            )
        console.print(table)
    console.print(
        Text(
            f"{len(rows)} commands. Offline: this reads the registry and the "
            f"vendored baselines, never the Orchestrator.",
            style="dim",
        )
    )


#: Drift statuses -> style. `undeclared` is dim rather than green on purpose:
#: it is not a passing row, it is a row nobody has said anything about.
_DRIFT_STYLES: dict[str, str] = {
    "drift": "bold yellow",
    "in-sync": "green",
    "undeclared": "dim",
    "unreadable": "bold red",
    "unsupported": "dim cyan",
}


def render_drift(console: Console, report: drift_report.Report) -> None:
    """Fabric-wide drift (epic #8).

    Every row is printed, including the ones that found nothing: a report that
    listed only drift would make an unreadable appliance and a clean one look
    identical, which is the failure this command exists to prevent.
    """
    table = Table(title=f"drift ({len(report.rows)} instance(s))")
    table.add_column("kind")
    table.add_column("appliance")
    table.add_column("instance")
    table.add_column("status")
    table.add_column("detail")
    for row in report.rows:
        table.add_row(
            row.noun,
            row.appliance or Text("-", style="dim"),
            row.name,
            Text(row.status.value, style=_DRIFT_STYLES.get(row.status.value, "")),
            Text(
                f"{row.entries} path(s): {row.detail}" if row.entries else row.detail,
                style="dim",
            ),
        )
    console.print(table)

    counts = report.counts
    console.print(
        "  ".join(
            f"{name}: {n}" for name, n in counts.items() if n or name in ("drift", "in-sync")
        )
    )
    for note in report.notes:
        console.print(Text(note, style="dim"))
    if not report.complete:
        # Said outright, not left to be inferred from a count: the whole
        # difference between this and a report that lies is this line.
        console.print(
            Text(
                f"incomplete: {len(report.inconclusive)} instance(s) could not be compared, "
                f"so \"no drift\" is not a claim this run can make",
                style="bold red",
            )
        )
