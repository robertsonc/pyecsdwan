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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan import journal as _journal
from pyecsdwan import watchdog as _watchdog
from pyecsdwan.candidate import CandidateItem, CandidateStore, IntentSource, materialize_desired
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
from pyecsdwan.journal import (
    TxnJournal,
    TxnState,
    authorizes,
    committed_history,
    is_legacy,
    orphaned_txns,
    prune_history,
    recovery_blocker,
    targets,
)
from pyecsdwan.locking import DEFAULT_TIMEOUT, HostLock
from pyecsdwan.registry import Registry


class CommitError(Exception):
    """Commit refused or failed; message is operator-facing.

    ``collisions`` carries the shared-write-target conflicts structurally when
    that is why the commit refused (#69). The message says the same thing in
    prose, and prose is what an operator reads — but "conflicts are found
    before the first API write" is worth nothing to a pipeline that can only
    regex an error string, so the objects travel with it. Rendering them into
    a command's JSON output is T12's, which owns the shared result schema;
    this is the data that surface will carry.
    """

    def __init__(self, message: str, collisions: tuple[Collision, ...] = ()) -> None:
        super().__init__(message)
        self.collisions = collisions


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
    #: The server object this item replaces, or None when it shares none
    #: (#69). Resolved once at plan time: unlike ownership it is a function of
    #: the ref and the resource, not of server state, so it cannot go stale
    #: between compare and commit.
    write_target: str | None = None
    #: The staged intent this item came from. ``--rebase`` needs it to
    #: re-materialize desired state against what the server holds *now*;
    #: without it a rebase would re-apply a desired state computed from the
    #: pre-drift world and silently drop the concurrent change it merged over.
    candidate_item: CandidateItem | None = None

    @property
    def changed(self) -> bool:
        return not self.diff.empty


@dataclasses.dataclass(frozen=True)
class Collision:
    """Two or more changed items that replace the same server object (#69)."""

    target: str
    refs: tuple[str, ...]

    def __str__(self) -> str:
        return f"{self.target}: {', '.join(self.refs)}"

    def as_json(self) -> dict[str, Any]:
        """The stable machine-readable form (#69).

        Both fields, always: the target alone does not say what conflicts, and
        the refs alone do not say what they conflict *over*. ``refs`` is a list
        and stays sorted by `_write_collisions`, so a consumer diffing two runs
        sees a real change rather than dict ordering.
        """
        return {"target": self.target, "refs": list(self.refs)}


@dataclasses.dataclass
class Plan:
    items: list[PlanItem]
    warnings: list[str] = dataclasses.field(default_factory=list)
    #: Shared write targets claimed by more than one changed item. Detected at
    #: plan time so `compare` shows them, refused at commit time so nothing is
    #: written first.
    collisions: list[Collision] = dataclasses.field(default_factory=list)

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


def build_plan(ctx: Ctx, registry: Registry, candidate: IntentSource) -> Plan:
    """Fetch + normalize + diff every staged item; detect ownership.

    ``candidate`` is any :class:`~pyecsdwan.candidate.IntentSource`: the
    candidate store, or a desired-state directory (epic #8). The plan, its
    guards and the commit that follows are identical either way — only where
    the intent came from differs, which is the whole reason declarative apply
    needs no second transaction engine.
    """
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
        # The diff goes with the question (spec 004 D1): "would a template
        # revert *this*" is answerable where "does a template govern this kind"
        # is not. Adding a BGP peer is local even on an appliance whose `bgp`
        # section is selected, because that template governs timers, not peers.
        ownership = (
            resource.managed_by(ctx, ref, diff)
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
                write_target=resource.write_target(ctx, ref) if diff.entries else None,
                candidate_item=cand,
            )
        )
    collisions = _write_collisions(items)
    for collision in collisions:
        warnings.append(
            f"shared write target {collision.target} — claimed by "
            f"{', '.join(collision.refs)}; commit will refuse"
        )
    return Plan(items=items, warnings=warnings, collisions=collisions)


