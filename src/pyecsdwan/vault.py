"""Envelope encryption for rollback-private secret material (#106).

What it protects: the two places this tool *persists* values the redaction
layer would hide — staged candidate intent, and the journal's pre-change
snapshots. Both must round-trip byte-perfect (a redacted snapshot cannot be
restored; a redacted candidate cannot be committed), so redaction is not an
option there and file mode 0600 was the only protection. This adds a real
one: AES-256-GCM under an envelope key that never touches the state
directory.

Key sourcing, in precedence order:

1. ``ECSDWAN_ENVELOPE_KEY`` — base64 of 32 bytes, for headless boxes with no
   keyring backend. The operator owns its storage.
2. OS keyring, service ``pyecsdwan``, username ``envelope-key``. Created on
   first need; during rotation the outgoing key sits under
   ``envelope-key-previous`` until every blob is re-sealed.

Fail-closed is the contract: material the detector marked secret is sealed or
it is not persisted. A box that can't seal refuses the save *before* anything
lands on disk or the fabric — and says which of the two sources to configure.
A box with no secrets staged never needs a key at all.

Backups: the state directory alone is not a backup once anything is sealed.
``ECSDWAN_ENVELOPE_KEY`` (or the keyring entry) must be preserved with it,
and ``docs/secrets.md`` says so where an operator will read it.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import os
import secrets as _secrets
from typing import Any

from pyecsdwan import redaction

ENV_ENVELOPE_KEY = "ECSDWAN_ENVELOPE_KEY"
KEYRING_SERVICE = "pyecsdwan"
KEYRING_USER = "envelope-key"
KEYRING_USER_PREVIOUS = "envelope-key-previous"

#: Marker field of a sealed blob. Namespaced so no plausible API payload
#: collides with it, and versioned so a future format change is a new value
#: rather than a guess.
SEALED_FIELD = "__ecsdwan_sealed__"
SEALED_VERSION = 1

KEY_BYTES = 32
_NONCE_BYTES = 12


class VaultUnavailable(Exception):
    """No envelope key can be loaded, so secret material cannot be sealed.

    Raised *before* any persistence, which is the point: the safe answer to
    "I cannot encrypt this" is to not write it, not to write it in the clear.
    The message carries both remedies because which one applies depends on
    the box, not on this code.
    """

    def __init__(self, reason: str):
        super().__init__(
            f"cannot access the secrets envelope key ({reason}). Secret-bearing "
            f"state is never persisted unencrypted. Either make the OS keyring "
            f"available (service {KEYRING_SERVICE!r}, username {KEYRING_USER!r}) "
            f"or set {ENV_ENVELOPE_KEY} to base64 of {KEY_BYTES} random bytes "
            f"(e.g. `python -c \"import base64,secrets;"
            f"print(base64.b64encode(secrets.token_bytes(32)).decode())\"`)."
        )
        self.reason = reason


class VaultOpenError(Exception):
    """A sealed blob would not open under any available key.

    Wrong key (rotated away without re-sealing, or a restored backup missing
    its key) or a tampered/corrupt blob — GCM cannot tell those apart, and
    neither can this. What it must never do is read as "no value here".
    """


def _aesgcm(key: bytes) -> Any:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


def _decode_key(encoded: str, source: str) -> bytes:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise VaultUnavailable(f"{source} is not valid base64: {exc}") from exc
    if len(raw) != KEY_BYTES:
        raise VaultUnavailable(
            f"{source} decodes to {len(raw)} bytes, need exactly {KEY_BYTES}"
        )
    return raw


def _keyring() -> Any:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - keyring is a hard dependency
        raise VaultUnavailable("the keyring package is not installed") from exc
    return keyring


def _keyring_get(username: str) -> str | None:
    try:
        found = _keyring().get_password(KEYRING_SERVICE, username)
    except VaultUnavailable:
        raise
    except Exception as exc:
        raise VaultUnavailable(
            f"the OS keyring would not open: {type(exc).__name__}: {exc}"
        ) from exc
    return found or None


def load_keys(create: bool = False) -> list[bytes]:
    """Every key blobs may be sealed under, current first.

    The environment key, when set, is the *only* key: an operator who
    exported one has taken ownership of key management, and silently mixing
    it with a keyring key would make "which key opened this?" unanswerable.
    Otherwise the keyring's current key, plus the previous one while a
    rotation is unfinished.
    """
    env = os.environ.get(ENV_ENVELOPE_KEY)
    if env:
        return [_decode_key(env, ENV_ENVELOPE_KEY)]
    keys: list[bytes] = []
    current = _keyring_get(KEYRING_USER)
    if current is not None:
        keys.append(_decode_key(current, f"keyring entry {KEYRING_USER!r}"))
    elif create:
        fresh = _secrets.token_bytes(KEY_BYTES)
        try:
            _keyring().set_password(
                KEYRING_SERVICE, KEYRING_USER, base64.b64encode(fresh).decode("ascii")
            )
        except Exception as exc:
            raise VaultUnavailable(
                f"the OS keyring would not store a new key: {type(exc).__name__}: {exc}"
            ) from exc
        keys.append(fresh)
    previous = _keyring_get(KEYRING_USER_PREVIOUS)
    if previous is not None:
        keys.append(_decode_key(previous, f"keyring entry {KEYRING_USER_PREVIOUS!r}"))
    return keys


def seal(value: Any, purpose: str) -> dict[str, Any]:
    """Encrypt one JSON-serializable value under the envelope key.

    ``purpose`` is bound in as GCM associated data, so a blob sealed as a
    snapshot cannot be spliced into a candidate file and decrypt as intent.
    """
    keys = load_keys(create=True)
    if not keys:
        raise VaultUnavailable("no envelope key is stored and none could be created")
    plaintext = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    nonce = _secrets.token_bytes(_NONCE_BYTES)
    ciphertext = _aesgcm(keys[0]).encrypt(nonce, plaintext, purpose.encode("utf-8"))
    return {
        SEALED_FIELD: SEALED_VERSION,
        "purpose": purpose,
        "data": base64.b64encode(nonce + ciphertext).decode("ascii"),
    }


def is_sealed(value: Any) -> bool:
    return isinstance(value, dict) and SEALED_FIELD in value


def unseal(blob: dict[str, Any], purpose: str) -> Any:
    """Decrypt a sealed blob, trying the current key then the rotation fallback."""
    if not is_sealed(blob):
        raise VaultOpenError("value is not a sealed blob")
    if blob.get(SEALED_FIELD) != SEALED_VERSION:
        raise VaultOpenError(
            f"sealed blob is version {blob.get(SEALED_FIELD)!r}; this build reads "
            f"version {SEALED_VERSION}. Refusing rather than guessing at a newer format."
        )
    if blob.get("purpose") != purpose:
        raise VaultOpenError(
            f"sealed blob is for {blob.get('purpose')!r}, not {purpose!r}; refusing "
            f"to open material outside the context it was sealed in"
        )
    try:
        raw = base64.b64decode(str(blob.get("data", "")), validate=True)
    except Exception as exc:
        raise VaultOpenError(f"sealed blob is corrupt: {exc}") from exc
    if len(raw) <= _NONCE_BYTES:
        raise VaultOpenError("sealed blob is truncated")
    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    keys = load_keys(create=False)
    if not keys:
        raise VaultOpenError(
            f"no envelope key is available to open sealed material. Restore the "
            f"keyring entry (service {KEYRING_SERVICE!r}, username {KEYRING_USER!r}) "
            f"or set {ENV_ENVELOPE_KEY} to the key this state was sealed under."
        )
    for key in keys:
        with contextlib.suppress(Exception):
            plaintext = _aesgcm(key).decrypt(nonce, ciphertext, purpose.encode("utf-8"))
            return json.loads(plaintext.decode("utf-8"))
    raise VaultOpenError(
        "sealed blob would not open under any available key — the envelope key "
        "was rotated or lost, or the blob is corrupt. Restore the original key, "
        "or discard and re-stage this state."
    )


def seal_secrets(tree: Any, purpose: str) -> Any:
    """A copy of ``tree`` with every secret-named value sealed in place.

    The tree stays inspectable — every non-secret field is plain JSON — which
    is what lets a candidate file remain diffable and debuggable while the
    values worth stealing are not in it.
    """
    if isinstance(tree, dict):
        return {
            key: seal(val, purpose)
            if redaction.looks_secret(key) and not is_sealed(val)
            else seal_secrets(val, purpose)
            for key, val in tree.items()
        }
    if isinstance(tree, list):
        return [seal_secrets(item, purpose) for item in tree]
    return tree


def unseal_secrets(tree: Any, purpose: str) -> Any:
    """The inverse of :func:`seal_secrets`: every sealed blob opened in place."""
    if is_sealed(tree):
        return unseal(tree, purpose)
    if isinstance(tree, dict):
        return {key: unseal_secrets(val, purpose) for key, val in tree.items()}
    if isinstance(tree, list):
        return [unseal_secrets(item, purpose) for item in tree]
    return tree


# -- key rotation -------------------------------------------------------------


@dataclasses.dataclass
class RotationReport:
    """What a rotation touched — the operator's receipt."""

    candidate_files: int = 0
    journal_files: int = 0
    blobs: int = 0

    def summary(self) -> str:
        return (
            f"re-sealed {self.blobs} value(s) across {self.candidate_files} "
            f"candidate store(s) and {self.journal_files} journal file(s)"
        )


