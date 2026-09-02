"""Transaction engine tests: commit, guards, partial-failure revert, rollback
history, orphan recovery — against an in-memory fake resource (no HTTP)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    DiffEntry,
    DiffOp,
    Ownership,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Tier,
)
from pyecsdwan.journal import TxnJournal, TxnState, list_txns
from pyecsdwan.locking import LockBusy
from pyecsdwan.registry import Registry


class FakeServer:
    """Dict-backed 'orchestrator' shared by fake resources."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.write_count = 0


class FakeResource(Resource):
    kind = "fake"
    scope_name = "orchestrator"
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED

    def __init__(self, server: FakeServer, kind: str = "fake",
                 dependencies: tuple[str, ...] = ()) -> None:
        self.kind = kind
        self.dependencies = dependencies
        self.server = server
        self.fail_apply_for: set[str] = set()

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        value = self.server.store.get(ref.key())
        return copy.deepcopy(value) if value is not None else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        return {k: raw[k] for k in sorted(raw) if k != "serverGeneratedId"}

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.ref.key() in self.fail_apply_for:
            self.server.write_count += 1  # the failed write may have landed
            return ApplyResult(ok=False, message="injected failure")
        self.server.write_count += 1
        if diff.desired is None:
            self.server.store.pop(diff.ref.key(), None)
        else:
            assert isinstance(diff.desired, dict)
            self.server.store[diff.ref.key()] = copy.deepcopy(diff.desired)
        return ApplyResult(ok=True)

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        self.server.write_count += 1
        if snapshot is None:
            self.server.store.pop(ref.key(), None)
        else:
            assert isinstance(snapshot, dict)
            self.server.store[ref.key()] = copy.deepcopy(snapshot)
        return ApplyResult(ok=True, message="restored")


@pytest.fixture
def world(state_home: Any, settings: config.Settings) -> dict[str, Any]:
    server = FakeServer()
    registry = Registry()
    res_a = FakeResource(server, kind="alpha")
    res_b = FakeResource(server, kind="beta", dependencies=("alpha",))
    registry.register(res_a)
    registry.register(res_b)
    ctx = Ctx(client=None, resolver=None)  # type: ignore[arg-type]
    candidate = CandidateStore("orch.example.com")
    return {
        "server": server, "registry": registry, "ctx": ctx,
        "candidate": candidate, "settings": settings,
        "alpha": res_a, "beta": res_b,
    }


def _plan(world: dict[str, Any]) -> txn.Plan:
    return txn.build_plan(world["ctx"], world["registry"], world["candidate"])


def test_commit_applies_and_is_idempotent(world: dict[str, Any]) -> None:
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert report.ok and report.state == TxnState.CONFIRMED
    assert world["server"].store["alpha:one"] == {"speed": 10}

    # DoD #1: same command twice — second run reports no changes, zero writes.
    writes = world["server"].write_count
    report2 = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert report2.ok and report2.state == "NO_CHANGES"
    assert world["server"].write_count == writes


def test_dependency_ordering(world: dict[str, Any]) -> None:
    # beta depends on alpha; submit beta first — apply order must be alpha, beta.
    world["candidate"].set_path(Ref("beta", "b1"), ["v"], 1)
    world["candidate"].set_path(Ref("alpha", "a1"), ["v"], 2)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert report.applied == ["alpha:a1", "beta:b1"]


def test_partial_failure_reverts_to_snapshot(world: dict[str, Any]) -> None:
    # Pre-existing state for alpha:one; changeset touches alpha then beta;
    # beta fails -> everything back to pre-commit snapshot (DoD #5).
    world["server"].store["alpha:one"] = {"speed": 1}
    world["beta"].fail_apply_for.add("beta:two")
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 99)
    world["candidate"].set_path(Ref("beta", "two"), ["mtu"], 9000)

    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert not report.ok
    assert report.state == TxnState.REVERTED
    assert world["server"].store["alpha:one"] == {"speed": 1}
    assert "beta:two" not in world["server"].store
    assert "alpha:one" in report.reverted
    assert any("FAILED" in m for m in report.messages)

    journal = list_txns()[0]
    assert journal.meta.state == TxnState.REVERTED
    events = [e["event"] for e in journal.events()]
    assert "REVERT_START" in events and "REVERT_RESULT" in events