def _write_collisions(items: list[PlanItem]) -> list[Collision]:
    """Changed items that replace the same server object (#69).

    Only changed items: an unchanged one issues no write, so it cannot
    overwrite anything — which is also why ``build_plan`` does not ask an
    unchanged item for a target it will never use.
    """
    by_target: dict[str, list[str]] = {}
    for item in items:
        if item.changed and item.write_target is not None:
            by_target.setdefault(item.write_target, []).append(item.ref.key())
    return [
        Collision(target=target, refs=tuple(refs))
        for target, refs in sorted(by_target.items())
        if len(refs) > 1
    ]


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

    # Named before the lock is taken, so the lock's owner record carries the
    # id. The orphan scan tells a commit that is *running* from one whose
    # driver died by matching that record's txn_id (#100); a lock taken in no
    # name matched nothing, so every live commit read as an orphan, and a
    # recovery queued behind it restored the window it had just opened.
    txn_id = _journal.new_txn_id()
    # Held across snapshot, apply, verify and any auto-revert: a second commit
    # that interleaved with this one could snapshot state this commit is
    # midway through writing, and then "restore" it later.
    with HostLock(
        settings.origin, "commit", root=lock_root, timeout=lock_timeout, txn_id=txn_id
    ):
        return _commit_locked(
            ctx,
            changed,
            settings,
            confirm_minutes,
            journal_root,
            rebase,
            override_template,
            txn_id,
        )


#: Transaction states that own the fabric until they resolve. A new commit
#: during either would be reverted out from under the operator: an
#: APPLIED_UNCONFIRMED window ends by restoring a snapshot taken *before* the
#: new commit, and a REVERTING transaction is mid-restore.
BLOCKING_STATES = frozenset({TxnState.APPLIED_UNCONFIRMED, TxnState.REVERTING})


@dataclasses.dataclass(frozen=True)
class StagedCommit:
    """What happened when the candidate was committed."""

    plan: Plan
    #: None when the plan was empty — nothing was attempted.
    report: CommitReport | None = None
    #: Ref keys another writer changed after this commit was planned, kept
    #: rather than acknowledged. The caller reports them.
    kept: tuple[str, ...] = ()


def _guard_unadopted_staging(candidate: CandidateStore) -> None:
    """Refuse "no changes" when there is staged work this build will not claim.

    An operator who staged twelve changes, upgraded, and is told there is
    nothing to commit concludes the work was lost and does it again. The
    staging is right there — it is only its *target* that is unknown, and that
    is a question with an answer the operator has and the file does not.

    Raised here rather than at each interface: the scriptable CLI and the
    shell both print "no changes" from their own code, and putting the check
    on the shared path is the same lesson as the acknowledgement above.
    """
    pending = candidate.legacy_pending()
    if not pending:
        return
    raise CommitError(
        f"nothing staged for {candidate.origin}, but {len(pending)} change(s) staged by "
        f"an older build are in {candidate.unadopted_legacy}. That file is keyed by a "
        f"hostname, which this Orchestrator's http:// and https:// endpoints — and every "
        f"tenant path under it — share, so this build will not assume the changes are "
        f"yours. If they are, run 'ec-cli adopt --candidate'; 'ec-cli adopt' on its own "
        f"lists them first."
    )


def commit_candidate(
    ctx: Ctx,
    registry: Registry,
    candidate: CandidateStore,
    settings: config.Settings,
    *,
    on_plan: Callable[[Plan], None] | None = None,
    confirm_minutes: float | None = None,
    force: bool = False,
    override_template: bool = False,
    allow_untransactional: bool = False,
    rebase: bool = False,
) -> StagedCommit:
    """Snapshot, plan, commit and acknowledge — the whole cycle, once.

    Exists because the two halves of this were duplicated across the
    scriptable CLI and the interactive shell, and only one of them was fixed
    when `clear()` was replaced by an acknowledgement (#63). The shell kept
    calling `clear()`, so the *primary* Junos-style interface went on deleting
    another shell's staged work while the scriptable path's tests stayed
    green.

    The snapshot has to be taken before the plan is built, and the
    acknowledgement has to use that snapshot. Leaving both to the caller means
    a third entry point gets one of them wrong; taking them together means it
    cannot.

    ``on_plan`` renders the plan between building and committing, because the
    two surfaces present it differently and neither should have to reimplement
    the ordering to do so.
    """
    # Before the plan, though `build_plan` neither reloads nor mutates the
    # store, so no test can currently tell the two orderings apart — the
    # mutation sweep confirmed that. Kept in this order because the snapshot
    # is what the acknowledgement is compared against, and the day something
    # here does re-read from disk, the correct order is already the one
    # written.
    staged = candidate.ordered_items()
    plan = build_plan(ctx, registry, candidate)
    if plan.empty:
        _guard_unadopted_staging(candidate)
        return StagedCommit(plan=plan)
    if on_plan is not None:
        on_plan(plan)
    report = commit(
        ctx,
        registry,
        plan,
        settings,
        confirm_minutes=confirm_minutes,
        force=force,
        override_template=override_template,
        allow_untransactional=allow_untransactional,
        rebase=rebase,
    )
    kept = tuple(candidate.clear_committed(staged)) if report.ok else ()
    return StagedCommit(plan=plan, report=report, kept=kept)


