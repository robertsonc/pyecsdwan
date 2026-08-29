"""Desired state read from a directory of YAML — the GitOps read half (epic #8).

`drift` compares live state against *staged intent*, which today means the
candidate store: whatever the operator typed since the last commit. That is the
wrong source for CI. A pipeline wants to check the fabric against a declaration
that lives in git, is reviewed, and is the same on every run.

This module is that source, and nothing more. It reads; it never writes. The
declarative *apply* half is a separate piece of work — the point of landing the
read half first is that a layout mistake here costs a rename, while the same
mistake underneath a write path costs a fabric.

Layout
------

::

    desired/
      fabric/
        interface-labels/global.yaml
        bio/CorpFabric.yaml
      appliances/
        BR1-EC/
          banners/global.yaml
          bgp/config.yaml

``fabric/<noun>/<instance>.yaml`` for orchestrator-scope,
``appliances/<name>/<noun>/<instance>.yaml`` for appliance-scope. Three
decisions worth stating, because each had a plausible alternative:

* **The directory names are user-facing nouns, never registry kinds.** Not a
  style preference: the kind for a per-appliance banner is
  ``appliance/banners``, which contains a path separator and would silently
  become two directory levels. #77 also settled that registry keys must not
  reach a surface an operator types, and a checked-in directory tree is about
  as typed as a surface gets.
* **The instance name is the file name, always** — including for singletons,
  where ``banners/global.yaml`` reads redundantly next to a bare
  ``banners.yaml``. One rule that is occasionally verbose beats two rules where
  the shorter one needs `list_refs` to decide whether a kind is a singleton,
  and answers differently on a fabric where it happens to have one instance.
  The shorthand can be added later without invalidating anything written under
  this rule; the reverse is not true.
* **Replace, not merge.** The file *is* the desired state: nothing is taken
  from the live object to fill it in. What an omitted key then means is
  ``normalize()``'s decision, not this module's — for a resource whose
  ``normalize()`` supplies a default, an omitted key means *the default*, which
  is what keeps declarations minimal without making them ambiguous.

  The difference from merge is the whole safety property. Under merge, an
  omitted key silently takes whatever the appliance currently has, so a file
  declaring one field would report "no drift" for every field it does not
  mention — an incomplete declaration would be indistinguishable from a
  complete one. Under replace, an omitted key takes the normalized default, so
  a box holding a non-default value for something nobody declared shows up as
  drift, which is the point.

Intent from here and intent from the candidate store go through *the same*
:func:`~pyecsdwan.candidate.materialize_desired`, deliberately. Two paths that
computed desired state differently would let `drift` report something `commit`
would not do, which is worse than either being wrong alone.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import structlog
import yaml

from pyecsdwan.candidate import CandidateItem, materialize_desired
from pyecsdwan.contract import Ref, Scope
from pyecsdwan.registry import Registry

log = structlog.get_logger(__name__)

#: Top-level directory names, matching the CLI's own scope nouns so the tree
#: reads the way the commands do (`show configuration fabric ...` /
#: `... appliance NAME ...`).
FABRIC_DIR = "fabric"
APPLIANCES_DIR = "appliances"

SUFFIXES = (".yaml", ".yml")


class DesiredError(Exception):
    """The directory could not be read as desired state. Always fatal: a
    partially-read declaration would report the undeclared remainder as
    ``undeclared`` and look like a smaller fabric rather than a broken input."""


@dataclasses.dataclass(frozen=True)
class Declared:
    """Every declared instance, keyed by ``Ref.key()``.

    Shaped as an intent source rather than a plain dict so
    ``reports.drift`` can take it and the candidate store through one seam.
    """

    items: dict[str, CandidateItem] = dataclasses.field(default_factory=dict)
    #: Where each ref was declared, for error messages that name a file.
    origins: dict[str, Path] = dataclasses.field(default_factory=dict)

    def item_for(self, ref: Ref) -> CandidateItem | None:
        return self.items.get(ref.key())

    def desired_for(self, item: CandidateItem, current: Any) -> Any:
        return materialize_desired(item, current)

    def __len__(self) -> int:
        return len(self.items)


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES)


def _read(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DesiredError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DesiredError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        # An empty file is ambiguous — "declare nothing" or "declare empty"? —
        # and the two differ by a whole resource. Refuse rather than pick.
        raise DesiredError(
            f"{path} is empty; write `{{}}` to declare an empty resource, or "
            f"delete the file to declare nothing"
        )
    if not isinstance(data, dict):
        raise DesiredError(
            f"{path}: top-level YAML value must be a mapping, got {type(data).__name__}"
        )
    return data


def _ref_for(registry: Registry, root: Path, path: Path) -> Ref:
    """Map one file's path to a Ref, or say precisely why it cannot."""
    parts = path.relative_to(root).parts
    where = path.relative_to(root)

    if parts[0] == FABRIC_DIR:
        if len(parts) != 3:
            raise DesiredError(
                f"{where}: expected {FABRIC_DIR}/<noun>/<instance>.yaml"
            )
        noun, filename, appliance = parts[1], parts[2], None
        scope: Scope | None = Scope.ORCHESTRATOR
    elif parts[0] == APPLIANCES_DIR:
        if len(parts) != 4:
            raise DesiredError(
                f"{where}: expected {APPLIANCES_DIR}/<appliance>/<noun>/<instance>.yaml"
            )
        appliance, noun, filename = parts[1], parts[2], parts[3]
        scope = Scope.APPLIANCE
    else:
        raise DesiredError(
            f"{where}: top-level directory must be {FABRIC_DIR!r} or {APPLIANCES_DIR!r}"
        )

    try:
        kind = registry.resolve_cli(noun, scope)
    # Broad: the registry's own exception types are its business, and any of
    # them means the same thing here — this file names something unresolvable.
    except Exception as exc:
        raise DesiredError(f"{where}: {exc}") from exc
    return Ref(kind=kind, name=Path(filename).stem, appliance=appliance)


def load(registry: Registry, root: Path) -> Declared:
    """Read a desired-state directory.

    Every file is read before anything is returned, so a typo in the last one
    is not reported after the first twenty have been accepted.
    """
    if not root.is_dir():
        raise DesiredError(f"{root} is not a directory")

    files = _yaml_files(root)
    if not files:
        # Not an error — an empty declaration is a legitimate starting point —
        # but silence here would look identical to a mistyped path, so the
        # caller is given the count to report.
        log.debug("desired_directory_empty", root=str(root))

    items: dict[str, CandidateItem] = {}
    origins: dict[str, Path] = {}
    for path in files:
        ref = _ref_for(registry, root, path)
        key = ref.key()
        if key in origins:
            raise DesiredError(
                f"{path.relative_to(root)} and {origins[key].relative_to(root)} both "
                f"declare {ref}; two files cannot own one instance"
            )
        # Replace mode: the file is the desired state, so an omitted section is
        # a section that should not exist (see the module docstring).
        items[key] = CandidateItem(ref_key=key, mode="replace", intent=_read(path))
        origins[key] = path
    return Declared(items=items, origins=origins)
