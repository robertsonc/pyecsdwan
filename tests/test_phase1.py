"""Phase-1 vertical slice: template groups, associations, BIOs, security
policy — end-to-end through the transaction engine against the bundled mock,
proving dependency ordering, idempotency, and rollback per resource kind."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref
from pyecsdwan.journal import TxnJournal, TxnState
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    resolver = Resolver(client)
    ctx = Ctx(client=client, resolver=resolver)
    candidate = CandidateStore(settings.origin)
    return {
        "ctx": ctx, "settings": settings, "candidate": candidate,
        "state": state, "client": client,
    }


def _commit(world: dict[str, Any], **kwargs: Any) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"], **kwargs)
    if report.ok:
        world["candidate"].clear()
    return report


def _assert_idempotent(world: dict[str, Any], reapply: dict[Ref, dict[str, Any]]) -> None:
    """Re-stage the same desired state; the plan must be empty."""
    for ref, desired in reapply.items():
        world["candidate"].set_desired(ref, desired)
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    assert plan.empty, [
        (i.ref.key(), [f"{e.op} {'.'.join(e.path)}: {e.old}->{e.new}" for e in i.diff])
        for i in plan.changed_items
    ]
    world["candidate"].clear()


def test_template_group_lifecycle(world: dict[str, Any]) -> None:
    desired = {"templates": {"securityMaps": {"data": {"rules": ["allow-dns"]}}}}
    ref = Ref("template-group", "Branch-Std")
    world["candidate"].set_desired(ref, desired)
    report = _commit(world)
    assert report.ok, report.messages
    assert "Branch-Std" in world["state"].template_groups

    _assert_idempotent(world, {ref: desired})

    # update content
    desired2 = {"templates": {"securityMaps": {"data": {"rules": ["allow-dns", "deny-any"]}}}}
    world["candidate"].set_desired(ref, desired2)
    assert _commit(world).ok
    _assert_idempotent(world, {ref: desired2})

    # rollback 1 -> back to first content
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok
    _assert_idempotent(world, {ref: desired})

    # delete
    world["candidate"].delete(ref)
    assert _commit(world).ok
    assert "Branch-Std" not in world["state"].template_groups
    # rollback of the delete restores the group
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok
    assert "Branch-Std" in world["state"].template_groups


def test_template_association_replaces_completely(world: dict[str, Any]) -> None:
    world["state"].template_groups["G1"] = {"name": "G1", "templates": []}
    world["state"].template_groups["G2"] = {"name": "G2", "templates": []}
    world["state"].template_association["3.NE"] = ["G1"]

    ref = Ref("template-association", "BR1-EC")
    desired = {"template_groups": ["G1", "G2"]}
    world["candidate"].set_desired(ref, desired)
    report = _commit(world)
    assert report.ok, report.messages
    assert sorted(world["state"].template_association["3.NE"]) == ["G1", "G2"]
    _assert_idempotent(world, {ref: desired})

    # rollback restores the original association (complete replacement back)
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok
    assert world["state"].template_association["3.NE"] == ["G1"]


def test_keyless_template_push_failure_auto_reverts(world: dict[str, Any]) -> None:
    """#21 acceptance: the mock's association POST is fire-and-204 (no action
    key); the injected per-appliance failure exists only in the action log.
    apply() must surface it via GET /action window polling, fail the commit,
    and auto-revert the association to the pre-commit snapshot."""
    world["state"].template_groups["G1"] = {"name": "G1", "templates": []}
    world["state"].template_groups["G2"] = {"name": "G2", "templates": []}
    world["state"].template_association["3.NE"] = ["G1"]
    world["state"].fail_next_action = True

    ref = Ref("template-association", "BR1-EC")
    world["candidate"].set_desired(ref, {"template_groups": ["G1", "G2"]})
    report = _commit(world)

    assert not report.ok
    assert report.state == TxnState.REVERTED
    assert report.reverted == ["template-association:BR1-EC"]
    assert world["state"].template_association["3.NE"] == ["G1"]
    assert any("FAILED" in m for m in report.messages), report.messages

    # The journal's APPLY_RESULT carries the per-appliance job outcome.
    assert report.txn_id is not None
    journal = TxnJournal.open(config.journal_root() / report.txn_id)
    apply_results = [e for e in journal.events() if e.get("event") == "APPLY_RESULT"]
    assert len(apply_results) == 1
    assert apply_results[0]["ok"] is False
    job_outcomes = apply_results[0]["jobs"]
    assert len(job_outcomes) == 1
    assert job_outcomes[0]["state"] == "FAILED"
    assert job_outcomes[0]["per_appliance"] == {"3.NE": "mock failure"}

    # #22: the same per-appliance breakdown is on the report itself, not
    # just reachable by re-opening the journal — this is what the CLI
    # renders (cli/render.py's render_report).
    #
    # Two outcomes, not one, since #64: the revert's own push is now polled
    # and confirmed rather than assumed from its 204. A revert that reports
    # success without checking is the same unverified claim the apply is not
    # allowed to make — and it is the one running after something has already
    # gone wrong, so it is the worse place to guess.
    assert len(report.jobs) == 2, report.jobs
    assert report.jobs[0].state == "FAILED"
    assert report.jobs[0].per_appliance == {"3.NE": "mock failure"}
    assert report.jobs[1].state == "SUCCESS"
    assert report.jobs[1].per_appliance == {"3.NE": "Success"}


def test_timeout_during_confirm_window_commit_reverts(
    state_home: Any, mock_server: tuple[str, MockState]
) -> None:
    """#24 acceptance: a job TIMEOUT during a `commit confirm N` apply must
    count as failure and auto-revert — never proceed to arm a confirm window
    over a changeset whose apply never actually finished (the contract's own
    "a TIMEOUT inside a commit-confirm window counts as failure upstream"
    promise, made explicit with a real timing regression rather than just
    asserted in a docstring).

    Uses its own fast-poll settings (short job_timeout, short poll delays)
    bound to the same shared mock server, rather than the module `world`
    fixture's 5s/1s defaults — this test needs a real timeout to happen
    quickly, not a slow one to prove the point.
    """
    base_url, state = mock_server
    state.reset()
    # Never resolves within the short job_timeout below: the mock's action
    # only finishes after `action_delay_polls` polls (see MockState.new_action).
    state.action_delay_polls = 10_000
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=0.1,
        job_poll_initial=0.02,
        job_poll_max=0.05,
    )
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    candidate = CandidateStore(settings.origin)

    state.template_groups["G1"] = {"name": "G1", "templates": []}
    state.template_association["3.NE"] = []

    ref = Ref("template-association", "BR1-EC")
    candidate.set_desired(ref, {"template_groups": ["G1"]})
    plan = txn.build_plan(ctx, default_registry, candidate)
    report = txn.commit(ctx, default_registry, plan, settings, confirm_minutes=5)

    assert not report.ok
    assert report.confirm_deadline is None  # the confirm window was never armed
    assert report.jobs and report.jobs[0].state == "TIMEOUT"
    # Nothing was actually associated — the pre-commit (empty) state holds,
    # not a half-applied push silently left in place.
    assert state.template_association["3.NE"] == []

    # ...but the revert is reported REVERT_FAILED, not REVERTED (#64). This
    # fabric never finishes *any* job, so the revert's own push times out too,
    # and an unconfirmed revert is not a confirmed one — even though the line
    # above shows it did in fact land. That asymmetry is the point: the CLI
    # knows what it POSTed, not what the fabric did with it, and the operator
    # who is told "reverted" stops looking.
    #
    # It also used to say REVERTED unconditionally, because rollback() did not
    # poll at all — so a revert whose push genuinely failed reported success.
    assert report.state == TxnState.REVERT_FAILED
    assert any("manual intervention required" in m for m in report.messages), report.messages
    assert any("TIMEOUT" in m for m in report.messages), report.messages


def test_bio_and_association_dependency_order(world: dict[str, Any]) -> None:
    """One changeset creates a new overlay AND its membership: the bio kind
    must apply before bio-association (which resolves the new overlay id)."""
    bio_ref = Ref("bio", "GuestFabric")
    assoc_ref = Ref("bio-association", "GuestFabric")
    bio_desired = {"name": "GuestFabric", "topology": "hub-spoke"}
    # Stage association FIRST to prove ordering comes from dependencies,
    # not submission order.
    world["candidate"].set_desired(assoc_ref, {"appliances": ["BR1-EC", "BR2-EC"]})
    world["candidate"].set_desired(bio_ref, bio_desired)

    report = _commit(world)
    assert report.ok, report.messages
    assert report.applied[0].startswith("bio:"), report.applied
    overlay = next(o for o in world["state"].overlays.values() if o["name"] == "GuestFabric")
    members = world["state"].overlay_association[str(overlay["id"])]
    assert sorted(members) == ["3.NE", "5.NE"]

    _assert_idempotent(
        world,
        {
            bio_ref: bio_desired,
            assoc_ref: {"appliances": ["BR2-EC", "BR1-EC"]},  # order-insensitive
        },
    )


def test_bio_association_membership_delta(world: dict[str, Any]) -> None:
    # Seeded overlay CorpFabric has 1.NE; move membership to 3.NE + 5.NE.
    ref = Ref("bio-association", "CorpFabric")
    world["candidate"].set_desired(ref, {"appliances": ["BR1-EC", "BR2-EC"]})
    report = _commit(world)
    assert report.ok, report.messages
    assert sorted(world["state"].overlay_association["1"]) == ["3.NE", "5.NE"]

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok
    assert world["state"].overlay_association["1"] == ["1.NE"]


def test_security_policy_roundtrip(world: dict[str, Any]) -> None:
    ref = Ref("security-policy", "0_0")
    desired = {
        "maps": {
            "map1": {
                "20_21": {
                    "prio": {
                        "1000": {
                            "match": {"either_dns": "*corp.example"},
                            "set": {"action": "allow"},
                            "misc": {"rule": "enable", "logging": "disable"},
                            "comment": "allow corp dns",
                        },
                        "65535": {
                            "match": {"acl": ""},
                            "set": {"action": "deny"},
                            "misc": {"rule": "enable", "logging": "disable"},
                            "comment": "",
                        },
                    }
                }
            }
        }
    }
    world["candidate"].set_desired(ref, desired)
    report = _commit(world)
    assert report.ok, report.messages

    stored = world["state"].security_policies["0_0"]
    # apply re-injects the 'self' echoes the API wants
    assert stored["map1"]["self"] == "map1"
    assert stored["map1"]["20_21"]["self"] == "20_21"
    assert stored["map1"]["20_21"]["prio"]["1000"]["self"] == 1000

    # ...and normalize strips them again: identical intent = empty diff
    _assert_idempotent(world, {ref: desired})

    # rollback restores the pre-change (empty) policy
    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok
    assert world["state"].security_policies["0_0"] == {}


def test_security_policy_name_validation(world: dict[str, Any]) -> None:
    world["candidate"].set_desired(Ref("security-policy", "corp"), {"maps": {}})
    with pytest.raises(ValueError, match="segment pair"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


def test_cross_kind_partial_failure_reverts_everything(world: dict[str, Any]) -> None:
    """Template group succeeds, then the association apply fails (unknown
    appliance) -> the whole changeset reverts to the pre-commit snapshot."""
    world["candidate"].set_desired(
        Ref("template-group", "WillRevert"), {"templates": {"dns": {"servers": ["10.0.0.1"]}}}
    )
    world["candidate"].set_desired(
        Ref("template-association", "BR1-EC"), {"template_groups": ["NoSuchGroup"]}
    )
    world["state"].fail_next_association = True  # type: ignore[attr-defined]

    # The mock accepts unknown groups; force failure at the HTTP layer instead
    # by pointing the association at a nePk the mock rejects.
    ref = Ref("template-association", "BR1-EC")
    world["candidate"].drop(ref)
    world["candidate"].set_desired(Ref("template-association", "GHOST-EC"), {"template_groups": []})

    plan_error: Exception | None = None
    try:
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    except Exception as exc:  # noqa: BLE001 - asserting the resolver rejects the ghost
        plan_error = exc
    assert plan_error is not None  # unknown appliance rejected at plan time

    # Now the real partial-failure path: valid plan, association POST fails.
    world["candidate"].clear()
    world["candidate"].set_desired(
        Ref("template-group", "WillRevert"), {"templates": {"dns": {"servers": ["10.0.0.1"]}}}
    )
    world["candidate"].set_desired(ref, {"template_groups": ["WillRevert"]})
    world["state"].reject_association_posts = True  # type: ignore[attr-defined]

    import pyecsdwan.resources.templates as tpl

    original_apply = tpl.TemplateAssociation.apply

    def failing_apply(self: Any, ctx: Any, diff: Any) -> Any:
        raise RuntimeError("simulated push failure")

    tpl.TemplateAssociation.apply = failing_apply  # type: ignore[method-assign]
    try:
        report = _commit(world)
    finally:
        tpl.TemplateAssociation.apply = original_apply  # type: ignore[method-assign]

    assert not report.ok
    assert report.state == TxnState.REVERTED
    assert "WillRevert" not in world["state"].template_groups


def test_ownership_join(world: dict[str, Any]) -> None:
    from pyecsdwan import ownership

    world["state"].template_groups["SecStd"] = {"name": "SecStd", "templates": []}
    world["state"].template_selection["SecStd"] = ["securityMaps", "dns"]
    world["state"].template_association["3.NE"] = ["SecStd"]

    ctx = world["ctx"]
    owns = ownership.owning_group(ctx, "appliance/security-policy", "3.NE")
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group SecStd"
    # SecStd is associated but selects no "bgp" section — and "bgp" is a
    # guessed name, so a non-match distinguishes nothing (#20).
    assert ownership.owning_group(ctx, "appliance/bgp", "3.NE").state is Owned.UNKNOWN
    # 5.NE has no associated group at all: the one clean negative.
    assert ownership.owning_group(ctx, "appliance/security-policy", "5.NE").state is Owned.UNOWNED
