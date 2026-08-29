"""Desired state read from a directory of YAML — the GitOps read half (epic #8).

Implements **T7** of `specs/003-declarative-apply` (ratified 1.0.0): the
versioned declaration envelope and the atomic loader. It reads; it never
writes, and it never touches the network — which is a requirement, not a
happy accident (R2: invalid or empty input must not so much as construct a
client).

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

``fabric/<noun>/<instance>.yaml`` for orchestrator-scope,
``appliances/<name>/<noun>/<instance>.yaml`` for appliance-scope. The
directory names are user-facing nouns, never registry kinds: the kind for a
per-appliance banner is ``appliance/banners``, which contains a path separator
and would silently become two directory levels, and #77 settled that registry
keys must not reach a surface an operator types.

The envelope
------------

::

    apiVersion: pyecsdwan/v1
    state: present
    spec:
      issue: "PROPERTY OF ACME"

``apiVersion`` and ``state`` are both required and both explicit, because the
alternative is inferring them — and every inference here is a guess about
whether an object should exist. A version this build does not know fails
closed *without rewriting the file*: the newer tool that wrote it must still
be able to use it (the same rule the candidate store follows, #108).

``state`` is the lifecycle, and it is the whole of D4: only an explicit
``absent`` may request deletion. Removing a file means "stop declaring this",
never "delete it" — a directory is a partial, additive statement about the
fabric (D1), so absence of a declaration carries no authority over an object.

Identity, and who wins
----------------------

A document may repeat its own address (``kind``, ``name``, ``appliance``). The
path supplies those when the document does not, but where both speak the
parsed declaration is the authority and a disagreement is invalid rather than
resolved. Silently preferring either one would mean a file whose contents say
``BR1-EC`` could be applied to ``BR2-EC`` because of where it happened to sit.

Loading is all-or-nothing
-------------------------

One unreadable file, one unknown noun, one duplicate reference, one symlink
pointing outside the tree — any of these invalidates the *entire* set, before
any API access. A partially-read declaration is the worse failure: the
remainder is reported as undeclared, so a broken input looks like a smaller
fabric. Every file is checked and *every* error is reported together, because
a CI gate that surfaces one typo per run costs a round trip each time.

An empty declaration set is invalid (D6). It is indistinguishable from a
mistyped path, a failed checkout, or a templating error that produced nothing,
and the cost of being wrong is an apply that silently does nothing while
reporting success.

What this module does not decide
--------------------------------

Materialization. A declaration is typed *partial* intent (D7), and proving
that writing it cannot erase unknown, unmodeled or write-only fields (D8) is
per-resource work — T8. Until then items are staged in ``replace`` mode
exactly as before, and ``absent`` is parsed but refused, because no resource
has yet declared evidence-backed deletion (D16, T11).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import structlog
import yaml

from pyecsdwan.candidate import CandidateItem, materialize_desired
from pyecsdwan.contract import Ref, Scope
from pyecsdwan.registry import Registry

log = structlog.get_logger(__name__)

#: Top-level directory names, matching the CLI's own scope nouns so the tree
#: reads the way the commands do.
FABRIC_DIR = "fabric"
APPLIANCES_DIR = "appliances"

SUFFIXES = (".yaml", ".yml")

#: The one envelope version this build reads. Anything else fails closed.
API_VERSION = "pyecsdwan/v1"

#: Lifecycle states. `absent` is parsed and refused until a resource can prove
#: evidence-backed deletion and rollback (D16 / T11).
STATE_PRESENT = "present"
STATE_ABSENT = "absent"
STATES = (STATE_PRESENT, STATE_ABSENT)

#: Keys an envelope may carry. Anything else is a typo or a newer field, and
#: both are safer refused than ignored — an ignored `speec:` would apply an
#: empty object.
ENVELOPE_KEYS = frozenset({"apiVersion", "state", "spec", "kind", "name", "appliance"})


class DesiredError(Exception):
    """The directory could not be read as desired state. Always fatal."""


@dataclasses.dataclass(frozen=True)
class Declaration:
    """One parsed file: what it addresses, and what it says about it."""

    ref: Ref
    state: str
    spec: dict[str, Any]
    origin: Path

    def as_canonical(self) -> dict[str, Any]:
        """The digest-bearing form: identity and intent, nothing incidental.

        The origin path is deliberately excluded. Two checkouts of the same
        declarations at different paths are the same desired state, and a
        digest that disagreed would make the identity useless for saying
        "this plan is the one that was reviewed".
        """
        return {"ref": self.ref.key(), "state": self.state, "spec": self.spec}


@dataclasses.dataclass(frozen=True)
class Declared:
    """Every declared instance, keyed by ``Ref.key()``.

    Shaped as an intent source rather than a plain dict so ``reports.drift``
    can take it and the candidate store through one seam.
    """

    items: dict[str, CandidateItem] = dataclasses.field(default_factory=dict)
    #: Where each ref was declared, for error messages that name a file.
    origins: dict[str, Path] = dataclasses.field(default_factory=dict)
    #: The parsed declarations, in ref order.
    declarations: tuple[Declaration, ...] = ()

    def ordered_items(self) -> list[CandidateItem]:
        """Insertion order, deterministic because :func:`_yaml_files` sorts."""
        return list(self.items.values())

    def item_for(self, ref: Ref) -> CandidateItem | None:
        return self.items.get(ref.key())

    def desired_for(self, item: CandidateItem, current: Any) -> Any:
        return materialize_desired(item, current)

    def __len__(self) -> int:
        return len(self.items)

    @property
    def digest(self) -> str:
        """Stable identity for this declaration set.

        Over canonical JSON with sorted keys, so it depends on what was
        declared and not on file order, whitespace, or where the checkout
        lives. Two runs of the same reviewed input produce the same digest;
        a single changed value produces a different one.
        """
        canonical = json.dumps(
            [d.as_canonical() for d in self.declarations],
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _yaml_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES)


def _within(root: Path, path: Path) -> bool:
    """Whether ``path`` really lives under ``root`` once symlinks resolve.

    ``rglob`` follows symlinks, so a link inside the tree can point at
    ``/etc`` — or at another checkout — and be read as though it were a
    declaration. The declaration set has to be exactly the reviewed directory.
    """
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def _address_from_path(registry: Registry, root: Path, path: Path) -> Ref:
    """The address a file's location implies, or why it implies none."""
    parts = path.relative_to(root).parts
    where = path.relative_to(root)

    if parts[0] == FABRIC_DIR:
        if len(parts) != 3:
            raise DesiredError(f"{where}: expected {FABRIC_DIR}/<noun>/<instance>.yaml")
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


