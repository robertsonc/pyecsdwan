"""Detached rollback watchdog for ``commit confirm``.

The target deployment is a Linux server reached over SSH, so the watchdog
must survive SSH session teardown: it daemonizes with the classic
double-fork/setsid sequence (immune to SIGHUP, reparented to init), then
watches one transaction directory.

Behavior:

* If ``confirm.marker`` appears before the deadline -> exit quietly.
* If the deadline passes without a marker -> revert the transaction from its
  journal snapshots and exit.
* If the transaction reaches a terminal state by other means (operator ran
  ``rollback --pending``) -> exit quietly.

Credentials: the watchdog re-reads ``ECSDWAN_*`` environment variables
(inherited from the arming process) or the OS keyring. Interactive
session-login auth cannot be replayed by a background process, which is why
the transaction engine refuses ``commit confirm`` without an API key.

An optional systemd user-timer backend is documented in
``docs/watchdog-backends.md`` but is NOT the default: on SSH-only accounts it
requires ``loginctl enable-linger`` or the user manager (and the timer with
it) dies with the last SSH session.

Run directly (used by the arming code, and by tests with ``--foreground``)::

    python -m pyecsdwan.watchdog <txn-dir> [--foreground]
"""

from __future__ import annotations

import datetime as _dt
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pyecsdwan import config
from pyecsdwan.journal import TxnJournal, TxnState

_ARM_TIMEOUT = 10.0


def arm(txn_dir: Path, settings: config.Settings) -> int:
    """Spawn the detached watchdog for ``txn_dir``; returns its pid.

    Raises RuntimeError if the watchdog fails to come up (no pid file within
    the arm timeout) — the caller treats that as a failed commit-confirm.
    """
    env = dict(os.environ)
    env[config.ENV_ORCH_URL] = settings.orch_url
    if settings.api_key:
        env[config.ENV_API_KEY] = settings.api_key
    if not settings.verify_tls:
        env[config.ENV_INSECURE] = "1"
    # Propagate a test/system override of the state root to the daemon.
    env[config.ENV_HOME] = str(config.state_root())

    pid_file = txn_dir / "watchdog.pid"
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, our own module
        [sys.executable, "-m", "pyecsdwan.watchdog", str(txn_dir)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # The launched process double-forks and exits; the daemon lives on under
    # init. Reap the intermediate so no zombie is left behind.
    proc.wait(timeout=_ARM_TIMEOUT)

    deadline = time.monotonic() + _ARM_TIMEOUT
    while time.monotonic() < deadline:
        try:
            # The pid file is "<pid> <start-token>"; take the first field.
            pid = int(pid_file.read_text().strip().split()[0])
        except (OSError, ValueError, IndexError):
            time.sleep(0.05)
            continue
        return pid
    raise RuntimeError(
        f"rollback watchdog failed to start for transaction {txn_dir.name}; "
        f"refusing to leave an unprotected unconfirmed commit"
    )


def _daemonize(log_path: Path) -> None:
    if os.fork() > 0:
        os._exit(0)  # first parent: lets Popen.wait() return promptly
    os.setsid()
    if os.fork() > 0:
        os._exit(0)  # second parent: guarantees no controlling terminal
    os.chdir("/")
    os.umask(0o077)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(null_fd)
    os.close(log_fd)


def _log(msg: str) -> None:
    ts = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
    print(f"{ts} {msg}", flush=True)


def watch(txn_dir: Path, poll_interval: float = 1.0) -> int:
    """Watch loop (runs inside the daemon, or inline under --foreground)."""
    journal = TxnJournal.open(txn_dir)
    pid = os.getpid()
    journal.write_watchdog_pid(pid)
    journal.append("WATCHDOG_ARMED", pid=pid)
    _log(f"watchdog armed pid={pid} txn={journal.meta.txn_id} "
         f"deadline={journal.meta.confirm_deadline}")

    # SIGTERM from `commit` (confirm path) is a clean shutdown *between* poll
    # iterations. The flag is checked at safe points; during the revert itself
    # SIGTERM is blocked so a late confirm can't kill the daemon mid-restore.
    terminate = {"flag": False}

    def _on_term(*_: object) -> None:
        terminate["flag"] = True

    signal.signal(signal.SIGTERM, _on_term)

    if journal.meta.confirm_deadline is None:
        _log("no confirm deadline set; nothing to do")
        return 0
    deadline = _dt.datetime.fromisoformat(journal.meta.confirm_deadline)

    while True:
        if terminate["flag"] or journal.confirm_marker.exists():
            journal.append("WATCHDOG_EXIT", reason="confirmed")
            _log("confirm marker/term seen; exiting")
            return 0
        journal = TxnJournal.open(txn_dir)
        if journal.meta.state in TxnState.TERMINAL:
            journal.append("WATCHDOG_EXIT", reason=f"state={journal.meta.state}")
            _log(f"transaction already terminal ({journal.meta.state}); exiting")
            return 0
        if _dt.datetime.now(tz=_dt.timezone.utc) >= deadline:
            break
        time.sleep(poll_interval)

    # Deadline reached. Re-check the marker one last time, then claim the
    # decision atomically: if a concurrent `commit` already claimed 'confirm'
    # (or wrote the marker), stand down without reverting.
    if journal.confirm_marker.exists() or journal.try_claim("revert") != "revert":
        journal.append("WATCHDOG_EXIT", reason="confirm won the decision claim")
        _log("confirm won the race; exiting without revert")
        return 0

    # Block SIGTERM for the duration of the revert: a late confirm must not
    # tear the daemon down mid-restore, leaving the fabric half-reverted.
    _block_sigterm()
    _log("confirm window expired; reverting from journal")
    journal.append("WATCHDOG_REVERT_TRIGGERED")
    try:
        from pyecsdwan.txn import revert_txn_dir

        revert_txn_dir(txn_dir, reason="commit-confirm window expired")
    except Exception as exc:  # noqa: BLE001 - last-resort guard: the daemon must record the failure, not die silently
        journal.append("WATCHDOG_REVERT_FAILED", error=str(exc))
        _log(f"REVERT FAILED: {exc}")
        return 1
    _log("revert complete")
    return 0


def _block_sigterm() -> None:
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    except (AttributeError, ValueError, OSError):
        # Non-POSIX or restricted; the atomic claim is still the real guard.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    foreground = "--foreground" in args
    if foreground:
        args.remove("--foreground")
    if len(args) != 1:
        print("usage: python -m pyecsdwan.watchdog <txn-dir> [--foreground]", file=sys.stderr)
        return 2
    txn_dir = Path(args[0]).resolve()
    if not (txn_dir / "meta.json").exists():
        print(f"not a transaction directory: {txn_dir}", file=sys.stderr)
        return 2
    if not foreground:
        _daemonize(txn_dir / "watchdog.log")
    return watch(txn_dir)


if __name__ == "__main__":
    sys.exit(main())
