"""Unit tests for pyecsdwan.journal: crash-safe transaction journal."""

import json
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


def test_prune_history_keeps_rollback_history_and_non_terminal(state_home, sequential_txn_ids):
    # CONFIRMED transactions form the rollback history (quota=keep); audit and
    # failed/reverted records are pruned under a SEPARATE, larger quota so a
    # burst of them can never evict a rollback point. Non-terminal is never
    # pruned.
    oldest_confirmed = _create()
    oldest_confirmed.set_state(TxnState.CONFIRMED)
    mid_confirmed = _create()
    mid_confirmed.set_state(TxnState.CONFIRMED)
    in_flight = _create()
    in_flight.set_state(TxnState.APPLYING)  # non-terminal: never pruned
    reverted = _create()
    reverted.set_state(TxnState.REVERTED)
    newest_confirmed = _create()
    newest_confirmed.set_state(TxnState.CONFIRMED)

    # keep=2 rollback points, audit_keep=1 audit/dead record.
    removed = prune_history(keep=2, audit_keep=1)
    assert removed == 1  # only the oldest CONFIRMED beyond the 2 kept
    assert not oldest_confirmed.dir.exists()
    remaining = {t.meta.txn_id for t in list_txns()}
    assert remaining == {
        mid_confirmed.meta.txn_id,
        newest_confirmed.meta.txn_id,
        in_flight.meta.txn_id,
        reverted.meta.txn_id,
    }


def test_prune_history_audit_burst_never_evicts_rollback_point(state_home, sequential_txn_ids):
    confirmed = _create()
    confirmed.set_state(TxnState.CONFIRMED)
    for _ in range(5):
        audit = _create()
        audit.set_state(TxnState.AUDIT_ONLY)
    prune_history(keep=1, audit_keep=200)
    # The lone rollback point survives a flood of audit records.
    assert confirmed.dir.exists()
    assert confirmed.meta.txn_id in {t.meta.txn_id for t in list_txns()}


# -- torn tails and corrupt history (#110) -----------------------------------


def _tear(journal, fragment=b'{"ts": "2026-01-01", "event": "APPLY_ST'):
    """Simulate a crash mid-append: bytes with no terminating newline."""
    with open(journal.dir / "events.jsonl", "ab") as fh:
        fh.write(fragment)


def test_an_append_after_a_torn_tail_keeps_the_new_event(state_home):
    """#110. `append` wrote `line + "\\n"`, so a file not ending in a newline
    meant the next record was concatenated onto the fragment and the whole
    line became unparseable — destroying the fragment *and* the new event,
    silently, because the reader skipped malformed lines."""
    txn = _create()
    _tear(txn)

    txn.append("APPLY_RESULT", ref="interface-labels:global", ok=True)

    events = [e["event"] for e in txn.events()]
    assert "APPLY_RESULT" in events


def test_a_lost_snapshot_is_what_made_that_serious(state_home):
    """The consequence, stated where it bites: the record after a tear is
    usually the pre-change snapshot, and without it a revert has nothing to
    restore from while `applied_refs` still says the resource was changed."""
    txn = _create()
    _tear(txn)
    txn.record_snapshot(REFS[0], {"before": "the only copy"})

    assert REFS[0].key() in txn.snapshots()


def test_the_repair_is_recorded_not_silent(state_home):
    """A journal that quietly edits itself is not an audit log. The discarded
    bytes are a digest and a length rather than quoted text — a fragment can
    be part of a snapshot body, which is why this file is 0600."""
    txn = _create()
    _tear(txn)
    txn.append("APPLY_START", ref="interface-labels:global")

    repair = next(e for e in txn.events() if e["event"] == "JOURNAL_REPAIRED")
    assert repair["action"] == "discarded"
    assert repair["bytes"] > 0
    assert len(repair["sha256"]) == 64
    assert "APPLY_ST" not in json.dumps(repair)


def test_a_complete_record_missing_only_its_newline_is_kept(state_home):
    """The case that makes truncation unsafe as a blanket rule.

    If the record bytes landed and only the newline did not, the record is
    whole — discarding it would be exactly the data loss this repair exists to
    prevent. So the tail is *parsed* rather than assumed partial, and a valid
    one is terminated instead of thrown away.
    """
    txn = _create()
    _tear(txn, b'{"ts": "2026-01-01", "event": "SNAPSHOT", "ref": "x", "exists": false}')

    txn.append("APPLY_START", ref="x")

    events = [e["event"] for e in txn.events()]
    assert "SNAPSHOT" in events, "a complete record was discarded as if partial"
    repair = next(e for e in txn.events() if e["event"] == "JOURNAL_REPAIRED")
    assert repair["action"] == "terminated"


