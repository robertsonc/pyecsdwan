"""Save-changes primitive (#11) end to end against the bundled mock.

Proxied appliance writes (``/appliance/rest?nePk=&url=``) mutate running
config only and set ``hasUnsavedChanges``; ``ctx.save_changes`` persists them
and clears the flag. Includes a minimal appliance-scope resource driven
through the transaction engine to prove the Phase-2 pattern (#12+): apply()/
rollback() proxy-write, then one batched save per operation, with a failed
save failing the commit and triggering auto-revert.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.journal import TxnState
from pyecsdwan.registry import Registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread


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


def _appliance(state: MockState, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


# -- the primitive against the mock -------------------------------------------


def test_proxy_write_then_save_clears_unsaved_flag(world: dict[str, Any]) -> None:
    client, state, ctx = world["client"], world["state"], world["ctx"]
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 9000})
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True

    outcome = ctx.save_changes(["3.NE"])
    assert outcome.state == "SUCCESS"
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    # The proxied write itself landed (save persists, it does not clobber).
    assert state.appliance_ecos["3.NE"]["virtualif/ip"] == {"mtu": 9000}


def test_batched_save_covers_multiple_appliances_in_one_action(world: dict[str, Any]) -> None:
    client, state, ctx = world["client"], world["state"], world["ctx"]
    client.appliance_request("POST", "3.NE", "dhcpd/config", json_body={"pool": "a"})
    client.appliance_request("POST", "5.NE", "dhcpd/config", json_body={"pool": "b"})
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True
    assert _appliance(state, "5.NE")["hasUnsavedChanges"] is True

    # Unordered input with a duplicate: one batched POST, one action key.
    outcome = ctx.save_changes(["5.NE", "3.NE", "5.NE"])
    assert outcome.state == "SUCCESS"
    assert len(state.actions) == 1
    assert outcome.per_appliance.keys() == {"3.NE", "5.NE"}
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    assert _appliance(state, "5.NE")["hasUnsavedChanges"] is False


def test_failed_save_reports_failure_and_leaves_flag_set(world: dict[str, Any]) -> None:
    client, state, ctx = world["client"], world["state"], world["ctx"]
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 1500})
    state.fail_next_action = True

    outcome = ctx.save_changes("3.NE")  # bare-string form is accepted too
    assert outcome.state == "FAILED"
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True


def test_dry_run_skips_the_save(world: dict[str, Any]) -> None:
    client, state = world["client"], world["state"]
    dry_ctx = Ctx(client=client, resolver=world["ctx"].resolver, dry_run=True)
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 1400})

    outcome = dry_ctx.save_changes(["3.NE"])
    assert outcome.state == "SUCCESS"
    assert "dry-run" in outcome.detail
    assert not state.actions  # no save POSTed
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True


def test_empty_save_is_noop(world: dict[str, Any]) -> None:
    outcome = world["ctx"].save_changes([])
    assert outcome.state == "SUCCESS"
    assert not world["state"].actions


# -- the #12+ pattern through the transaction engine --------------------------
#
# There are no shipped appliance-scope resources yet (Phase 2 starts with
# #12); this test-local resource emulates the pattern the promotion checklist
# prescribes — proxy-write, then one batched ctx.save_changes per apply()/
# rollback() — proving the primitive composes with plan/commit/verify/revert.

_ECOS_PATH = "fakeEcos/settings"


class EcosEcho(Resource):
    kind = "ecos-echo"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        assert ref.appliance is not None
        return ctx.resolver.ne_pk_for(ref.appliance)

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _ECOS_PATH)
        return raw if isinstance(raw, dict) and raw else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict) or not raw:
            return None
        return {key: raw[key] for key in sorted(raw)}

    def _write(self, ctx: Ctx, ref: Ref, payload: RawState, action: str) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        if payload is None:
            ctx.client.appliance_request("DELETE", ne_pk, _ECOS_PATH)
        else:
            ctx.client.appliance_request("POST", ne_pk, _ECOS_PATH, json_body=payload)
        save = ctx.save_changes([ne_pk], f"save {ref}")
        if save.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=f"{action} not persisted — save-changes {save.state}: {save.detail}",
                jobs=[save],
            )
        return ApplyResult(ok=True, jobs=[save], message=f"{action} persisted")

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        return self._write(ctx, diff.ref, diff.desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        return self._write(ctx, ref, self.normalize(snapshot), "rollback")


@pytest.fixture
def engine_world(world: dict[str, Any]) -> dict[str, Any]:
    registry = Registry()
    registry.register(EcosEcho())
    world["registry"] = registry
    world["candidate"] = CandidateStore(world["settings"].origin)
    return world


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], world["registry"], world["candidate"])
    return txn.commit(world["ctx"], world["registry"], plan, world["settings"])


def test_txn_apply_persists_and_is_idempotent(engine_world: dict[str, Any]) -> None:
    world = engine_world
    state = world["state"]
    ref = Ref(kind="ecos-echo", name="settings", appliance="BR1-EC")
    world["candidate"].set_desired(ref, {"mtu": 9000})

    report = _commit(world)
    assert report.ok, report.messages
    assert state.appliance_ecos["3.NE"][_ECOS_PATH] == {"mtu": 9000}
    # apply() saved: the change is persisted, not just running config.
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    saves_after_apply = len(state.actions)
    assert saves_after_apply == 1

    # Idempotency: same intent again -> empty plan, zero writes, zero saves.
    world["candidate"].clear()
    world["candidate"].set_desired(ref, {"mtu": 9000})
    plan = txn.build_plan(world["ctx"], world["registry"], world["candidate"])
    assert plan.empty, [(i.ref.key(), i.diff.entries) for i in plan.changed_items]
    assert len(state.actions) == saves_after_apply

    # rollback <1> restores the pre-change (absent) state AND persists it.
    world["candidate"].clear()
    restore = txn.rollback_history_txn(world["ctx"], world["registry"], world["settings"], n=1)
    assert restore.ok, restore.messages
    assert _ECOS_PATH not in state.appliance_ecos["3.NE"]
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    assert len(state.actions) == saves_after_apply + 1


def test_txn_failed_save_fails_commit_and_reverts(engine_world: dict[str, Any]) -> None:
    world = engine_world
    state = world["state"]
    # Pre-existing persisted config on the appliance.
    state.appliance_ecos["3.NE"] = {_ECOS_PATH: {"mtu": 1400}}
    ref = Ref(kind="ecos-echo", name="settings", appliance="BR1-EC")
    world["candidate"].set_desired(ref, {"mtu": 9000})
    state.fail_next_action = True  # the apply's save fails

    report = _commit(world)
    assert not report.ok
    assert report.state == TxnState.REVERTED
    # Running config restored from the snapshot, and the restore persisted.
    assert state.appliance_ecos["3.NE"][_ECOS_PATH] == {"mtu": 1400}
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