def test_verify_failure_triggers_revert(world: dict[str, Any]) -> None:
    class LyingResource(FakeResource):
        def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
            self.server.write_count += 1
            return ApplyResult(ok=True)  # claims success, writes nothing

    lying = LyingResource(world["server"], kind="liar")
    world["registry"].register(lying)
    world["candidate"].set_path(Ref("liar", "x"), ["a"], 1)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert not report.ok
    assert any("verify" in m for m in report.messages)


def test_irreversible_guards(world: dict[str, Any]) -> None:
    world["alpha"].reversibility = Reversibility.IRREVERSIBLE
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 1)
    plan = _plan(world)
    with pytest.raises(txn.CommitError, match="--force"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"])
    with pytest.raises(txn.CommitError, match="fake safety"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"], confirm_minutes=5)
    report = txn.commit(world["ctx"], world["registry"], plan, world["settings"], force=True)
    assert report.ok


def test_low_tier_refused_in_confirm_changeset(world: dict[str, Any],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    world["alpha"].tier = Tier.GENERATED
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 1)
    plan = _plan(world)
    assert any("tier-1" in w for w in plan.warnings)
    with pytest.raises(txn.CommitError, match="allow-untransactional"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"], confirm_minutes=5)
    monkeypatch.setattr("pyecsdwan.watchdog.arm", lambda *a, **k: 4_000_000)
    report = txn.commit(
        world["ctx"], world["registry"], plan, world["settings"],
        confirm_minutes=5, allow_untransactional=True,
    )
    assert report.ok and report.state == TxnState.APPLIED_UNCONFIRMED


def test_unknown_ownership_is_refused_just_as_owned_is(world: dict[str, Any]) -> None:
    """#20's whole point at the guard. Before this, a resource that could not
    determine ownership returned None and committed as freely as one that had
    checked and found nothing — so the two situations an operator most needs
    told apart were the same situation."""

    class OpaqueResource(FakeResource):
        def managed_by(self, ctx: Ctx, ref: Ref, diff: object = None) -> Ownership:
            return Ownership.unknown("template selection unreadable (403)")

    world["registry"].register(OpaqueResource(world["server"], kind="opaque"))
    world["candidate"].set_path(Ref("opaque", "x"), ["a"], 1)
    plan = _plan(world)
    assert any("ownership-unknown" in w for w in plan.warnings)

    with pytest.raises(txn.CommitError, match="ownership unknown"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"])
    # The reason travels to the operator: "refusing" without "403" leaves them
    # nothing to fix.
    with pytest.raises(txn.CommitError, match="403"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"])

    report = txn.commit(
        world["ctx"], world["registry"], plan, world["settings"], override_template=True
    )
    assert report.ok


def test_a_plan_item_built_without_an_ownership_check_is_refused(
    world: dict[str, Any],
) -> None:
    """The default on the dataclass is a guard in its own right, and the
    mutation sweep found nothing testing it: flipping it to UNOWNED left every
    other test green.

    `build_plan` always sets the field, so this is about the next caller —
    another code path, a test helper, a future bulk-change entry point — that
    constructs a PlanItem directly. Forgetting to check ownership must land on
    "refuse", never on "proceed".
    """
    ref = Ref("alpha", "x")
    item = txn.PlanItem(
        ref=ref,
        resource=world["registry"].get("alpha"),
        delete=False,
        current_raw=None,
        current=None,
        desired={"a": 1},
        diff=Diff(
            ref=ref,
            entries=[DiffEntry(DiffOp.ADD, ("a",), None, 1)],
            desired={"a": 1},
            current=None,
        ),
        # ownership deliberately not passed
    )
    assert item.ownership.blocks_write
    with pytest.raises(txn.CommitError, match="ownership unknown"):
        txn._guard([item], world["settings"], None, False, False, False)


def test_an_item_with_no_diff_does_not_trip_the_guard(world: dict[str, Any]) -> None:
    """Guards the guard above from being too eager. `managed_by()` is skipped
    for unchanged items — two round trips each — and the PlanItem default is
    UNKNOWN, so a careless skip would refuse commits over instances nobody
    asked to change."""

    class OpaqueResource(FakeResource):
        def managed_by(  # pragma: no cover
            self, ctx: Ctx, ref: Ref, diff: object = None
        ) -> Ownership:
            raise AssertionError("managed_by must not be called for an unchanged item")

    world["registry"].register(OpaqueResource(world["server"], kind="opaque2"))
    world["server"].store["opaque2:x"] = {"a": 1}
    world["candidate"].set_path(Ref("opaque2", "x"), ["a"], 1)  # already the server value
    plan = _plan(world)
    assert plan.empty
    for item in plan.items:
        assert not item.ownership.blocks_write


def test_ownership_is_rechecked_before_the_write(world: dict[str, Any]) -> None:
    """A plan-time answer is a fact about a moment that has passed (#20).

    Between compare and commit an operator can select a template section, and
    the plan would carry a stale "unowned" straight into a write the next push
    reverts. The re-read happens inside the commit lock, before the first
    apply — asserted here by counting writes, because a guard that fires after
    a partial apply is not a guard.
    """
    flips: dict[str, bool] = {"owned": False}

    class FlipResource(FakeResource):
        def managed_by(self, ctx: Ctx, ref: Ref, diff: object = None) -> Ownership:
            if flips["owned"]:
                return Ownership.owned("template-group Late-Arrival")
            flips["owned"] = True  # ... owned from the second call onward
            return Ownership.unowned("nothing selects it (yet)")

    world["registry"].register(FlipResource(world["server"], kind="flip"))
    world["candidate"].set_path(Ref("flip", "x"), ["a"], 1)
    plan = _plan(world)
    assert not plan.items[0].ownership.blocks_write  # the plan was clean

    writes = world["server"].write_count
    report = txn.commit(world["ctx"], world["registry"], plan, world["settings"])
    assert not report.ok
    assert report.state == "OWNERSHIP"
    assert "Late-Arrival" in " ".join(report.messages)
    assert world["server"].write_count == writes, "refused after writing"


def test_the_recheck_is_skipped_when_overriding(world: dict[str, Any]) -> None:
    """Guards the guard: the re-read costs two round trips per item, and an
    operator who passed --override-template has already accepted the risk, so
    paying for it again would be pure latency."""
    calls: list[str] = []

    class CountingResource(FakeResource):
        def managed_by(self, ctx: Ctx, ref: Ref, diff: object = None) -> Ownership:
            calls.append(ref.key())
            return Ownership.owned("template-group Branch-Std")

    world["registry"].register(CountingResource(world["server"], kind="counted"))
    world["candidate"].set_path(Ref("counted", "x"), ["a"], 1)
    plan = _plan(world)
    assert len(calls) == 1  # build_plan

    report = txn.commit(
        world["ctx"], world["registry"], plan, world["settings"], override_template=True
    )
    assert report.ok
    assert len(calls) == 1, "re-checked despite the override"


def test_the_journal_records_which_ownership_the_write_went_ahead_under(
    world: dict[str, Any],
) -> None:
    """An override is a decision, and a decision that leaves no trace cannot be
    audited afterwards — "why did this change get pushed over a template?" has
    to be answerable from the journal alone (#20)."""

    class OwnedResource(FakeResource):
        def managed_by(self, ctx: Ctx, ref: Ref, diff: object = None) -> Ownership:
            return Ownership.owned("template-group Branch-Std")

    world["registry"].register(OwnedResource(world["server"], kind="journaled"))
    world["candidate"].set_path(Ref("journaled", "x"), ["a"], 1)
    report = txn.commit(
        world["ctx"], world["registry"], _plan(world), world["settings"], override_template=True
    )
    assert report.ok
    journal = TxnJournal.open(list_txns()[0].dir)
    starts = [e for e in journal.events() if e.get("event") == "APPLY_START"]
    assert starts and starts[0]["ownership"] == "owned"
    assert starts[0]["owner"] == "template-group Branch-Std"


def test_ownership_refused_without_override(world: dict[str, Any]) -> None:
    class OwnedResource(FakeResource):
        def managed_by(self, ctx: Ctx, ref: Ref, diff: object = None) -> Ownership:
            return Ownership.owned("template-group Branch-Std")

    owned = OwnedResource(world["server"], kind="owned")
    world["registry"].register(owned)
    world["candidate"].set_path(Ref("owned", "x"), ["a"], 1)
    plan = _plan(world)
    assert any("managed-by" in w for w in plan.warnings)
    with pytest.raises(txn.CommitError, match="--override-template"):
        txn.commit(world["ctx"], world["registry"], plan, world["settings"])
    report = txn.commit(
        world["ctx"], world["registry"], plan, world["settings"], override_template=True
    )
    assert report.ok


def test_confirm_requires_api_key(world: dict[str, Any]) -> None:
    world["settings"].api_key = None
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 1)
    with pytest.raises(txn.CommitError, match="API-key"):
        txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"],
                   confirm_minutes=5)


def test_commit_confirm_then_confirm_pending(world: dict[str, Any],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyecsdwan.watchdog.arm", lambda *a, **k: 4_000_000)
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 7)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"],
                        confirm_minutes=10)
    assert report.ok and report.state == TxnState.APPLIED_UNCONFIRMED
    assert report.confirm_deadline is not None

    confirm = txn.confirm_pending(world["settings"])
    assert confirm.ok and confirm.state == TxnState.CONFIRMED
    journal = TxnJournal.open(list_txns()[0].dir)
    assert journal.meta.state == TxnState.CONFIRMED
    assert journal.confirm_marker.exists()
    # Nothing pending anymore.
    again = txn.confirm_pending(world["settings"])
    assert not again.ok


def test_watchdog_arm_failure_auto_reverts(world: dict[str, Any],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("no fork for you")

    monkeypatch.setattr("pyecsdwan.watchdog.arm", boom)
    world["server"].store["alpha:one"] = {"v": 1}
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 2)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"],
                        confirm_minutes=10)
    assert not report.ok
    assert world["server"].store["alpha:one"] == {"v": 1}
    assert report.state == TxnState.REVERTED


def test_rollback_history(world: dict[str, Any]) -> None:
    # change 1
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 1)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()
    # change 2
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 2)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()
    assert world["server"].store["alpha:one"] == {"v": 2}

    # rollback 1 = undo change 2
    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)
    assert report.ok
    assert world["server"].store["alpha:one"] == {"v": 1}
    # the rollback is itself a confirmed txn; rolling back 1 again undoes it
    report2 = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)
    assert report2.ok
    assert world["server"].store["alpha:one"] == {"v": 2}


