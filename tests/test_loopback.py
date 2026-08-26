"""Tests for the loopback resources (issue #18): two unrelated-scope kinds.

* ``appliance/loopback`` — per-appliance loopback interfaces, read+diff only
  (mirrors tests/test_bgp.py's Stage-1 pattern: fetch/normalize round-trip,
  idempotent normalize(), apply()/rollback() always raising).
* ``loopback-orch`` — fabric-wide loopback orchestration, full-structure
  GET-then-POST REVERSIBLE (mirrors tests/test_zones_e2e.py's candidate ->
  plan -> commit -> rollback pattern), plus a dedicated test proving the
  vendored SDK's mgmtIp/mgmtIP casing bug is handled defensively.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.loopback import Loopback, LoopbackOrch

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

LOOPBACK_REF = Ref(kind="appliance/loopback", name="loopback", appliance="HUB1-EC")
ORCH_REF = Ref(kind="loopback-orch", name="global")

_LIVE_LO0_SAMPLE = {
    "admin": True,
    "gms_marked": False,
    "ipaddr": "192.168.255.12",
    "label": "",
    "nmask": 32,
    "role_id": 0,
    "vrf_id": 0,
    "zone": 0,
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
    candidate = CandidateStore(settings.host)
    return {
        "ctx": ctx,
        "settings": settings,
        "candidate": candidate,
        "state": state,
        "client": client,
    }


# =============================================================================
# 1. appliance/loopback — read+diff only
# =============================================================================


def test_loopback_registered_as_appliance_scope_curated_irreversible() -> None:
    res = default_registry.get("appliance/loopback")
    assert res.scope is Scope.APPLIANCE
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.IRREVERSIBLE
    assert res.deletable is False


def test_loopback_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Loopback()

    raw = res.fetch(ctx, LOOPBACK_REF)
    assert raw == {"lo0": _LIVE_LO0_SAMPLE}

    canonical = res.normalize(raw)
    # gms_marked is bookkeeping, stripped from canonical state (surfaced via
    # managed_by() instead) — every other field passes through unmodified.
    expected = {k: v for k, v in _LIVE_LO0_SAMPLE.items() if k != "gms_marked"}
    assert canonical == {"lo0": expected}


def test_loopback_fetch_scoped_per_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    # 3.NE and 5.NE were seeded with no loopback interfaces configured.
    other = Ref(kind="appliance/loopback", name="loopback", appliance="BR1-EC")
    raw = Loopback().fetch(ctx, other)
    assert raw == {}


def test_loopback_normalize_is_idempotent() -> None:
    res = Loopback()
    raw = {"lo0": _LIVE_LO0_SAMPLE, "lo1": {**_LIVE_LO0_SAMPLE, "ipaddr": "192.168.255.13"}}
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_loopback_normalize_none_is_absent() -> None:
    assert Loopback().normalize(None) is None


def test_loopback_normalize_empty_dict_is_a_real_empty_table() -> None:
    # Unlike "absent", zero configured loopbacks is a legitimate reading.
    assert Loopback().normalize({}) == {}


def test_loopback_normalize_rejects_non_mapping_entry() -> None:
    with pytest.raises(ValueError, match="mapping"):
        Loopback().normalize({"lo0": "not-a-mapping"})


def test_loopback_normalize_rejects_non_mapping_top_level() -> None:
    with pytest.raises(ValueError, match="mapping"):
        Loopback().normalize(["not", "a", "mapping"])  # type: ignore[arg-type]


def test_loopback_diff_empty_when_desired_matches_fetched(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Loopback()
    current = res.normalize(res.fetch(ctx, LOOPBACK_REF))
    desired = res.canonicalize_desired(ctx, LOOPBACK_REF, current)
    assert res.diff(LOOPBACK_REF, current, desired).empty


# -- managed_by: per-entry gms_marked only, no template-section join ---------


def test_loopback_managed_by_none_when_no_gms_marked_entry(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    # The live sample has gms_marked: false.
    assert Loopback().managed_by(ctx, LOOPBACK_REF) is None


def test_loopback_managed_by_reports_gms_when_marked(world: dict[str, Any]) -> None:
    ctx, state = world["ctx"], world["state"]
    state.appliance_ecos["3.NE"]["virtualif/loopback"] = {
        "lo0": {**_LIVE_LO0_SAMPLE, "gms_marked": True}
    }
    ref = Ref(kind="appliance/loopback", name="loopback", appliance="BR1-EC")
    owner = Loopback().managed_by(ctx, ref)
    assert owner is not None
    assert "gms_marked" in owner


# -- apply/rollback: no documented write endpoint -----------------------------


def test_loopback_apply_raises_not_implemented(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = Loopback()
    current = res.normalize(res.fetch(ctx, LOOPBACK_REF))
    desired = {**current, "lo1": {"admin": True, "ipaddr": "192.168.255.99", "nmask": 32}}
    diff = res.diff(LOOPBACK_REF, current, desired)
    assert not diff.empty  # a real diff exists; apply() must still refuse

    with pytest.raises(NotImplementedError, match="no documented write endpoint"):
        res.apply(ctx, diff)


def test_loopback_rollback_also_raises_not_implemented(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    with pytest.raises(NotImplementedError, match="no documented write endpoint"):
        Loopback().rollback(ctx, LOOPBACK_REF, None)


def test_loopback_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    refs = Loopback().list_refs(ctx)
    assert {r.appliance for r in refs} == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    assert all(r.kind == "appliance/loopback" and r.name == "loopback" for r in refs)


# =============================================================================
# 2. loopback-orch — fabric-wide, REVERSIBLE, full-structure GET-then-POST
# =============================================================================

_LIVE_ORCH_SAMPLE = {
    "0": {
        "loopbackPool": "10.41.0.0/16",
        "interfaces": {"20000": {"mgmtIP": True, "label": "149", "zone": 27}},
    }
}


def test_orch_registered_as_orchestrator_scope_curated_reversible() -> None:
    res = default_registry.get("loopback-orch")
    assert res.scope is Scope.ORCHESTRATOR
    assert res.tier is Tier.CURATED
    assert res.reversibility is Reversibility.REVERSIBLE
    assert res.deletable is False


def test_orch_fetch_normalize_round_trips_against_mock(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = LoopbackOrch()

    raw = res.fetch(ctx, ORCH_REF)
    assert raw == _LIVE_ORCH_SAMPLE

    canonical = res.normalize(raw)
    assert canonical == _LIVE_ORCH_SAMPLE


def test_orch_normalize_is_idempotent() -> None:
    res = LoopbackOrch()
    raw = {
        "0": {"loopbackPool": "10.41.0.0/16", "interfaces": {"20000": {"mgmtIP": False}}},
        "1": {"loopbackPool": "10.42.0.0/16", "interfaces": {}},
    }
    once = res.normalize(raw)
    assert res.normalize(once) == once


def test_orch_normalize_none_and_empty_are_the_empty_table() -> None:
    # loopbackOrch always "exists" (fabric-wide setting, like zones) — an
    # empty structure means "no segments configured", not absence.
    assert LoopbackOrch().normalize(None) == {}
    assert LoopbackOrch().normalize({}) == {}


def test_orch_normalize_rejects_non_mapping_segment() -> None:
    with pytest.raises(ValueError, match="mapping"):
        LoopbackOrch().normalize({"0": "not-a-mapping"})


def test_orch_normalize_rejects_non_mapping_interfaces() -> None:
    with pytest.raises(ValueError, match="mapping"):
        LoopbackOrch().normalize({"0": {"loopbackPool": "10.41.0.0/16", "interfaces": "nope"}})


def test_orch_diff_empty_when_desired_matches_fetched(world: dict[str, Any]) -> None:
    ctx = world["ctx"]
    res = LoopbackOrch()
    current = res.normalize(res.fetch(ctx, ORCH_REF))
    desired = res.canonicalize_desired(ctx, ORCH_REF, current)
    assert res.diff(ORCH_REF, current, desired).empty


# -- the mgmtIp / mgmtIP SDK case-mismatch bug --------------------------------


def test_orch_normalize_folds_mgmtIp_alias_to_mgmtIP() -> None:
    """The vendored SDK's own POST-builder writes 'mgmtIp'; GET returns
    'mgmtIP'. normalize() must canonicalize to the real GET casing so this
    never becomes permanent phantom drift."""
    res = LoopbackOrch()
    raw = {"0": {"loopbackPool": "10.41.0.0/16", "interfaces": {"20000": {"mgmtIp": True}}}}
    canonical = res.normalize(raw)
    iface = canonical["0"]["interfaces"]["20000"]
    assert iface == {"mgmtIP": True}
    assert "mgmtIp" not in iface


def test_orch_normalize_mgmtIP_wins_when_both_keys_present() -> None:
    """Real GET truth takes precedence over the alias if both ever appear."""
    res = LoopbackOrch()
    raw = {
        "0": {
            "loopbackPool": "10.41.0.0/16",
            "interfaces": {"20000": {"mgmtIp": False, "mgmtIP": True}},
        }
    }
    iface = res.normalize(raw)["0"]["interfaces"]["20000"]
    assert iface == {"mgmtIP": True}


def test_orch_canonicalize_desired_also_folds_mgmtIp_alias(world: dict[str, Any]) -> None:
    """canonicalize_desired() (hand-written YAML `set` input path) folds the
    alias too — it defaults to routing through normalize()."""
    ctx = world["ctx"]
    res = LoopbackOrch()
    desired_input = {
        "0": {"loopbackPool": "10.41.0.0/16", "interfaces": {"20000": {"mgmtIp": True}}}
    }
    canonical = res.canonicalize_desired(ctx, ORCH_REF, desired_input)
    assert canonical is not None
    assert canonical["0"]["interfaces"]["20000"] == {"mgmtIP": True}


def test_orch_apply_and_replan_with_mgmtIp_alias_produce_no_drift(
    world: dict[str, Any],
) -> None:
    """End-to-end proof the casing bug can't round-trip as drift: diffing a
    freshly-fetched 'mgmtIP' current state against a hand-written 'mgmtIp'
    desired input is empty once both sides pass through normalize()."""
    ctx = world["ctx"]
    res = LoopbackOrch()
    current = res.normalize(res.fetch(ctx, ORCH_REF))
    assert current is not None
    aliased_input = {
        "0": {
            "loopbackPool": current["0"]["loopbackPool"],
            "interfaces": {"20000": {**current["0"]["interfaces"]["20000"], "mgmtIp": True}},
        }
    }
    # aliased_input's 20000 entry now carries a redundant "mgmtIp": True
    # alongside the real "mgmtIP": True already there.
    desired = res.canonicalize_desired(ctx, ORCH_REF, aliased_input)
    assert res.diff(ORCH_REF, current, desired).empty


# -- e2e: candidate -> plan -> commit -> idempotent replan -> rollback -------


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


def test_orch_lifecycle_full_structure_replace_idempotency_rollback(
    world: dict[str, Any],
) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    # Add a second segment under a placeholder-free explicit id, alongside
    # the seeded segment "0" — the write must carry BOTH segments (never a
    # partial-object POST that would silently drop segment "0").
    candidate.set_path(ORCH_REF, ["1", "loopbackPool"], "10.42.0.0/16")
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "mgmtIP"], False)
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "label"], "150")
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "zone"], 27)
    report = _commit(world)
    assert report.ok, report.messages
    assert state.loopback_orch == {
        "0": {
            "loopbackPool": "10.41.0.0/16",
            "interfaces": {"20000": {"mgmtIP": True, "label": "149", "zone": 27}},
        },
        "1": {
            "loopbackPool": "10.42.0.0/16",
            "interfaces": {"20100": {"mgmtIP": False, "label": "150", "zone": 27}},
        },
    }

    # Replaying the identical intent diffs empty (idempotent).
    candidate.set_path(ORCH_REF, ["1", "loopbackPool"], "10.42.0.0/16")
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "mgmtIP"], False)
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "label"], "150")
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "zone"], 27)
    assert _plan_is_empty(world)

    # Delete segment "1", then roll the transaction back: the full structure
    # (both segments) is restored from the snapshot via the same
    # GET-then-POST write path.
    candidate.delete(ORCH_REF, ["1"])
    assert _commit(world).ok
    assert "1" not in state.loopback_orch

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.loopback_orch == {
        "0": {
            "loopbackPool": "10.41.0.0/16",
            "interfaces": {"20000": {"mgmtIP": True, "label": "149", "zone": 27}},
        },
        "1": {
            "loopbackPool": "10.42.0.0/16",
            "interfaces": {"20100": {"mgmtIP": False, "label": "150", "zone": 27}},
        },
    }


def test_orch_set_with_mgmtIp_alias_writes_mgmtIP_casing(world: dict[str, Any]) -> None:
    """A hand-written `set ... mgmtIp true` must land on the wire (and in the
    mock's state) as `mgmtIP`, never round-tripping the buggy casing."""
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.set_path(ORCH_REF, ["1", "loopbackPool"], "10.43.0.0/16")
    candidate.set_path(ORCH_REF, ["1", "interfaces", "20100", "mgmtIp"], True)
    report = _commit(world)
    assert report.ok, report.messages

    written = state.loopback_orch["1"]["interfaces"]["20100"]
    assert written == {"mgmtIP": True}
    assert "mgmtIp" not in written


def test_orch_default_segment_removal_is_a_real_change_not_protected(
    world: dict[str, Any],
) -> None:
    """Unlike zones' server-managed Default zone, loopbackOrch has no
    server-injected segment — removing segment "0" is an honest change that
    actually applies (no silent re-injection)."""
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    candidate.delete(ORCH_REF, ["0"])
    assert _commit(world).ok
    assert state.loopback_orch == {}


# -- read-only views: pool detail + reclaim -----------------------------------


def test_orch_pool_detail_view(world: dict[str, Any]) -> None:
    from pyecsdwan.resources.loopback import pool_detail

    ctx = world["ctx"]
    detail = pool_detail(ctx)
    assert detail == {
        "0": {"segment": 0, "subnet": "10.41.0.0/16", "totalAddr": 65534,
              "addrAllocated": 1, "addrDeleted": 0}
    }


def test_orch_reclaim_all_and_single(world: dict[str, Any]) -> None:
    from pyecsdwan.resources.loopback import pool_detail, reclaim_deleted_ips

    ctx, state = world["ctx"], world["state"]
    state.loopback_orch_pool["0"]["addrDeleted"] = 3

    reclaim_deleted_ips(ctx, loopback_id=42)
    assert pool_detail(ctx)["0"]["addrDeleted"] == 2

    reclaim_deleted_ips(ctx)
    assert pool_detail(ctx)["0"]["addrDeleted"] == 0
