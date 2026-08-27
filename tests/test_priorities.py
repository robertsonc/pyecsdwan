"""Overlay + template-group priorities (#36): unit normalize/validation plus a
candidate -> plan -> commit -> rollback round trip against the bundled mock.

The two resources deliberately normalize differently — a keyed bijection for
overlay priorities, an order-preserving list for the template-group apply
order — so both halves are pinned here, including the "order is data, never
sorted" property that is the opposite of this codebase's usual list rule.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.priorities import OverlayPriority, TemplateGroupPriority

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread

OVERLAY_REF = Ref(kind="overlay-priority", name="global")
TG_REF = Ref(kind="template-group-priority", name="global")

OVERLAY = OverlayPriority()
TG = TemplateGroupPriority()


# -- unit: overlay priority map ----------------------------------------------


def test_overlay_normalize_accepts_wire_and_canonical_shapes() -> None:
    wire = {"1": 5, "2": 6}
    canonical = OVERLAY.normalize(wire)
    assert canonical == {"priorities": {"1": 5, "2": 6}}
    # Feeding canonical state back in is a no-op: normalize(normalize(x)) == normalize(x).
    assert OVERLAY.normalize(canonical) == canonical
    assert OVERLAY.normalize(None) == {"priorities": {}}
    assert OVERLAY.normalize({}) == {"priorities": {}}


def test_overlay_normalize_orders_by_priority_and_canonicalizes_keys() -> None:
    canonical = OVERLAY.normalize({"10": 3, "02": 2, "1": "1"})
    assert canonical == {"priorities": {"1": 1, "2": 2, "10": 3}}
    # Numeric (not lexicographic) key order, so diffs and renders are stable.
    assert isinstance(canonical, dict)
    assert list(canonical["priorities"]) == ["1", "2", "10"]


def test_overlay_duplicate_overlay_id_is_rejected_pre_flight() -> None:
    with pytest.raises(ValueError) as excinfo:
        OVERLAY.normalize({"1": 5, "2": 5})
    message = str(excinfo.value)
    assert "id 5" in message
    assert "1" in message and "2" in message


def test_overlay_duplicate_priority_after_key_canonicalization_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate overlay priority 1"):
        OVERLAY.normalize({"1": 5, "01": 6})


@pytest.mark.parametrize(
    "raw",
    [
        {"first": 5},  # priority key is not an integer
        {"1": "CorpFabric"},  # names resolve in canonicalize_desired, not normalize
        {"1": True},  # bools are ints in Python but never an overlay id
        {"1": None},
        {"priorities": ["1", "2"]},  # wrong container type under the wrapper
    ],
)
def test_overlay_normalize_rejects_non_integer_entries(raw: Any) -> None:
    with pytest.raises(ValueError):
        OVERLAY.normalize(raw)


def test_overlay_resource_class_attributes() -> None:
    assert OVERLAY.kind == "overlay-priority"
    assert OVERLAY.reversibility is Reversibility.REVERSIBLE
    assert OVERLAY.tier is Tier.CURATED
    assert OVERLAY.deletable is False
    assert OVERLAY.dependencies == ("bio",)
    assert "overlay-priority" in default_registry


# -- unit: template group apply order ----------------------------------------


def test_template_normalize_preserves_order() -> None:
    # Order IS the data: this list must come back exactly as given, NOT sorted.
    raw = {"priorities": ["Zulu", "Alpha", "Mike"]}
    canonical = TG.normalize(raw)
    assert canonical == {"priorities": ["Zulu", "Alpha", "Mike"]}
    assert TG.normalize(canonical) == canonical
    assert canonical != TG.normalize({"priorities": sorted(raw["priorities"])})


def test_template_normalize_shapes() -> None:
    assert TG.normalize(None) == {"priorities": []}
    assert TG.normalize({}) == {"priorities": []}
    assert TG.normalize(["A", "B"]) == {"priorities": ["A", "B"]}
    # The vendored SDK posts {"templateIds": [...]}; folded so YAML copied
    # from it round-trips instead of diffing as phantom drift.
    assert TG.normalize({"templateIds": ["A"]}) == {"priorities": ["A"]}


def test_template_duplicate_group_is_rejected_pre_flight() -> None:
    with pytest.raises(ValueError) as excinfo:
        TG.normalize({"priorities": ["Branch-Std", "HQ", "Branch-Std"]})
    message = str(excinfo.value)
    assert "Branch-Std" in message
    assert "0" in message and "2" in message


@pytest.mark.parametrize(
    "raw",
    [
        {"priorities": [""]},
        {"priorities": [None]},
        {"priorities": [3]},
        {"priorities": "Branch-Std"},
        {"unexpected": []},
    ],
)
def test_template_normalize_rejects_bad_entries(raw: Any) -> None:
    with pytest.raises(ValueError):
        TG.normalize(raw)


def test_template_resource_class_attributes() -> None:
    assert TG.kind == "template-group-priority"
    assert TG.reversibility is Reversibility.REVERSIBLE
    assert TG.tier is Tier.CURATED
    assert TG.deletable is False
    assert TG.dependencies == ("template-group",)
    assert "template-group-priority" in default_registry


# -- end-to-end against the mock ---------------------------------------------


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
    ctx = Ctx(client=client, resolver=Resolver(client))
    candidate = CandidateStore(settings.host)
    return {"ctx": ctx, "settings": settings, "candidate": candidate, "state": state}


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    if report.ok:
        world["candidate"].clear()
    return report


def _plan(world: dict[str, Any]) -> txn.Plan:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    return plan


def test_overlay_priority_roundtrip_by_name_then_reorder_then_rollback(
    world: dict[str, Any],
) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    state.overlays["2"] = {"id": 2, "name": "Guest", "modifiedTime": 0}
    assert state.overlay_priority == {"1": 1}

    # Intent may name the overlay; canonical state speaks the server id.
    candidate.set_path(OVERLAY_REF, ["priorities", "2"], "Guest")
    report = _commit(world)
    assert report.ok, report.messages
    assert state.overlay_priority == {"1": 1, "2": 2}

    # Replaying the same intent is a no-op (idempotent normalize + resolve).
    candidate.set_path(OVERLAY_REF, ["priorities", "2"], "Guest")
    assert _plan(world).empty

    # Swap the two overlays' route-map order; the POST carries the whole map.
    candidate.set_path(OVERLAY_REF, ["priorities", "1"], 2)
    candidate.set_path(OVERLAY_REF, ["priorities", "2"], 1)
    assert _commit(world).ok
    assert state.overlay_priority == {"1": 2, "2": 1}

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.overlay_priority == {"1": 1, "2": 2}

    # Entry-level delete on the singleton: the reduced map is POSTed whole.
    candidate.delete(OVERLAY_REF, ["priorities", "1"])
    assert _commit(world).ok
    assert state.overlay_priority == {"2": 2}


def test_overlay_priority_collision_fails_before_any_write(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    state.overlays["2"] = {"id": 2, "name": "Guest", "modifiedTime": 0}

    # Overlay 1 already holds priority 1; giving it priority 2 as well is the
    # collision the API would 4xx on. It must fail at plan time instead.
    candidate.set_path(OVERLAY_REF, ["priorities", "2"], "CorpFabric")
    with pytest.raises(ValueError, match="two priorities"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    assert state.overlay_priority == {"1": 1}


def test_template_group_priority_order_is_a_real_change(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    assert state.template_group_priorities == ["Default Template Group"]

    candidate.set_path(TG_REF, ["priorities"], ["Default Template Group", "Branch-Std"])
    assert _commit(world).ok
    assert state.template_group_priorities == ["Default Template Group", "Branch-Std"]

    # Same intent again: no change.
    candidate.set_path(TG_REF, ["priorities"], ["Default Template Group", "Branch-Std"])
    assert _plan(world).empty

    # Reordering the *same* names is a real change, not a no-op — the whole
    # point of not sorting the list in normalize().
    candidate.set_path(TG_REF, ["priorities"], ["Branch-Std", "Default Template Group"])
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    assert not plan.empty
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    assert report.ok, report.messages
    world["candidate"].clear()
    assert state.template_group_priorities == ["Branch-Std", "Default Template Group"]

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.template_group_priorities == ["Default Template Group", "Branch-Std"]


def test_template_group_duplicate_fails_before_any_write(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(TG_REF, ["priorities"], ["Branch-Std", "Branch-Std"])
    with pytest.raises(ValueError, match="appears twice"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    assert state.template_group_priorities == ["Default Template Group"]


@pytest.mark.parametrize("ref", [OVERLAY_REF, TG_REF])
def test_priority_singletons_refuse_whole_resource_delete(
    world: dict[str, Any], ref: Ref
) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.delete(ref)
    with pytest.raises(txn.CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


def test_rollback_refuses_an_absent_snapshot(world: dict[str, Any]) -> None:
    # A missing snapshot must never replay as "POST an empty structure": that
    # would wipe the fabric's ordering rather than restore it.
    for resource, ref in ((OVERLAY, OVERLAY_REF), (TG, TG_REF)):
        result = resource.rollback(world["ctx"], ref, None)
        assert not result.ok
        assert "refusing" in result.message


# -- optional live smoke test (never runs in CI; needs a real Orchestrator) ---


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL"),
    reason="live Orchestrator smoke test: set ECSDWAN_ORCH_URL (and credentials) to run",
)
def test_live_priority_shapes_are_readable() -> None:
    """Read-only: both endpoints exist and their real payloads normalize."""
    from pyecsdwan.runtime import bootstrap

    ctx, _registry, _settings = bootstrap()
    overlay = OVERLAY.normalize(OVERLAY.fetch(ctx, OVERLAY_REF))
    template = TG.normalize(TG.fetch(ctx, TG_REF))
    assert isinstance(overlay, dict) and isinstance(overlay["priorities"], dict)
    assert isinstance(template, dict) and isinstance(template["priorities"], list)
    # Idempotency against real server state, not just fixtures.
    assert OVERLAY.normalize(overlay) == overlay
    assert TG.normalize(template) == template
