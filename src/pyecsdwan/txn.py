"""Transaction engine: commit-confirm emulation over a transactionless API.

Flow: candidate changeset -> plan (fetch/normalize/diff/ownership) -> commit
(snapshot-before-write journal -> dependency-ordered apply -> verify), with:

* ``commit confirm <minutes>``: arm a detached watchdog that reverts from the
  journal unless a confirm marker appears in time.
* Partial failure at step k of n: auto-revert steps k..1 from the journal and
  report exactly what was reverted. Never leave a half-applied changeset
  silently.
* ``rollback <n>``: restore the nth prior confirmed transaction's snapshots,
  journaled as a new transaction (so a rollback is itself revertible).

Two things keep those guarantees true when this CLI is *not* the only writer
(issue #63):

* Every critical section that writes to the fabric — commit, confirm, revert,
  rollback — runs under the host's ``commit`` lock, so two of them cannot
  interleave their snapshot and apply phases against one Orchestrator.
* A commit whose diff moved between compare and commit **aborts before the
  first write**. The engine used to recompute the diff and carry on, which
  quietly turned another operator's concurrent change into part of this
  operator's changeset. Absorbing that movement is now an explicit choice
  (``--rebase``), because the safe default when the world changed underneath
  a plan is to stop and show the operator what moved.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan import watchdog as _watchdog
from pyecsdwan.candidate import CandidateItem, CandidateStore, materialize_desired
from pyecsdwan.contract import (
    CanonicalState,
    Ctx,
    Diff,
    JobOutcome,
    Owned,
    Ownership,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Tier,
)
from pyecsdwan.journal import TxnJournal, TxnState, committed_history, orphaned_txns, prune_history
from pyecsdwan.locking import DEFAULT_TIMEOUT, HostLock
from pyecsdwan.registry import Registry


class CommitError(Exception):
    """Commit refused or failed; message is operator-facing."""


#: The detached watchdog's revert waits far longer for the commit lock than an
#: interactive command does. A watchdog that gave up because another commit was
#: in flight would leave an unconfirmed change applied — exactly the outcome the
#: confirm window exists to prevent. Waiting is the safe direction; the bound
#: only stops a wedged process from waiting forever.
REVERT_LOCK_TIMEOUT = 900.0


@dataclasses.dataclass
class PlanItem:
    ref: Ref
    resource: Resource
    delete: bool
    current_raw: RawState
    current: CanonicalState
    desired: CanonicalState
    diff: Diff
    #: Whether a template push would revert this write, as a tri-state (#20).
    #: Defaults to UNKNOWN rather than "unowned" so an item built without a
    #: check is refused rather than waved through — the guard's job is to be
    #: told, not to assume.
    ownership: Ownership = dataclasses.field(
        default_factory=lambda: Ownership.unknown("ownership was never checked")
    )
    #: The staged intent this item came from. ``--rebase`` needs it to
    #: re-materialize desired state against what the server holds *now*;
    #: without it a rebase would re-apply a desired state computed from the
    #: pre-drift world and silently drop the concurrent change it merged over.
    candidate_item: CandidateItem | None = None

    @property
    def changed(self) -> bool:
        return not self.diff.empty


@dataclasses.dataclass
class Plan:
    items: list[PlanItem]
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def changed_items(self) -> list[PlanItem]:
        return [i for i in self.items if i.changed]

    @property
    def empty(self) -> bool:
        return not self.changed_items


@dataclasses.dataclass
class CommitReport:
    ok: bool
    txn_id: str | None = None
    state: str = ""
    applied: list[str] = dataclasses.field(default_factory=list)
    reverted: list[str] = dataclasses.field(default_factory=list)
    messages: list[str] = dataclasses.field(default_factory=list)
    confirm_deadline: str | None = None
    #: Every JobOutcome returned by an apply()/rollback() this commit ran,
    #: in order. Group pushes populate `.per_appliance` — the CLI renders a
    #: per-appliance breakdown for any job that carries it (issue #22).
    jobs: list[JobOutcome] = dataclasses.field(default_factory=list)


def build_plan(ctx: Ctx, registry: Registry, candidate: CandidateStore) -> Plan:
    """Fetch + normalize + diff every candidate item; detect ownership."""
    warnings: list[str] = []
    refs = [item.ref for item in candidate.ordered_items()]
    deletes = {i.ref_key for i in candidate.ordered_items() if i.mode == "delete"}
    ordered = registry.order_refs(refs, deletes=deletes)
    by_key = {i.ref_key: i for i in candidate.ordered_items()}

    items: list[PlanItem] = []
    for ref in ordered:
        cand = by_key[ref.key()]
        resource = registry.get(ref.kind)
        if cand.mode == "delete" and not resource.deletable:
            raise CommitError(
                f"{ref.kind} is a singleton and cannot be deleted as a whole; "
                f"delete individual entries instead (e.g. "
                f"`delete {ref.kind} {ref.name} wan <label-id>`)"
            )
        current_raw = resource.fetch(ctx, ref)
        current = resource.normalize(current_raw)
        desired_input = candidate.desired_for(cand, current)
        desired: CanonicalState
        if desired_input is None:
            desired = None
        else:
            desired = resource.canonicalize_desired(ctx, ref, desired_input)
        diff = resource.diff(ref, current, desired)
        # Only for items that actually change something: managed_by() costs two
        # API round trips per item, and a no-op item cannot be reverted by a
        # template push because there is nothing to revert. An unchanged item
        # keeps the UNOWNED default rather than the UNKNOWN one, so it never
        # trips the guard on a check that was deliberately skipped.
        ownership = (
            resource.managed_by(ctx, ref)
            if diff.entries
            else Ownership.unowned("no change staged for this instance")
        )
        if ownership.blocks_write:
            warnings.append(
                f"{ref}: {ownership.label} — direct change requires --override-template"
            )
        if resource.tier < Tier.CURATED:
            warnings.append(
                f"{ref}: tier-{int(resource.tier)} resource — best-effort snapshot only, "
                f"no full transactional guarantees"
            )
        items.append(
            PlanItem(
                ref=ref,
                resource=resource,
                delete=cand.mode == "delete",
                current_raw=current_raw,
                current=current,
                desired=desired,
                diff=diff,
                ownership=ownership,
                candidate_item=cand,
            )
        )
    return Plan(items=items, warnings=warnings)


def commit(
    ctx: Ctx,
    registry: Registry,
    plan: Plan,
    settings: config.Settings,
    confirm_minutes: float | None = None,
    force: bool = False,
    override_template: bool = False,
    allow_untransactional: bool = False,
    journal_root: Path | None = None,
    rebase: bool = False,
    lock_root: Path | None = None,
    lock_timeout: float = DEFAULT_TIMEOUT,
) -> CommitReport:
    changed = plan.changed_items
    if not changed:
        return CommitReport(ok=True, state="NO_CHANGES", messages=["no changes"])

    _guard(changed, settings, confirm_minutes, force, override_template, allow_untransactional)

    # Held across snapshot, apply, verify and any auto-revert: a second commit
    # that interleaved with this one could snapshot state this commit is
    # midway through writing, and then "restore" it later.
    with HostLock(
        settings.host, "commit", root=lock_root, timeout=lock_timeout
    ):
        return _commit_locked(
            ctx,
            changed,
            settings,
            confirm_minutes,
            journal_root,
            rebase,
            override_template,
        )


def _commit_locked(
    ctx: Ctx,
    changed: list[PlanItem],
    settings: config.Settings,
    confirm_minutes: float | None,
    journal_root: Path | None,
    rebase: bool,
    override_template: bool,
) -> CommitReport:
    journal = TxnJournal.create(
        settings.host, [i.ref for i in changed], root=journal_root
    )
    report = CommitReport(ok=False, txn_id=journal.meta.txn_id)

    # Snapshot-before-write: re-fetch every resource at commit time so the
    # journal holds true pre-change state, then recompute each diff against
    # that fresh state so we apply exactly what we snapshot.
    stale: list[str] = []
    newly_blocked: list[tuple[str, Ownership]] = []
    work: list[PlanItem] = []
    for item in changed:
        fresh_raw = item.resource.fetch(ctx, item.ref)
        journal.record_snapshot(item.ref, fresh_raw)
        fresh_current = item.resource.normalize(fresh_raw)
        # A rebase re-merges the staged *intent* over what the server holds
        # now. Re-diffing the old desired state against fresh state would not
        # be a rebase: for a `set` (merge-mode) item the desired state was
        # materialized over the pre-drift world, so re-applying it would
        # delete whatever the concurrent writer added — the same lost update,
        # arrived at by a different route.
        fresh_desired = item.desired
        if rebase and item.candidate_item is not None:
            desired_input = materialize_desired(item.candidate_item, fresh_current)
            fresh_desired = (
                None
                if desired_input is None
                else item.resource.canonicalize_desired(ctx, item.ref, desired_input)
            )
        fresh_diff = item.resource.diff(item.ref, fresh_current, fresh_desired)
        if fresh_diff.entries != item.diff.entries:
            stale.append(item.ref.key())
        if fresh_diff.empty:
            continue
        # Ownership is re-read here, not reused from the plan, because the
        # plan-time answer is a fact about a moment that has passed. Between
        # compare and commit an operator can associate a template group, or
        # select a section in one already associated, and the plan would carry
        # a stale "unowned" straight through the guard into a write the next
        # push reverts. Skipped when overriding: the operator already said they
        # accept the risk, and this costs two round trips per item.
        fresh_ownership = item.ownership
        if not override_template:
            fresh_ownership = item.resource.managed_by(ctx, item.ref)
            if fresh_ownership.blocks_write:
                newly_blocked.append((item.ref.key(), fresh_ownership))
        work.append(
            dataclasses.replace(
                item,
                current_raw=fresh_raw,
                current=fresh_current,
                desired=fresh_desired,
                diff=fresh_diff,
                ownership=fresh_ownership,
            )
        )
    if newly_blocked:
        # Before the first write, like the drift abort below it, and for the
        # same reason: a guard that fires after a partial apply is not a guard.
        journal.append("OWNERSHIP_ABORT", refs=[key for key, _ in newly_blocked])
        journal.set_state(TxnState.AUDIT_ONLY, note="template ownership changed since compare")
        report.state = "OWNERSHIP"
        report.messages.append(
            "refusing: template ownership changed since compare for: "
            + "; ".join(f"{key} — {own.label}" for key, own in newly_blocked)
            + ". A template push would now revert these changes (or may — see above). "
            "Re-run `compare`, or `commit --override-template` to proceed anyway."
        )
        return report
    if stale and not rebase:
        # Before the first write, and it stays that way: this block must not
        # move below the apply loop.
        journal.append("DRIFT_ABORT", refs=stale)
        journal.set_state(TxnState.AUDIT_ONLY, note="server state moved since compare")
        report.state = "DRIFT"
        report.messages.append(
            f"refusing: server state moved since compare for: {', '.join(stale)}. "
            f"Another operator, a template push, or the Orchestrator UI changed these "
            f"between plan and commit. Re-run `compare` to see the current diff, or "
            f"`commit --rebase` to apply against the state just read."
        )
        return report
    if stale:
        report.messages.append(
            f"--rebase: server state moved since compare for: {', '.join(stale)} "
            f"(diff recomputed against current state)"
        )
        journal.append("DRIFT_REBASED", refs=stale)
    if not work:
        # Record as AUDIT_ONLY, never CONFIRMED: a no-op commit must not enter
        # the rollback history and shift every `rollback <n>` index by one.
        journal.set_state(TxnState.AUDIT_ONLY, note="no changes at commit time")
        report.ok = True
        report.state = "NO_CHANGES"
        report.messages.append("no changes (server already matches)")
        return report

    journal.set_state(TxnState.APPLYING)
    applied: list[PlanItem] = []
    failure: str | None = None
    failed_item: PlanItem | None = None
    for item in work:
        journal.append(
            "APPLY_START",
            ref=item.ref.key(),
            delete=item.delete,
            entries=len(item.diff),
            reversibility=item.resource.reversibility.value,
            ownership=item.ownership.state.value,
            owner=item.ownership.owner,
            ownership_reason=item.ownership.reason,
        )
        try:
            result = item.resource.apply(ctx, item.diff)
        except Exception as exc:  # noqa: BLE001 - a plugin/API failure mid-changeset must trigger auto-revert, not propagate
            failure = f"{item.ref}: apply raised {type(exc).__name__}: {exc}"
            failed_item = item
            journal.append("APPLY_RESULT", ref=item.ref.key(), ok=False, error=str(exc))
            break
        journal.append(
            "APPLY_RESULT",
            ref=item.ref.key(),
            ok=result.ok,
            message=result.message,
            jobs=[dataclasses.asdict(j) for j in result.jobs],
        )
        report.jobs.extend(result.jobs)
        if not result.ok:
            failure = f"{item.ref}: {result.message or 'apply failed'}"
            failed_item = item
            break
        if not item.resource.verify(ctx, item.ref, item.desired):
            failure = f"{item.ref}: post-apply verify found drift from desired state"
            failed_item = item
            journal.append("VERIFY_FAILED", ref=item.ref.key())
            break
        journal.append("VERIFIED", ref=item.ref.key())
        applied.append(item)
        report.applied.append(item.ref.key())

    if failure is not None:
        report.messages.append(f"FAILED: {failure}")
        # The failed step's write may have partially landed; revert it too,
        # then the successfully applied steps in reverse order.
        to_revert = ([failed_item] if failed_item is not None else []) + list(reversed(applied))
        _revert_items(ctx, journal, to_revert, report)
        return report

    if confirm_minutes is not None:
        deadline = _dt.datetime.now(tz=_dt.timezone.utc) + _dt.timedelta(minutes=confirm_minutes)
        journal.set_confirm_deadline(deadline)
        journal.set_state(TxnState.APPLIED_UNCONFIRMED)
        try:
            pid = _watchdog.arm(journal.dir, settings)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            # No watchdog = no safety net. Honor confirm semantics by
            # reverting immediately rather than leaving an unprotected commit.
            report.messages.append(f"watchdog failed to arm ({exc}); auto-reverting")
            _revert_items(ctx, journal, list(reversed(applied)), report)
            return report
        report.ok = True
        report.state = TxnState.APPLIED_UNCONFIRMED
        report.confirm_deadline = deadline.isoformat()
        report.messages.append(
            f"commit confirmed will be rolled back in {confirm_minutes} minute(s) "
            f"unless confirmed (watchdog pid {pid})"
        )
        return report

    journal.set_state(TxnState.CONFIRMED)
    prune_history(settings.rollback_history, root=journal_root, host=settings.host)
    report.ok = True
    report.state = TxnState.CONFIRMED
    report.messages.append(f"commit complete: {len(applied)} change(s) applied")
    return report


def _guard(
    changed: list[PlanItem],
    settings: config.Settings,
    confirm_minutes: float | None,
    force: bool,
    override_template: bool,
    allow_untransactional: bool,
) -> None:
    # `blocks_write`, not `state is OWNED`: UNKNOWN refuses on exactly the same
    # footing (#20). Reading the state directly here is how the fail-open comes
    # back, so the two lists below are split only to word the message.
    blocked = [i for i in changed if i.ownership.blocks_write]
    if blocked and not override_template:
        owned = [i for i in blocked if i.ownership.state is Owned.OWNED]
        unknown = [i for i in blocked if i.ownership.state is Owned.UNKNOWN]
        parts: list[str] = []
        if owned:
            parts.append(
                "template-managed: "
                + ", ".join(f"{i.ref} ({i.ownership.owner})" for i in owned)
                + ". The next template push would silently revert these changes"
            )
        if unknown:
            parts.append(
                "ownership unknown: "
                + "; ".join(f"{i.ref} — {i.ownership.reason}" for i in unknown)
                + ". Refusing rather than assuming nothing owns them"
            )
        raise CommitError(
            "refusing: " + ". ".join(parts) + ". Use --override-template to proceed anyway."
        )
    irreversible = [i for i in changed if i.resource.reversibility is Reversibility.IRREVERSIBLE]
    if irreversible:
        names = ", ".join(i.ref.key() for i in irreversible)
        if confirm_minutes is not None:
            raise CommitError(
                f"refusing commit confirm: {names} is IRREVERSIBLE — a confirm "
                f"window would be fake safety. Commit without confirm and --force."
            )
        if not force:
            raise CommitError(
                f"refusing: {names} is IRREVERSIBLE (no rollback exists). "
                f"Re-run with --force to proceed."
            )
    low_tier = [i for i in changed if i.resource.tier < Tier.CURATED]
    if low_tier and confirm_minutes is not None and not allow_untransactional:
        names = ", ".join(f"{i.ref.key()} (tier-{int(i.resource.tier)})" for i in low_tier)
        raise CommitError(
            f"refusing commit confirm: {names} lack curated rollback support; "
            f"mixing them in downgrades the whole transaction's guarantees. "
            f"Use --allow-untransactional to accept best-effort snapshots."
        )
    if confirm_minutes is not None and not settings.api_key:
        raise CommitError(
            "commit confirm requires API-key authentication: the background "
            "watchdog cannot replay an interactive login session."
        )


def _revert_items(
    ctx: Ctx, journal: TxnJournal, items: list[PlanItem], report: CommitReport
) -> None:
    journal.set_state(TxnState.REVERTING)
    snapshots = journal.snapshots()
    revert_failures: list[str] = []
    for item in items:
        snap = snapshots.get(item.ref.key())
        journal.append("REVERT_START", ref=item.ref.key())
        try:
            result = item.resource.rollback(ctx, item.ref, snap)
            ok = result.ok
            detail = result.message
            report.jobs.extend(result.jobs)
        except Exception as exc:  # noqa: BLE001 - collect every revert failure; the report must state exactly what is left un-reverted
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        journal.append("REVERT_RESULT", ref=item.ref.key(), ok=ok, message=detail)
        if ok:
            report.reverted.append(item.ref.key())
        else:
            revert_failures.append(f"{item.ref}: {detail}")
    if revert_failures:
        journal.set_state(TxnState.REVERT_FAILED)
        report.state = TxnState.REVERT_FAILED
        report.messages.append(
            "REVERT INCOMPLETE — manual intervention required: " + "; ".join(revert_failures)
        )
        report.messages.append(
            f"fabric state: {len(report.reverted)} change(s) reverted, "
            f"{len(revert_failures)} left in a modified state (see journal {journal.meta.txn_id})"
        )
    else:
        journal.set_state(TxnState.REVERTED)
        report.state = TxnState.REVERTED
        if report.reverted:
            report.messages.append(
                f"fabric restored to pre-commit snapshot "
                f"({len(report.reverted)} change(s) reverted)"
            )


def confirm_pending(
    settings: config.Settings,
    journal_root: Path | None = None,
    txn_id: str | None = None,
    lock_root: Path | None = None,
    lock_timeout: float = DEFAULT_TIMEOUT,
) -> CommitReport:
    """Bare ``commit`` inside a confirm window: claim the decision, write the
    marker, stop the watchdog. Scoped to ``settings.host`` so a confirm against
    one Orchestrator can never finalize an unconfirmed change on another."""

    with HostLock(settings.host, "commit", root=lock_root, timeout=lock_timeout):
        return _confirm_locked(settings, journal_root, txn_id)


def _confirm_locked(
    settings: config.Settings, journal_root: Path | None, txn_id: str | None
) -> CommitReport:
    from pyecsdwan.journal import list_txns

    candidates = [
        t
        for t in list_txns(journal_root)
        if t.meta.state == TxnState.APPLIED_UNCONFIRMED
        and t.meta.orch_host == settings.host
        and (txn_id is None or t.meta.txn_id == txn_id)
    ]
    if not candidates:
        return CommitReport(ok=False, state="NONE", messages=["no unconfirmed transaction found"])
    txn = candidates[0]
    # Win the decision atomically; if the watchdog already claimed 'revert',
    # the confirm loses and we report that rather than a false success.
    if txn.try_claim("confirm") != "confirm":
        return CommitReport(
            ok=False,
            txn_id=txn.meta.txn_id,
            state="NONE",
            messages=[f"transaction {txn.meta.txn_id} is already being rolled back"],
        )
    txn.write_confirm_marker()
    txn.append("CONFIRM")
    pid = txn.watchdog_pid()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Re-open and compare-and-swap on state: never clobber a REVERTED/REVERTING
    # record the watchdog may have written from a race we didn't win.
    fresh = TxnJournal.open(txn.dir)
    if fresh.meta.state not in (TxnState.APPLIED_UNCONFIRMED, TxnState.CONFIRMED):
        return CommitReport(
            ok=False,
            txn_id=txn.meta.txn_id,
            state=fresh.meta.state,
            messages=[f"transaction {txn.meta.txn_id} was already {fresh.meta.state}"],
        )
    fresh.set_state(TxnState.CONFIRMED)
    prune_history(settings.rollback_history, root=journal_root, host=settings.host)
    return CommitReport(
        ok=True,
        txn_id=txn.meta.txn_id,
        state=TxnState.CONFIRMED,
        messages=[f"transaction {txn.meta.txn_id} confirmed"],
    )


def revert_txn_dir(
    txn_dir: Path,
    reason: str,
    ctx: Ctx | None = None,
    registry: Registry | None = None,
    lock_root: Path | None = None,
    lock_timeout: float = REVERT_LOCK_TIMEOUT,
) -> CommitReport:
    """Restore a transaction's snapshots (watchdog expiry, orphan recovery).

    Builds a fresh client from the environment when no ctx is given — this is
    the path the detached watchdog uses after the CLI is long gone.

    Takes the same host-scoped commit lock as ``commit``, so a revert cannot
    interleave with a commit already in flight against that Orchestrator. It
    waits much longer for it than an interactive command would: this is the
    safety net firing, and abandoning it would leave the unconfirmed change
    applied.
    """
    journal = TxnJournal.open(txn_dir)
    host = journal.meta.orch_host
    with HostLock(host, "commit", root=lock_root, timeout=lock_timeout, txn_id=journal.meta.txn_id):
        return _revert_txn_dir_locked(journal, reason, ctx, registry)


def _revert_txn_dir_locked(
    journal: TxnJournal,
    reason: str,
    ctx: Ctx | None,
    registry: Registry | None,
) -> CommitReport:
    if ctx is None or registry is None:
        from pyecsdwan.runtime import bootstrap

        ctx, registry, _settings = bootstrap()
    # Never restore one Orchestrator's snapshot into another.
    client_host = getattr(getattr(ctx.client, "settings", None), "host", None)
    if client_host is not None and journal.meta.orch_host != client_host:
        return CommitReport(
            ok=False,
            txn_id=journal.meta.txn_id,
            state="NONE",
            messages=[
                f"refusing: transaction targets Orchestrator {journal.meta.orch_host!r} "
                f"but the session is connected to {client_host!r}"
            ],
        )
    applied = journal.applied_refs()
    if not applied:
        # A journal with zero APPLY_START events (e.g. an interrupted Tier-0
        # `api` call, or a snapshot-phase crash) has nothing to restore.
        # Marking it REVERTED would be a lie about the fabric — close it out
        # as AUDIT_ONLY instead.
        journal.append("REVERT_SKIPPED", reason="no applied changes to revert")
        journal.set_state(TxnState.AUDIT_ONLY)
        return CommitReport(
            ok=True,
            txn_id=journal.meta.txn_id,
            state=TxnState.AUDIT_ONLY,
            messages=["nothing to revert (no changes were applied)"],
        )
    journal.append("REVERT_TRIGGERED", reason=reason)
    report = CommitReport(ok=False, txn_id=journal.meta.txn_id)
    snapshots = journal.snapshots()
    items: list[PlanItem] = []
    for key in reversed(applied):
        ref = Ref.from_key(key)
        resource = registry.get(ref.kind)
        items.append(
            PlanItem(
                ref=ref,
                resource=resource,
                delete=False,
                current_raw=None,
                current=None,
                desired=None,
                diff=Diff(ref=ref),
            )
        )
    _revert_items(ctx, journal, items, report)
    report.ok = report.state == TxnState.REVERTED
    _ = snapshots  # snapshots are read inside _revert_items via the journal
    return report


def rollback_history_txn(
    ctx: Ctx,
    registry: Registry,
    settings: config.Settings,
    n: int = 1,
    journal_root: Path | None = None,
    lock_root: Path | None = None,
    lock_timeout: float = DEFAULT_TIMEOUT,
) -> CommitReport:
    """Junos-style ``rollback <n>``: restore the nth prior confirmed
    transaction's pre-change snapshots, journaled as a new transaction.

    Under the same commit lock as everything else that writes to the fabric —
    a rollback racing a commit would snapshot and restore half of it."""
    with HostLock(settings.host, "commit", root=lock_root, timeout=lock_timeout):
        return _rollback_locked(ctx, registry, settings, n, journal_root)


def _rollback_locked(
    ctx: Ctx,
    registry: Registry,
    settings: config.Settings,
    n: int,
    journal_root: Path | None,
) -> CommitReport:
    history = committed_history(journal_root, host=settings.host)
    if n < 1 or n > len(history):
        return CommitReport(
            ok=False,
            state="NONE",
            messages=[f"no such rollback point {n}; history depth is {len(history)}"],
        )
    source = history[n - 1]
    applied = source.applied_refs()
    if not applied:
        return CommitReport(
            ok=False,
            state="NONE",
            messages=[f"transaction {source.meta.txn_id} applied no changes; nothing to roll back"],
        )
    snapshots = source.snapshots()

    refs = [Ref.from_key(k) for k in applied]
    journal = TxnJournal.create(settings.host, refs, root=journal_root)
    journal.append("ROLLBACK_OF", source_txn=source.meta.txn_id, n=n)
    report = CommitReport(ok=False, txn_id=journal.meta.txn_id)

    # Snapshot current state first, so this rollback is itself revertible.
    for ref in refs:
        resource = registry.get(ref.kind)
        journal.record_snapshot(ref, resource.fetch(ctx, ref))

    journal.set_state(TxnState.APPLYING)
    failures: list[str] = []
    for ref in reversed(refs):
        resource = registry.get(ref.kind)
        journal.append("APPLY_START", ref=ref.key(), rollback_restore=True)
        try:
            result = resource.rollback(ctx, ref, snapshots.get(ref.key()))
            ok, detail = result.ok, result.message
        except Exception as exc:  # noqa: BLE001 - report every restore failure rather than abort the rest of the rollback
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        journal.append("APPLY_RESULT", ref=ref.key(), ok=ok, message=detail)
        if ok:
            report.applied.append(ref.key())
        else:
            failures.append(f"{ref}: {detail}")
    if failures:
        journal.set_state(TxnState.REVERT_FAILED)
        report.state = TxnState.REVERT_FAILED
        report.messages.append("rollback incomplete: " + "; ".join(failures))
    else:
        journal.set_state(TxnState.CONFIRMED)
        report.ok = True
        report.state = TxnState.CONFIRMED
        report.messages.append(
            f"restored {len(report.applied)} resource(s) from transaction {source.meta.txn_id}"
        )
        prune_history(settings.rollback_history, root=journal_root, host=settings.host)
    return report


def pending_rollbacks(
    journal_root: Path | None = None, host: str | None = None
) -> list[TxnJournal]:
    """Orphaned unconfirmed transactions (CLI/watchdog died) for
    ``rollback --pending`` and the startup scan, scoped to ``host`` so
    recovery never touches another Orchestrator's transactions."""
    return orphaned_txns(journal_root, host=host)


def format_report(report: CommitReport) -> str:
    lines: list[str] = []
    for message in report.messages:
        lines.append(message)
    if report.txn_id:
        lines.append(f"transaction: {report.txn_id} [{report.state}]")
    return "\n".join(lines)


__all__ = [
    "CommitError",
    "CommitReport",
    "Plan",
    "PlanItem",
    "build_plan",
    "commit",
    "confirm_pending",
    "format_report",
    "pending_rollbacks",
    "revert_txn_dir",
    "rollback_history_txn",
]


def _unused_any_guard(value: Any) -> Any:  # pragma: no cover
    return value