def test_rollback_out_of_range(world: dict[str, Any]) -> None:
    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=3)
    assert not report.ok


def test_orphan_recovery(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyecsdwan.watchdog.arm", lambda *a, **k: 4_000_000)
    world["server"].store["alpha:one"] = {"v": "before"}
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], "after")
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"],
                        confirm_minutes=10)
    assert report.ok
    assert world["server"].store["alpha:one"] == {"v": "after"}

    # CLI and watchdog both die: pid 4000000 does not exist -> orphaned.
    pending = txn.pending_rollbacks()
    assert len(pending) == 1
    result = txn.revert_txn_dir(pending[0].dir, reason="test recovery",
                                ctx=world["ctx"], registry=world["registry"])
    assert result.ok
    assert world["server"].store["alpha:one"] == {"v": "before"}
    assert not txn.pending_rollbacks()


def test_delete_resource_roundtrip(world: dict[str, Any]) -> None:
    world["server"].store["alpha:gone"] = {"v": 1}
    world["candidate"].delete(Ref("alpha", "gone"))
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert report.ok
    assert "alpha:gone" not in world["server"].store
    # rollback restores the deleted object from its snapshot
    restore = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)
    assert restore.ok
    assert world["server"].store["alpha:gone"] == {"v": 1}


# -- concurrency and commit-time drift (issue #63) ---------------------------