def _guard_no_pending_confirm(
    settings: config.Settings, journal_root: Path | None
) -> None:
    """Refuse a new transaction while one still owns the fabric (#100).

    This check used to live in the ``commit`` CLI command only, which made it
    a property of one entry point rather than of the engine. ``apply --from``
    called ``txn.commit`` directly and so did not have it, and neither would
    any library caller: a declarative apply during an active confirm window
    was accepted, wrote to the fabric, and was then silently erased when the
    first window expired and its watchdog restored a snapshot taken before the
    second write ever happened. No error, no journal entry saying why — the
    change is simply gone.

    Inside the commit lock and before ``TxnJournal.create``, so nothing is
    journaled and no API call is made for a transaction that cannot proceed.

    Deliberately not applied to ``revert_txn_dir`` / ``rollback_history_txn``:
    recovery is precisely what an operator needs *during* one of these states,
    and neither goes through ``commit()``.
    """
    from pyecsdwan.journal import list_txns

    blocking = [
        t
        for t in list_txns(journal_root)
        if targets(t, settings.origin) and t.meta.state in BLOCKING_STATES
    ]
    if not blocking:
        return
    first = blocking[0]
    raise CommitError(
        f"transaction {first.meta.txn_id} is {first.meta.state} on {settings.origin}; "
        f"refusing to start another that it would revert away. Run 'ec-cli confirm' "
        f"or 'ec-cli rollback --pending' first."
    )


