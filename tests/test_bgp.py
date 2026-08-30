"""Unit + e2e tests for the appliance/bgp resource (issue #16, Stage 2: read+write).

Covers the acceptance criteria: fetch/normalize round-trips against the
bundled mock via the appliance proxy, idempotent normalize(), managed_by(),
and a real apply()/rollback() write path (full-object POST of system +
neighbor table, save-changes, REVERSIBLE snapshot/restore) exercised through
the transaction engine end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.bgp import Bgp

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

REF = Ref(kind="appliance/bgp", name="config", appliance="BR1-EC")
NE_PK = "3.NE"  # BR1-EC — seeded enabled, with a real neighbor (see mock/server.py)
OTHER_REF = Ref(kind="appliance/bgp", name="config", appliance="HUB1-EC")

_ENABLED_SYSTEM_SAMPLE = {
    "stale_path_time": 150,
    "enable_gms_marked": False,
    "enable": True,
    "remote_as_path_advertise": True,
    "log_nbr_msgs": True,
    "route_target": {"0": {"self": 0, "export": "0:0", "import": "0:0"}},
    "rtr_id": "192.168.255.13",
    "asn": 65534,
    "max_restart_time": 120,
    "graceful_restart_en": False,
}
_ENABLED_NEIGHBOR_SAMPLE = {
    "10.127.1.1": {
        "as_override": False,
        "bfd_desired": False,
        "directly_connected": False,
        "enable": True,
        "evpn": False,
        "gms_marked": False,
        "hold": 9,
        "ka": 6,
        "lcl_interface": "any",
        "next_hop_self": True,
        "password": "",
        "remote_as": 65001,
        "rtmap_inbound": "default_rtmap_bgp_inbound_br",
        "rtmap_outbound": "default_rtmap_bgp_outbound_br",
        "store_received_routes": True,
        "type": "Branch",
    }
}
_DISABLED_SYSTEM_SAMPLE = {
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
    candidate = CandidateStore(settings.origin)
    return {
        "ctx": ctx, "settings": settings, "state": state, "client": client,
        "candidate": candidate,
    }


def _commit(world: dict[str, Any], **kwargs: Any) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"], **kwargs)
    if report.ok:
        world["candidate"].clear()
    return report


# -- registration + contract-level shape -------------------------------------


def test_registered_as_appliance_scope_curated_reversible():
    res = default_registry.get("appliance/bgp")
    assert res.scope is Scope.APPLIANCE
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.deletable is False


# -- fetch + normalize round-trip against the mock (via the appliance proxy) --


def test_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Bgp()

    raw = res.fetch(ctx, REF)
    assert raw == {"system": _ENABLED_SYSTEM_SAMPLE, "neighbors": _ENABLED_NEIGHBOR_SAMPLE}

    canonical = res.normalize(raw)
    assert canonical == {
        "system": dict(sorted(_ENABLED_SYSTEM_SAMPLE.items())),
        "neighbors": _ENABLED_NEIGHBOR_SAMPLE,
    }


def test_fetch_isolates_per_appliance_ecos_store(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    # Mutate BR1-EC's ECOS store directly (as a live appliance's own state
    # would differ from the seed) and confirm fetch() reads it live, while a
    # different appliance's store is unaffected.
    state.appliance_ecos[NE_PK]["bgp/config/system"] = {**_ENABLED_SYSTEM_SAMPLE, "asn": 65001}
    state.appliance_ecos[NE_PK]["bgp/config/neighbor"] = {
        "10.1.1.1": {"remote_as": 65002, "enable": True},
    }

    raw = Bgp().fetch(ctx, REF)
    assert raw["system"]["asn"] == 65001
    assert raw["neighbors"] == {"10.1.1.1": {"remote_as": 65002, "enable": True}}

    other_raw = Bgp().fetch(ctx, OTHER_REF)
    assert other_raw["system"] == _DISABLED_SYSTEM_SAMPLE
    assert other_raw["neighbors"] == {}


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
        "system": {**_ENABLED_SYSTEM_SAMPLE, "asn": 65001, "vendorExtra": "x"},
        "neighbors": {"10.1.1.1": {"remote_as": 65002}, "10.1.1.2": {"remote_as": 65003}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_normalize_none_and_empty_raw_are_absent():
    res = Bgp()
    assert res.normalize(None) is None
    assert res.normalize({}) is None


def test_normalize_accepts_neighbor_list_shape_defensively():
    """Defensive insurance kept from Stage 1: normalize() must not crash if a
    future server version returns a list instead of the confirmed
    keyed-by-peer-IP mapping."""
    res = Bgp()
    raw = {
        "system": _DISABLED_SYSTEM_SAMPLE,
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


def test_managed_by_unowned_when_no_group_is_associated(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    assert Bgp().managed_by(ctx, REF).state is Owned.UNOWNED


def test_managed_by_unowned_when_an_associated_group_does_not_select_bgp(
    world: dict[str, Any],
) -> None:
    """A live probe turned this from UNKNOWN into a clean negative.

    While `bgp` was a guessed name — spelled after the ECOS path, never seen in
    a selected-section list — a group that did not select it told us nothing:
    it might select the real section under a name we never compared. That had
    to be UNKNOWN, and #20 made UNKNOWN refuse.

    Orchestrator 9.7 reports `bgp` in its own template vocabulary, so the name
    is confirmed and a non-match now means what it says. The refusal is gone
    because the uncertainty is gone, not because the guard was relaxed — the
    unverified branch still exists and `appliance/vrrp` still takes it.
    """
    ctx, state = world["ctx"], world["state"]
    state.template_groups["Branch-Std"] = {"name": "Branch-Std"}
    state.template_selection["Branch-Std"] = ["dns"]
    state.template_association[NE_PK] = ["Branch-Std"]

    owns = Bgp().managed_by(ctx, REF)
    assert owns.state is Owned.UNOWNED
    assert not owns.blocks_write


def test_managed_by_reports_owning_template_group(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.template_groups["Default Template Group"] = {"name": "Default Template Group"}
    state.template_selection["Default Template Group"] = ["bgp"]
    state.template_association[NE_PK] = ["Default Template Group"]

    owns = Bgp().managed_by(ctx, REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Default Template Group"


# -- apply/rollback: real writes through the transaction engine ---------------


def test_idempotent_replan_after_commit_is_empty(world: dict[str, Any]) -> None:
    ctx, candidate = world["ctx"], world["candidate"]
    current = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert isinstance(current, dict)
    candidate.set_desired(REF, current)  # re-stage exactly what's already there
    report = _commit(world)
    assert report.ok
    assert "no changes" in report.messages[0].lower() or report.messages == []

    # A fresh replan against the same (unmodified) live state stays empty.
    plan = txn.build_plan(ctx, default_registry, candidate)
    assert plan.empty


def test_apply_writes_system_and_neighbor_then_rollback_restores(world: dict[str, Any]) -> None:
    ctx, candidate, state = world["ctx"], world["candidate"], world["state"]
    before = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert isinstance(before, dict)

    desired = {
        "system": {**before["system"], "asn": 65099, "enable": False},
        "neighbors": {},  # remove the neighbor entirely
    }
    candidate.set_desired(REF, desired)
    report = _commit(world)
    assert report.ok, report.messages

    after_apply = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert isinstance(after_apply, dict)
    assert after_apply["system"]["asn"] == 65099
    assert after_apply["system"]["enable"] is False
    assert after_apply["neighbors"] == {}
    # save-changes cleared the dirty flag on the live (mock) appliance.
    appliance = next(a for a in state.appliances if a["nePk"] == NE_PK)
    assert appliance["hasUnsavedChanges"] is False

    rollback_report = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert rollback_report.ok, rollback_report.messages

    after_rollback = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert after_rollback == before


def test_failed_save_changes_fails_and_reverts_apply(world: dict[str, Any]) -> None:
    ctx, candidate, state = world["ctx"], world["candidate"], world["state"]
    before = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert isinstance(before, dict)

    desired = {"system": {**before["system"], "asn": 65098}, "neighbors": before["neighbors"]}
    candidate.set_desired(REF, desired)
    state.fail_next_action = True
    report = _commit(world)
    assert not report.ok

    # Auto-reverted: live state matches what it was before the failed apply.
    after = Bgp().normalize(Bgp().fetch(ctx, REF))
    assert after == before


# -- list_refs -------------------------------------------------------------------


def test_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    refs = Bgp().list_refs(ctx)
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/bgp" and r.name == "config" for r in refs)
