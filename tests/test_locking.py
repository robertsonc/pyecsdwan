"""Host-scoped advisory locking (issue #63).

The interesting cases here are all adversarial: a second process racing the
first, a holder that dies without releasing, a recycled pid wearing a dead
owner's clothes. Single-process assertions cannot show any of them, so the
cross-process tests really do spawn processes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import config, locking
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ref
from pyecsdwan.locking import HostLock, LockBusy, LockOwner

HOST = "orch.example.com"
OTHER = "orch2.example.com"


# -- helpers -----------------------------------------------------------------


def _run_child(home: Path, body: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    """Run a snippet in a separate interpreter against the same state home."""
    env = dict(os.environ, ECSDWAN_HOME=str(home))
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )


_TRY_ACQUIRE = """
import sys
from pyecsdwan.locking import HostLock, LockBusy
try:
    with HostLock({host!r}, {scope!r}, timeout={timeout!r}):
        print("ACQUIRED")
except LockBusy as exc:
    print("BUSY", exc)
"""


def _spawn_holder(home: Path, host: str, scope: str, ready: Path) -> subprocess.Popen[str]:
    """A child that takes the lock, announces itself, and then just sits there."""
    body = f"""
import time
from pathlib import Path
from pyecsdwan.locking import HostLock
with HostLock({host!r}, {scope!r}, timeout=10.0):
    Path({str(ready)!r}).write_text("ready")
    time.sleep(120)
