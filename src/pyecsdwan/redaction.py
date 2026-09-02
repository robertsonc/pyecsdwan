"""Recursive secret redaction for everything this tool renders or exports (#106).

One detector, used everywhere a value leaves the process in readable form:
diff rendering, candidate dumps, audit exports, journaled raw-API parameters,
and API error text. The alternative — each surface keeping its own list of
sensitive field names — is how one surface's list rots while another's grows,
and a password that `compare` hides then walks out through `show journal
--events` anyway.

Detection is by *field name*, because that is the only signal a generic tree
walk has. The names come from what the Orchestrator API actually carries:
BGP neighbor ``password``, OSPF ``authKey``/``md5Password``, SNMP
``community`` strings and v3 ``privPassword``, and the token/credential
fields of the integration endpoints. A secret stored under a name this list
does not recognise is not redacted — which is why redaction is the *outer*
layer of #106, behind file modes, the journal's private/audit split, and
envelope encryption (:mod:`pyecsdwan.vault`), never the only one.

A redacted value keeps the field name and a truncated digest of what was
there, so two exports can still answer "did it change?" without either
revealing it. The digest is deliberately short: it is a change hint, not a
commitment — and even so, a low-entropy secret is confirmable from any
unsalted digest by dictionary, so nothing here treats the digest as safe to
publish where the name alone would not be.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any

#: Substrings that mark a field name as secret-bearing, matched against the
#: lowercased name with separators removed (so ``md5_password``,
#: ``md5Password`` and ``MD5-PASSWORD`` are one spelling). Substring match is
#: the deliberate bias: ``neighborPassword`` and ``communityString`` must hit,
#: and the false-positive cost of redacting an innocent field is a hidden
#: value, not a leaked one.
SECRET_NAME_TOKENS: tuple[str, ...] = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "community",
    "token",
    "credential",
    "apikey",
    "authkey",
    "authenticationkey",
    "md5key",
    "privatekey",
    "presharedkey",
    "sharedkey",
    "encryptionkey",
    "psk",
)

_STRIP_SEPARATORS = re.compile(r"[^a-z0-9]")

#: What a redacted value renders as. The prefix is a constant so tests — and
#: humans grepping an export — can find every redaction with one string.
REDACTED_PREFIX = "<redacted"


def looks_secret(name: object) -> bool:
    """Whether a field name marks its value as secret-bearing."""
    folded = _STRIP_SEPARATORS.sub("", str(name).lower())
    return any(token in folded for token in SECRET_NAME_TOKENS)


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical JSON form.

    The one definition of "the digest of a body", shared by the audit export
    and the journal's snapshot split so the two are comparable record for
    record.
    """
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _marker(value: Any) -> str:
    """The string a secret value is replaced with.

    An empty value gets no digest: ``password: ""`` is "no password set",
    which is state worth distinguishing from "some password", and a digest of
    the empty string would let anyone confirm emptiness anyway.
    """
    if value in (None, "", {}, []):
        return f"{REDACTED_PREFIX}:empty>"
    return f"{REDACTED_PREFIX}:{digest(value)[:8]}>"


def mask(value: Any) -> str:
    """The redaction marker for one value, for callers that already know the
    value is secret — a diff whose *path* ends inside a secret-named field has
    no dict key left to trigger :func:`redact_tree`."""
    return _marker(value)


def redact_tree(value: Any) -> Any:
    """A deep copy of ``value`` with every secret-named field replaced.

    The whole subtree under a secret name goes — a dict under ``credentials``
    is redacted as one value, because its inner names are not what marked it
    and may not mark themselves.
    """
    if isinstance(value, dict):
        return {
            key: _marker(val) if looks_secret(key) else redact_tree(val)
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    return value


def finds_secrets(value: Any) -> bool:
    """Whether a tree holds at least one secret-named field.

    What decides that a candidate save or a journal snapshot needs the vault
    at all — so a box with no keyring pays nothing until it stages something
    the detector recognises.
    """
    if isinstance(value, dict):
        return any(
            looks_secret(key) or finds_secrets(val) for key, val in value.items()
        )
    if isinstance(value, list):
        return any(finds_secrets(item) for item in value)
    return False


def redact_params(params: dict[str, str]) -> dict[str, str]:
    """Query parameters safe to journal: secret-named values become markers."""
    return {
        key: _marker(val) if looks_secret(key) else val for key, val in params.items()
    }


def redact_query(path: str) -> str:
    """An API path safe to journal or render: secret-named query values masked.

    The path half is untouched — it is what the audit trail exists to record —
    and a path with no query string comes back byte-identical.
    """
    if "?" not in path:
        return path
    base, _, query = path.partition("?")
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return path
    kept = [
        (key, _marker(val) if looks_secret(key) else val) for key, val in pairs
    ]
    rebuilt = "&".join(f"{key}={val}" for key, val in kept)
    return f"{base}?{rebuilt}"


def find_markers(value: Any, _path: str = "") -> list[str]:
    """Dotted paths of every redaction or sealed marker inside a tree.

    For preflight (spec 003 R12): a declaration holding one of these was
    built from a redacted export or a sealed store, and writing it would
    replace the real secret with the mask. Checked on *input* trees; the
    output-side markers this module writes are supposed to be there.
    """
    from pyecsdwan import vault

    if isinstance(value, str):
        return [_path or "(root)"] if value.startswith(REDACTED_PREFIX) else []
    if isinstance(value, dict):
        if vault.is_sealed(value):
            return [_path or "(root)"]
        out: list[str] = []
        for key, val in value.items():
            out.extend(find_markers(val, f"{_path}.{key}" if _path else str(key)))
        return out
    if isinstance(value, list):
        out = []
        for index, item in enumerate(value):
            out.extend(find_markers(item, f"{_path}.{index}" if _path else str(index)))
        return out
    return []
