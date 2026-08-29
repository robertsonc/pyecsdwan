"""Versioned local state: refuse the future, never erase what you did not model.

Issue #108. Three defects, all the same shape — reading state through a schema
you cannot prove applies to it, then writing it back:

* the candidate store wrote `format: 1` and never read it, so a file from a
  newer binary was parsed as if it were current and rewritten as format 1;
* a candidate item's mode fell through to `merge` when it was missing or
  unrecognised, so `replace-all` — one typo from `replace` — silently kept
  every live key the operator staged a replace to remove;
* `TxnMeta.from_json` dropped unmodelled fields, and `_write_meta` rewrites
  the whole file, so the next state transition erased them.

The unifying rule is that refusing must not damage: a future-format file is
not corrupt, it is newer, and the binary that wrote it can still use it — so
these paths leave the bytes alone rather than quarantining or rewriting.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pyecsdwan import candidate as candidate_mod
from pyecsdwan import journal as journal_mod
from pyecsdwan.candidate import (
    CANDIDATE_FORMAT,
    SUPPORTED_MODES,
    CandidateFormatError,
    CandidateItem,
    CandidateStore,
    materialize_desired,
)
from pyecsdwan.contract import Ref
from pyecsdwan.journal import META_FORMAT, JournalFormatError, TxnJournal, TxnMeta

HOST = "orch.example.com"
REF = Ref("appliance/banners", "global", appliance="BR1-EC")


def _staged(state_home: Any) -> CandidateStore:
    store = CandidateStore(HOST)
    store.set_desired(REF, {"issue": "staged"})
    return store


def _rewrite(store: CandidateStore, **changes: Any) -> bytes:
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw.update(changes)
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    return store.path.read_bytes()


# -- candidate format --------------------------------------------------------


def test_a_future_candidate_format_is_refused(state_home: Any) -> None:
    store = _staged(state_home)
    _rewrite(store, format=CANDIDATE_FORMAT + 1)

    with pytest.raises(CandidateFormatError) as caught:
        CandidateStore(HOST)

    assert str(CANDIDATE_FORMAT + 1) in str(caught.value)


def test_refusing_a_future_format_leaves_the_bytes_alone(state_home: Any) -> None:
    """The property that separates this from corruption handling.

    `CandidateCorruptError` *quarantines* — it renames the file — which is
    right for damage and wrong here: the newer binary that wrote this can
    still use it, and moving or rewriting it would destroy working state.
    """
    store = _staged(state_home)
    before = _rewrite(store, format=CANDIDATE_FORMAT + 1)

    with pytest.raises(CandidateFormatError):
        CandidateStore(HOST)

    assert store.path.read_bytes() == before
    assert not store.path.with_suffix(".corrupt").exists()


def test_the_current_format_still_loads(state_home: Any) -> None:
    """Guards the guard: a check that refused everything would make the
    candidate store unusable and every test above would still pass."""
    store = _staged(state_home)
    assert json.loads(store.path.read_text())["format"] == CANDIDATE_FORMAT

    assert len(CandidateStore(HOST)) == 1


def test_a_missing_format_is_read_as_the_first_one(state_home: Any) -> None:
    """Files written before the field existed. Absent means oldest, not
    newest — the opposite reading would refuse state this build can handle."""
    store = _staged(state_home)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    del raw["format"]
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    assert len(CandidateStore(HOST)) == 1


# -- candidate modes ---------------------------------------------------------


@pytest.mark.parametrize("mode", ["replace-all", "REPLACE", "patch", "", None])
def test_an_unrecognised_mode_is_refused_on_load(state_home: Any, mode: Any) -> None:
    """`replace-all` and `REPLACE` are the realistic ones: near-misses for a
    real mode, and merging when a replace was meant leaves on the appliance
    exactly the keys the operator was removing."""
    store = _staged(state_home)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["items"][0]["mode"] = mode
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CandidateFormatError):
        CandidateStore(HOST)


def test_every_supported_mode_still_round_trips(state_home: Any) -> None:
    """Guards the guard for the mode check, over the declared set rather than
    a hand-copied list — a mode added to `SUPPORTED_MODES` without a loader
    path fails here instead of in the field."""
    store = _staged(state_home)
    for mode in sorted(SUPPORTED_MODES):
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        raw["items"][0]["mode"] = mode
        store.path.write_text(json.dumps(raw), encoding="utf-8")

        assert CandidateStore(HOST).items[REF.key()].mode == mode


def test_materialize_refuses_an_unknown_mode_too() -> None:
    """Checked at the point of use as well as at load, because
    `commit --rebase` re-materializes intent through this function directly
    and never passes back through the loader."""
    item = CandidateItem(ref_key=REF.key(), mode="replace-all", intent={"issue": "only this"})

    with pytest.raises(CandidateFormatError):
        materialize_desired(item, {"issue": "old", "motd": "would have survived"})


def test_merge_still_merges_and_replace_still_replaces() -> None:
    """The behaviour the mode check protects, stated so a fix that refused
    everything is not mistaken for a fix."""
    live = {"issue": "old", "motd": "keep under merge"}
    merged = materialize_desired(
        CandidateItem(ref_key="k", mode="merge", intent={"issue": "new"}), live
    )
    replaced = materialize_desired(
        CandidateItem(ref_key="k", mode="replace", intent={"issue": "new"}), live
    )

    assert merged == {"issue": "new", "motd": "keep under merge"}
    assert replaced == {"issue": "new"}


# -- journal metadata --------------------------------------------------------


def test_a_future_meta_format_is_refused(state_home: Any) -> None:
    with pytest.raises(JournalFormatError):
        TxnMeta.from_json(
            {"txn_id": "x", "created_at": "t", "orch_host": HOST, "format": META_FORMAT + 1}
        )


def test_an_unmodelled_field_survives_a_state_write(state_home: Any) -> None:
    """`_write_meta` rewrites the whole file, so a field this build does not
    model is erased by the next STATE change unless it is carried."""
    journal = TxnJournal.create(HOST, [REF])
    path = journal.dir / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["future_field"] = "must survive"
    path.write_text(json.dumps(raw), encoding="utf-8")

    reopened = TxnJournal.open(journal.dir)
    reopened.set_state(journal_mod.TxnState.APPLYING)

    assert json.loads(path.read_text(encoding="utf-8"))["future_field"] == "must survive"


def test_modelled_fields_win_over_carried_ones(state_home: Any) -> None:
    """`extra` carries passengers, never overrides. If a stale copy of a field
    this build owns could win, the state machine would read its own decisions
    back from data it had already superseded."""
    meta = TxnMeta.from_json(
        {"txn_id": "x", "created_at": "t", "orch_host": HOST, "state": "PENDING"}
    )
    meta.extra["state"] = "CONFIRMED"

    assert meta.to_json()["state"] == "PENDING"


def test_a_future_format_journal_is_listed_as_unreadable_not_fatal(
    state_home: Any,
) -> None:
    """One future-format directory must not take down `show journal` for every
    other transaction — it is surfaced through the same unreadable channel as
    a torn meta.json."""
    journal = TxnJournal.create(HOST, [REF])
    readable = TxnJournal.create(HOST, [REF])
    path = journal.dir / "meta.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["format"] = META_FORMAT + 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    ids = {t.meta.txn_id for t in journal_mod.list_txns()}
    assert readable.meta.txn_id in ids
    assert journal.meta.txn_id not in ids
    assert journal.dir.name in journal_mod.unreadable_txn_dirs()


def test_the_declared_format_is_what_gets_written(state_home: Any) -> None:
    """Pins the constant to the file rather than to a literal, so bumping
    `CANDIDATE_FORMAT` without a migration entry is caught here."""
    store = _staged(state_home)

    assert json.loads(store.path.read_text())["format"] == CANDIDATE_FORMAT
    assert set(candidate_mod.CANDIDATE_MIGRATIONS) <= set(range(1, CANDIDATE_FORMAT))