def _commit_locked(
    ctx: Ctx,
    changed: list[PlanItem],
    settings: config.Settings,
    confirm_minutes: float | None,
    journal_root: Path | None,
    rebase: bool,
    override_template: bool,
    txn_id: str | None = None,
) -> CommitReport:
    _guard_no_pending_confirm(settings, journal_root)
    journal = TxnJournal.create(
        settings.origin, [i.ref for i in changed], root=journal_root, txn_id=txn_id
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
            # Re-checked under the lock with the same diff, so the answer is
            # about the same change the first check saw.
            fresh_ownership = item.resource.managed_by(ctx, item.ref, item.diff)
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
        try:
            verified = item.resource.verify(ctx, item.ref, item.desired)
        except Exception as exc:  # noqa: BLE001 - verify runs *after* a write landed; escaping here strands the fabric
            # #103. `apply` was wrapped and `verify` was not, so a read timeout
            # or an odd response while confirming the write propagated out of
            # commit() entirely: the caller got a raw exception, the fabric
            # kept the change, and the transaction sat in APPLYING with no
            # revert and no terminal state. An unverifiable write is a failed
            # one — the whole point of verify is that we do not get to assume.
            #
            # No `verified = False` here: `failure` and the `break` below carry
            # the outcome, and the mutation sweep proved the assignment was
            # never read — flipping it to True changed nothing.
            journal.append(
                "VERIFY_FAILED",
                ref=item.ref.key(),
                error=f"{type(exc).__name__}: {exc}",
            )
            failure = (
                f"{item.ref}: post-apply verify raised {type(exc).__name__}: {exc}; "
                f"the write cannot be confirmed"
            )
            failed_item = item
            break
        if not verified:
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
    prune_history(settings.rollback_history, root=journal_root, origin=settings.origin)
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
    # First, because it is the most destructive class of problem here and the
    # only one with no legitimate override: two writes to one object mean one
    # of the two changes is silently discarded, and there is no flag for
    # "discard my other change on purpose". Splitting the changeset into two
    # commits is the fix, and the message says so (#69).
    collisions = _write_collisions(changed)
    if collisions:
        raise CommitError(
            "refusing: two changes in this changeset replace the same server object — "
            + "; ".join(str(c) for c in collisions)
            + ". Whichever applies second overwrites the first (deployment posts the "
            "whole object it computed at plan time, so ordering does not save it). "
            "Commit them separately.",
            collisions=tuple(collisions),
        )

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
    _guard_confirm_auth(settings, confirm_minutes)


def _guard_confirm_auth(settings: config.Settings, confirm_minutes: float | None) -> None:
    """A confirm window needs a credential the detached watchdog can replay.

    Extracted from the guard chain so it can be tested directly: the message
    it produces is the one an operator reads at the moment their commit is
    refused, and it is worth more than a line inside a forty-line function.
    """
    if confirm_minutes is None or settings.api_key:
        return
    # Name the keyring failure when there was one. Without it this message
    # tells an operator who *did* store a key that they have no key, and they
    # go looking in the wrong place — re-storing it into the keyring that is
    # not opening.
    because = (
        f" The keyring was unreadable, so a stored key could not be used: "
        f"{settings.keyring_error}."
        if settings.keyring_error
        else ""
    )
    raise CommitError(
        "commit confirm requires API-key authentication: the background "
        "watchdog cannot replay an interactive login session." + because
    )


def _confirm_restored(
    ctx: Ctx, item: PlanItem, snap: RawState, detail: str
) -> tuple[bool, str]:
    """Re-read the resource and compare it to the snapshot it was restored from.

    Rollback used to be believed on its own word: ``result.ok`` from the
    resource plugin was the whole evidence, and a plugin that returned success
    without restoring anything produced "fabric restored to pre-commit
    snapshot" over a fabric that still held the change (#103). The report a
    transaction hands back is the operator's only account of what state the
    network is in — it cannot be a restatement of what the write path claimed.

    A snapshot of ``None`` means the resource did not exist before, so rollback
    deleted it. That case is left unconfirmed *and says so*: confirming a
    deletion means reading an absence, and a fetch that raises is not proof of
    absence — it is equally a timeout. Guessing there would be the same
    absence-of-evidence inference this function exists to remove.
    """
    if snap is None:
        note = "deletion not independently confirmed (absence is not readable)"
        return True, f"{detail}; {note}" if detail else note
    try:
        restored = item.resource.verify(ctx, item.ref, item.resource.normalize(snap))
    except Exception as exc:  # noqa: BLE001 - an unreadable resource is an unconfirmed restore, not a failed one to hide
        return False, (
            f"rollback reported success but the restore could not be confirmed: "
            f"{type(exc).__name__}: {exc}"
        )
    if not restored:
        return False, (
            "rollback reported success but the resource does not match its "
            "pre-change snapshot"
        )
    return True, detail


def _revert_items(
    ctx: Ctx, journal: TxnJournal, items: list[PlanItem], report: CommitReport
) -> None:
    journal.set_state(TxnState.REVERTING)
    snapshots = journal.snapshots()
    revert_failures: list[str] = []
    for item in items:
        if item.ref.key() not in snapshots:
            # Not the same as a snapshot recorded as absent (#110). Both used
            # to arrive here as None, and `rollback(ctx, ref, None)` means
            # "this did not exist before, remove it" — so a snapshot the
            # journal lost would make the revert *delete* a resource that was
            # there all along. Refuse loudly instead; the item stays modified
            # and the report says so.
            journal.append("REVERT_START", ref=item.ref.key())
            detail = (
                "no pre-change snapshot recorded in the journal, so there is "
                "nothing to restore from; left as-is rather than deleted"
            )
            journal.append("REVERT_RESULT", ref=item.ref.key(), ok=False, message=detail)
            revert_failures.append(f"{item.ref}: {detail}")
            continue
        snap = snapshots[item.ref.key()]
        journal.append("REVERT_START", ref=item.ref.key())
        try:
            result = item.resource.rollback(ctx, item.ref, snap)
            ok = result.ok
            detail = result.message
            report.jobs.extend(result.jobs)
        except Exception as exc:  # noqa: BLE001 - collect every revert failure; the report must state exactly what is left un-reverted
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        if ok:
            ok, detail = _confirm_restored(ctx, item, snap, detail)
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
    marker, stop the watchdog. Scoped to ``settings.origin`` so a confirm against
    one Orchestrator can never finalize an unconfirmed change on another."""

    with HostLock(
        settings.origin, "commit", root=lock_root, timeout=lock_timeout, txn_id=txn_id
    ):
        return _confirm_locked(settings, journal_root, txn_id)


def _confirm_locked(
    settings: config.Settings, journal_root: Path | None, txn_id: str | None
) -> CommitReport:
    from pyecsdwan.journal import list_txns

    unconfirmed = [
        t
        for t in list_txns(journal_root)
        if t.meta.state == TxnState.APPLIED_UNCONFIRMED
        and targets(t, settings.origin)
        and (txn_id is None or t.meta.txn_id == txn_id)
    ]
    # Confirming writes a marker, kills the watchdog and marks the transaction
    # CONFIRMED — it is what stops the fabric being put back. A hostname match
    # is not enough to authorize that against a journal that might belong to
    # another Orchestrator on the same name (#63).
    candidates = [t for t in unconfirmed if authorizes(t, settings.origin)]
    if not candidates:
        unproven = [t for t in unconfirmed if is_legacy(t)]
        if unproven:
            # Named rather than reported as "none found": an operator who can
            # see the transaction in `show journal` and is told it does not
            # exist goes looking in the wrong place.
            return CommitReport(
                ok=False,
                state="NONE",
                messages=[_unproven(unproven[0], "confirm")],
            )
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
    prune_history(settings.rollback_history, root=journal_root, origin=settings.origin)
    return CommitReport(
        ok=True,
        txn_id=txn.meta.txn_id,
        state=TxnState.CONFIRMED,
        messages=[f"transaction {txn.meta.txn_id} confirmed"],
    )


#: What to tell an operator holding a journal whose target cannot be proven.
ADOPT_HINT = (
    "Its target cannot be established from the file, and a hostname is not "
    "proof: the http:// and https:// endpoints on one name, and every tenant "
    "path under it, share it. If you know which Orchestrator it belongs to, "
    "connect to that one and run 'ec-cli adopt --txn {txn_id}'."
)


def _unproven(txn: TxnJournal, action: str) -> str:
    return (
        f"refusing to {action} transaction {txn.meta.txn_id}: it was written before "
        f"this build recorded which Orchestrator a transaction targets, and records "
        f"only the hostname {txn.meta.orch_host!r}. "
    ) + ADOPT_HINT.format(txn_id=txn.meta.txn_id)


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
    if is_legacy(journal):
        # Refused before any lock is taken, because none can be named: the
        # journal records no origin, and a guessed one is a *different* lock
        # from the one a live commit against the actual target holds — so the
        # guess would not even serialize against the thing it must (#63).
        return CommitReport(
            ok=False,
            txn_id=journal.meta.txn_id,
            state="NONE",
            messages=[_unproven(journal, "restore")],
        )
    origin = journal.meta.orch_origin
    with HostLock(
        origin, "commit", root=lock_root, timeout=lock_timeout, txn_id=journal.meta.txn_id
    ):
        # Re-open *inside* the lock. The journal above was read before the
        # wait, and waiting is exactly when it goes stale: recovery blocks on
        # the lock a live commit is holding, that commit finishes and marks
        # itself CONFIRMED, the lock is released, and this would then restore
        # snapshots over work that had just succeeded (#100). The compare is
        # against what is on disk now, not what was true when we queued.
        fresh = TxnJournal.open(txn_dir)
        if fresh.meta.state in TxnState.TERMINAL:
            return CommitReport(
                ok=False,
                txn_id=fresh.meta.txn_id,
                state=fresh.meta.state,
                messages=[
                    f"refusing recovery: transaction {fresh.meta.txn_id} reached "
                    f"{fresh.meta.state} while this recovery waited for the commit "
                    f"lock; it is settled and restoring it would undo a completed "
                    f"transaction"
                ],
            )
        # Terminal is not the only way a transaction stops being recoverable
        # while recovery waited. The commit it queued behind can finish into a
        # confirm window — APPLIED_UNCONFIRMED, watchdog armed, lock released
        # — and a scan that listed it as an orphan *before* that commit even
        # started would now restore over a window an operator is about to
        # confirm. Asked again here, from disk, with the lock held (#100).
        blocker = recovery_blocker(fresh, self_pid=os.getpid())
        if blocker is not None:
            return CommitReport(
                ok=False,
                txn_id=fresh.meta.txn_id,
                state=fresh.meta.state,
                messages=[
                    f"refusing recovery: transaction {fresh.meta.txn_id} is not "
                    f"orphaned — {blocker}"
                ],
            )
        # `fresh`, not the pre-lock `journal`. No test can tell them apart
        # today — the callee reads `applied_refs()` and `snapshots()` from
        # disk and touches `.meta` only for the host, which cannot change —
        # and the mutation sweep confirmed that. It is passed anyway so the
        # invariant holds structurally: everything past this point acts on
        # what was re-read under the lock, and the next field someone reads
        # from `.meta` is then correct by construction rather than by luck.
        return _revert_txn_dir_locked(fresh, reason, ctx, registry)


def _revert_txn_dir_locked(
    journal: TxnJournal,
    reason: str,
    ctx: Ctx | None,
    registry: Registry | None,
) -> CommitReport:
    if ctx is None or registry is None:
        from pyecsdwan.runtime import bootstrap

        ctx, registry, _settings = bootstrap()
    # Never restore one Orchestrator's snapshot into another. Compared on the
    # canonical origin: the display host collapses two tenants under one
    # hostname onto the same string, so this guard used to wave through the
    # single worst thing the tool can do (#63).
    client_origin = getattr(getattr(ctx.client, "settings", None), "origin", None)
    if client_origin is not None and not authorizes(journal, client_origin):
        # `authorizes`, not `targets`: a hostname match is enough to *list* a
        # transaction and not nearly enough to restore one, and this is the
        # path that writes another fabric's snapshots over live config.
        if is_legacy(journal):
            return CommitReport(
                ok=False,
                txn_id=journal.meta.txn_id,
                state="NONE",
                messages=[_unproven(journal, "restore")],
            )
        return CommitReport(
            ok=False,
            txn_id=journal.meta.txn_id,
            state="NONE",
            messages=[
                f"refusing: transaction targets Orchestrator "
                f"{journal.meta.orch_origin!r} but the session is connected to "
                f"{client_origin!r}"
            ],
        )
    caveats: list[str] = []
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
            messages=[*caveats, "nothing to revert (no changes were applied)"],
        )
    journal.append("REVERT_TRIGGERED", reason=reason)
    report = CommitReport(ok=False, txn_id=journal.meta.txn_id, messages=list(caveats))
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
    a rollback racing a commit would snapshot and restore half of it. Taken in
    the name of the rollback transaction for the same reason ``commit`` does:
    a rollback in flight is an APPLYING transaction another terminal's orphan
    scan would otherwise offer for recovery (#100)."""
    txn_id = _journal.new_txn_id()
    with HostLock(
        settings.origin, "commit", root=lock_root, timeout=lock_timeout, txn_id=txn_id
    ):
        return _rollback_locked(ctx, registry, settings, n, journal_root, txn_id)


def _rollback_locked(
    ctx: Ctx,
    registry: Registry,
    settings: config.Settings,
    n: int,
    journal_root: Path | None,
    txn_id: str | None = None,
) -> CommitReport:
    # `committed_history` lists by `targets()`: the broad, hostname-level match
    # for showing an operator what might be theirs. Restoring is a write into
    # this fabric and takes the exact match, as `confirm` and `rollback
    # --pending` already do — an unadopted pre-#63 journal on a shared
    # hostname is *listed* here and authorizes nothing, and this path used to
    # select it by number and write its snapshots into whichever tenant asked.
    listed = committed_history(journal_root, origin=settings.origin)
    history = [t for t in listed if authorizes(t, settings.origin)]
    unproven = [t for t in listed if is_legacy(t)]
    excluded = (
        f"{len(unproven)} confirmed transaction(s) written before this build recorded "
        f"targets are listed but not numbered until adopted: run "
        f"'ec-cli adopt --txn <id>' connected to the Orchestrator they belong to"
    )
    if n < 1 or n > len(history):
        note = f"no such rollback point {n}; history depth is {len(history)}"
        return CommitReport(
            ok=False,
            state="NONE",
            messages=[note, excluded] if unproven else [note],
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
    journal = TxnJournal.create(settings.origin, refs, root=journal_root, txn_id=txn_id)
    journal.append("ROLLBACK_OF", source_txn=source.meta.txn_id, n=n)
    report = CommitReport(ok=False, txn_id=journal.meta.txn_id)
    if unproven:
        # Said on success too: the numbering an operator counted in
        # `show journal` includes these, and the numbering here does not.
        report.messages.append(excluded)

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
        prune_history(settings.rollback_history, root=journal_root, origin=settings.origin)
    return report


def pending_rollbacks(
    journal_root: Path | None = None, origin: str | None = None
) -> list[TxnJournal]:
    """Orphaned unconfirmed transactions (CLI/watchdog died) for
    ``rollback --pending`` and the startup scan, scoped to ``origin`` so
    recovery never touches another Orchestrator's transactions."""
    return orphaned_txns(journal_root, origin=origin)


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
