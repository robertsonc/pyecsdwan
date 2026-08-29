"""Unit tests for the interface-labels resource (Phase-0 full-loop proof),
plus the advanced constraints of issue #39: label ids unique across wan+lan,
labels in use by an overlay cannot be removed, and the deleteDependencies
cascade directive."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchApiError, OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.interface_labels import InterfaceLabels, LabelInUseError

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread

LABELS_URL = "https://orch.example.com/gms/rest/gms/interfaceLabels"
OVERLAYS_URL = "https://orch.example.com/gms/rest/gms/overlays/config"
REF = Ref(kind="interface-labels", name="global")


def _ctx(settings):
    return Ctx(client=OrchClient(settings), resolver=None)


def _ctx_with_resolver(settings, tmp_path):
    client = OrchClient(settings)
    return Ctx(client=client, resolver=Resolver(client, cache_dir=tmp_path / "cache"))


def _delete_dependencies(request) -> str | None:
    return dict(request.url.params).get("deleteDependencies")


def test_normalize_none_yields_empty_sides():
    assert InterfaceLabels().normalize(None) == {"wan": {}, "lan": {}}


def test_normalize_coerces_types_and_passes_unknown_fields():
    raw = {
        "wan": {
            123: {"name": "MPLS", "topology": "2", "active": 1, "vendorTag": "x"},
        },
        "lan": None,
    }
    assert InterfaceLabels().normalize(raw) == {
        "wan": {
            "123": {"name": "MPLS", "topology": 2, "active": True, "vendorTag": "x"},
        },
        "lan": {},
    }


def test_normalize_is_idempotent():
    res = InterfaceLabels()
    raw = {
        "wan": {"1": {"name": "MPLS", "topology": "0", "active": 1}},
        "lan": {"2": {"name": "lan0", "topology": 2, "active": False}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_no_phantom_drift_identical_states_diff_empty():
    res = InterfaceLabels()
    server_raw = {"wan": {"1": {"name": "MPLS", "active": True, "topology": 0}}, "lan": {}}
    user_intent = {"wan": {1: {"name": "MPLS", "active": 1, "topology": "0"}}, "lan": {}}
    current = res.normalize(server_raw)
    desired = res.normalize(user_intent)
    assert res.diff(REF, current, desired).empty


@respx.mock
def test_apply_posts_full_replacement_payload(settings):
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    res = InterfaceLabels()
    current = res.normalize({"wan": {}, "lan": {}})
    desired = res.normalize(
        {"wan": {"1": {"name": "MPLS", "active": True, "topology": 0}}, "lan": {}}
    )
    diff = res.diff(REF, current, desired)
    assert not diff.empty

    result = res.apply(_ctx(settings), diff)
    assert result.ok
    assert result.changed
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "wan": {"1": {"name": "MPLS", "active": True, "topology": 0}},
        "lan": {},
    }
    # #39: a pure add must not carry the destructive cascade the resource
    # used to send unconditionally.
    assert _delete_dependencies(route.calls.last.request) == "false"


@respx.mock
def test_apply_is_noop_on_empty_diff(settings):
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    res = InterfaceLabels()
    state = res.normalize({"wan": {"1": {"name": "MPLS"}}, "lan": {}})
    diff = res.diff(REF, state, state)
    assert diff.empty

    result = res.apply(_ctx(settings), diff)
    assert result.ok
    assert result.changed is False
    assert route.call_count == 0  # no API call for an empty diff


@respx.mock
def test_rollback_posts_normalized_snapshot(settings):
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    snapshot = {"wan": {"7": {"name": "LTE", "active": 1, "topology": "2"}}, "lan": {}}

    result = InterfaceLabels().rollback(_ctx(settings), REF, snapshot)
    assert result.ok
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "wan": {"7": {"name": "LTE", "active": True, "topology": 2}},
        "lan": {},
    }
    # A restore must land regardless of what picked up the labels it removes.
    assert _delete_dependencies(route.calls.last.request) == "true"


# -- #39: label ids unique across wan+lan --------------------------------------


def test_normalize_rejects_duplicate_id_across_wan_and_lan():
    raw = {"wan": {"3": {"name": "INET"}}, "lan": {3: {"name": "Voice"}}}
    with pytest.raises(ValueError) as exc:
        InterfaceLabels().normalize(raw)
    message = str(exc.value)
    assert "'3'" in message
    assert "wan" in message and "lan" in message


def test_normalize_rejects_duplicate_id_within_one_side():
    # {1: ...} and {"1": ...} collapse to the same id once keys are stringified.
    with pytest.raises(ValueError, match="duplicate interface-label id '1'"):
        InterfaceLabels().normalize({"wan": {1: {"name": "a"}, "1": {"name": "b"}}, "lan": {}})


def test_normalize_allows_the_same_name_on_both_sides():
    state = InterfaceLabels().normalize(
        {"wan": {"1": {"name": "Shared"}}, "lan": {"2": {"name": "Shared"}}}
    )
    assert set(state["wan"]) == {"1"} and set(state["lan"]) == {"2"}


# -- #39: overlay in-use constraint + deleteDependencies directive -------------

_OVERLAYS_USING_LABEL_1 = [
    {
        "id": 1,
        "name": "CorpFabric",
        "wanPorts": {"primary": ["1"], "secondary": [], "backup": [], "crossConnect": []},
    },
    {
        "id": 2,
        "name": "GuestNet",
        "internetPolicy": {"localBreakout": {"primary": ["1"], "backup": ["9"]}},
    },
]

_CURRENT = {
    "wan": {"1": {"name": "MPLS1", "active": True, "topology": 0}},
    "lan": {"4": {"name": "Voice", "active": True, "topology": 0}},
}
_WITHOUT_LABEL_1 = {"wan": {}, "lan": _CURRENT["lan"]}


def _removal_diff(res, desired_extra=None):
    current = res.normalize(_CURRENT)
    desired = res.normalize(_WITHOUT_LABEL_1)
    assert isinstance(desired, dict)
    if desired_extra:
        desired.update(desired_extra)
    return res.diff(REF, current, desired)


@respx.mock
def test_apply_refuses_removing_a_label_an_overlay_uses(settings, tmp_path):
    respx.get(OVERLAYS_URL).mock(return_value=httpx.Response(200, json=_OVERLAYS_USING_LABEL_1))
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    res = InterfaceLabels()

    with pytest.raises(LabelInUseError) as exc:
        res.apply(_ctx_with_resolver(settings, tmp_path), _removal_diff(res))

    message = str(exc.value)
    assert "1 (overlay CorpFabric, GuestNet)" in message
    assert "deleteDependencies true" in message
    assert route.call_count == 0  # nothing was written


@respx.mock
def test_apply_allows_the_removal_on_the_cascade_path(settings, tmp_path):
    respx.get(OVERLAYS_URL).mock(return_value=httpx.Response(200, json=_OVERLAYS_USING_LABEL_1))
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    res = InterfaceLabels()

    result = res.apply(
        _ctx_with_resolver(settings, tmp_path),
        _removal_diff(res, {"deleteDependencies": True}),
    )

    assert result.ok
    assert _delete_dependencies(route.calls.last.request) == "true"
    # The directive is never part of the written payload.
    assert json.loads(route.calls.last.request.content) == {"wan": {}, "lan": _CURRENT["lan"]}


@respx.mock
def test_apply_allows_removing_a_label_no_overlay_references(settings, tmp_path):
    respx.get(OVERLAYS_URL).mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "CorpFabric"}])
    )
    route = respx.post(LABELS_URL).mock(return_value=httpx.Response(200, json={}))
    res = InterfaceLabels()

    result = res.apply(_ctx_with_resolver(settings, tmp_path), _removal_diff(res))

    assert result.ok
    assert "-1 label(s): 1" in result.message
    assert _delete_dependencies(route.calls.last.request) == "false"


def test_overlay_fallback_option_counts_as_a_reference():
    from pyecsdwan.resources.interface_labels import labels_referenced_by

    assert labels_referenced_by({"overlayFallbackOption": "label_7"}) == {"7"}
    assert labels_referenced_by({"overlayFallbackOption": "3"}) == set()
    assert labels_referenced_by(
        {"hubInternetPolicies": {"1.NE": {"internetPolicy": {"localBreakout": {"backup": [8]}}}}}
    ) == {"8"}


def test_cascade_directive_is_intent_not_state():
    """The directive must never diff — the server never returns it, so leaving
    it in canonical state would be permanent phantom drift (and would fail
    post-apply verify)."""
    res = InterfaceLabels()
    current = res.normalize(_CURRENT)
    desired = res.normalize(_CURRENT)
    assert isinstance(desired, dict)
    desired["deleteDependencies"] = True
    assert res.diff(REF, current, desired).empty


@respx.mock
def test_canonicalize_desired_carries_the_directive_and_preflights(settings, tmp_path):
    respx.get(LABELS_URL).mock(return_value=httpx.Response(200, json=_CURRENT))
    respx.get(OVERLAYS_URL).mock(return_value=httpx.Response(200, json=_OVERLAYS_USING_LABEL_1))
    res = InterfaceLabels()
    ctx = _ctx_with_resolver(settings, tmp_path)

    # Plan time: dropping label 1 without the directive is refused up front.
    with pytest.raises(LabelInUseError):
        res.canonicalize_desired(ctx, REF, _WITHOUT_LABEL_1)

    staged = res.canonicalize_desired(ctx, REF, {**_WITHOUT_LABEL_1, "deleteDependencies": True})
    assert staged["deleteDependencies"] is True
    assert staged["wan"] == {}


# -- #39 end-to-end against the bundled mock ----------------------------------
#
# The mock enforces both constraints the way the real Orchestrator does (see
# "interface-labels constraints (#39)" in mock/server.py). These tests prove
# the client-side pre-flight fires *first*: the operator sees our named error
# and the mock's 400 is never reached.



@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    # The seeded overlay now pins WAN label 1 (MPLS1) as its primary port, so
    # removing that label is a genuine in-use violation on both sides.
    state.overlays["1"]["wanPorts"] = {"primary": ["1"], "secondary": [], "backup": []}
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    return {
        "ctx": ctx,
        "settings": settings,
        "candidate": CandidateStore(settings.origin),
        "state": state,
    }


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    if report.ok:
        world["candidate"].clear()
    return report


def test_e2e_happy_path_still_round_trips(world: dict[str, Any]) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(REF, ["wan", "9", "name"], "LTE")
    candidate.set_path(REF, ["wan", "9", "topology"], 2)

    report = _commit(world)
    assert report.ok, report.messages
    assert world["state"].interface_labels["wan"]["9"] == {
        "name": "LTE",
        "topology": 2,
        "active": False,
    }

    # Re-planning the same intent is a no-op (idempotency).
    candidate.set_path(REF, ["wan", "9", "name"], "LTE")
    candidate.set_path(REF, ["wan", "9", "topology"], 2)
    assert txn.build_plan(world["ctx"], default_registry, candidate).empty


def test_e2e_in_use_label_is_refused_before_the_server_sees_it(world: dict[str, Any]) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.delete(REF, ["wan", "1"])

    with pytest.raises(LabelInUseError) as exc:
        txn.build_plan(world["ctx"], default_registry, candidate)

    # Ours, not the mock's 400 — the pre-flight fired first.
    assert not isinstance(exc.value, OrchApiError)
    assert "CorpFabric" in str(exc.value)
    assert "1" in world["state"].interface_labels["wan"]


def test_e2e_cascade_directive_allows_the_removal(world: dict[str, Any]) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.delete(REF, ["wan", "1"])
    candidate.set_path(REF, ["deleteDependencies"], True)

    report = _commit(world)
    assert report.ok, report.messages
    assert "1" not in world["state"].interface_labels["wan"]
    assert "2" in world["state"].interface_labels["wan"]
    # The directive never lands in the stored table.
    assert "deleteDependencies" not in world["state"].interface_labels


def test_e2e_duplicate_id_across_sides_is_refused_at_plan_time(world: dict[str, Any]) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(REF, ["lan", "1", "name"], "Clash")

    with pytest.raises(ValueError, match="unique across wan\\+lan"):
        txn.build_plan(world["ctx"], default_registry, candidate)

    assert "1" not in world["state"].interface_labels["lan"]


def test_e2e_mock_rejects_a_violating_write_the_way_the_server_does(
    world: dict[str, Any],
) -> None:
    """The mock is a real enforcer, not a rubber stamp — bypassing the
    pre-flight by posting directly reproduces the Orchestrator's rejection."""
    client: OrchClient = world["ctx"].client
    labels = world["state"].interface_labels

    with pytest.raises(OrchApiError) as exc:
        client.post(
            "/gms/interfaceLabels",
            {"wan": {"2": labels["wan"]["2"]}, "lan": labels["lan"]},
            params={"deleteDependencies": "false"},
        )
    assert exc.value.status_code == 400

    with pytest.raises(OrchApiError) as exc:
        client.post(
            "/gms/interfaceLabels",
            {"wan": labels["wan"], "lan": {**labels["lan"], "1": {"name": "Clash"}}},
            params={"deleteDependencies": "true"},
        )
    assert exc.value.status_code == 400
