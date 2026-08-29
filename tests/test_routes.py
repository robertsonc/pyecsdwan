"""Tests for the static-routes / subnets resource (#15).

Covers the acceptance criteria: idempotent prefix-sorted normalize(),
managed_by() preferring the per-object gms_marked flag over the coarser
template-section join, add/delete-delta apply() (never a full-table
replace), and rollback as the precise inverse of what one operation
changed — add-then-rollback removes only what was added, delete-then-
rollback restores only what was removed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref
from pyecsdwan.journal import TxnState
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.routes import Routes, all_routes
from pyecsdwan.txn import CommitError

REF = Ref(kind="appliance/routes", name="global", appliance="BR1-EC")
NE_PK = "3.NE"


# -- normalize: pure function, no ctx/network involved -------------------------


def test_normalize_none_yields_empty_table() -> None:
    assert Routes().normalize(None) == {"prefix": {}}


def test_normalize_strips_self_echoes_and_gms_marked() -> None:
    raw = {
        "prefix": {
            "0.0.0.0/0": {
                "self": "0.0.0.0/0",
                "advert": True,
                "advert_bgp": False,
                "advert_ospf": False,
                "local": True,
                "nhop": {
                    "0.0.0.0": {
                        "self": "0.0.0.0",
                        "interface": {
                            "default": {
                                "self": "default",
                                "gms_marked": True,
                                "zone_id": 65534,
                                "metric": 50,
                                "dir": "ANY",
                            }
                        },
                    }
                },
            }
        }
    }
    once = Routes().normalize(raw)
    prefix = once["prefix"]["0.0.0.0/0"]
    nhop = prefix["nhop"]["0.0.0.0"]
    iface = nhop["interface"]["default"]
    assert "self" not in prefix
    assert "self" not in nhop
    assert "self" not in iface
    assert "gms_marked" not in iface
    # Idempotent round-trip (the Tier-2 promotion checklist's litmus test).
    assert Routes().normalize(once) == once


def test_normalize_fills_confirmed_server_defaults() -> None:
    raw = {"prefix": {"10.0.0.0/8": {"nhop": {"10.1.1.1": {"interface": {"wan0": {}}}}}}}
    once = Routes().normalize(raw)
    entry = once["prefix"]["10.0.0.0/8"]
    assert entry["advert"] is False
    assert entry["advert_bgp"] is False
    assert entry["advert_ospf"] is False
    assert entry["local"] is True
    iface = entry["nhop"]["10.1.1.1"]["interface"]["wan0"]
    assert iface["zone_id"] == 65534  # confirmed "no zone" sentinel
    assert iface["metric"] == 0
    assert iface["dir"] == "ANY"


def test_normalize_passes_through_unknown_confirmed_fields() -> None:
    raw = {
        "prefix": {
            "0.0.0.0/0": {
                "nhop": {
                    "0.0.0.0": {
                        "interface": {
                            "default": {
                                "comment": "Default route",
                                "dest_mac": "00:00:00:00:00:00",
                                "label": 1,
                                "no_subshared": False,
                                "vni": 16777216,
                                "vxlan": False,
                            }
                        }
                    }
                }
            }
        }
    }
    default_route = Routes().normalize(raw)["prefix"]["0.0.0.0/0"]
    iface = default_route["nhop"]["0.0.0.0"]["interface"]["default"]
    assert iface["comment"] == "Default route"
    assert iface["dest_mac"] == "00:00:00:00:00:00"
    assert iface["label"] == 1
    assert iface["no_subshared"] is False
    assert iface["vni"] == 16777216
    assert iface["vxlan"] is False


def test_normalize_sorts_prefixes_numerically_by_cidr() -> None:
    raw = {
        "prefix": {
            "192.168.0.0/16": {"nhop": {}},
            "0.0.0.0/0": {"nhop": {}},
            "10.0.0.0/8": {"nhop": {}},
        }
    }
    assert list(Routes().normalize(raw)["prefix"]) == [
        "0.0.0.0/0",
        "10.0.0.0/8",
        "192.168.0.0/16",
    ]


def test_normalize_rejects_non_mapping_route() -> None:
    with pytest.raises(ValueError, match="mapping"):
        Routes().normalize({"prefix": {"10.0.0.0/8": "not-a-mapping"}})


def test_normalize_rejects_non_mapping_nhop() -> None:
    with pytest.raises(ValueError, match="nhop"):
        Routes().normalize({"prefix": {"10.0.0.0/8": {"nhop": "nope"}}})


def test_ne_pk_requires_appliance_on_ref() -> None:
    with pytest.raises(ValueError, match="appliance-scoped"):
        Routes._ne_pk(None, Ref(kind="appliance/routes", name="global"))  # type: ignore[arg-type]


def test_registered_as_appliance_routes() -> None:
    resource = default_registry.get("appliance/routes")
    assert resource.kind == "appliance/routes"
    assert resource.deletable is False


# -- e2e against the bundled mock: fetch, apply deltas, rollback ---------------


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
    candidate = CandidateStore(settings.host)
    return {"ctx": ctx, "settings": settings, "state": state, "candidate": candidate}


def _appliance(state: MockState, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    if report.ok:
        world["candidate"].clear()
    return report


def _plan_is_empty(world: dict[str, Any]) -> bool:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    return plan.empty


def test_fetch_reads_configured_not_all(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    raw = Routes().fetch(ctx, REF)
    assert raw is not None
    prefixes = raw.get("prefix") or {}
    assert "0.0.0.0/0" in prefixes  # seeded configured route
    assert "10.99.0.0/24" not in prefixes  # seeded "all"-only learned route


def test_all_routes_view_merges_configured_and_learned(world: dict[str, Any]) -> None:
    view = all_routes(world["ctx"], "BR1-EC")
    assert "0.0.0.0/0" in view["prefix"]
    assert "10.99.0.0/24" in view["prefix"]


def test_idempotent_replan_is_empty(world: dict[str, Any]) -> None:
    ctx, candidate = world["ctx"], world["candidate"]
    assert _plan_is_empty(world)  # nothing staged
    current = Routes().normalize(Routes().fetch(ctx, REF))
    candidate.set_desired(REF, current)
    assert _plan_is_empty(world)  # staging exactly the server's own state


def test_add_route_then_rollback_removes_only_what_was_added(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(
        REF,
        ["prefix", "10.0.0.0/8", "nhop", "10.1.1.1", "interface", "wan0", "metric"],
        100,
    )
    candidate.set_path(REF, ["prefix", "10.0.0.0/8", "local"], True)

    report = _commit(world)
    assert report.ok, report.messages
    configured = state.static_routes[NE_PK]["prefix"]
    assert "10.0.0.0/8" in configured
    assert "0.0.0.0/0" in configured  # untouched by the add
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    configured_after = state.static_routes[NE_PK]["prefix"]
    assert "10.0.0.0/8" not in configured_after  # precisely reverted
    assert "0.0.0.0/0" in configured_after  # still untouched
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False


def test_delete_route_then_rollback_restores_only_what_was_removed(
    world: dict[str, Any],
) -> None:
    candidate, state = world["candidate"], world["state"]
    state.static_routes[NE_PK]["prefix"]["172.16.0.0/12"] = {
        "self": "172.16.0.0/12",
        "advert": False,
        "advert_bgp": False,
        "advert_ospf": False,
        "local": True,
        "nhop": {
            "172.16.0.1": {
                "self": "172.16.0.1",
                "interface": {
                    "wan0": {
                        "self": "wan0",
                        "zone_id": 65534,
                        "metric": 10,
                        "dir": "ANY",
                        "gms_marked": False,
                    }
                },
            }
        },
    }

    candidate.delete(REF, ["prefix", "172.16.0.0/12"])
    report = _commit(world)
    assert report.ok, report.messages
    configured = state.static_routes[NE_PK]["prefix"]
    assert "172.16.0.0/12" not in configured
    assert "0.0.0.0/0" in configured  # untouched by the delete

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    configured_after = state.static_routes[NE_PK]["prefix"]
    assert "172.16.0.0/12" in configured_after  # precisely restored
    assert "0.0.0.0/0" in configured_after


def test_modify_route_deletes_stale_then_adds_new(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(
        REF,
        ["prefix", "0.0.0.0/0", "nhop", "0.0.0.0", "interface", "default", "metric"],
        75,
    )
    report = _commit(world)
    assert report.ok, report.messages
    entry = state.static_routes[NE_PK]["prefix"]["0.0.0.0/0"]
    assert entry["nhop"]["0.0.0.0"]["interface"]["default"]["metric"] == 75

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    entry_after = state.static_routes[NE_PK]["prefix"]["0.0.0.0/0"]
    assert entry_after["nhop"]["0.0.0.0"]["interface"]["default"]["metric"] == 50


def test_failed_save_fails_apply_and_reverts(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(
        REF,
        ["prefix", "10.0.0.0/8", "nhop", "10.1.1.1", "interface", "wan0", "metric"],
        100,
    )
    state.fail_next_action = True  # consumed by the apply's own save-changes

    report = _commit(world)
    assert not report.ok
    assert report.state == TxnState.REVERTED
    configured = state.static_routes[NE_PK]["prefix"]
    assert "10.0.0.0/8" not in configured  # revert's compensating delete landed
    assert "0.0.0.0/0" in configured
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False  # revert's save succeeded


def test_whole_resource_delete_is_refused(world: dict[str, Any]) -> None:
    world["candidate"].delete(REF)  # no path -> whole-resource delete
    with pytest.raises(CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


# -- managed_by: gms_marked preferred over the template-section join -----------


def test_managed_by_unowned_when_no_group_is_associated(world: dict[str, Any]) -> None:
    assert Routes().managed_by(world["ctx"], REF).state is Owned.UNOWNED


def test_managed_by_prefers_gms_marked(world: dict[str, Any]) -> None:
    state = world["state"]
    iface = state.static_routes[NE_PK]["prefix"]["0.0.0.0/0"]["nhop"]["0.0.0.0"]["interface"]
    iface["default"]["gms_marked"] = True

    owns = Routes().managed_by(world["ctx"], REF)
    assert owns.state is Owned.OWNED
    assert "gms_marked" in owns.owner


def test_managed_by_falls_back_to_template_section(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["Branch-Std"] = {"name": "Branch-Std", "templates": []}
    state.template_selection["Branch-Std"] = ["routes"]
    state.template_association[NE_PK] = ["Branch-Std"]

    owns = Routes().managed_by(world["ctx"], REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Branch-Std"