def _external_writer_adds_mtu(world: dict[str, Any]) -> None:
    """Someone else — a UI operator, a template push, another CLI — writes to
    the same object between plan and commit."""
    world["server"].store["alpha:one"] = {"mtu": 9000}


def test_commit_aborts_when_server_state_moved_since_compare(world: dict[str, Any]) -> None:
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    plan = _plan(world)
    _external_writer_adds_mtu(world)

    report = txn.commit(world["ctx"], world["registry"], plan, world["settings"])

    assert not report.ok
    assert report.state == "DRIFT"
    assert "alpha:one" in " ".join(report.messages)
    assert "--rebase" in " ".join(report.messages)
    # The point of aborting: it happens before the first write, so the
    # concurrent writer's change is still there afterwards.
    assert world["server"].write_count == 0
    assert world["server"].store["alpha:one"] == {"mtu": 9000}

    journal = list_txns()[0]
    assert journal.meta.state == TxnState.AUDIT_ONLY
    assert "DRIFT_ABORT" in [e["event"] for e in journal.events()]


def test_commit_rebase_remerges_intent_over_the_concurrent_change(
    world: dict[str, Any],
) -> None:
    """``--rebase`` re-merges staged intent over what the server holds now.

    Re-diffing the plan-time desired state would delete ``mtu`` — the very
    lost update the drift check exists to prevent, arrived at by consent.
    """
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    plan = _plan(world)
    _external_writer_adds_mtu(world)

    report = txn.commit(world["ctx"], world["registry"], plan, world["settings"], rebase=True)

    assert report.ok and report.state == TxnState.CONFIRMED
    assert world["server"].store["alpha:one"] == {"mtu": 9000, "speed": 10}
    assert any("--rebase" in m for m in report.messages)
    assert "DRIFT_REBASED" in [e["event"] for e in list_txns()[0].events()]