"""
    env = dict(os.environ, ECSDWAN_HOME=str(home))
    return subprocess.Popen(
        [sys.executable, "-c", body],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _await(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


# -- mutual exclusion --------------------------------------------------------


def test_a_second_process_cannot_take_a_held_lock(state_home: Path) -> None:
    with HostLock(HOST, "commit"):
        out = _run_child(state_home, _TRY_ACQUIRE.format(host=HOST, scope="commit", timeout=0.2))
    assert "BUSY" in out.stdout, out


def test_the_lock_is_free_again_once_the_holder_exits(state_home: Path) -> None:
    with HostLock(HOST, "commit"):
        pass
    out = _run_child(state_home, _TRY_ACQUIRE.format(host=HOST, scope="commit", timeout=0.2))
    assert "ACQUIRED" in out.stdout, out


def test_locks_are_host_scoped(state_home: Path) -> None:
    # Work against one Orchestrator must never block work against another.
    with HostLock(HOST, "commit"):
        out = _run_child(state_home, _TRY_ACQUIRE.format(host=OTHER, scope="commit", timeout=0.2))
    assert "ACQUIRED" in out.stdout, out


def test_scopes_are_independent(state_home: Path) -> None:
    with HostLock(HOST, "commit"):
        out = _run_child(
            state_home, _TRY_ACQUIRE.format(host=HOST, scope="candidate", timeout=0.2)
        )
    assert "ACQUIRED" in out.stdout, out


def test_lock_nests_within_one_process(state_home: Path) -> None:
    # Regression: flock attaches to the open file description, so two HostLock
    # objects for the same host in one process would deadlock rather than
    # nest. `commit` nesting inside commit-scoped work has to keep working.
    outer = HostLock(HOST, "commit", timeout=0.5)
    inner = HostLock(HOST, "commit", timeout=0.5)
    with outer:
        with inner:
            assert outer.held and inner.held
        # Releasing the inner object must NOT drop the lock the outer holds.
        assert outer.held
        out = _run_child(state_home, _TRY_ACQUIRE.format(host=HOST, scope="commit", timeout=0.2))
        assert "BUSY" in out.stdout, out
    assert not outer.held


def test_busy_message_names_the_holder(state_home: Path) -> None:
    ready = state_home / "holder.ready"
    child = _spawn_holder(state_home, HOST, "commit", ready)
    try:
        _await(ready)
        with pytest.raises(LockBusy) as excinfo:
            HostLock(HOST, "commit", timeout=0.2).acquire()
        message = str(excinfo.value)
        assert f"pid {child.pid}" in message, message
        assert "commit lock for" in message and HOST in message
    finally:
        child.kill()
        child.wait(timeout=10)


# -- crash recovery ----------------------------------------------------------


def test_lock_is_released_when_the_holder_is_killed(state_home: Path) -> None:
    """A SIGKILLed holder must not leave a lock nobody can ever take.

    On the flock path the kernel drops the lock when the process dies, so
    there is no stale lock to recover — which is the point of using it.
    """
    ready = state_home / "killme.ready"
    child = _spawn_holder(state_home, HOST, "commit", ready)
    try:
        _await(ready)
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
        # No sleep-and-hope: the lock is free the instant the process is gone.
        with HostLock(HOST, "commit", timeout=10.0):
            pass
    finally:
        if child.poll() is None:  # pragma: no cover - only on an unexpected failure
            child.kill()
            child.wait(timeout=10)


# -- owner metadata and pid reuse -------------------------------------------


def test_owner_liveness_requires_a_matching_start_token() -> None:
    pid = os.getpid()
    live = LockOwner(
        pid=pid,
        start_token=locking.proc_start_token(pid),
        host=HOST,
        scope="commit",
        command="ec-cli commit",
        acquired_utc="now",
    )
    assert live.is_alive()

    # Same pid, different process: the recorded owner is gone and the pid was
    # handed to someone else. Treating this as alive is the PID-reuse mistake.
    recycled = LockOwner(
        pid=pid,
        start_token="0",
        host=HOST,
        scope="commit",
        command="ec-cli commit",
        acquired_utc="then",
    )
    assert not recycled.is_alive()


def test_owner_liveness_is_false_for_a_dead_pid() -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    owner = LockOwner(
        pid=dead.pid, start_token="", host=HOST, scope="commit", command="x", acquired_utc="t"
    )
    assert not owner.is_alive()


def test_lock_file_records_the_holder_without_leaking_arguments(state_home: Path) -> None:
    lock = HostLock(HOST, "commit")
    with lock:
        owner = lock.read_owner()
    assert owner is not None
    assert owner.pid == os.getpid()
    assert owner.host == HOST and owner.scope == "commit"
    assert owner.start_token == locking.proc_start_token(os.getpid())
    # The command is argv[0] + subcommand only. Resource names and values an
    # operator staged are not diagnostics and have no business on disk here.
    assert "\n" not in owner.command
    assert len(owner.command.split()) <= 2


# -- the O_EXCL fallback (platforms without flock) ---------------------------


@pytest.fixture
def no_flock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locking, "HAVE_FLOCK", False)


def _write_lock_file(path: Path, owner: LockOwner) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(owner.to_json(), sort_keys=True), encoding="utf-8")


def test_fallback_excludes_a_second_holder(state_home: Path, no_flock: None) -> None:
    # Same process, but a lock file whose owner is this live process — which is
    # exactly what a second shell would find.
    lock = HostLock(HOST, "commit", timeout=0.1)
    with lock:
        assert lock.path.exists()
        contender = HostLock(HOST, "commit", timeout=0.1)
        assert contender._try_exclusive_create() is False
    # Released: the holder unlinks its own lock file on the fallback path.
    assert not lock.path.exists()


def test_fallback_breaks_a_dead_owners_lock(state_home: Path, no_flock: None) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    lock = HostLock(HOST, "commit", timeout=0.5)
    _write_lock_file(
        lock.path,
        LockOwner(
            pid=dead.pid, start_token="", host=HOST, scope="commit", command="x", acquired_utc="t"
        ),
    )
    with lock:  # the owner is provably gone, so the lock is ours to take
        assert lock.held


def test_fallback_will_not_break_a_live_owners_lock(state_home: Path, no_flock: None) -> None:
    lock = HostLock(HOST, "commit", timeout=0.2)
    pid = os.getpid()
    _write_lock_file(
        lock.path,
        LockOwner(
            pid=pid,
            start_token=locking.proc_start_token(pid),
            host=HOST,
            scope="commit",
            command="ec-cli commit",
            acquired_utc="t",
        ),
    )
    with pytest.raises(LockBusy):
        lock.acquire()


def test_fallback_treats_an_unreadable_owner_as_held(state_home: Path, no_flock: None) -> None:
    # Fail closed: a lock file we cannot parse is a lock we must not break.
    lock = HostLock(HOST, "commit", timeout=0.2)
    lock.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LockBusy):
        lock.acquire()


def test_fallback_does_not_break_a_lock_recorded_without_a_token(
    state_home: Path, no_flock: None
) -> None:
    # No token (older file, or no /proc) falls back to bare liveness, which can
    # only make us wait — never break a lock that is genuinely held.
    lock = HostLock(HOST, "commit", timeout=0.2)
    _write_lock_file(
        lock.path,
        LockOwner(
            pid=os.getpid(), start_token="", host=HOST, scope="commit", command="x",
            acquired_utc="t",
        ),
    )
    with pytest.raises(LockBusy):
        lock.acquire()


# -- candidate store: concurrent staging ------------------------------------


def test_two_stores_do_not_lose_each_others_staged_changes(state_home: Path) -> None:
    """The lost-update case, in-process: two long-lived shells on one host.

    Both stores load at T0. Without a locked re-read, the second one's save
    would publish a view of the world from before the first one's change and
    the first operator's staged work would vanish with no error anywhere.
    """
    first = CandidateStore(HOST)
    second = CandidateStore(HOST)

    first.set_path(Ref("alpha", "one"), ["speed"], 10)
    second.set_path(Ref("beta", "two"), ["mtu"], 9000)

    final = CandidateStore(HOST)
    keys = {i.ref_key for i in final.ordered_items()}
    assert keys == {"alpha:one", "beta:two"}


def test_concurrent_processes_do_not_lose_staged_changes(state_home: Path) -> None:
    """The same case across real processes, all racing the same host."""
    writers = 8
    body = """
