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
import json
from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
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
        "candidate": CandidateStore(settings.origin),
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


# -- what every declared target must be (#69, T6) ----------------------------


def _declaring_kinds() -> list[str]:
    return [
        kind
        for kind in default_registry.kinds()
        if type(default_registry.get(kind)).write_target is not Resource.write_target
    ]


class _TargetCtx:
    """Enough context to ask for a target: nePk resolution and nothing else."""

    class _Resolver:
        @staticmethod
        def ne_pk_for(name: str) -> str:
            return {"BR1-EC": "1.NE", "BR2-EC": "2.NE"}[name]

    resolver = _Resolver()


#: Declaring kinds whose ref *name* is the appliance (their apply() resolves
#: ne_pk from ref.name, so their write_target must too).
_APPLIANCE_NAMED = frozenset({"template-association", "region-association", "appliance-info"})


def _targets_for(appliance: str) -> dict[str, str]:
    out = {}
    for kind in _declaring_kinds():
        name = appliance if kind in _APPLIANCE_NAMED else "global"
        target = default_registry.get(kind).write_target(
            _TargetCtx(), Ref(kind=kind, name=name, appliance=appliance)
        )
        if target is not None:
            out[kind] = target
    return out


#: Declaring kinds whose object is Orchestrator-wide: one instance by
#: definition, so the target is *expected* to be identical whichever
#: appliance the ref happens to mention. Everything else must differ per
#: appliance. Spelled as a list rather than inferred from the kind's name,
#: because `template-association` writes a per-appliance object without an
#: `appliance/` prefix — the prefix rule this replaces silently skipped it.
_SINGLETON_TARGET_KINDS = frozenset({
    "app-express-association",
    "interface-labels",
    "internal-subnets",
    "loopback-orch",
    "overlay-priority",
    "schedule-timezone",
    "security-policy",   # scoped by map name, which does not vary here
    "snat-maps",
    "template-group-priority",
    "zones",
})


def test_a_declared_target_is_instance_scoped() -> None:
    """The docstring's rule, enforced: a target names the object *and its
    instance*. A target that ignored the appliance would make one legitimate
    fan-out — the same setting pushed to two appliances — look like a
    conflict, and refusing that is worse than the bug this prevents. Both
    directions are asserted: a singleton whose target varied by appliance
    would fragment one object into phantom non-conflicts."""
    first, second = _targets_for("BR1-EC"), _targets_for("BR2-EC")
    for kind, target in first.items():
        if kind in _SINGLETON_TARGET_KINDS:
            assert target == second[kind], (
                f"{kind} is Orchestrator-wide but returns different targets "
                f"for two appliances, splitting one object in two"
            )
        else:
            assert target != second[kind], (
                f"{kind} returns the same target for two appliances, so two "
                f"appliances would read as one object"
            )


def test_only_the_known_pair_collides_today() -> None:
    """Guards against a *false* refusal, which is the failure mode a wave of
    new declarations introduces. Every declaration added for #69 must leave
    exactly the real overlap and invent no others; a new legitimate pair here
    means someone's target string is too coarse."""
    by_target: dict[str, set[str]] = {}
    for appliance in ("BR1-EC", "BR2-EC"):
        for kind, target in _targets_for(appliance).items():
            by_target.setdefault(target, set()).add(kind)
    shared = {t: sorted(k) for t, k in by_target.items() if len(k) > 1}
    assert shared == {
        "appliance 1.NE deployment": ["appliance/deployment", "appliance/dhcp"],
        "appliance 2.NE deployment": ["appliance/deployment", "appliance/dhcp"],
    }, shared