def test_commit_proceeds_normally_when_nothing_moved(world: dict[str, Any]) -> None:
    # The drift check must not fire on an unchanged server, or every commit
    # would need --rebase and the flag would mean nothing.
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])
    assert report.ok and report.state == TxnState.CONFIRMED


def test_a_second_commit_cannot_interleave(world: dict[str, Any], lock_holder: Any) -> None:
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    plan = _plan(world)
    lock_holder(world["settings"].origin, "commit")
    with pytest.raises(LockBusy) as excinfo:
        txn.commit(world["ctx"], world["registry"], plan, world["settings"], lock_timeout=0.2)
    assert "commit lock" in str(excinfo.value)
    # Refused before writing anything, and before opening a journal.
    assert world["server"].write_count == 0
    assert list_txns() == []


def test_confirm_takes_the_commit_lock(world: dict[str, Any], lock_holder: Any) -> None:
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    report = txn.commit(
        world["ctx"], world["registry"], _plan(world), world["settings"], confirm_minutes=5
    )
    assert report.state == TxnState.APPLIED_UNCONFIRMED
    holder = lock_holder(world["settings"].origin, "commit")
    with pytest.raises(LockBusy):
        txn.confirm_pending(world["settings"], lock_timeout=0.2)
    holder.kill()
    holder.wait(timeout=10)
    # Still confirmable once the lock is free — refusing must not strand it.
    assert txn.confirm_pending(world["settings"]).ok


def test_rollback_takes_the_commit_lock(world: dict[str, Any], lock_holder: Any) -> None:
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    holder = lock_holder(world["settings"].origin, "commit")
    with pytest.raises(LockBusy):
        txn.rollback_history_txn(
            world["ctx"], world["registry"], world["settings"], 1, lock_timeout=0.2
        )
    assert world["server"].store["alpha:one"] == {"speed": 10}  # untouched
    holder.kill()
    holder.wait(timeout=10)
    assert txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], 1).ok


