"""Unit tests for pyecsdwan.journal: crash-safe transaction journal."""

import os

import pytest

from pyecsdwan import journal as journal_mod
from pyecsdwan.contract import Ref
from pyecsdwan.journal import (
    TxnJournal,
    TxnState,
    committed_history,
    list_txns,
    orphaned_txns,
    prune_history,
)

REFS = [
    Ref(kind="interface-labels", name="global"),
    Ref(kind="bio", appliance="edge1", name="corp"),
]


@pytest.fixture
def sequential_txn_ids(monkeypatch):
    """Deterministic, strictly increasing txn ids (creation order == sort order)."""
    counter = iter(range(1, 100))

    def fake_id():
        n = next(counter)
        return f"202601{n:02d}-000000-{n:08d}"

    monkeypatch.setattr(journal_mod, "new_txn_id", fake_id)


def _create():
    return TxnJournal.create("orch.example.com", REFS)


def test_create_open_meta_round_trip(state_home):
    txn = _create()
    assert str(txn.dir).startswith(str(state_home))
    assert txn.meta.state == TxnState.PENDING
    assert txn.meta.orch_host == "orch.example.com"
    assert txn.meta.items == ["interface-labels:global", "bio:edge1:corp"]

    reopened = TxnJournal.open(txn.dir)
    assert reopened.meta == txn.meta


def test_append_and_events_ordering(state_home):
    txn = _create()
    txn.append("APPLY_START", ref="interface-labels:global")
    txn.append("APPLY_END", ref="interface-labels:global", ok=True)
    events = txn.events()
    assert [e["event"] for e in events] == ["TXN_BEGIN", "APPLY_START", "APPLY_END"]
    assert events[0]["items"] == txn.meta.items
    assert events[2]["ok"] is True
    assert all("ts" in e for e in events)


def test_torn_final_line_is_tolerated(state_home):
    txn = _create()
    txn.append("APPLY_START", ref="interface-labels:global")
    with open(txn.dir / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-08-26T00:00:00+00:00", "event": "TOR')  # crash mid-write
    events = txn.events()
    assert [e["event"] for e in events] == ["TXN_BEGIN", "APPLY_START"]


def test_record_snapshot_and_snapshots(state_home):
    txn = _create()
    txn.record_snapshot(REFS[0], {"wan": {"1": {"name": "MPLS"}}, "lan": {}})
    txn.record_snapshot(REFS[1], None)  # resource did not exist pre-change
    assert txn.snapshots() == {
        "interface-labels:global": {"wan": {"1": {"name": "MPLS"}}, "lan": {}},
        "bio:edge1:corp": None,
    }


def test_set_state_rewrites_meta_on_disk(state_home):
    txn = _create()
    txn.set_state(TxnState.APPLYING)
    assert TxnJournal.open(txn.dir).meta.state == "APPLYING"
    txn.set_state(TxnState.CONFIRMED)
    assert TxnJournal.open(txn.dir).meta.state == "CONFIRMED"
    states = [e["state"] for e in txn.events() if e["event"] == "STATE"]
    assert states == ["APPLYING", "CONFIRMED"]


def test_write_confirm_marker(state_home):
    txn = _create()
    assert not txn.confirm_marker.exists()
    txn.write_confirm_marker()
    assert txn.confirm_marker.exists()
    assert txn.confirm_marker.read_text(encoding="utf-8").startswith("20")


def test_list_txns_newest_first(state_home, sequential_txn_ids):
    created = [_create().meta.txn_id for _ in range(3)]
    listed = [t.meta.txn_id for t in list_txns()]
    assert listed == list(reversed(created))


def test_committed_history_filters_confirmed(state_home, sequential_txn_ids):
    first = _create()
    first.set_state(TxnState.CONFIRMED)
    failed = _create()
    failed.set_state(TxnState.FAILED)
    second = _create()
    second.set_state(TxnState.CONFIRMED)
    unconfirmed = _create()
    unconfirmed.set_state(TxnState.APPLIED_UNCONFIRMED)

    history = [t.meta.txn_id for t in committed_history()]
    assert history == [second.meta.txn_id, first.meta.txn_id]


def test_orphaned_txns(state_home):
    orphan = _create()
    orphan.set_state(TxnState.APPLIED_UNCONFIRMED)  # no watchdog pid file

    watched = _create()
    watched.set_state(TxnState.APPLIED_UNCONFIRMED)
    watched.watchdog_pid_file.write_text(str(os.getpid()), encoding="utf-8")

    confirmed = _create()
    confirmed.set_state(TxnState.CONFIRMED)
    failed = _create()
    failed.set_state(TxnState.FAILED)

    assert {t.meta.txn_id for t in orphaned_txns()} == {orphan.meta.txn_id}


def test_prune_history_keeps_non_terminal_and_newest_terminal(state_home, sequential_txn_ids):
    old_confirmed = _create()
    old_confirmed.set_state(TxnState.CONFIRMED)
    old_failed = _create()
    old_failed.set_state(TxnState.FAILED)
    in_flight = _create()
    in_flight.set_state(TxnState.APPLYING)  # non-terminal: never pruned
    reverted = _create()
    reverted.set_state(TxnState.REVERTED)
    newest = _create()
    newest.set_state(TxnState.CONFIRMED)

    removed = prune_history(keep=2)
    assert removed == 2
    assert not old_confirmed.dir.exists()
    assert not old_failed.dir.exists()
    remaining = {t.meta.txn_id for t in list_txns()}
    assert remaining == {
        in_flight.meta.txn_id,
        reverted.meta.txn_id,
        newest.meta.txn_id,
    }