def test_every_curated_kind_is_decided_declare_or_exempt() -> None:
    """#69's completeness criterion, the whole of it.

    The endpoint-sharing check catches a kind that collides with one already
    declared; the declaration list catches a kind someone remembered to
    classify. What neither catches is the kind nobody looked at — and an
    unexamined kind is exactly where the next deployment/dhcp overlap ships
    from. So the registry is partitioned: every curated kind is either a
    declared full-object replacement or a recorded exemption with the reason
    a target would be wrong for it. A new curated kind fails here until its
    author has traced apply() to the object it writes, which is the decision
    this project spent a session learning cannot be derived mechanically.
    """
    from pyecsdwan.contract import Tier

    curated = {
        kind
        for kind in default_registry.kinds()
        if default_registry.get(kind).tier is Tier.CURATED
    }
    declared = set(_declaring_kinds()) & curated
    exempt = set(WRITE_TARGET_EXEMPT)

    both = declared & exempt
    assert not both, (
        f"{sorted(both)} both declare a target and claim exemption; an "
        f"exemption for a kind that declares is a stale reason waiting to "
        f"mislead — remove the exempt entry"
    )
    undecided = curated - declared - exempt
    assert not undecided, (
        f"{sorted(undecided)} are curated but neither declare a write_target "
        f"nor record an exemption in WRITE_TARGET_EXEMPT; trace apply() to "
        f"the object it writes and decide"
    )
    stale = exempt - curated
    assert not stale, (
        f"{sorted(stale)} are exempted but not curated; remove the entries"
    )
    assert declared == set(FULL_OBJECT_REPLACEMENT), (
        "FULL_OBJECT_REPLACEMENT and the kinds actually declaring "
        "write_target() have drifted apart: "
        f"only in the list: {sorted(set(FULL_OBJECT_REPLACEMENT) - declared)}; "
        f"only declaring: {sorted(declared - set(FULL_OBJECT_REPLACEMENT))}"
    )


def test_every_full_object_replacement_kind_declares_a_target() -> None:
    """The completeness criterion #69 actually asks for.

    Endpoint-sharing (above) only catches a kind that collides with one
    *already declared*. A resource that replaces a whole server object nobody
    else writes today still has to say so, or the first kind added alongside it
    collides silently — the declaration is what makes the future pair visible,
    and it cannot be added retroactively by whoever adds the second resource.
    """
    declared = _targets_for("BR1-EC")
    for kind in FULL_OBJECT_REPLACEMENT:
        resource = default_registry.get(kind)
        assert type(resource).write_target is not Resource.write_target, (
            f"{kind} replaces a whole server object but declares no "
            f"write_target(); a kind added alongside it would collide silently"
        )
        # And that it *answers*. Overriding the method and returning None is
        # indistinguishable from not overriding it as far as the collision
        # check is concerned, and the mutation sweep found exactly that hole:
        # blanking the shared base's return left every test green.
        assert declared.get(kind), (
            f"{kind} overrides write_target() but returns no target, which "
            f"detects nothing — the override is the shape of a declaration "
            f"without being one"
        )


#: Kinds whose `apply()` replaces a whole server object rather than patching
#: fields, so a second writer of that object is a destructive overlap. Listed
#: rather than derived because the property is semantic: it is what `apply()`
#: does with the body, which no endpoint declaration records. Deriving it was
#: tried and abandoned — `template-group` declares `/template/templateGroups`
#: and `/template/templateCreate`, whose common prefix is the nonsense string
#: `/template/template`, and a wrong target asserted mechanically is worse than
#: an absent one.
FULL_OBJECT_REPLACEMENT = (
    "appliance/deployment",
    "appliance/dhcp",
    "appliance/banners",
    "appliance/logging",
    "appliance/mgmt-services",
    "appliance/snmp",
    "appliance/inbound-shaper",
    "appliance/shaper",
    "appliance/optimization-map",
    "appliance/qos-map",
    "appliance/route-map",
    "internal-subnets",
    "overlay-priority",
    "template-group-priority",
    # The #69 completeness sweep (2026-08-30): every remaining curated kind
    # traced to the object its apply() writes. These replace whole objects.
    "app-express-association",   # complete association list, POST
    "appliance/acl",             # complete ACL table, merge: false
    "appliance/bgp",             # bgp/config/system + neighbor, both whole
    "appliance/nat-maps",        # complete NAT maps object, merge: false
    "appliance/nat-pools",       # per-id deletes + whole-object POST
    "appliance/ospf",            # ospf/config/system + interfaces, both whole
    "appliance/security-maps",   # complete map set via appliance proxy
    "appliance/vrrp",            # complete instance list
    "appliance/zones",           # complete per-appliance zone table
    "interface-labels",          # full-replace POST of the label table
    "loopback-orch",             # full structure, never partial
    "region-association",        # per-appliance PUT replaces the association
    "appliance-info",            # per-appliance POST replaces the extraInfo object
    "schedule-timezone",         # whole (one-field) singleton object
    "security-policy",           # complete policy per segment pair, merge: false
    "snat-maps",                 # full-table replace per the SDK's own warning
    "template-association",      # complete group list, triggers the push
    "zones",                     # zone table + nextId + eeEnable, one identity
)