def test_a_well_formed_log_is_left_alone(state_home):
    """Guards the guard: a repair that fired every time would append a
    JOURNAL_REPAIRED to every transaction and make the audit trail noise."""
    txn = _create()
    txn.append("APPLY_START", ref="interface-labels:global")
    txn.append("VERIFIED", ref="interface-labels:global")

    assert not [e for e in txn.events() if e["event"] == "JOURNAL_REPAIRED"]


def test_recovery_refuses_a_history_with_a_hole_in_it(state_home):
    """A malformed *interior* line cannot be a torn tail — the tail repair
    runs before every append — so it is corruption. Rollback computed from a
    history with a hole is worse than a refused one."""
    txn = _create()
    txn.record_snapshot(REFS[0], {"before": 1})
    path = txn.dir / "events.jsonl"
    lines = path.read_text().splitlines()
    lines.insert(1, "{not json at all")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(journal_mod.JournalCorrupt):
        txn.snapshots()
    with pytest.raises(journal_mod.JournalCorrupt):
        txn.applied_refs()


def test_display_stays_lenient_so_a_corrupt_journal_is_still_visible(state_home):
    """The other half of that decision. `show journal` refusing to render a
    corrupt journal would hide the very transaction an operator is trying to
    find, which is the opposite of failing closed."""
    txn = _create()
    path = txn.dir / "events.jsonl"
    path.write_text(path.read_text() + "{not json at all\n", encoding="utf-8")

    assert [e["event"] for e in txn.events()] == ["TXN_BEGIN"]


# -- the repair's own boundaries ---------------------------------------------
#
# `_repair_tail` branches on: no file, empty file, already terminated, a tail
# that is the *whole* file, a tail that is not valid UTF-8. The tests above
# cover the two interesting middles; these cover the edges, because #110's
# fourth criterion asks for every append boundary and shipping the branches
# untested is how the original bug got in.


def test_repair_on_a_journal_with_no_event_log(state_home):
    """`TxnJournal.open` on a directory whose events.jsonl never got created —
    the crash happened between mkdir and the first append."""
    txn = _create()
    (txn.dir / "events.jsonl").unlink()

    txn.append("APPLY_START", ref="x")

    assert [e["event"] for e in txn.events()] == ["APPLY_START"]


def test_repair_on_an_empty_event_log(state_home):
    """Zero bytes is well-formed, not torn. Truncating or terminating it would
    write a JOURNAL_REPAIRED into a log that had nothing wrong with it."""
    txn = _create()
    (txn.dir / "events.jsonl").write_bytes(b"")

    txn.append("APPLY_START", ref="x")

    assert [e["event"] for e in txn.events()] == ["APPLY_START"]


def test_a_file_that_is_nothing_but_a_fragment(state_home):
    """The `cut == 0` branch: the crash landed before any record completed, so
    there is no preceding newline to cut back to and the whole file goes."""
    txn = _create()
    (txn.dir / "events.jsonl").write_bytes(b'{"ts": "2026-01-01", "eve')

    txn.append("APPLY_START", ref="x")

    events = [e["event"] for e in txn.events()]
    assert events == ["JOURNAL_REPAIRED", "APPLY_START"]


def test_a_tail_of_invalid_utf8_is_discarded_not_crashed(state_home):
    """A partial write can cut a multi-byte character in half. Decoding it
    raises something other than JSONDecodeError, and an append that died here
    would leave the journal permanently unwritable — the failure mode being
    fixed, with an extra step."""
    txn = _create()
    with open(txn.dir / "events.jsonl", "ab") as fh:
        fh.write(b'{"ts": "2026-01-01", "event": "\xe2\x82')  # truncated euro sign

    txn.append("APPLY_START", ref="x")

    events = [e["event"] for e in txn.events()]
    assert "APPLY_START" in events
    repair = next(e for e in txn.events() if e["event"] == "JOURNAL_REPAIRED")
    assert repair["action"] == "discarded"


def test_repair_is_idempotent_across_consecutive_appends(state_home):
    """Once repaired, the log is well-formed, so the next append must not
    repair again — a JOURNAL_REPAIRED per event would drown the audit trail."""
    txn = _create()
    _tear(txn)
    txn.append("APPLY_START", ref="x")
    txn.append("VERIFIED", ref="x")

    repairs = [e for e in txn.events() if e["event"] == "JOURNAL_REPAIRED"]
    assert len(repairs) == 1
