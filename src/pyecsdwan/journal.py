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
import json
import os
import secrets
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.contract import RawState, Ref


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


def _utcnow() -> str:
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
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


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
        meta = TxnMeta(
            txn_id=txn_id,
            created_at=_utcnow(),
            orch_host=orch_host,
            items=[r.key() for r in items],
        )
        journal = cls(txn_dir, meta)
        journal._write_meta()
        journal.append("TXN_BEGIN", host=orch_host, items=meta.items)
        return journal

    @classmethod
    def open(cls, txn_dir: Path) -> TxnJournal:
        meta_path = txn_dir / "meta.json"
        with open(meta_path, encoding="utf-8") as fh:
            meta = TxnMeta.from_json(json.load(fh))
        return cls(txn_dir, meta)

    # -- event log -----------------------------------------------------------

    def append(self, event: str, **fields: Any) -> None:
        record = {"ts": _utcnow(), "event": event, **fields}
        line = json.dumps(record, sort_keys=True, default=str)
        with open(self._events_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def events(self) -> list[dict[str, Any]]:
        if not self._events_path.exists():
            return []
        out: list[dict[str, Any]] = []
        with open(self._events_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # A torn final line after a crash is expected; anything
                    # before it is intact because records are fsynced.
                    break
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
        """Pre-change raw state per ref key, from the event log."""
        snaps: dict[str, RawState] = {}
        for ev in self.events():
            if ev.get("event") == "SNAPSHOT":
                snaps[ev["ref"]] = ev.get("raw") if ev.get("exists") else None
        return snaps

    def applied_refs(self) -> list[str]:
        """Ref keys whose apply started (APPLY_START), in order."""
        out: list[str] = []
        for ev in self.events():
            if ev.get("event") == "APPLY_START":
                out.append(ev["ref"])
        return out

    # -- confirm marker / watchdog ------------------------------------------

    @property
    def confirm_marker(self) -> Path:
        return self.dir / "confirm.marker"

    def write_confirm_marker(self) -> None:
        marker = self.confirm_marker
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(_utcnow())
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_dir(self.dir)

    @property
    def watchdog_pid_file(self) -> Path:
        return self.dir / "watchdog.pid"

    def watchdog_pid(self) -> int | None:
        try:
            return int(self.watchdog_pid_file.read_text().strip())
        except (OSError, ValueError):
            return None


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
            continue
    out.sort(key=lambda t: (t.meta.created_at, t.meta.txn_id), reverse=True)
    return out


def committed_history(root: Path | None = None) -> list[TxnJournal]:
    """Confirmed transactions, newest first — the `rollback <n>` history."""
    return [t for t in list_txns(root) if t.meta.state == TxnState.CONFIRMED]


def orphaned_txns(root: Path | None = None) -> list[TxnJournal]:
    """Unconfirmed/interrupted transactions needing operator attention.

    A transaction is orphaned when it is non-terminal and no live watchdog or
    CLI process is driving it: an APPLYING txn whose CLI died mid-commit, or
    an APPLIED_UNCONFIRMED txn whose watchdog is gone.
    """
    out: list[TxnJournal] = []
    for txn in list_txns(root):
        if txn.meta.state in TxnState.TERMINAL:
            continue
        if txn.confirm_marker.exists():
            continue
        pid = txn.watchdog_pid()
        if pid is not None and _pid_alive(pid):
            continue
        out.append(txn)
    return out


def prune_history(keep: int, root: Path | None = None) -> int:
    """Delete oldest terminal transactions beyond ``keep``. Non-terminal
    journals are never pruned. Returns number removed."""
    import shutil

    terminal = [t for t in list_txns(root) if t.meta.state in TxnState.TERMINAL]
    removed = 0
    for txn in terminal[keep:]:
        shutil.rmtree(txn.dir, ignore_errors=True)
        removed += 1
    return removed


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