import sys
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ref
store = CandidateStore({host!r}, lock_timeout=30.0)
store.set_path(Ref("alpha", sys.argv[1]), ["speed"], int(sys.argv[1]))
"""
    env = dict(os.environ, ECSDWAN_HOME=str(state_home))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", body.format(host=HOST), str(n)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for n in range(writers)
    ]
    for proc in procs:
        _out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err

    final = CandidateStore(HOST)
    keys = {i.ref_key for i in final.ordered_items()}
    assert keys == {f"alpha:{n}" for n in range(writers)}, keys


def test_a_failed_mutation_does_not_persist_or_strand_the_lock(state_home: Path) -> None:
    store = CandidateStore(HOST)
    store.set_path(Ref("alpha", "one"), ["speed"], 10)

    class Boom(Exception):
        pass

    # Blow up inside the read-modify-write cycle.
    with pytest.raises(Boom):
        with store._mutate():
            store.items.clear()
            raise Boom
    # Nothing persisted...
    assert {i.ref_key for i in CandidateStore(HOST).ordered_items()} == {"alpha:one"}
    # ...and the lock came back.
    assert not store.lock.held
    with HostLock(HOST, "candidate", timeout=0.5):
        pass


# -- show locks --------------------------------------------------------------


def test_active_locks_reports_held_and_free(state_home: Path) -> None:
    ready = state_home / "listme.ready"
    child = _spawn_holder(state_home, HOST, "commit", ready)
    try:
        _await(ready)
        rows = {name: (owner, held) for name, owner, held in locking.active_locks()}
        name = f"{HOST}.commit.lock"
        assert name in rows, rows
        owner, held = rows[name]
        assert held is True
        assert owner is not None and owner.pid == child.pid
    finally:
        child.kill()
        child.wait(timeout=10)
    rows2 = {name: held for name, _owner, held in locking.active_locks()}
    assert rows2[f"{HOST}.commit.lock"] is False


def test_active_locks_does_not_report_our_own_lock_as_free(state_home: Path) -> None:
    # Probing by acquisition would nest and answer "free", the one answer that
    # is certainly wrong.
    with HostLock(HOST, "commit"):
        rows = {name: held for name, _owner, held in locking.active_locks()}
    assert rows[f"{HOST}.commit.lock"] is True


def test_lock_root_lives_under_the_state_home(state_home: Path) -> None:
    assert locking.lock_root() == config.state_root() / "locks"


def _unused(value: Any) -> Any:  # pragma: no cover
    return value


# -- CLI surface -------------------------------------------------------------


def test_show_locks_renders_held_and_free(state_home: Path) -> None:
    from rich.console import Console

    from pyecsdwan.cli.main import render_locks_table

    ready = state_home / "cli.ready"
    child = _spawn_holder(state_home, HOST, "commit", ready)
    try:
        _await(ready)
        buffer = Console(record=True, width=200)
        render_locks_table(buffer, locking.active_locks())
        text = buffer.export_text()
    finally:
        child.kill()
        child.wait(timeout=10)
    assert "HELD" in text
    assert f"pid {child.pid}" in text

    buffer2 = Console(record=True, width=200)
    render_locks_table(buffer2, locking.active_locks())
    after = buffer2.export_text()
    assert "free" in after
    # The file outlives the lock, so a released row must not present its last
    # holder as a current one.
    assert "(last:" in after


def test_show_locks_with_nothing_to_show(state_home: Path) -> None:
    from rich.console import Console

    from pyecsdwan.cli.main import render_locks_table

    buffer = Console(record=True, width=200)
    render_locks_table(buffer, [])
    assert "no locks" in buffer.export_text()


def test_shell_commit_accepts_rebase() -> None:
    from pyecsdwan.cli.shell import _parse_commit_args

    _minutes, flags = _parse_commit_args(["rebase"])
    assert flags["rebase"] is True
    _minutes2, flags2 = _parse_commit_args([])
    assert flags2["rebase"] is False


def test_shell_commit_rejects_an_unknown_option() -> None:
    from pyecsdwan.cli.shell import _parse_commit_args

    with pytest.raises(ValueError, match="rebase"):
        _parse_commit_args(["rebasse"])
