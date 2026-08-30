"""Transaction lifecycle under the commit lock (#100).

Two invariants, both about the same mistake: deciding what to do to a
transaction from information gathered before you had the right to act on it.

* **Orphan classification.** The watchdog exists only for confirm windows, so
  an ordinary `APPLYING` transaction — a commit running *right now* in another
  process — has no watchdog pid and read as an orphan. Recovery would then wait
  on the commit lock that live commit was holding, acquire it the moment the
  commit finished, and restore snapshots over work that had just succeeded.
* **Compare-and-set.** `revert_txn_dir` read the journal *before* waiting for
  the lock, and waiting is exactly when it goes stale.

Both are tested with a real second process. Holding a lock in-process proves
nothing: the locks are deliberately re-entrant per process, so an in-process
"holder" nests instead of excluding.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import txn
from pyecsdwan.contract import Ref
from pyecsdwan.journal import TxnJournal, TxnState, orphaned_txns

HOST = "orch.example.com"
REF = Ref("appliance/banners", "global", appliance="BR1-EC")

#: Holds the commit lock for one transaction, then optionally settles it.
_HOLDER = """
import sys, time
from pathlib import Path
from pyecsdwan.journal import TxnJournal, TxnState
from pyecsdwan.locking import HostLock

host, txn_id, ready, go, txn_dir, settle = sys.argv[1:7]
with HostLock(host, "commit", timeout=20.0, txn_id=txn_id):
    Path(ready).write_text("ready")
    deadline = time.monotonic() + 30
    while not Path(go).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if settle == "settle":
        TxnJournal.open(Path(txn_dir)).set_state(TxnState.CONFIRMED)
"""


@pytest.fixture
def holder(state_home: Any, tmp_path: Path) -> Any:
    procs: list[subprocess.Popen[str]] = []

    def _start(journal: TxnJournal, settle: bool = False) -> tuple[Path, Path]:
        ready = tmp_path / f"ready-{journal.meta.txn_id}"
        go = tmp_path / f"go-{journal.meta.txn_id}"
        proc = subprocess.Popen(
            [
                sys.executable, "-c", _HOLDER, HOST, journal.meta.txn_id,
                str(ready), str(go), str(journal.dir),
                "settle" if settle else "no",
            ],
            env=dict(os.environ, ECSDWAN_HOME=str(state_home)),
            text=True,
        )
        procs.append(proc)
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "holder never took the commit lock"
        return proc, go

    yield _start
    for proc in procs:
        proc.kill()
        proc.wait()


def _applying(state: str = TxnState.APPLYING) -> TxnJournal:
    journal = TxnJournal.create(HOST, [REF])
    journal.record_snapshot(REF, {"issue": "before"})
    journal.append("APPLY_START", ref=REF.key())
    journal.set_state(state)
    return journal


def _orphan_ids() -> list[str]:
    return [t.meta.txn_id for t in orphaned_txns(origin=HOST)]


# -- orphan classification ---------------------------------------------------


def test_a_commit_in_flight_is_not_an_orphan(state_home: Any, holder: Any) -> None:
    """The bug: an APPLYING transaction has no watchdog, so the scan offered a
    running commit for recovery."""
    journal = _applying()
    holder(journal)

    assert journal.meta.txn_id not in _orphan_ids()


def test_it_becomes_recoverable_once_its_driver_dies(
    state_home: Any, holder: Any
) -> None:
    """Guards the guard, and it is the one that matters: a rule that protected
    every transaction would make `rollback --pending` useless and leave real
    orphans stranded forever."""
    journal = _applying()
    proc, _go = holder(journal)
    assert journal.meta.txn_id not in _orphan_ids()

    proc.kill()
    proc.wait()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and journal.meta.txn_id not in _orphan_ids():
        time.sleep(0.05)

    assert journal.meta.txn_id in _orphan_ids()


def test_a_lock_held_for_another_transaction_protects_nothing(
    state_home: Any, holder: Any
) -> None:
    """Matching on the owner's `txn_id` rather than on "the lock is busy".

    Two transactions can exist on one host, and a lock held for one says
    nothing about the other — treating busy-ness as protection would hide a
    genuine orphan behind any unrelated commit."""
    driven = _applying()
    stranded = _applying()
    holder(driven)

    ids = _orphan_ids()
    assert driven.meta.txn_id not in ids
    assert stranded.meta.txn_id in ids


def test_a_dead_lock_owner_protects_nothing(state_home: Any) -> None:
    """The lock file outlives the process. An owner record whose pid is gone is
    a former holder, and the transaction it named is exactly the orphan this
    scan exists to find."""
    import json

    from pyecsdwan.locking import HostLock, LockOwner

    journal = _applying()
    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()

    lock = HostLock(HOST, "commit", timeout=0.0, txn_id=journal.meta.txn_id)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            LockOwner(
                pid=dead.pid,
                start_token="stale",
                origin=HOST,
                scope="commit",
                command="ec-cli commit",
                acquired_utc="2026-01-01T00:00:00+00:00",
                txn_id=journal.meta.txn_id,
            ).to_json()
        ),
        encoding="utf-8",
    )

    assert journal.meta.txn_id in _orphan_ids()


# -- compare-and-set before restoring ----------------------------------------


def test_recovery_refuses_a_transaction_that_settled_while_it_waited(
    state_home: Any, holder: Any
) -> None:
    """The race, run for real.

    Recovery blocks on the commit lock; the live commit finishes and marks
    itself CONFIRMED; the lock is released. Reading the journal before the
    wait — which is what `revert_txn_dir` did — means restoring snapshots over
    a transaction that had just succeeded.
    """
    journal = _applying(TxnState.APPLIED_UNCONFIRMED)
    _proc, go = holder(journal, settle=True)

    result: dict[str, Any] = {}

    def recover() -> None:
        result["report"] = txn.revert_txn_dir(
            journal.dir, reason="orphan recovery", lock_timeout=30.0
        )

    thread = threading.Thread(target=recover)
    thread.start()
    time.sleep(0.3)          # let the recovery reach the lock and block
    go.write_text("go")      # holder now settles the txn and releases
    thread.join(timeout=40)
    assert not thread.is_alive(), "recovery never returned"

    report = result["report"]
    assert not report.ok
    assert report.state == TxnState.CONFIRMED
    assert any("refusing recovery" in m for m in report.messages)


def test_recovery_refuses_an_already_terminal_transaction(state_home: Any) -> None:
    """The same guard without the race — a transaction settled long ago must
    not be restorable by a stale `rollback --pending` invocation."""
    journal = _applying()
    journal.set_state(TxnState.CONFIRMED)

    report = txn.revert_txn_dir(journal.dir, reason="stale recovery")

    assert not report.ok
    assert any("refusing recovery" in m for m in report.messages)
