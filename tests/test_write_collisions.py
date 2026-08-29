"""Two changes, one server object (#69).

`appliance/dhcp` has no endpoint of its own: DHCP configuration is a subtree of
the same `deployment` object `appliance/deployment` replaces, so both POST
`/deployment` on the same appliance. Individually each is correct. Together
they are order-dependent, and the order is whatever the planner happens to
produce.

Ordering is not a fix, and the asymmetry is why:

* `dhcp` re-reads the live object and splices its subtree in, so it survives
  being applied second;
* `deployment` posts the whole body it computed at *plan* time, so anything
  applied before it is overwritten.

So `dhcp` then `deployment` silently discards the DHCP change. There is no
flag for "discard my other change on purpose", which is why this refuses with
no override: the fix is two commits.

The pair is not hardcoded here. It is **derived** from what the resources
declare they write, so a third resource that starts POSTing `/deployment` — or
any future pair sharing an endpoint — is caught by the completeness test rather
than by someone remembering to add it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import (
    Ctx,
    Diff,
    DiffEntry,
    DiffOp,
    Ownership,
    Ref,
    Resource,
    Scope,
)
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "settings": settings,
        "candidate": CandidateStore(settings.host),
        "state": state,
    }


def _shared_write_endpoints() -> dict[str, list[str]]:
    """Write endpoints declared by more than one registered kind."""
    by_endpoint: dict[str, list[str]] = {}
    for kind in default_registry.kinds():
        for endpoint in getattr(default_registry.get(kind), "endpoints", ()):
            _scope, method, _path = endpoint.split(" ", 2)
            if method in WRITE_METHODS:
                by_endpoint.setdefault(endpoint, []).append(kind)
    return {ep: kinds for ep, kinds in by_endpoint.items() if len(kinds) > 1}


# -- the derivation ----------------------------------------------------------


def test_there_is_a_shared_write_endpoint_to_check() -> None:
    """Guards the guard. With no shared endpoint in the registry the
    completeness test below would pass over an empty loop — which is how this
    whole class of check quietly stops working."""
    shared = _shared_write_endpoints()
    assert shared, "expected at least one write endpoint claimed by two kinds"
    assert "appliance POST /deployment" in shared


def test_every_kind_sharing_a_write_endpoint_declares_a_target() -> None:
    """The completeness criterion, derived rather than listed.

    A kind that POSTs an endpoint another kind also POSTs is a candidate for
    destructive overlap, and `write_target` is how it says which object it
    replaces. Silence there is the failure mode: the collision check simply
    never fires, and the changeset applies both writes.
    """
    for endpoint, kinds in sorted(_shared_write_endpoints().items()):
        for kind in kinds:
            resource = default_registry.get(kind)
            assert type(resource).write_target is not Resource.write_target, (
                f"{kind} shares {endpoint} with {[k for k in kinds if k != kind]} "
                f"but declares no write_target()"
            )


def test_the_deployment_pair_targets_the_same_object_per_appliance(
    world: dict[str, Any],
) -> None:
    """And the targets have to actually *match*, or the declaration above is
    satisfied by two resources naming the same object differently."""
    ctx = world["ctx"]
    dep = default_registry.get("appliance/deployment")
    dhcp = default_registry.get("appliance/dhcp")

    dep_ref = Ref("appliance/deployment", "deployment", appliance="BR1-EC")
    dhcp_ref = Ref("appliance/dhcp", "dhcp", appliance="BR1-EC")
    assert dep.write_target(ctx, dep_ref) == dhcp.write_target(ctx, dhcp_ref)

    # ... and two appliances are two objects, not a conflict. A target of bare
    # "deployment" would satisfy the equality above and refuse every fabric-wide
    # changeset that touched two appliances.
    other = Ref("appliance/dhcp", "dhcp", appliance="BR2-EC")
    assert dhcp.write_target(ctx, other) != dhcp.write_target(ctx, dhcp_ref)


# -- planning and the guard --------------------------------------------------


class _Shared(Resource):
    """Two of these, same target, no coupling — the shape of the real pair."""

    scope = Scope.ORCHESTRATOR

    def __init__(self, kind: str, target: str | None) -> None:
        self.kind = kind
        self._target = target

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        return self._target


def _item(kind: str, name: str, target: str | None, *, changed: bool = True) -> txn.PlanItem:
    ref = Ref(kind, name)
    entries = [DiffEntry(DiffOp.ADD, ("a",), None, 1)] if changed else []
    return txn.PlanItem(
        ref=ref,
        resource=_Shared(kind, target),
        delete=False,
        current_raw=None,
        current=None,
        desired={"a": 1},
        diff=Diff(ref=ref, entries=entries, desired={"a": 1}, current=None),
        write_target=target,
    )


def test_two_items_on_one_target_collide() -> None:
    found = txn._write_collisions(
        [_item("alpha", "x", "appliance 3.NE deployment"),
         _item("beta", "y", "appliance 3.NE deployment")]
    )
    assert len(found) == 1
    assert found[0].target == "appliance 3.NE deployment"
    assert found[0].refs == ("alpha:x", "beta:y")


def test_different_targets_do_not_collide() -> None:
    assert not txn._write_collisions(
        [_item("alpha", "x", "appliance 3.NE deployment"),
         _item("beta", "y", "appliance 5.NE deployment")]
    )


def test_undeclared_targets_do_not_collide_with_each_other() -> None:
    """`None` means "shares nothing", not "unknown" — so two `None`s are two
    different objects. Grouping them would refuse almost every changeset,
    since most resources declare no target at all."""
    assert not txn._write_collisions(
        [_item("alpha", "x", None), _item("beta", "y", None)]
    )


def test_an_unchanged_item_cannot_collide() -> None:
    """It issues no write. Counting it would refuse a changeset over an
    instance nobody asked to modify."""
    assert not txn._write_collisions(
        [_item("alpha", "x", "shared"),
         _item("beta", "y", "shared", changed=False)]
    )


def test_the_guard_refuses_and_says_what_to_do(settings: Any) -> None:
    changed = [_item("alpha", "x", "appliance 3.NE deployment"),
               _item("beta", "y", "appliance 3.NE deployment")]
    with pytest.raises(txn.CommitError) as caught:
        txn._guard(changed, settings, None, False, False, False)
    message = str(caught.value)
    assert "same server object" in message
    assert "appliance 3.NE deployment" in message
    assert "alpha:x" in message and "beta:y" in message
    # An operator told only that it refused has nowhere to go.
    assert "Commit them separately" in message


def test_no_flag_lets_a_collision_through(settings: Any) -> None:
    """Deliberate, and worth pinning: `--override-template`, `--force` and
    `--allow-untransactional` all exist for risks an operator can knowingly
    accept. "Silently discard one of my two changes" is not one of them —
    nobody wants that outcome, so there is nothing to opt into."""
    changed = [_item("alpha", "x", "shared"), _item("beta", "y", "shared")]
    for override, force, untransactional in (
        (True, False, False),
        (False, True, False),
        (False, False, True),
        (True, True, True),
    ):
        with pytest.raises(txn.CommitError, match="same server object"):
            txn._guard(changed, settings, None, force, override, untransactional)


# -- end to end, through a real changeset ------------------------------------


def test_deployment_and_dhcp_in_one_changeset_are_refused(world: dict[str, Any]) -> None:
    """The real pair, planned against the bundled mock and committed.

    Asserted through `txn.commit` rather than `_guard` directly, so the path an
    operator actually takes is the one under test — and the appliance's
    deployment object is compared before and after, because a refusal that
    happened *after* a write would still be a lost change.
    """
    ctx, state, candidate = world["ctx"], world["state"], world["candidate"]
    # The mock materializes "deployment" in its ECOS store only on a *write*
    # (a GET is served from a seed function), which makes the key's absence a
    # direct proof that no POST landed — better than comparing two copies of a
    # value a refusal would never have reached.
    assert "deployment" not in state.appliance_ecos["3.NE"]

    candidate.set_path(
        Ref("appliance/dhcp", "dhcp", appliance="BR1-EC"),
        ["dhcpFailover", "lan0", "peerIP"],
        "10.0.0.9",
    )
    candidate.set_path(
        Ref("appliance/deployment", "deployment", appliance="BR1-EC"),
        ["sysConfig", "hostname"],
        "renamed",
    )

    plan = txn.build_plan(ctx, default_registry, candidate)
    assert len(plan.collisions) == 1, plan.collisions
    assert set(plan.collisions[0].refs) == {
        "appliance%2Fdhcp:BR1-EC:dhcp",
        "appliance%2Fdeployment:BR1-EC:deployment",
    }
    assert any("shared write target" in w for w in plan.warnings)

    with pytest.raises(txn.CommitError, match="same server object"):
        txn.commit(ctx, default_registry, plan, world["settings"])
    assert "deployment" not in state.appliance_ecos["3.NE"], "refused after writing"


def test_either_one_alone_still_commits(world: dict[str, Any]) -> None:
    """Guards the guard: a collision check that fired on a single item would
    make both resources permanently uncommittable, and every test above would
    still pass."""
    ctx, candidate = world["ctx"], world["candidate"]
    candidate.set_path(
        Ref("appliance/dhcp", "dhcp", appliance="BR1-EC"),
        ["dhcpFailover", "lan0", "peerIP"],
        "10.0.0.9",
    )
    plan = txn.build_plan(ctx, default_registry, candidate)
    assert not plan.collisions
    assert plan.items and any(i.changed for i in plan.items)
    # And the guard lets it through — the collision check is the only thing
    # under test here, so ownership is set aside rather than satisfied.
    changed = [
        dataclasses.replace(i, ownership=Ownership.unowned("not under test"))
        for i in plan.changed_items
    ]
    txn._guard(changed, world["settings"], None, False, False, False)


def test_compare_shows_the_collision_before_commit_refuses_it() -> None:
    """`compare` is where an operator looks before committing, so a collision
    that only surfaced as a CommitError would arrive after they had already
    staged and reviewed the whole changeset. Red, and above the dim warnings:
    it is not advice, it is a commit that will be refused."""
    import io

    from rich.console import Console

    from pyecsdwan.cli import render

    plan = txn.Plan(
        items=[_item("alpha", "x", "appliance 3.NE deployment")],
        warnings=["something dim and advisory"],
        collisions=[
            txn.Collision(
                target="appliance 3.NE deployment",
                refs=("appliance%2Fdeployment:BR1-EC:deployment",
                      "appliance%2Fdhcp:BR1-EC:dhcp"),
            )
        ],
    )
    buf = io.StringIO()
    render.render_plan(Console(file=buf, width=200, no_color=True), plan)
    out = buf.getvalue()
    assert "shared write target appliance 3.NE deployment" in out
    assert "appliance%2Fdhcp:BR1-EC:dhcp" in out
    # Ahead of the warnings, not buried among them.
    assert out.index("shared write target") < out.index("something dim")


def test_the_overlap_really_does_lose_data_in_one_order(world: dict[str, Any]) -> None:
    """The evidence for refusing, rather than warning and ordering.

    `dhcp.py`'s module docstring used to say the overlap was harmless — "no
    data is lost", because whichever applies second does a read-modify-write.
    That is true of `dhcp`, which re-reads the live object and splices. It is
    not true of `deployment`, which posts the body it computed at plan time
    (`_write(ctx, ref, diff.desired, ...)`), so whatever landed in between is
    overwritten.

    So the guard is deliberately bypassed here and the two applies are driven
    in the losing order, to show the loss is real. Without this the refusal
    would rest on reading the code and believing it.
    """
    ctx, state, candidate = world["ctx"], world["state"], world["candidate"]
    dhcp_ref = Ref("appliance/dhcp", "dhcp", appliance="BR1-EC")
    dep_ref = Ref("appliance/deployment", "deployment", appliance="BR1-EC")

    candidate.set_path(dhcp_ref, ["dhcpFailover", "lan0", "peerIP"], "10.0.0.9")
    candidate.set_path(dep_ref, ["sysConfig", "hostname"], "renamed")
    plan = txn.build_plan(ctx, default_registry, candidate)
    by_kind = {i.ref.kind: i for i in plan.changed_items}

    # DHCP first: it splices onto a fresh read, and its change lands.
    assert default_registry.get("appliance/dhcp").apply(ctx, by_kind["appliance/dhcp"].diff).ok
    live = state.appliance_ecos["3.NE"]["deployment"]
    assert live["dhcpFailover"]["lan0"]["peerIP"] == "10.0.0.9"

    # Deployment second: it posts a body computed before that write.
    assert default_registry.get("appliance/deployment").apply(
        ctx, by_kind["appliance/deployment"].diff
    ).ok
    live = state.appliance_ecos["3.NE"]["deployment"]
    assert live["sysConfig"]["hostname"] == "renamed"
    assert live.get("dhcpFailover", {}).get("lan0", {}).get("peerIP") != "10.0.0.9", (
        "expected the DHCP change to be overwritten — if this now passes, "
        "deployment.apply() has started re-reading and the refusal can soften"
    )
