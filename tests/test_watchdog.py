"""Watchdog tests: foreground loop semantics, and the full detached-daemon
commit-confirm auto-revert against the bundled mock Orchestrator (DoD #2)."""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import config, txn, watchdog
from pyecsdwan.contract import Ref
from pyecsdwan.journal import TxnJournal, TxnState


def _make_unconfirmed_txn(deadline_offset_s: float) -> TxnJournal:
    journal = TxnJournal.create("orch.example.com", [Ref("fake", "x")])
    journal.record_snapshot(Ref("fake", "x"), {"v": 1})
    journal.append("APPLY_START", ref="fake:x")
    journal.set_confirm_deadline(
        dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(seconds=deadline_offset_s)
    )
    journal.set_state(TxnState.APPLIED_UNCONFIRMED)
    return journal


def test_foreground_watch_exits_on_marker(state_home: Any) -> None:
    journal = _make_unconfirmed_txn(deadline_offset_s=30)
    journal.write_confirm_marker()
    assert watchdog.watch(journal.dir, poll_interval=0.01) == 0
    events = [e["event"] for e in journal.events()]
    assert "WATCHDOG_EXIT" in events


def test_foreground_watch_exits_on_terminal_state(state_home: Any) -> None:
    journal = _make_unconfirmed_txn(deadline_offset_s=30)
    journal.set_state(TxnState.REVERTED)
    assert watchdog.watch(journal.dir, poll_interval=0.01) == 0


def test_foreground_watch_reverts_after_deadline(state_home: Any,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    journal = _make_unconfirmed_txn(deadline_offset_s=0.05)
    calls: list[str] = []

    def fake_revert(txn_dir: Path, reason: str) -> txn.CommitReport:
        calls.append(reason)
        j = TxnJournal.open(txn_dir)
        j.set_state(TxnState.REVERTED)
        return txn.CommitReport(ok=True, state=TxnState.REVERTED)

    monkeypatch.setattr(txn, "revert_txn_dir", fake_revert)
    assert watchdog.watch(journal.dir, poll_interval=0.01) == 0
    assert calls == ["commit-confirm window expired"]
    events = [e["event"] for e in journal.events()]
    assert "WATCHDOG_REVERT_TRIGGERED" in events


@pytest.mark.slow
def test_detached_watchdog_reverts_real_commit(state_home: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """DoD #2 end-to-end: commit confirm, CLI process contributes nothing
    further (the watchdog is a detached daemon), config reverts on its own."""
    server_mod = pytest.importorskip("pyecsdwan.mock.server")
    base_url, _state, shutdown = server_mod.run_in_thread()
    try:
        monkeypatch.setenv(config.ENV_ORCH_URL, base_url)
        monkeypatch.setenv(config.ENV_API_KEY, "test-key")
        from pyecsdwan.runtime import bootstrap

        ctx, registry, settings = bootstrap()
        settings.job_timeout = 10.0

        labels_before = ctx.client.get("/gms/interfaceLabels")

        from pyecsdwan.candidate import CandidateStore

        candidate = CandidateStore(settings.host)
        candidate.set_path(
            Ref("interface-labels", "global"), ["wan", "3"],
            {"name": "LTE", "active": True, "topology": 2},
        )
        plan = txn.build_plan(ctx, registry, candidate)
        assert not plan.empty
        # ~3 second confirm window
        report = txn.commit(ctx, registry, plan, settings, confirm_minutes=0.05)
        assert report.ok, report.messages
        changed = ctx.client.get("/gms/interfaceLabels")
        assert "3" in changed["wan"]

        journal = TxnJournal.open(config.journal_root() / str(report.txn_id))
        pid = None
        for _ in range(100):
            pid = journal.watchdog_pid()
            if pid is not None:
                break
            time.sleep(0.1)
        assert pid is not None and _pid_alive(pid), "watchdog daemon not running"

        # No confirm: the detached watchdog must revert on its own.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            journal = TxnJournal.open(journal.dir)
            if journal.meta.state in (TxnState.REVERTED, TxnState.REVERT_FAILED):
                break
            time.sleep(0.25)
        assert journal.meta.state == TxnState.REVERTED, _watchdog_log(journal.dir)
        reverted = ctx.client.get("/gms/interfaceLabels")
        assert reverted["wan"].keys() == labels_before["wan"].keys()
        assert "3" not in reverted["wan"]
    finally:
        shutdown()


@pytest.mark.slow
def test_detached_watchdog_honors_confirm(state_home: Any,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    server_mod = pytest.importorskip("pyecsdwan.mock.server")
    base_url, _state, shutdown = server_mod.run_in_thread()
    try:
        monkeypatch.setenv(config.ENV_ORCH_URL, base_url)
        monkeypatch.setenv(config.ENV_API_KEY, "test-key")
        from pyecsdwan.runtime import bootstrap

        ctx, registry, settings = bootstrap()
        from pyecsdwan.candidate import CandidateStore

        candidate = CandidateStore(settings.host)
        candidate.set_path(
            Ref("interface-labels", "global"), ["wan", "6"],
            {"name": "INET2", "active": True, "topology": 0},
        )
        plan = txn.build_plan(ctx, registry, candidate)
        report = txn.commit(ctx, registry, plan, settings, confirm_minutes=0.1)
        assert report.ok, report.messages

        confirm = txn.confirm_pending(settings)
        assert confirm.ok

        # Wait past the original deadline; the change must survive.
        time.sleep(8)
        labels = ctx.client.get("/gms/interfaceLabels")
        assert "6" in labels["wan"]
        journal = TxnJournal.open(config.journal_root() / str(report.txn_id))
        assert journal.meta.state == TxnState.CONFIRMED
    finally:
        shutdown()


def test_watchdog_module_usage_error(state_home: Any) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "pyecsdwan.watchdog"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 2
    assert "usage" in proc.stderr


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _watchdog_log(txn_dir: Path) -> str:
    log = txn_dir / "watchdog.log"
    return log.read_text() if log.exists() else "(no watchdog.log)"
