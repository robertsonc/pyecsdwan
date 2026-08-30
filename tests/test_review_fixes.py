"""Regression tests for the adversarial-review fixes (Phase-0/1 hardening).

Each test pins a specific finding so the fix can't silently regress.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore, deep_merge, prune_path
from pyecsdwan.client import OrchClient, _guard_relative_path
from pyecsdwan.contract import Ref
from pyecsdwan.journal import TxnJournal, TxnState, orphaned_txns

BASE = "https://orch.example.com/gms/rest"


@pytest.fixture
def settings() -> config.Settings:
    return config.Settings(orch_url="https://orch.example.com", api_key="sekret-key-123")


# -- SEC-1: absolute-URL / traversal key exfiltration ------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "http://evil.example/steal",
        "https://evil/x",
        "//evil/steal",
        "../../etc/passwd",
        "/a/../../x",
    ],
)
def test_path_guard_blocks_escape(bad: str) -> None:
    with pytest.raises(ValueError):
        _guard_relative_path(bad)


@pytest.mark.parametrize("ok", ["/action/status", "/gms/interfaceLabels", "/appliance/rest"])
def test_path_guard_allows_relative(ok: str) -> None:
    _guard_relative_path(ok)  # no raise


@respx.mock
def test_request_refuses_absolute_url(settings: config.Settings) -> None:
    route = respx.get("http://evil.example/steal").mock(return_value=httpx.Response(200))
    client = OrchClient(settings)
    with pytest.raises(ValueError, match="absolute"):
        client.request("GET", "http://evil.example/steal")
    assert not route.called  # the request was never sent


# -- SEC-7: token scrubbed from error text -----------------------------------


@respx.mock
def test_error_text_scrubs_api_key(settings: config.Settings) -> None:
    respx.get(f"{BASE}/x").mock(
        return_value=httpx.Response(400, text="bad header X-Auth-Token: sekret-key-123")
    )
    client = OrchClient(settings)
    with pytest.raises(Exception) as exc:
        client.get("/x", expected=(200,))
    assert "sekret-key-123" not in str(exc.value)
    assert "REDACTED" in str(exc.value)


# -- DIF-8: Ref keys survive ':' in names ------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        Ref("appliance/bgp", "peer:10.1.1.1", appliance="BR1-EC"),
        Ref("interface-labels", "site:A"),
        Ref("bio", "Corp:Fabric"),
    ],
)
def test_ref_key_roundtrip_with_colons(ref: Ref) -> None:
    assert Ref.from_key(ref.key()) == ref


# -- DIF-7: delete-subtree then set keeps only the set value -----------------


def test_delete_subtree_then_set_prunes_base(state_home: Any) -> None:
    store = CandidateStore("orch.example.com")
    ref = Ref("interface-labels", "global")
    store.delete(ref, ["wan"])
    store.set_path(ref, ["wan", "9", "name"], "LTE")
    current = {"wan": {"1": {"name": "MPLS"}, "2": {"name": "INET"}}, "lan": {}}
    desired = store.desired_for(store.items[ref.key()], current)
    # The pre-existing wan labels are gone; only label 9 remains.
    assert desired["wan"] == {"9": {"name": "LTE"}}


def test_deep_merge_and_prune_helpers() -> None:
    merged = deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
    assert merged == {"a": {"x": 1, "y": 2}}
    state = {"a": {"b": 1, "c": 2}}
    prune_path(state, ["a", "b"])
    assert state == {"a": {"c": 2}}


# -- CRITICAL: host scoping on orphan/rollback -------------------------------


def test_orphaned_txns_scoped_by_host(state_home: Any) -> None:
    a = TxnJournal.create("orch-A", [Ref("interface-labels", "global")])
    a.record_snapshot(Ref("interface-labels", "global"), {"wan": {}, "lan": {}})
    a.append("APPLY_START", ref="interface-labels:global")
    a.set_state(TxnState.APPLIED_UNCONFIRMED)
    b = TxnJournal.create("orch-B", [Ref("interface-labels", "global")])
    b.set_state(TxnState.APPLIED_UNCONFIRMED)

    a_only = orphaned_txns(origin="orch-A")
    # Canonical origins: hostnames are case-insensitive, so the identity is
    # lowercased and carries the scheme it was reached over.
    assert [t.meta.orch_origin for t in a_only] == ["https://orch-a"]
    assert {t.meta.orch_origin for t in orphaned_txns()} == {
        "https://orch-a",
        "https://orch-b",
    }


def test_revert_refuses_cross_host(state_home: Any, settings: config.Settings) -> None:
    other = TxnJournal.create("some-other-host", [Ref("interface-labels", "global")])
    other.record_snapshot(Ref("interface-labels", "global"), {"wan": {}, "lan": {}})
    other.append("APPLY_START", ref="interface-labels:global")
    other.set_state(TxnState.APPLIED_UNCONFIRMED)

    import pyecsdwan.resources  # noqa: F401
    from pyecsdwan.contract import Ctx
    from pyecsdwan.registry import default_registry
    from pyecsdwan.resolver import Resolver

    client = OrchClient(settings)  # settings.origin == https://orch.example.com
    ctx = Ctx(client=client, resolver=Resolver(client))
    report = txn.revert_txn_dir(other.dir, reason="test", ctx=ctx, registry=default_registry)
    assert not report.ok
    assert "refusing" in report.messages[0].lower()


# -- CRITICAL: confirm-vs-revert atomic claim --------------------------------


def test_decision_claim_is_exclusive(state_home: Any) -> None:
    j = TxnJournal.create("orch-A", [Ref("x", "y")])
    assert j.try_claim("confirm") == "confirm"
    # A second claim (e.g. the watchdog) reads back the winner.
    assert j.try_claim("revert") == "confirm"
    assert j.try_claim("confirm") == "confirm"


# -- API-5: jobs terminal/success table ---------------------------------------


def test_job_at_100pct_still_in_progress_is_not_finished() -> None:
    from pyecsdwan.jobs import _record_finished

    # percentComplete 100 but explicitly In Progress -> not yet terminal.
    assert not _record_finished(
        {"taskStatus": "In Progress", "percentComplete": 100, "endTime": 0}
    )
    assert _record_finished({"taskStatus": "Completed", "percentComplete": 100, "endTime": 1})


def test_completed_with_error_result_is_failure() -> None:
    """The original API-5 regression, re-pointed at `_record_state` (#64).

    Its second assertion used to read
    ``assert _record_succeeded({"taskStatus": "Completed", "result": "template
    pushed"})`` — i.e. it *required* the poller to call an unrecognised result
    text a success. That is the very hole #64 closes, so the assertion is now
    inverted: an unrecognised shape is UNKNOWN, and every caller branches on
    ``state != "SUCCESS"``.
    """
    from pyecsdwan.jobs import _record_state

    assert _record_state({"taskStatus": "Completed", "result": "error: push denied"}) == "FAILED"
    assert _record_state({"taskStatus": "Completed", "result": "template pushed"}) == "UNKNOWN"
    assert _record_state({"taskStatus": "Completed", "result": "Success"}) == "SUCCESS"


# -- DIF-4: interface-labels default fill (idempotency vs injecting server) ----


def test_interface_labels_default_fill_idempotent(state_home: Any) -> None:
    import pyecsdwan.resources.interface_labels as il

    res = il.InterfaceLabels()
    # User intent: only 'name' set. normalize() fills active/topology so it
    # converges with a server that injects those defaults.
    user = res.normalize({"wan": {"9": {"name": "LTE"}}, "lan": {}})
    server = res.normalize(
        {"wan": {"9": {"name": "LTE", "active": False, "topology": 0}}, "lan": {}}
    )
    assert user == server
    # And a whole-resource delete of a singleton is refused at plan time via
    # deletable=False (checked in txn.build_plan); the attribute is set here.
    assert res.deletable is False


def test_interface_labels_rollback_refuses_empty_snapshot(state_home: Any) -> None:
    import pyecsdwan.resources.interface_labels as il

    res = il.InterfaceLabels()
    result = res.rollback(ctx=None, ref=Ref("interface-labels", "global"), snapshot=None)  # type: ignore[arg-type]
    assert not result.ok
    assert "empty" in result.message.lower()
