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
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.contract import Ref


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


def prune_path(state: dict[str, Any], path: list[str]) -> None:
    node: Any = state
    for seg in path[:-1]:
        if not isinstance(node, dict) or seg not in node:
            return
        node = node[seg]
    if isinstance(node, dict):
        node.pop(path[-1], None)


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
    def __init__(self, host: str, root: Path | None = None):
        root = root if root is not None else config.candidate_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", host)
        self.path = root / f"{safe}.json"
        self.items: dict[str, CandidateItem] = {}
        self._load()

    # -- mutation ------------------------------------------------------------

    def set_path(self, ref: Ref, path: list[str], value: Any) -> None:
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
        self._save()

    def set_desired(self, ref: Ref, desired: dict[str, Any]) -> None:
        item = self._item(ref)
        item.mode = "replace"
        item.intent = copy.deepcopy(desired)
        item.delete_paths = []
        self._save()

    def delete(self, ref: Ref, path: list[str] | None = None) -> None:
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
        self._save()

    def drop(self, ref: Ref) -> None:
        self.items.pop(ref.key(), None)
        self._save()

    def clear(self) -> None:
        self.items = {}
        self._save()

    # -- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.items)

    def ordered_items(self) -> list[CandidateItem]:
        return list(self.items.values())

    def desired_for(self, item: CandidateItem, current_canonical: Any) -> Any:
        """Materialize the desired canonical-input state for one item."""
        if item.mode == "delete":
            return None
        if item.mode == "replace":
            return copy.deepcopy(item.intent)
        base = copy.deepcopy(current_canonical) if isinstance(current_canonical, dict) else {}
        # Prune the deleted subtrees from the base FIRST, then merge intent, so
        # a `delete <subtree>` followed by a `set` under it keeps only the set
        # value (the delete is honored, not silently cancelled).
        for path in item.delete_paths:
            prune_path(base, path)
        return deep_merge(base, item.intent)

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
        for entry in data.get("items", []):
            item = CandidateItem(
                ref_key=entry["ref_key"],
                mode=entry.get("mode", "merge"),
                intent=entry.get("intent", {}),
                delete_paths=entry.get("delete_paths", []),
            )
            self.items[item.ref_key] = item

    def _save(self) -> None:
        payload = {"format": 1, "items": [dataclasses.asdict(i) for i in self.items.values()]}
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