#: Kinds whose apply() does NOT replace a whole shared object, each with the
#: reason a target would be wrong for it. This is the other half of #69's
#: completeness criterion: a kind is *decided*, never merely undeclared — the
#: partition test below refuses a curated kind that is in neither list.
#: A wrongly-asserted target refuses legitimate changesets, so the honest
#: answer for these is a recorded exemption, not a guessed string.
WRITE_TARGET_EXEMPT = {
    "app-express-group": (
        "per-entry POST/DELETE by group name; the spec is explicit that the "
        "POST edits a single group and the table is never replaced"
    ),
    "appliance/loopback": (
        "apply() raises NotImplementedError — no write path exists to declare "
        "a target for; the decision belongs to whoever implements it"
    ),
    "appliance/routes": (
        "pure add/delete delta per prefix through the add and delete "
        "endpoints; the code never posts the table whole"
    ),
    "bio": (
        "per-overlay create (POST), update (PUT ?overlayId=) and delete "
        "(DELETE ?overlayId=); other overlays are never in the body"
    ),
    "bio-association": (
        "membership deltas — adds and removes are separate POSTs of only the "
        "changed nePks; nothing is replaced"
    ),
    "ip-address-group": (
        "per-entry create/replace/delete by name; the group table is untouched"
    ),
    "ip-service-group": (
        "per-entry create/replace/delete by name; the group table is untouched"
    ),
    "region": (
        "per-entry create (POST), update (PUT ?regionId=) and delete "
        "(DELETE ?regionId=); other regions are never in the body"
    ),
    "regional-overlay": (
        "region-scoped read-modify-write splice: everything except "
        "[overlayId][regionId] is carried through from a fresh GET at apply "
        "time, so two splicers are order-safe. The destructive partner would "
        "be a kind that PUTs the whole table from plan-time state — none "
        "exists, and one that appears must declare the table path and move "
        "this kind beside it (the deployment/dhcp shape)"
    ),
    "template-group": (
        "per-group create/update/delete addressed by ?templateGroup=; other "
        "groups are never in the body"
    ),
}


# -- the structured conflict, and both entry points ---------------------------


def test_a_collision_has_a_stable_machine_readable_form() -> None:
    """"Conflicts are found before the first API write" buys a pipeline
    nothing if the only way to learn what conflicted is to regex an error
    string. Both fields, always: the target does not say what conflicts, the
    refs do not say what they conflict over."""
    collision = txn.Collision(target="appliance 3.NE deployment", refs=("a", "b"))
    assert collision.as_json() == {
        "target": "appliance 3.NE deployment",
        "refs": ["a", "b"],
    }
    assert json.loads(json.dumps(collision.as_json())) == collision.as_json()


def test_the_refusal_carries_the_conflicts_not_just_prose(settings: Any) -> None:
    """The refusal an automated caller catches is the one that has to be
    parseable, so the objects travel on the exception."""
    items = [
        _item("appliance/deployment", "BR1-EC", "appliance 1.NE deployment"),
        _item("appliance/dhcp", "BR1-EC", "appliance 1.NE deployment"),
    ]
    with pytest.raises(txn.CommitError) as excinfo:
        txn._guard(items, settings, None, False, False, False)
    (conflict,) = excinfo.value.collisions
    assert conflict.as_json() == {
        "target": "appliance 1.NE deployment",
        "refs": [
            Ref("appliance/deployment", "BR1-EC").key(),
            Ref("appliance/dhcp", "BR1-EC").key(),
        ],
    }


def test_a_refusal_with_no_collisions_carries_an_empty_tuple(settings: Any) -> None:
    """Guards the guard: an attribute that only ever exists on one path is one
    every caller has to getattr() defensively."""
    assert txn.CommitError("something else").collisions == ()


def test_both_entry_points_compute_collisions_from_one_place() -> None:
    """Entry-point parity, structurally rather than by driving each command.

    `commit` and `apply --from` are two callers of `build_plan`, and it is
    `build_plan` that populates `Plan.collisions` — so neither can reach a plan
    that skipped the check. Asserted on the source because the alternative is
    two end-to-end tests that pass while a third entry point added later has
    no check at all, which is the shape of #100.
    """
    import inspect

    source = inspect.getsource(cli_main)
    plan_builders = [
        line.strip()
        for line in source.splitlines()
        if "txn.build_plan(" in line or ("build_plan(" in line and "def " not in line)
    ]
    assert len(plan_builders) >= 2, plan_builders
    # And the one function every one of them lands in does the detection.
    assert "collisions = _write_collisions(items)" in inspect.getsource(txn.build_plan)
    # And the commit-time re-check, which is the one that actually refuses.
    assert "collisions = _write_collisions(changed)" in inspect.getsource(txn._guard)
