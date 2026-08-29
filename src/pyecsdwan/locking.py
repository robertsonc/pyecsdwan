"""Host-scoped advisory locks (issue #63).

Atomic file replacement gives us torn-file safety; it gives us no concurrency
control. Two shells can load the same candidate, mutate independently, and
have the last writer silently erase the first one's staged work. Two commits
against the same Orchestrator can interleave their snapshot/apply phases.

So every critical section that spans a *read* and a later *write* takes a lock
named for the Orchestrator host it touches:

* ``candidate`` — held across one candidate read-modify-write cycle.
* ``commit`` — held across the whole commit / confirm / revert / rollback
  critical section, so a second commit cannot interleave with the first.

Locks are host-scoped by construction: the file name is derived from the
Orchestrator host, so work against one Orchestrator can never block work
against another.

Mechanism
---------
``fcntl.flock`` where it exists, which is every platform this CLI is
supported on. The kernel owns the lock and drops it when the holding *file
descriptor* closes — including when the process dies, is ``SIGKILL``ed, or
the machine loses power. That makes a stale lock structurally impossible on
the primary path, which is a stronger guarantee than any pid-file scheme can
offer.

Owner metadata (pid, process start token, host, command, transaction) is
written *inside* the locked file anyway, because "who holds this lock" is the
first question an operator asks. It is diagnostic, never load-bearing.

Where ``fcntl`` is unavailable the fallback is an ``O_EXCL`` lock file whose
staleness *is* decided from that metadata. There a dead owner has to be
detected rather than assumed, so a pid alone is not enough: pids get reused,
and a reused pid would make a dead owner's lock look alive forever. The
kernel start-time token that ``journal.py`` already uses to keep a recycled
pid from masquerading as a live watchdog does the same job here.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import functools
import json
import os
import re
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.journal import pid_alive, proc_start_token, utcnow

try:  # pragma: no cover - exercised by whichever branch the platform takes
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # pragma: no cover - non-POSIX
    HAVE_FLOCK = False

#: How long ``acquire`` waits for a contended lock before giving up. A commit
#: can legitimately run for minutes, so we do not wait for one to finish —
#: telling the operator who holds it beats blocking an interactive shell.
DEFAULT_TIMEOUT = 5.0
_POLL_INTERVAL = 0.05


class LockBusy(RuntimeError):
    """Another process holds the lock; message names the holder."""


@dataclasses.dataclass
class _Held:
    """A lock this process currently holds, and how deeply it is nested."""

    fd: int
    depth: int


#: Locks held by *this process*, keyed by lock-file path.
#:
#: Re-entrancy has to be process-wide, not per-instance. ``flock`` attaches to
#: the open file description, so two ``HostLock`` objects for the same host in
#: one process hold two different descriptions — and the second would block on
#: the first forever. Since ``commit`` legitimately nests inside other
#: commit-scoped work, that would be a deadlock waiting for a caller to find.
#: Keying on the path instead means any second acquire in this process nests,
#: whichever object asks for it.
_HELD: dict[str, _Held] = {}
_HELD_GUARD = threading.RLock()


@dataclasses.dataclass
class LockOwner:
    """Diagnostic record of who took a lock, and when."""

    pid: int
    start_token: str
    #: Canonical origin of the Orchestrator the lock is scoped to (#63), not
    #: the display host — two tenants behind one hostname take two locks and
    #: an operator reading `show locks` has to be able to tell them apart.
    origin: str
    scope: str
    command: str
    acquired_utc: str
    txn_id: str | None = None

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> LockOwner:
        return LockOwner(
            pid=int(data.get("pid", 0)),
            start_token=str(data.get("start_token", "")),
            # `host` is the pre-#63 field name; a record written by an older
            # process can still be on disk while that process holds the lock.
            origin=str(data.get("origin", data.get("host", ""))),
            scope=str(data.get("scope", "")),
            command=str(data.get("command", "")),
            acquired_utc=str(data.get("acquired_utc", "")),
            txn_id=data.get("txn_id"),
        )

    def describe(self) -> str:
        who = f"pid {self.pid}"
        if self.command:
            who += f" ({self.command})"
        when = f" since {self.acquired_utc}" if self.acquired_utc else ""
        txn = f" txn {self.txn_id}" if self.txn_id else ""
        return f"{who}{when}{txn}"

    def is_alive(self) -> bool:
        """Whether the recorded process is still the process that took the lock.

        A live pid is not enough. Pids are reused, and a reused pid would keep
        a dead owner's lock alive forever — so the start-time token has to
        match too. An owner recorded without a token (older file, or a
        platform with no ``/proc``) falls back to bare liveness, which is the
        conservative direction: it can only make us wait, never break a lock
        that is genuinely held.
        """
        if self.pid <= 0:
            return False
        if not pid_alive(self.pid):
            return False
        if not self.start_token:
            return True
        current = proc_start_token(self.pid)
        return current == "" or current == self.start_token


#: Re-exported so callers can say ``locking.lock_root()`` without reaching
#: into config; the path itself lives with its sibling state roots.
lock_root = config.lock_root

def _safe(component: str) -> str:
    """Filename-safe form of a *scope* name — never of an identity.

    Identities go through `config.origin_slug`, which keeps a digest of the
    unsanitized origin. This sanitizer is lossy by construction (`orch:443`
    and `orch_443` both become `orch_443`), which is fine for the fixed
    vocabulary of scope names and was a collision for host names (#63).
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", component)


def _this_command() -> str:
    """A short, non-secret description of the running command.

    Deliberately argv[0] plus the subcommand only. Full argv can carry
    resource names and values an operator would not expect to find sitting in
    a world-readable-by-owner file, and the lock file is a diagnostic, not an
    audit log — the journal is the audit log.
    """
    argv = sys.argv
    name = os.path.basename(argv[0]) if argv else "ec-cli"
    sub = next((a for a in argv[1:] if not a.startswith("-")), "")
    return f"{name} {sub}".strip()


class HostLock:
    """An advisory, host-scoped, process-re-entrant lock.

    Advisory in the POSIX sense: it binds every writer that takes it, which is
    every writer in this CLI. It cannot bind the Orchestrator UI or someone
    else's automation — that is what the commit-time drift check in ``txn.py``
    is for. The lock stops *this* tool from racing itself; the drift check
    catches the rest of the world.
    """

    def __init__(
        self,
        origin: str,
        scope: str,
        root: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        txn_id: str | None = None,
    ):
        # Canonicalized before it is stored, not just before the path is
        # built: `self.origin` is what `show locks` and the busy message
        # report, and reporting the raw spelling would show two names for
        # the one lock two callers are actually contending for (#63).
        self.origin = config.as_origin(origin)
        self.scope = scope
        self.timeout = timeout
        self.txn_id = txn_id
        root = root if root is not None else lock_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / f"{config.origin_slug(self.origin)}.{_safe(scope)}.lock"
        self._init_state()

    @classmethod
    def _for_path(cls, path: Path, *, timeout: float = DEFAULT_TIMEOUT) -> HostLock:
        """A lock bound to an existing file, for inspection rather than intent.

        `active_locks` scans a directory and has to reopen exactly the files it
        found. Rebuilding the path from the name is not possible by design: the
        name carries a one-way digest of the origin (#63), so feeding it back
        through the constructor would slug an already-slugged string and probe
        a *different* file — reporting every lock as free and littering the
        directory on every `show locks`.
        """
        lock = cls.__new__(cls)
        stem = path.name[: -len(".lock")] if path.name.endswith(".lock") else path.name
        _slug, _, scope = stem.rpartition(".")
        # The origin is not recoverable from the name; the owner record carries
        # it. Left empty rather than guessed, so nothing can mistake it for one.
        lock.origin = ""
        lock.scope = scope
        lock.timeout = timeout
        lock.txn_id = None
        lock.path = path
        lock._init_state()
        return lock

    def _init_state(self) -> None:
        self._key = str(self.path)
        self._fd: int | None = None
        #: How many times *this object* contributed to the process-wide depth,
        #: so an unbalanced release through one object cannot drop a lock
        #: another object is still relying on.
        self._mine = 0

    # -- acquisition ---------------------------------------------------------

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            with _HELD_GUARD:
                held = _HELD.get(self._key)
                if held is not None:
                    held.depth += 1
                    self._mine += 1
                    return
                if self._try_acquire() and self._fd is not None:
                    _HELD[self._key] = _Held(fd=self._fd, depth=1)
                    self._mine += 1
                    self._write_owner(self._fd)
                    return
            if time.monotonic() >= deadline:
                raise LockBusy(self._busy_message())
            time.sleep(_POLL_INTERVAL)

    def release(self) -> None:
        with _HELD_GUARD:
            if not self._mine:
                return
            self._mine -= 1
            held = _HELD.get(self._key)
            if held is None:
                return
            held.depth -= 1
            if held.depth > 0:
                return
            del _HELD[self._key]
            fd = held.fd
            self._fd = None
            if HAVE_FLOCK:
                # Truncate before unlocking, not after: once the lock is
                # dropped the next holder may already be writing its own
                # metadata, and a late truncate from us would erase it.
                with contextlib.suppress(OSError):
                    os.ftruncate(fd, 0)
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            else:
                os.close(fd)
                # Only the holder unlinks, and only while still holding it.
                with contextlib.suppress(OSError):
                    self.path.unlink()

    def __enter__(self) -> HostLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @property
    def held(self) -> bool:
        with _HELD_GUARD:
            return self._key in _HELD

    # -- mechanism -----------------------------------------------------------

    def _try_acquire(self) -> bool:
        if HAVE_FLOCK:
            return self._try_flock()
        return self._try_exclusive_create()

    def _try_flock(self) -> bool:
        # No O_TRUNC: the file still holds the current owner's metadata, and a
        # contender must be able to read it to say who is in the way.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        self._fd = fd
        return True

    def _try_exclusive_create(self) -> bool:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            return True
        except FileExistsError:
            pass
        # Someone holds it — or died holding it. Breaking a lock is only safe
        # when we can show the owner is gone; anything unreadable or ambiguous
        # counts as held.
        owner = self.read_owner()
        if owner is None or owner.is_alive():
            return False
        try:
            self.path.unlink()
        except OSError:
            return False
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            return True
        except FileExistsError:
            # Another contender broke it first and took it. Theirs.
            return False

    # -- owner metadata ------------------------------------------------------

    def _write_owner(self, fd: int) -> None:
        pid = os.getpid()
        owner = LockOwner(
            pid=pid,
            start_token=proc_start_token(pid),
            origin=self.origin,
            scope=self.scope,
            command=_this_command(),
            acquired_utc=utcnow(),
            txn_id=self.txn_id,
        )
        blob = json.dumps(owner.to_json(), sort_keys=True).encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, blob)
            os.fsync(fd)
        except OSError:
            # Metadata is diagnostic. Failing to record it must not fail a
            # lock the kernel has already granted us.
            pass

    def read_owner(self) -> LockOwner | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return LockOwner.from_json(data)

    def _busy_message(self) -> str:
        owner = self.read_owner()
        who = owner.describe() if owner is not None else "another process"
        return (
            f"{self.scope} lock for {self.origin} is held by {who}. "
            f"Wait for it to finish, or investigate with `ec-cli show locks`."
        )


