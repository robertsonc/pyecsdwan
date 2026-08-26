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
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.journal import TxnState
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
    candidate = CandidateStore(settings.host)
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
    owner = ownership.owning_group(ctx, "appliance/security-policy", "3.NE")
    assert owner == "template-group SecStd"
    assert ownership.owning_group(ctx, "appliance/bgp", "3.NE") is None
    assert ownership.owning_group(ctx, "appliance/security-policy", "5.NE") is None
