"""Crash-safe transaction journal.

Layout (one directory per transaction, under ``~/.pyecsdwan/journal/``)::

    <txn-id>/
        meta.json        # small state record, atomically rewritten (tmp+rename+fsync)
        events.jsonl     # append-only event log, fsynced per record
        confirm.marker   # written by bare `commit` inside a confirm window
        watchdog.pid     # pid of the detached rollback watchdog, if armed

``events.jsonl`` is the source of truth for rollback: SNAPSHOT events embed
the full pre-change raw server state per resource. ``meta.json`` is a fast
index for listing/orphan-scan; if the two ever disagree, events win.

Journal doubles as the audit log: Tier-0 raw API calls are journaled here too
(state AUDIT_ONLY), with no rollback data beyond what a GET-before-write could
capture.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.contract import RawState, Ref


class JournalCorrupt(Exception):
    """The event log holds a record that is neither valid JSON nor explicable
    as a torn tail. Recovery must refuse rather than proceed on a history it
    knows is incomplete (#110)."""


class TxnState:
    """Transaction lifecycle states (plain strings for JSON friendliness)."""

    PENDING = "PENDING"                      # created; snapshotting
    APPLYING = "APPLYING"                    # writes in flight
    APPLIED_UNCONFIRMED = "APPLIED_UNCONFIRMED"  # commit confirm armed
    CONFIRMED = "CONFIRMED"                  # terminal success
    REVERTING = "REVERTING"
    REVERTED = "REVERTED"                    # terminal, rolled back
    REVERT_FAILED = "REVERT_FAILED"          # terminal, NEEDS ATTENTION
    FAILED = "FAILED"                        # apply failed before any write landed
    AUDIT_ONLY = "AUDIT_ONLY"                # tier-0 passthrough record

    TERMINAL = frozenset({CONFIRMED, REVERTED, REVERT_FAILED, FAILED, AUDIT_ONLY})


def utcnow() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def new_txn_id() -> str:
    # Microsecond precision keeps rapid consecutive transactions ordered by id.
    stamp = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d-%H%M%S.%f")
    return f"{stamp}-{secrets.token_hex(4)}"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    # Unique temp name per writer: a fixed ``meta.tmp`` would let a second
    # concurrent writer O_TRUNC the first writer's staging file mid-write,
    # publishing a truncated meta.json. mkstemp gives each writer its own.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@dataclasses.dataclass
class TxnMeta:
    txn_id: str
    created_at: str
    orch_host: str
    state: str = TxnState.PENDING
    #: ISO timestamp after which an unconfirmed commit must be reverted.
    confirm_deadline: str | None = None
    #: Ref keys included in the changeset, in apply order.
    items: list[str] = dataclasses.field(default_factory=list)
    format: int = 1

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> TxnMeta:
        known = {f.name for f in dataclasses.fields(TxnMeta)}
        return TxnMeta(**{k: v for k, v in data.items() if k in known})


class TxnJournal:
    """Handle to one transaction's on-disk journal."""

    def __init__(self, txn_dir: Path, meta: TxnMeta):
        self.dir = txn_dir
        self.meta = meta
        self._events_path = txn_dir / "events.jsonl"

    # -- construction --------------------------------------------------------

    @classmethod
    def create(cls, orch_host: str, items: list[Ref], root: Path | None = None) -> TxnJournal:
        root = root if root is not None else config.journal_root()
        txn_id = new_txn_id()
        txn_dir = root / txn_id
        txn_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        _fsync_dir(root)  # make the new txn dir itself durable before we fill it
        meta = TxnMeta(
            txn_id=txn_id,
            created_at=utcnow(),
            orch_host=orch_host,
            items=[r.key() for r in items],
        )
        journal = cls(txn_dir, meta)
        journal._write_meta()
        journal.append("TXN_BEGIN", host=orch_host, items=meta.items)
        _fsync_dir(txn_dir)  # durably link events.jsonl on first creation
        return journal

    @classmethod
    def open(cls, txn_dir: Path) -> TxnJournal:
        meta_path = txn_dir / "meta.json"
        with open(meta_path, encoding="utf-8") as fh:
            meta = TxnMeta.from_json(json.load(fh))
        return cls(txn_dir, meta)

    # -- event log -----------------------------------------------------------

    def _write_line(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str)
        # 0o600 on first creation; snapshots can embed sensitive server config.
        fd = os.open(self._events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _repair_tail(self) -> dict[str, Any] | None:
        """Terminate or discard an unterminated final record (#110).

        Every complete record ends with a newline, written in the same call as
        the record itself, so a file not ending in one means the last append
        did not finish. Without this, the *next* append concatenated onto that
        fragment and produced a single unparseable line — destroying both the
        fragment and the new event, silently. A SNAPSHOT lost that way leaves
        a transaction whose ``applied_refs`` says a resource was changed and
        whose ``snapshots`` cannot say what it looked like.

        Two cases, distinguished by parsing rather than assumed:

        * the tail is valid JSON — the record landed and only its newline did
          not. Terminate it; discarding a complete record would be the very
          data loss this exists to stop.
        * the tail is not valid JSON — it is provably partial. Truncate it.

        Returns what was done, for the audit event the caller writes, or None
        when the log was already well-formed. The discarded bytes are recorded
        as a digest and a length rather than quoted: a fragment can be part of
        a snapshot body, and this file is 0600 for that reason.
        """
        try:
            data = self._events_path.read_bytes()
        except FileNotFoundError:
            return None
        if not data or data.endswith(b"\n"):
            return None
        cut = data.rfind(b"\n") + 1  # 0 when the whole file is one unterminated line
        tail = data[cut:]
        try:
            json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            action = "discarded"
        else:
            action = "terminated"
        with open(self._events_path, "r+b") as fh:
            if action == "discarded":
                fh.truncate(cut)
            else:
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        return {
            "action": action,
            "bytes": len(tail),
            "sha256": hashlib.sha256(tail).hexdigest(),
        }

    def append(self, event: str, **fields: Any) -> None:
        repair = self._repair_tail()
        if repair is not None:
            # Written before the event that prompted it, so the log reads in
            # the order things happened rather than blaming the wrong record.
            self._write_line({"ts": utcnow(), "event": "JOURNAL_REPAIRED", **repair})
        self._write_line({"ts": utcnow(), "event": event, **fields})

    def events(self, strict: bool = False) -> list[dict[str, Any]]:
        """Every parsed record.

        ``strict`` is for the recovery path. A malformed line is no longer
        explicable as a torn tail — :meth:`_repair_tail` handles that case
        before any append — so it is corruption, and a rollback computed from
        a history with a hole in it is worse than a refused one (#110). Display
        paths stay lenient: a corrupt journal must still be *visible*, and
        `show journal` refusing to render is not an improvement.
        """
        if not self._events_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self._events_path, encoding="utf-8") as fh:
            for number, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    if strict:
                        raise JournalCorrupt(
                            f"{self._events_path}: line {number} is not valid JSON "
                            f"({exc}); the transaction history has a hole in it and "
                            f"recovery from it would be incomplete"
                        ) from exc
                    continue
        return out

    # -- state transitions ---------------------------------------------------

    def set_state(self, state: str, **fields: Any) -> None:
        self.meta.state = state
        self._write_meta()
        self.append("STATE", state=state, **fields)

    def set_confirm_deadline(self, deadline_utc: _dt.datetime) -> None:
        self.meta.confirm_deadline = deadline_utc.isoformat()
        self._write_meta()

    def _write_meta(self) -> None:
        _atomic_write_json(self.dir / "meta.json", self.meta.to_json())

    # -- snapshots -----------------------------------------------------------

    def record_snapshot(self, ref: Ref, raw: RawState) -> None:
        self.append("SNAPSHOT", ref=ref.key(), exists=raw is not None, raw=raw)

    def snapshots(self) -> dict[str, RawState]:
        """Pre-change raw state per ref key, from the event log.

        Strict: this is what a revert restores from, and a missing key here is
        read by the caller as "the resource did not exist before", which would
        make it *delete* something the journal simply lost.
        """
        snaps: dict[str, RawState] = {}
        for ev in self.events(strict=True):
            if ev.get("event") == "SNAPSHOT":
                snaps[ev["ref"]] = ev.get("raw") if ev.get("exists") else None
        return snaps

    def applied_refs(self) -> list[str]:
        """Ref keys whose apply started (APPLY_START), in order. Strict, for
        the same reason as :meth:`snapshots`: a short list here means a change
        that landed on the fabric is never offered for revert."""
        out: list[str] = []
        for ev in self.events(strict=True):
            if ev.get("event") == "APPLY_START":
                out.append(ev["ref"])
        return out

    # -- confirm marker / watchdog ------------------------------------------

    @property
    def confirm_marker(self) -> Path:
        return self.dir / "confirm.marker"

    def write_confirm_marker(self) -> None:
        marker = self.confirm_marker
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(utcnow())
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(self.dir)

    @property
    def decision_claim(self) -> Path:
        return self.dir / "decision.claim"

    def try_claim(self, decision: str) -> str:
        """Atomically claim the confirm-vs-revert decision for this txn.

        The first caller (bare ``commit`` writing 'confirm', or the watchdog
        writing 'revert' at deadline) wins via O_EXCL; every later caller
        reads back the winner. Returns the decision that actually holds — so
        a loser sees the other side's word and stands down instead of acting
        on a transaction the other party is already finalizing."""
        try:
            fd = os.open(self.decision_claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                return self.decision_claim.read_text().strip() or decision
            except OSError:
                return decision
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(decision)
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(self.dir)
        return decision

    @property
    def watchdog_pid_file(self) -> Path:
        return self.dir / "watchdog.pid"

    def write_watchdog_pid(self, pid: int) -> None:
        """Record the watchdog pid plus a start-time token, so a recycled pid
        (reboot, wraparound) can't masquerade as a live watchdog."""
        token = proc_start_token(pid)
        fd = os.open(self.watchdog_pid_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{pid} {token}")
            fh.flush()
            os.fsync(fh.fileno())

    def watchdog_pid(self) -> int | None:
        try:
            return int(self.watchdog_pid_file.read_text().strip().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def watchdog_alive(self) -> bool:
        """True only if the recorded pid is live AND still the same process
        (start-time token matches)."""
        try:
            parts = self.watchdog_pid_file.read_text().strip().split()
            pid = int(parts[0])
            token = parts[1] if len(parts) > 1 else ""
        except (OSError, ValueError, IndexError):
            return False
        if not pid_alive(pid):
            return False
        # An older pid file without a token falls back to bare liveness.
        return token == "" or proc_start_token(pid) == token


# -- journal-wide operations -------------------------------------------------


def list_txns(root: Path | None = None) -> list[TxnJournal]:
    """All transactions, newest first (by creation time, not directory name —
    ids created within the same second would otherwise tie-break randomly)."""
    root = root if root is not None else config.journal_root()
    if not root.exists():
        return []
    out: list[TxnJournal] = []
    for entry in root.iterdir():
        if not entry.is_dir() or not (entry / "meta.json").exists():
            continue
        try:
            out.append(TxnJournal.open(entry))
        except (OSError, json.JSONDecodeError, TypeError):
            # An unreadable journal (torn meta.json after a crash) is not
            # silently dropped from the world — it is surfaced so a stuck
            # transaction can't vanish from `show journal` / orphan recovery.
            _UNREADABLE.add(entry.name)
            continue
    out.sort(key=lambda t: (t.meta.created_at, t.meta.txn_id), reverse=True)
    return out


#: Names of journal directories that failed to parse in the last list_txns().
#: The CLI reports these so a corrupt journal is loud, not invisible.
_UNREADABLE: set[str] = set()


def unreadable_txn_dirs(root: Path | None = None) -> list[str]:
    _UNREADABLE.clear()
    list_txns(root)
    return sorted(_UNREADABLE)


def _for_host(txns: list[TxnJournal], host: str | None) -> list[TxnJournal]:
    if host is None:
        return txns
    return [t for t in txns if t.meta.orch_host == host]


def committed_history(root: Path | None = None, host: str | None = None) -> list[TxnJournal]:
    """Confirmed transactions for ``host`` (all hosts if None), newest first —
    the ``rollback <n>`` history. Host scoping is mandatory in practice: a
    snapshot from one Orchestrator must never be restored into another."""
    return _for_host(
        [t for t in list_txns(root) if t.meta.state == TxnState.CONFIRMED], host
    )


def orphaned_txns(root: Path | None = None, host: str | None = None) -> list[TxnJournal]:
    """Unconfirmed/interrupted transactions for ``host`` needing attention.

    A transaction is orphaned when it is non-terminal and no live watchdog or
    CLI process is driving it: an APPLYING txn whose CLI died mid-commit, or
    an APPLIED_UNCONFIRMED txn whose watchdog is gone. A confirm deadline more
    than a grace period in the past counts as orphaned even if some unrelated
    process now holds the recorded pid (pid recycling / reboot)."""
    out: list[TxnJournal] = []
    for txn in _for_host(list_txns(root), host):
        if txn.meta.state in TxnState.TERMINAL:
            continue
        if txn.confirm_marker.exists():
            continue
        if _deadline_passed(txn, grace_s=120):
            out.append(txn)
            continue
        if txn.watchdog_alive():
            continue
        out.append(txn)
    return out


def _deadline_passed(txn: TxnJournal, grace_s: float) -> bool:
    if txn.meta.confirm_deadline is None:
        return False
    try:
        deadline = _dt.datetime.fromisoformat(txn.meta.confirm_deadline)
    except ValueError:
        return False
    return _dt.datetime.now(tz=_dt.timezone.utc) > deadline + _dt.timedelta(seconds=grace_s)


def prune_history(keep: int, root: Path | None = None, host: str | None = None,
                  audit_keep: int = 200) -> int:
    """Prune terminal transactions, counting rollback history and audit
    records under separate quotas so a burst of Tier-0 ``api`` calls
    (AUDIT_ONLY) can never evict a CONFIRMED rollback point. Returns the
    number removed."""
    import shutil

    scoped = _for_host(list_txns(root), host)
    history = [t for t in scoped if t.meta.state == TxnState.CONFIRMED]
    audit_and_dead = [
        t for t in scoped
        if t.meta.state in TxnState.TERMINAL and t.meta.state != TxnState.CONFIRMED
    ]
    removed = 0
    for txn in history[keep:] + audit_and_dead[audit_keep:]:
        shutil.rmtree(txn.dir, ignore_errors=True)
        removed += 1
    return removed


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_start_token(pid: int) -> str:
    """A per-process token that changes when the pid is reused: the kernel
    start-time (field 22 of /proc/<pid>/stat on Linux). Empty when
    unavailable (non-Linux, dead pid) — callers fall back to bare liveness."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        # comm (field 2) may contain spaces/parens; split after the final ')'.
        tail = data[data.rfind(")") + 1 :].split()
        # tail[0] is field 3 (state); field 22 (starttime) is tail[19].
        return tail[19] if len(tail) > 19 else ""
    except (OSError, IndexError):
        return ""
