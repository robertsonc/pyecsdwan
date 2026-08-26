"""Phase-3 zones slice end-to-end: candidate -> plan -> commit -> rollback
against the bundled mock, proving placeholder allocation via /zones/nextId,
idempotent replans, deleteDependencies on zone removal, and that the id
allocator is never rewound by a rollback."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread

REF = Ref(kind="zones", name="global")


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


def _plan_is_empty(world: dict[str, Any]) -> bool:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    return plan.empty


def test_zone_lifecycle_allocation_idempotency_rollback(world: dict[str, Any]) -> None:
    state: MockState = world["state"]
    candidate: CandidateStore = world["candidate"]

    # Create a new zone under a placeholder key: its id must come from the
    # allocator (mock seeds nextId=1), which then advances past the new id.
    candidate.set_path(REF, ["zones", "guest", "name"], "Guest")
    report = _commit(world)
    assert report.ok, report.messages
    assert state.zones == {"0": {"name": "Default"}, "1": {"name": "Guest"}}
    assert state.zones_next_id == 2

    # Replaying the same intent resolves the placeholder to the existing id:
    # the plan is empty and nothing is written (idempotency).
    candidate.set_path(REF, ["zones", "guest", "name"], "Guest")
    assert _plan_is_empty(world)

    # Toggle end-to-end ZBFW; the zone table itself is untouched.
    candidate.set_path(REF, ["endToEnd"], True)
    assert _commit(world).ok
    assert state.zones_ee_enable is True
    assert state.zones == {"0": {"name": "Default"}, "1": {"name": "Guest"}}

    # Delete the zone (applies with deleteDependencies=true), then roll the
    # transaction back: the table and flag are restored from the snapshot,
    # but the id allocator is never rewound.
    candidate.delete(REF, ["zones", "1"])
    assert _commit(world).ok
    assert "1" not in state.zones

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.zones == {"0": {"name": "Default"}, "1": {"name": "Guest"}}
    assert state.zones_ee_enable is True
    assert state.zones_next_id == 2  # ids are never reused, even after rollback


def test_default_zone_cannot_be_deleted(world: dict[str, Any]) -> None:
    # Removing zone 0 is a silent no-op: normalize() re-injects the
    # server-managed Default zone on both sides, so the plan stays empty
    # (the server would re-add it to any POSTed table anyway).
    candidate: CandidateStore = world["candidate"]
    candidate.delete(REF, ["zones", "0"])
    assert _plan_is_empty(world)
