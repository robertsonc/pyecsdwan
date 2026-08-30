"""Structural diff over canonical states, and its Junos-style rendering.

The diff engine only ever sees *canonical* states (post-``normalize()``).
It is deliberately dumb: recursive compare of dicts/lists/scalars producing
add/remove/replace entries with full paths. Semantic knowledge (which list is
a set, which key is an ID) belongs in ``normalize()``, not here.
"""

from __future__ import annotations

from typing import Any

from pyecsdwan import redaction
from pyecsdwan.contract import CanonicalState, Diff, DiffEntry, DiffOp

_MISSING = object()


def structural_diff(current: CanonicalState, desired: CanonicalState) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    _walk((), current if current is not None else _MISSING,
          desired if desired is not None else _MISSING, entries)
    return entries


def _walk(path: tuple[str, ...], old: Any, new: Any, out: list[DiffEntry]) -> None:
    if old is _MISSING and new is _MISSING:
        return
    if old is _MISSING:
        out.append(DiffEntry(op=DiffOp.ADD, path=path, new=new))
        return
    if new is _MISSING:
        out.append(DiffEntry(op=DiffOp.REMOVE, path=path, old=old))
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            _walk(
                (*path, str(key)),
                old.get(key, _MISSING),
                new.get(key, _MISSING),
                out,
            )
        return
    if isinstance(old, list) and isinstance(new, list):
        # Canonical lists are stably sorted by normalize(); compare positionally.
        for i in range(max(len(old), len(new))):
            _walk(
                (*path, str(i)),
                old[i] if i < len(old) else _MISSING,
                new[i] if i < len(new) else _MISSING,
                out,
            )
        return
    if old != new:
        out.append(DiffEntry(op=DiffOp.REPLACE, path=path, old=old, new=new))


def render_diff_lines(diff: Diff) -> list[tuple[str, str]]:
    """Render as (marker, text) pairs; marker is '+', '-', or '~'.

    The CLI colorizes: '-' red, '+' green. Junos flavor: replace shows as a
    remove line followed by an add line.

    This is the one place diff values become text, which makes it the one
    place to redact them (#106): every human surface — compare, plan, drift —
    renders through here, and a secret shown "just in the diff" is a secret
    in a terminal scrollback and a copy-pasted ticket. The `-`/`+` pair still
    shows *that* a secret changed, because the markers keep a change hint.
    """
    lines: list[tuple[str, str]] = []
    for e in diff.entries:
        dotted = ".".join(e.path) if e.path else "(root)"
        old, new = _shielded(e)
        if e.op is DiffOp.ADD:
            lines.append(("+", f"{dotted}: {_fmt(new)}"))
        elif e.op is DiffOp.REMOVE:
            lines.append(("-", f"{dotted}: {_fmt(old)}"))
        else:
            lines.append(("-", f"{dotted}: {_fmt(old)}"))
            lines.append(("+", f"{dotted}: {_fmt(new)}"))
    return lines


def _shielded(e: DiffEntry) -> tuple[object, object]:
    """The entry's values, with secrets masked.

    Two ways a secret reaches an entry: the path itself ends inside a
    secret-named field (a scalar diff at ``neighbors.0.password``), or the
    value is a subtree that contains one (a whole-object add). Checking every
    path segment covers the first — a diff *under* a secret field, like
    ``community.0``, names the secret in the middle of the path, not the end.
    """
    if any(redaction.looks_secret(seg) for seg in e.path):
        # None is an absent side (an ADD has no old), not a secret worth a
        # marker; whichever side the op renders is the one that is real.
        return (
            None if e.old is None else redaction.mask(e.old),
            None if e.new is None else redaction.mask(e.new),
        )
    return redaction.redact_tree(e.old), redaction.redact_tree(e.new)


def _fmt(value: Any) -> str:
    import json

    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return repr(value)
