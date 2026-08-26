"""Unit tests for the appliance/ospf resource (issue #17, Stage 1: read+diff).

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
from pyecsdwan.resources.ospf import Ospf

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

REF = Ref(kind="appliance/ospf", name="config", appliance="BR1-EC")
NE_PK = "3.NE"

_LIVE_SYSTEM_SAMPLE = {
    "redistMapToOSPF": "default_rtmap_to_ospf",
    "enable": False,
    "routerId": "0.0.0.0",
    "opaque_enable": True,
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
    res = default_registry.get("appliance/ospf")
    assert res.scope is Scope.APPLIANCE
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.IRREVERSIBLE
    assert res.deletable is False


# -- fetch + normalize round-trip against the mock ----------------------------


def test_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Ospf()

    raw = res.fetch(ctx, REF)
    assert raw == {"system": _LIVE_SYSTEM_SAMPLE, "interfaces": {}}

    canonical = res.normalize(raw)
    assert canonical == {
        "system": dict(sorted(_LIVE_SYSTEM_SAMPLE.items())),
        "interfaces": {},
    }


def test_fetch_uses_ne_pk_query_param(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.ospf_system[NE_PK] = {**_LIVE_SYSTEM_SAMPLE, "routerId": "10.0.0.1", "enable": True}
    state.ospf_interfaces[NE_PK] = {
        "ge1": {"area": "0.0.0.0", "cost": 10},
    }

    raw = Ospf().fetch(ctx, REF)
    assert raw["system"]["routerId"] == "10.0.0.1"
    assert raw["interfaces"] == {"ge1": {"area": "0.0.0.0", "cost": 10}}

    # A different appliance's OSPF config is unaffected.
    other = Ref(kind="appliance/ospf", name="config", appliance="HUB1-EC")
    other_raw = Ospf().fetch(ctx, other)
    assert other_raw["system"]["routerId"] == "0.0.0.0"


def test_diff_empty_when_desired_matches_fetched(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Ospf()
    current = res.normalize(res.fetch(ctx, REF))
    desired = res.canonicalize_desired(ctx, REF, current)
    assert res.diff(REF, current, desired).empty


# -- normalize: idempotent, tolerant of unknown fields and interface shapes ---


def test_normalize_is_idempotent():
    res = Ospf()
    raw = {
        "system": {**_LIVE_SYSTEM_SAMPLE, "routerId": "10.0.0.1", "vendorExtra": "x"},
        "interfaces": {"ge1": {"area": "0.0.0.0"}, "ge2": {"area": "0.0.0.1"}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_normalize_none_and_empty_raw_are_absent():
    res = Ospf()
    assert res.normalize(None) is None
    assert res.normalize({}) is None


def test_normalize_accepts_interface_list_shape_defensively():
    """The live interface shape is unconfirmed (module docstring); normalize()
    must not crash if a populated sample turns out to be a list instead of a
    keyed mapping."""
    res = Ospf()
    raw = {
        "system": _LIVE_SYSTEM_SAMPLE,
        "interfaces": [
            {"ifName": "ge1", "area": "0.0.0.0"},
            {"area": "0.0.0.1"},  # no recognizable key field -> falls back to index
        ],
    }
    canonical = res.normalize(raw)
    assert canonical is not None
    interfaces = canonical["interfaces"]
    assert interfaces["ge1"] == {"ifName": "ge1", "area": "0.0.0.0"}
    assert interfaces["1"] == {"area": "0.0.0.1"}
    # Idempotent even after the list->dict conversion.
    assert res.normalize(canonical) == canonical


def test_normalize_rejects_non_mapping_system():
    with pytest.raises(ValueError, match="mapping"):
        Ospf().normalize({"system": "not-a-mapping", "interfaces": {}})


def test_normalize_rejects_non_mapping_interface_entry():
    with pytest.raises(ValueError, match="mapping"):
        Ospf().normalize({"system": {}, "interfaces": {"ge1": "not-a-mapping"}})


# -- managed_by ----------------------------------------------------------------


def test_managed_by_none_when_no_template_group_selects_ospf(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert Ospf().managed_by(ctx, REF) is None


def test_managed_by_reports_owning_template_group(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.template_groups["Default Template Group"] = {"name": "Default Template Group"}
    state.template_selection["Default Template Group"] = ["ospf"]
    state.template_association[NE_PK] = ["Default Template Group"]

    owner = Ospf().managed_by(ctx, REF)
    assert owner == "template-group Default Template Group"


# -- apply: no write path exists -----------------------------------------------


def test_apply_raises_not_implemented_naming_the_missing_endpoint(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Ospf()
    current = res.normalize(res.fetch(ctx, REF))
    desired = {**current, "system": {**current["system"], "enable": True}}
    diff = res.diff(REF, current, desired)
    assert not diff.empty  # a real diff exists; apply() must still refuse

    with pytest.raises(NotImplementedError, match="no modeled write endpoint"):
        res.apply(ctx, diff)


def test_rollback_also_raises_not_implemented(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    with pytest.raises(NotImplementedError, match="no modeled write endpoint"):
        Ospf().rollback(ctx, REF, None)


# -- list_refs -------------------------------------------------------------------


def test_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    refs = Ospf().list_refs(ctx)
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/ospf" and r.name == "config" for r in refs)
