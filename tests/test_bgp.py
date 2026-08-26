"""Unit tests for the appliance/bgp resource (issue #16, Stage 1: read+diff).

Covers the acceptance criteria: fetch/normalize round-trips against the
bundled mock, idempotent normalize(), and apply() raising NotImplementedError
with a message naming the missing write endpoint (no write path exists).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.bgp import Bgp

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

REF = Ref(kind="appliance/bgp", name="config", appliance="BR1-EC")
NE_PK = "3.NE"

_LIVE_SYSTEM_SAMPLE = {
    "stale_path_time": 150,
    "enable_gms_marked": False,
    "enable": False,
    "remote_as_path_advertise": False,
    "log_nbr_msgs": True,
    "rtr_id": "0.0.0.0",
    "asn": 65534,
    "max_restart_time": 120,
    "graceful_restart_en": False,
}


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
    ctx = Ctx(client=client, resolver=Resolver(client))
    return {"ctx": ctx, "settings": settings, "state": state, "client": client}


# -- registration + contract-level shape -------------------------------------


def test_registered_as_appliance_scope_curated_irreversible():
    res = default_registry.get("appliance/bgp")
    assert res.scope is Scope.APPLIANCE
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.IRREVERSIBLE
    assert res.deletable is False


# -- fetch + normalize round-trip against the mock ----------------------------


def test_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Bgp()

    raw = res.fetch(ctx, REF)
    assert raw == {"system": _LIVE_SYSTEM_SAMPLE, "neighbors": {}}

    canonical = res.normalize(raw)
    assert canonical == {
        "system": dict(sorted(_LIVE_SYSTEM_SAMPLE.items())),
        "neighbors": {},
    }


def test_fetch_uses_ne_pk_query_param(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.bgp_system[NE_PK] = {**_LIVE_SYSTEM_SAMPLE, "asn": 65001, "enable": True}
    state.bgp_neighbor[NE_PK] = {
        "10.1.1.1": {"remote_as": 65002, "admin_status": "up"},
    }

    raw = Bgp().fetch(ctx, REF)
    assert raw["system"]["asn"] == 65001
    assert raw["neighbors"] == {"10.1.1.1": {"remote_as": 65002, "admin_status": "up"}}

    # A different appliance's BGP config is unaffected.
    other = Ref(kind="appliance/bgp", name="config", appliance="HUB1-EC")
    other_raw = Bgp().fetch(ctx, other)
    assert other_raw["system"]["asn"] == 65534


def test_diff_empty_when_desired_matches_fetched(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Bgp()
    current = res.normalize(res.fetch(ctx, REF))
    desired = res.canonicalize_desired(ctx, REF, current)
    assert res.diff(REF, current, desired).empty


# -- normalize: idempotent, tolerant of unknown fields and neighbor shapes ----


def test_normalize_is_idempotent():
    res = Bgp()
    raw = {
        "system": {**_LIVE_SYSTEM_SAMPLE, "asn": 65001, "vendorExtra": "x"},
        "neighbors": {"10.1.1.1": {"remote_as": 65002}, "10.1.1.2": {"remote_as": 65003}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_normalize_none_and_empty_raw_are_absent():
    res = Bgp()
    assert res.normalize(None) is None
    assert res.normalize({}) is None


def test_normalize_accepts_neighbor_list_shape_defensively():
    """The live neighbor shape is unconfirmed (module docstring); normalize()
    must not crash if a populated sample turns out to be a list instead of a
    keyed mapping."""
    res = Bgp()
    raw = {
        "system": _LIVE_SYSTEM_SAMPLE,
        "neighbors": [
            {"peer_ip": "10.1.1.1", "remote_as": 65002},
            {"remote_as": 65003},  # no recognizable key field -> falls back to index
        ],
    }
    canonical = res.normalize(raw)
    assert canonical is not None
    neighbors = canonical["neighbors"]
    assert neighbors["10.1.1.1"] == {"peer_ip": "10.1.1.1", "remote_as": 65002}
    assert neighbors["1"] == {"remote_as": 65003}
    # Idempotent even after the list->dict conversion.
    assert res.normalize(canonical) == canonical


def test_normalize_rejects_non_mapping_system():
    with pytest.raises(ValueError, match="mapping"):
        Bgp().normalize({"system": "not-a-mapping", "neighbors": {}})


def test_normalize_rejects_non_mapping_neighbor_entry():
    with pytest.raises(ValueError, match="mapping"):
        Bgp().normalize({"system": {}, "neighbors": {"10.1.1.1": "not-a-mapping"}})


# -- managed_by ----------------------------------------------------------------


def test_managed_by_none_when_no_template_group_selects_bgp(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert Bgp().managed_by(ctx, REF) is None


def test_managed_by_reports_owning_template_group(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.template_groups["Default Template Group"] = {"name": "Default Template Group"}
    state.template_selection["Default Template Group"] = ["bgp"]
    state.template_association[NE_PK] = ["Default Template Group"]

    owner = Bgp().managed_by(ctx, REF)
    assert owner == "template-group Default Template Group"


# -- apply: no write path exists -----------------------------------------------


def test_apply_raises_not_implemented_naming_the_missing_endpoint(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Bgp()
    current = res.normalize(res.fetch(ctx, REF))
    desired = {**current, "system": {**current["system"], "enable": True}}
    diff = res.diff(REF, current, desired)
    assert not diff.empty  # a real diff exists; apply() must still refuse

    with pytest.raises(NotImplementedError, match="no modeled write endpoint"):
        res.apply(ctx, diff)


def test_rollback_also_raises_not_implemented(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    with pytest.raises(NotImplementedError, match="no modeled write endpoint"):
        Bgp().rollback(ctx, REF, None)


# -- list_refs -------------------------------------------------------------------


def test_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    refs = Bgp().list_refs(ctx)
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/bgp" and r.name == "config" for r in refs)
