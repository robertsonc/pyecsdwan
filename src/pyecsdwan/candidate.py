"""Candidate changeset store — Junos candidate-config emulation.

``set``/``delete`` operations accumulate here; nothing touches the
Orchestrator until commit. One candidate per Orchestrator host, on disk under
``~/.pyecsdwan/candidate/``, so a dropped SSH session keeps its pending work.

Merge semantics: a candidate item records user *intent*, not full state.

* mode ``merge``   — intent fragments deep-merged over the server's current
  canonical state at compare/commit time (``set`` commands).
* mode ``replace`` — intent is the complete desired state (``--file x.yaml``).
* mode ``delete``  — the resource is to be removed entirely.

``delete_paths`` prunes subtrees during a merge (``delete bio X topology``).

Concurrency (issue #63): atomic replacement stops a *torn* file, not a *lost*
one. Two shells that both loaded the candidate at T0 and saved at T1 and T2
would leave only the second one's work, with no error and nothing in the
journal to show the first operator's staged changes ever existed.

So a mutation is not a save — it is a locked read-modify-write cycle. Every
mutator re-reads the store from disk under the host's candidate lock before
touching it, which is what makes a long-lived interactive shell safe: its
in-memory view is refreshed at the moment of the change rather than trusted
from whenever the shell started.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol

from pyecsdwan import config
from pyecsdwan.contract import Ref
from pyecsdwan.locking import DEFAULT_TIMEOUT, HostLock


class CandidateCorruptError(Exception):
    """The on-disk candidate store could not be parsed; it has been quarantined."""


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


#: On-disk schema version for the candidate store. Bumped only alongside an
#: entry in :data:`CANDIDATE_MIGRATIONS`; a file claiming anything higher was
#: written by a newer binary and is refused rather than guessed at (#108).
CANDIDATE_FORMAT = 1

#: Older formats this binary knows how to read, mapped to the function that
#: brings one forward. Empty because 1 is the first version — the mapping
#: exists so adding version 2 is a change here rather than a new mechanism.
CANDIDATE_MIGRATIONS: dict[int, Any] = {}

#: Every mode `materialize_desired` knows how to honour. Anything else is an
#: error: modes decide whether omitted keys keep their live values, and
#: guessing that is the difference between a replace and a merge.
SUPPORTED_MODES = frozenset({"merge", "replace", "delete"})


class CandidateFormatError(Exception):
    """The candidate store was written by a binary this one cannot read.

    Distinct from :class:`CandidateCorruptError`, which quarantines the file:
    a future format is not damaged, it is simply newer, and moving it would
    destroy state the binary that wrote it can still use. Refuse and leave the
    bytes alone (#108).
    """


def materialize_desired(item: CandidateItem, current_canonical: Any) -> Any:
    """The desired canonical-input state for one candidate item.

    A free function, not just a store method, because ``txn.commit --rebase``
    has to re-materialize intent against state it re-read at commit time and
    has no store in hand at that point.
    """
    if item.mode == "delete":
        return None
    if item.mode == "replace":
        return copy.deepcopy(item.intent)
    if item.mode != "merge":
        # Not a fallback to merge. An unrecognised mode used to land here
        # silently, so `replace-all` — one typo from `replace` — merged
        # instead, leaving on the appliance exactly the keys the operator
        # meant to remove (#108). Checked here as well as at load because
        # `commit --rebase` re-materializes through this function directly.
        raise CandidateFormatError(
            f"candidate item {item.ref_key} has unknown mode {item.mode!r}; "
            f"expected one of {', '.join(sorted(SUPPORTED_MODES))}"
        )
    base = copy.deepcopy(current_canonical) if isinstance(current_canonical, dict) else {}
    # Prune the deleted subtrees from the base FIRST, then merge intent, so
    # a `delete <subtree>` followed by a `set` under it keeps only the set
    # value (the delete is honored, not silently cancelled).
    for path in item.delete_paths:
        prune_path(base, path)
    return deep_merge(base, item.intent)


def prune_path(state: dict[str, Any], path: list[str]) -> None:
    node: Any = state
    for seg in path[:-1]:
        if not isinstance(node, dict) or seg not in node:
            return
        node = node[seg]
    if isinstance(node, dict):
        node.pop(path[-1], None)


class IntentSource(Protocol):
    """Where "what this instance should be" comes from.

    Two things implement it: :class:`CandidateStore` (what the operator typed
    since the last commit) and :class:`pyecsdwan.desired.Declared` (a directory
    of YAML in git). ``txn.build_plan`` and ``reports.drift`` both consume it,
    so a declared change and a staged one are planned, diffed, guarded and
    committed by exactly the same code — the only difference is where the
    intent came from.

    That sameness is the point. Two paths that materialized desired state
    differently would let `drift` report something `apply` would not do.
    """

    def ordered_items(self) -> list[CandidateItem]:
        """Every staged/declared item. Dependency ordering is the registry's
        job, so the order here only has to be deterministic."""
        ...

    def item_for(self, ref: Ref) -> CandidateItem | None: ...

    def desired_for(self, item: CandidateItem, current_canonical: Any) -> Any: ...


@dataclasses.dataclass
class CandidateItem:
    ref_key: str
    mode: str = "merge"  # merge | replace | delete
    intent: dict[str, Any] = dataclasses.field(default_factory=dict)
    delete_paths: list[list[str]] = dataclasses.field(default_factory=list)

    @property
    def ref(self) -> Ref:
        return Ref.from_key(self.ref_key)


class CandidateStore:
    def __init__(
        self,
        host: str,
        root: Path | None = None,
        lock_root: Path | None = None,
        lock_timeout: float = DEFAULT_TIMEOUT,
    ):
        root = root if root is not None else config.candidate_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
        self.path = root / f"{safe}.json"
        self.host = host
        self.items: dict[str, CandidateItem] = {}
        self.lock = HostLock(host, "candidate", root=lock_root, timeout=lock_timeout)
        self._load()

    # -- mutation ------------------------------------------------------------

    @contextlib.contextmanager
    def _mutate(self) -> Iterator[None]:
        """One read-modify-write cycle, serialized against other writers.

        Re-reading inside the lock is the whole point. Locking only the write
        would still lose an update: both writers would be serialized, and the
        second would still be writing a view of the world from before the
        first one's change.
        """
        with self.lock:
            self.reload()
            yield
            self._save()

    def reload(self) -> None:
        """Discard the in-memory view and re-read from disk."""
        self.items = {}
        self._load()

    def set_path(self, ref: Ref, path: list[str], value: Any) -> None:
        with self._mutate():
            item = self._item(ref)
            if item.mode == "delete":
                # A set after a delete resurrects the resource as a fresh replace.
                item.mode = "replace"
                item.intent = {}
                item.delete_paths = []
            node = item.intent
            for seg in path[:-1]:
                nxt = node.get(seg)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[seg] = nxt
                node = nxt
            if path:
                node[path[-1]] = value
            # Drop only an exact-match delete of this same path (set overrides a
            # prior delete of the identical leaf). A delete of a *parent* subtree
            # is preserved — desired_for prunes the base first, then merges this
            # set on top, giving correct Junos semantics ("delete wan; set wan 9 …"
            # leaves only label 9).
            item.delete_paths = [p for p in item.delete_paths if p != path]

    def set_desired(self, ref: Ref, desired: dict[str, Any]) -> None:
        with self._mutate():
            item = self._item(ref)
            item.mode = "replace"
            item.intent = copy.deepcopy(desired)
            item.delete_paths = []

    def delete(self, ref: Ref, path: list[str] | None = None) -> None:
        with self._mutate():
            item = self._item(ref)
            if not path:
                item.mode = "delete"
                item.intent = {}
                item.delete_paths = []
            else:
                if item.mode == "delete":
                    return
                item.delete_paths.append(path)
                prune_path(item.intent, path)

    def drop(self, ref: Ref) -> None:
        with self._mutate():
            self.items.pop(ref.key(), None)

    def clear(self) -> None:
        """Discard everything staged. The operator's explicit `discard`.

        Not what a successful commit should call — see
        :meth:`clear_committed`.
        """
        with self._mutate():
            self.items = {}

    def clear_committed(self, committed: Iterable[CandidateItem]) -> list[str]:
        """Remove exactly what a commit applied, keeping anything staged since.

        `commit` used to call :meth:`clear`, which takes the lock, *re-reads
        the file* — picking up whatever another shell staged in the meantime —
        and then wipes all of it. So shell A committing X destroyed shell B's
        unrelated Y, silently, as the last act of a successful transaction
        (#63).

        The acknowledgement is per item and by value. An item whose key was
        not in the commit's snapshot was staged afterwards and is not this
        commit's to remove; an item whose *content* changed since the plan was
        built is new intent that nobody has committed yet, so it survives too.
        Returns the keys kept for that second reason, which the caller reports
        — silently keeping them would be its own surprise.
        """
        planned = {item.ref_key: copy.deepcopy(item) for item in committed}
        kept: list[str] = []
        with self._mutate():
            for key, current in list(self.items.items()):
                snapshot = planned.get(key)
                if snapshot is None:
                    continue
                if current == snapshot:
                    del self.items[key]
                else:
                    kept.append(key)
        return kept

    # -- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.items)

    def ordered_items(self) -> list[CandidateItem]:
        return list(self.items.values())

    def item_for(self, ref: Ref) -> CandidateItem | None:
        """Lookup half of :class:`IntentSource`.

        Deliberately not `_item()`, which *creates* on miss — a report asking
        "is anything staged for this?" must not stage something by asking.
        """
        return self.items.get(ref.key())

    def desired_for(self, item: CandidateItem, current_canonical: Any) -> Any:
        """Materialize the desired canonical-input state for one item."""
        return materialize_desired(item, current_canonical)

    # -- persistence ---------------------------------------------------------

    def _item(self, ref: Ref) -> CandidateItem:
        key = ref.key()
        if key not in self.items:
            self.items[key] = CandidateItem(ref_key=key)
        return self.items[key]

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError:
            return
        except json.JSONDecodeError as exc:
            # A corrupt candidate must not silently read as empty — that would
            # make commit report "no changes" and the next save overwrite the
            # evidence. Quarantine it and tell the operator.
            corrupt = self.path.with_suffix(".corrupt")
            try:
                self.path.replace(corrupt)
            except OSError:
                corrupt = self.path
            raise CandidateCorruptError(
                f"candidate store {self.path} is corrupt ({exc}); moved to {corrupt}. "
                f"Re-stage your changes."
            ) from exc
        fmt = data.get("format", CANDIDATE_FORMAT)
        if not isinstance(fmt, int) or fmt > CANDIDATE_FORMAT:
            # Refused *before* anything is parsed and without touching the
            # file: the newer binary that wrote it must still be able to use
            # it, which quarantining or rewriting would prevent.
            raise CandidateFormatError(
                f"candidate store {self.path} is format {fmt!r}, but this build "
                f"reads at most {CANDIDATE_FORMAT}; upgrade, or discard it with a "
                f"build that understands it. The file has not been modified."
            )
        migrate = CANDIDATE_MIGRATIONS.get(fmt)
        if migrate is not None:
            data = migrate(data)
        for entry in data.get("items", []):
            mode = entry.get("mode")
            if mode not in SUPPORTED_MODES:
                # A missing mode used to default to merge, which is the unsafe
                # direction: it silently keeps live values the operator may
                # have staged a replace to remove.
                raise CandidateFormatError(
                    f"candidate store {self.path} holds item "
                    f"{entry.get('ref_key')!r} with mode {mode!r}; expected one of "
                    f"{', '.join(sorted(SUPPORTED_MODES))}. The file has not been "
                    f"modified."
                )
            item = CandidateItem(
                ref_key=entry["ref_key"],
                mode=mode,
                intent=entry.get("intent", {}),
                delete_paths=entry.get("delete_paths", []),
            )
            self.items[item.ref_key] = item

    def _save(self) -> None:
        payload = {
            "format": CANDIDATE_FORMAT,
            "items": [dataclasses.asdict(i) for i in self.items.values()],
        }
        # Unique temp name + fsync + 0o600: two shells staging against the same
        # host must not O_TRUNC each other's staging file, and the candidate can
        # hold secret config values.
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
