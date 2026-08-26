"""Unit tests for the interface-labels resource (Phase-0 full-loop proof)."""

import json

import httpx
import respx

from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.resources.interface_labels import InterfaceLabels

LABELS_URL = "https://orch.example.com/gms/rest/gms/interfaceLabels"
REF = Ref(kind="interface-labels", name="global")


def _ctx(settings):
    return Ctx(client=OrchClient(settings), resolver=None)


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