def _reseal_tree(tree: Any, count: list[int]) -> Any:
    """Every sealed blob in ``tree`` re-sealed under the current key.

    Purpose comes from the blob itself — rotation re-keys material, it never
    re-contextualizes it.
    """
    if is_sealed(tree):
        count[0] += 1
        purpose = str(tree.get("purpose", ""))
        return seal(unseal(tree, purpose), purpose)
    if isinstance(tree, dict):
        return {key: _reseal_tree(val, count) for key, val in tree.items()}
    if isinstance(tree, list):
        return [_reseal_tree(item, count) for item in tree]
    return tree


def _atomic_rewrite(path: Any, text: str) -> None:
    import tempfile
    from pathlib import Path

    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def rotate_key() -> RotationReport:
    """Retire the keyring envelope key and re-seal everything under a new one.

    Crash-safe by ordering, not by luck: the outgoing key moves to the
    ``envelope-key-previous`` slot *before* the new key replaces it, and is
    deleted only after every sealed blob on disk has been rewritten. At every
    intermediate point :func:`load_keys` returns both keys and every blob
    still opens — an interrupted rotation is re-run, never recovered from.

    Refused when ``ECSDWAN_ENVELOPE_KEY`` is set: an operator who exported
    the key owns its lifecycle, and rotating the keyring entry underneath
    them would strand every blob sealed under the environment key.

    Run it quiesced. Candidate stores are rewritten under their per-origin
    lock, but a commit in flight is writing journal snapshots this walk
    could miss.
    """
    from pyecsdwan import config
    from pyecsdwan.locking import HostLock

    if os.environ.get(ENV_ENVELOPE_KEY):
        raise VaultUnavailable(
            f"{ENV_ENVELOPE_KEY} is set, so key management belongs to whoever "
            f"set it; rotate by re-sealing under a new exported key, not here"
        )
    ring = _keyring()
    current = _keyring_get(KEYRING_USER)
    if current is None:
        raise VaultUnavailable(
            "no envelope key is stored, so there is nothing to rotate — a key "
            "is created the first time secret-bearing state is saved"
        )
    if _keyring_get(KEYRING_USER_PREVIOUS) is None:
        # A leftover -previous entry means an interrupted rotation: keep it,
        # finish the re-seal, and the delete below retires it.
        ring.set_password(KEYRING_SERVICE, KEYRING_USER_PREVIOUS, current)
        fresh = base64.b64encode(_secrets.token_bytes(KEY_BYTES)).decode("ascii")
        ring.set_password(KEYRING_SERVICE, KEYRING_USER, fresh)

    report = RotationReport()
    candidate_root = config.candidate_root()
    if candidate_root.exists():
        for path in sorted(candidate_root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue  # quarantine/corruption is the candidate loader's problem
            origin = data.get("origin") if isinstance(data, dict) else None
            count = [0]
            resealed = _reseal_tree(data, count)
            if count[0] == 0:
                continue
            lock = HostLock(str(origin), "candidate") if origin else None
            if lock is not None:
                with lock:
                    _atomic_rewrite(path, json.dumps(resealed, sort_keys=True))
            else:
                _atomic_rewrite(path, json.dumps(resealed, sort_keys=True))
            report.candidate_files += 1
            report.blobs += count[0]
    journal_root = config.journal_root()
    if journal_root.exists():
        for path in sorted(journal_root.glob("*/private.jsonl")):
            lines_out: list[str] = []
            count = [0]
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    lines_out.append(line)  # a torn tail is the journal's to repair
                    continue
                lines_out.append(
                    json.dumps(_reseal_tree(record, count), sort_keys=True, default=str)
                )
            if count[0] == 0:
                continue
            _atomic_rewrite(path, "".join(f"{ln}\n" for ln in lines_out))
            report.journal_files += 1
            report.blobs += count[0]

    try:
        ring.delete_password(KEYRING_SERVICE, KEYRING_USER_PREVIOUS)
    except Exception:  # noqa: BLE001, S110 - the entry may already be gone; both keys still work
        pass
    return report