def test_watchdog_revert_takes_the_commit_lock(
    world: dict[str, Any], lock_holder: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog's revert coordinates through the same lock.

    It waits far longer for it than an interactive command would, but it does
    take it: a revert interleaving with a commit already in flight would
    snapshot and restore half of that commit's work.

    The revert below stands in for the watchdog's and is made from *this*
    process, so the window must have no live watchdog of its own: recovery
    re-checks that under the lock (#100) and would — rightly — refuse to
    restore a window another process is still driving.
    """
    monkeypatch.setattr("pyecsdwan.watchdog.arm", lambda *a, **k: 4_000_000)
    world["candidate"].set_path(Ref("alpha", "one"), ["speed"], 10)
    report = txn.commit(
        world["ctx"], world["registry"], _plan(world), world["settings"], confirm_minutes=5
    )
    txn_dir = list_txns()[0].dir
    assert report.state == TxnState.APPLIED_UNCONFIRMED

    holder = lock_holder(world["settings"].origin, "commit")
    with pytest.raises(LockBusy):
        txn.revert_txn_dir(
            txn_dir,
            "deadline",
            ctx=world["ctx"],
            registry=world["registry"],
            lock_timeout=0.2,
        )
    assert world["server"].store["alpha:one"] == {"speed": 10}  # not yet reverted
    holder.kill()
    holder.wait(timeout=10)

    result = txn.revert_txn_dir(
        txn_dir, "deadline", ctx=world["ctx"], registry=world["registry"]
    )
    assert result.ok and "alpha:one" not in world["server"].store


def test_revert_default_lock_wait_is_generous() -> None:
    # A watchdog that gave up because another commit was running would leave
    # the unconfirmed change applied — the one outcome the confirm window
    # exists to prevent.
    assert txn.REVERT_LOCK_TIMEOUT >= 600.0


# -- the rollback <n> path: provenance and the commit lock (#120, #100) ------


def _confirmed_legacy(world: dict[str, Any], snapshot: dict[str, Any]) -> TxnJournal:
    """A CONFIRMED journal as a pre-#63 build wrote it: a hostname, no origin."""
    import json

    ref = Ref("alpha", "one")
    legacy = TxnJournal.create(world["settings"].origin, [ref])
    legacy.record_snapshot(ref, snapshot)
    legacy.append("APPLY_START", ref=ref.key())
    legacy.append("APPLY_RESULT", ref=ref.key(), ok=True)
    legacy.set_state(TxnState.CONFIRMED)
    meta = json.loads((legacy.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (legacy.dir / "meta.json").write_text(json.dumps(meta))
    return TxnJournal.open(legacy.dir)


def test_rollback_n_does_not_restore_an_unadopted_legacy_journal(world: dict[str, Any]) -> None:
    """`confirm` and `rollback --pending` refuse a journal whose target is a
    hostname guess; `rollback <n>` selected it by number and wrote its
    snapshots into this fabric — the write #120 exists to prevent, through the
    one restore path it did not cover."""
    world["server"].store["alpha:one"] = {"v": "this tenant, live"}
    legacy = _confirmed_legacy(world, {"v": "some other tenant"})

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert not report.ok
    assert world["server"].store["alpha:one"] == {"v": "this tenant, live"}
    assert "history depth is 0" in report.messages[0]
    # Named, not hidden: the operator can see it in `show journal`, and the
    # way out is adoption from the Orchestrator it actually belongs to.
    assert any("ec-cli adopt --txn" in m for m in report.messages)
    assert TxnJournal.open(legacy.dir).meta.state == TxnState.CONFIRMED


def test_rollback_n_numbers_only_what_it_may_restore_and_says_so(
    world: dict[str, Any],
) -> None:
    """Journals newest-first: [real 2, legacy, real 1]. `rollback 2` is real 1,
    not the legacy journal between them — and the report says one was
    skipped, because the numbering an operator counted in `show journal`
    includes it and this numbering does not."""
    world["server"].store["alpha:one"] = {"v": 0}
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 1)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()
    _confirmed_legacy(world, {"v": "some other tenant"})
    world["candidate"].set_path(Ref("alpha", "one"), ["v"], 2)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=2)

    assert report.ok
    assert world["server"].store["alpha:one"] == {"v": 0}
    assert any("not numbered until adopted" in m for m in report.messages)


def test_adoption_puts_a_legacy_journal_back_in_the_rollback_history(
    world: dict[str, Any],
) -> None:
    """Guards the guard: excluded until adopted, not forever."""
    from pyecsdwan import journal as journal_mod

    world["server"].store["alpha:one"] = {"v": "live"}
    legacy = _confirmed_legacy(world, {"v": "before"})
    journal_mod.adopt(legacy, world["settings"].origin)

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert report.ok
    assert world["server"].store["alpha:one"] == {"v": "before"}
    assert not any("not numbered" in m for m in report.messages)


class _LockProbe(FakeResource):
    """Records, from inside the write, who the commit lock says is driving."""

    def __init__(self, server: FakeServer, origin: str, seen: dict[str, Any]) -> None:
        super().__init__(server, kind="probe")
        self.origin = origin
        self.seen = seen

    def _look(self) -> None:
        from pyecsdwan.journal import orphaned_txns
        from pyecsdwan.locking import HostLock

        owner = HostLock(self.origin, "commit", timeout=0.0).read_owner()
        self.seen["owner_txn"] = owner.txn_id if owner is not None else None
        self.seen["orphans"] = [t.meta.txn_id for t in orphaned_txns(origin=self.origin)]

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        self._look()
        return super().apply(ctx, diff)

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        self._look()
        return super().rollback(ctx, ref, snapshot)


def test_a_commit_takes_the_lock_in_its_own_name(world: dict[str, Any]) -> None:
    """#100's orphan scan matches the commit lock's owner record on txn_id.
    `commit` took the lock in no name, so the record matched nothing and a
    commit running in another terminal read as an orphan. Asserted from
    inside `apply`, where the commit is in flight and has no watchdog."""
    seen: dict[str, Any] = {}
    world["registry"].register(_LockProbe(world["server"], world["settings"].origin, seen))
    world["candidate"].set_path(Ref("probe", "x"), ["v"], 1)

    report = txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"])

    assert report.ok
    assert seen["owner_txn"] == report.txn_id
    assert report.txn_id not in seen["orphans"]


def test_a_rollback_takes_the_lock_in_its_own_name(world: dict[str, Any]) -> None:
    """A `rollback <n>` in flight is an APPLYING transaction too."""
    seen: dict[str, Any] = {}
    world["registry"].register(_LockProbe(world["server"], world["settings"].origin, seen))
    world["candidate"].set_path(Ref("probe", "x"), ["v"], 1)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()
    seen.clear()

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert report.ok
    assert seen["owner_txn"] == report.txn_id
    assert report.txn_id not in seen["orphans"]


# -- the rollback <n> path: #103 and #110's second copies ---------------------


def test_rollback_n_verifies_the_restore(world: dict[str, Any]) -> None:
    """#103 on the third restore path. Auto-revert and `rollback --pending`
    re-read the resource after restoring it; `rollback <n>` believed
    `rollback()` on its own word — CONFIRMED, "restored 1 resource(s)", and
    the fabric still holding the change."""

    class Liar(FakeResource):
        def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
            return ApplyResult(ok=True, message="restored")  # restores nothing

    world["registry"].register(Liar(world["server"], kind="liar"))
    for value in (1, 2):
        world["candidate"].set_path(Ref("liar", "x"), ["v"], value)
        assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
        world["candidate"].clear()

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert not report.ok
    assert report.state == TxnState.REVERT_FAILED
    assert any("does not match its pre-change snapshot" in m for m in report.messages)
    assert world["server"].store["liar:x"] == {"v": 2}


def test_rollback_n_refuses_a_lost_snapshot_instead_of_deleting(world: dict[str, Any]) -> None:
    """#110's second copy: `_revert_items` refuses a missing snapshot;
    `rollback <n>` read it with `.get()`, and None means "did not exist
    before, remove it"."""
    world["server"].store["alpha:one"] = {"v": "live and precious"}
    source = TxnJournal.create(world["settings"].origin, [Ref("alpha", "one")])
    source.append("APPLY_START", ref="alpha:one")
    source.append("APPLY_RESULT", ref="alpha:one", ok=True)
    source.set_state(TxnState.CONFIRMED)

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert not report.ok
    assert report.state == TxnState.REVERT_FAILED
    assert world["server"].store["alpha:one"] == {"v": "live and precious"}
    assert any("nothing to restore from" in m for m in report.messages)


def test_rollback_n_still_deletes_what_a_commit_created(world: dict[str, Any]) -> None:
    """Guards the guard: a snapshot *recorded* as absent is not a lost one.
    Rolling back the commit that created a resource removes it, and the
    report says the deletion could not be independently confirmed rather
    than claiming it read an absence."""
    world["candidate"].set_path(Ref("alpha", "new"), ["v"], 1)
    assert txn.commit(world["ctx"], world["registry"], _plan(world), world["settings"]).ok
    world["candidate"].clear()

    report = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)

    assert report.ok
    assert "alpha:new" not in world["server"].store
    events = TxnJournal.open(list_txns()[0].dir).events()
    results = [e for e in events if e["event"] == "APPLY_RESULT"]
    assert results and "not independently confirmed" in results[0]["message"]