@contextlib.contextmanager
def candidate_lock(
    origin: str, root: Path | None = None, timeout: float = DEFAULT_TIMEOUT
) -> Iterator[HostLock]:
    """Serialize one candidate read-modify-write cycle for ``origin``."""
    lock = HostLock(origin, "candidate", root=root, timeout=timeout)
    with lock:
        yield lock


@contextlib.contextmanager
def commit_lock(
    origin: str,
    root: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    txn_id: str | None = None,
) -> Iterator[HostLock]:
    """Serialize the commit/confirm/revert critical section for ``origin``."""
    lock = HostLock(origin, "commit", root=root, timeout=timeout, txn_id=txn_id)
    with lock:
        yield lock


@dataclasses.dataclass(frozen=True)
class LockState:
    """One lock file as ``show locks`` reports it."""

    #: File name on disk. Keyed by a one-way digest of the origin (#63), so it
    #: identifies the lock but does not spell out what it locks.
    name: str
    #: ``candidate`` or ``commit`` — still readable in the file name.
    scope: str
    #: Which Orchestrator, read from the owner record. ``None`` for a lock file
    #: that has never been held, where nothing on disk says.
    origin: str | None
    owner: LockOwner | None
    held: bool


def active_locks(root: Path | None = None) -> list[LockState]:
    """Every lock file on disk, for ``show locks``.

    ``held`` is probed by trying to take the lock, which is the only honest
    answer on the flock path: the file outlives the lock, so its mere
    existence proves nothing.
    """
    root = root if root is not None else lock_root()
    if not root.exists():
        return []
    out: list[LockState] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".lock"):
            continue
        probe = HostLock._for_path(entry, timeout=0.0)
        owner = probe.read_owner()
        origin = owner.origin if owner is not None and owner.origin else None
        state = functools.partial(
            LockState, name=entry.name, scope=probe.scope, origin=origin, owner=owner
        )
        with _HELD_GUARD:
            ours = probe._key in _HELD
        if ours:
            # Acquiring would nest and report "free", which is the one answer
            # that is certainly wrong.
            out.append(state(held=True))
            continue
        try:
            probe.acquire()
        except LockBusy:
            out.append(state(held=True))
            continue
        # We got it, so nobody held it. Release without unlinking the record.
        probe.release()
        out.append(state(held=False))
    return out