def _parse(registry: Registry, root: Path, path: Path) -> Declaration:
    """One file to one declaration, or a `DesiredError` saying exactly why."""
    where = path.relative_to(root)
    if not _within(root, path):
        raise DesiredError(
            f"{where}: resolves outside {root} (symlink); a declaration set is "
            f"exactly the reviewed directory"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DesiredError(f"{where}: cannot read ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DesiredError(f"{where}: invalid YAML ({exc})") from exc
    if not isinstance(data, dict):
        got = "nothing" if data is None else type(data).__name__
        raise DesiredError(f"{where}: top-level YAML value must be a mapping, got {got}")

    version = data.get("apiVersion")
    if version is None:
        raise DesiredError(
            f"{where}: no apiVersion. Add `apiVersion: {API_VERSION}` — the "
            f"envelope is explicit so a future format can be told apart from "
            f"this one rather than guessed at"
        )
    if version != API_VERSION:
        # Not rewritten, not migrated: the tool that wrote it may still need it.
        raise DesiredError(
            f"{where}: apiVersion {version!r} is not {API_VERSION!r}; this build "
            f"cannot read it and has not modified it"
        )

    unknown = sorted(set(data) - ENVELOPE_KEYS)
    if unknown:
        raise DesiredError(
            f"{where}: unknown envelope key(s) {', '.join(repr(k) for k in unknown)}; "
            f"expected {', '.join(sorted(ENVELOPE_KEYS))}"
        )

    state = data.get("state")
    if state not in STATES:
        raise DesiredError(
            f"{where}: state must be one of {', '.join(STATES)}, got {state!r}. It is "
            f"required because absence of a file never means deletion — only an "
            f"explicit `state: absent` does"
        )

    ref = _reconcile(registry, root, path, data)

    spec = data.get("spec")
    if state == STATE_ABSENT:
        if spec is not None:
            raise DesiredError(
                f"{where}: `state: absent` must not carry a spec; there is no desired "
                f"body for an object that should not exist"
            )
        raise DesiredError(
            f"{where}: `state: absent` is not supported yet. No resource has the "
            f"verified deletion and rollback evidence it requires, and declaring "
            f"deletion the tool cannot prove it can undo is the one thing this "
            f"format must not allow"
        )
    if spec is None:
        raise DesiredError(
            f"{where}: `state: present` requires a `spec` mapping (use `spec: {{}}` "
            f"to declare an empty object)"
        )
    if not isinstance(spec, dict):
        raise DesiredError(f"{where}: spec must be a mapping, got {type(spec).__name__}")

    return Declaration(ref=ref, state=state, spec=spec, origin=path)


def _reconcile(registry: Registry, root: Path, path: Path, data: dict[str, Any]) -> Ref:
    """The address, from the document where it speaks and the path otherwise.

    Where both speak they must agree. Resolving a disagreement either way
    would mean a file whose contents name one appliance could be applied to
    another because of where it happened to sit.
    """
    where = path.relative_to(root)
    from_path = _address_from_path(registry, root, path)

    stated_kind = data.get("kind")
    if stated_kind is not None:
        scope = Scope.APPLIANCE if from_path.appliance is not None else Scope.ORCHESTRATOR
        try:
            resolved = registry.resolve_cli(str(stated_kind), scope)
        except Exception as exc:
            raise DesiredError(f"{where}: kind {stated_kind!r}: {exc}") from exc
        if resolved != from_path.kind:
            raise DesiredError(
                f"{where}: document declares kind {stated_kind!r} but its path says "
                f"{registry.cli_name(from_path.kind)!r}; refusing rather than "
                f"choosing between them"
            )
    for field, stated, implied in (
        ("name", data.get("name"), from_path.name),
        ("appliance", data.get("appliance"), from_path.appliance),
    ):
        if stated is not None and str(stated) != str(implied):
            raise DesiredError(
                f"{where}: document declares {field} {stated!r} but its path says "
                f"{implied!r}; refusing rather than choosing between them"
            )
    return from_path


def load(registry: Registry, root: Path) -> Declared:
    """Read a desired-state directory, atomically.

    Every file is parsed and *every* failure is reported before anything is
    returned, so a CI gate sees all its typos in one run rather than one per
    round trip. Nothing here touches the resolver or the API: an invalid or
    empty input must fail before a client is ever built (R2).
    """
    if not root.is_dir():
        raise DesiredError(f"{root} is not a directory")

    files = _yaml_files(root)
    if not files:
        # D6. Indistinguishable from a mistyped path, a failed checkout, or a
        # template that rendered nothing — and the cost of guessing "the
        # operator meant to change nothing" is an apply that reports success
        # having done nothing.
        raise DesiredError(
            f"{root} contains no declarations. An empty desired-state directory is "
            f"refused rather than treated as 'change nothing': it is the same shape "
            f"as a wrong path or a failed checkout"
        )

    declarations: list[Declaration] = []
    problems: list[str] = []
    for path in files:
        try:
            declarations.append(_parse(registry, root, path))
        except DesiredError as exc:
            problems.append(str(exc))

    origins: dict[str, Path] = {}
    for decl in declarations:
        key = decl.ref.key()
        if key in origins:
            problems.append(
                f"{decl.origin.relative_to(root)} and {origins[key].relative_to(root)} "
                f"both declare {decl.ref}; two files cannot own one instance"
            )
            continue
        origins[key] = decl.origin

    if problems:
        raise DesiredError(
            f"{len(problems)} problem(s) in {root}; nothing was read:\n  - "
            + "\n  - ".join(problems)
        )

    declarations.sort(key=lambda d: d.ref.key())
    items = {
        d.ref.key(): CandidateItem(ref_key=d.ref.key(), mode="replace", intent=d.spec)
        for d in declarations
    }
    declared = Declared(
        items=items, origins=origins, declarations=tuple(declarations)
    )
    log.debug("desired_loaded", root=str(root), count=len(items), digest=declared.digest)
    return declared
