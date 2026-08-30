"""Envelope encryption for rollback-private state (#106).

The properties that matter are the fail-closed ones. Encryption working is
table stakes; what this module *promises* is that secret-bearing state is
sealed or not persisted, that a blob opens only in the context it was sealed
for, and that a lost or rotated-away key produces a loud refusal rather than
a quiet empty read.
"""

from __future__ import annotations

import base64
import sys
from typing import Any

import pytest

from pyecsdwan import vault

SECRET = {"password": "s3ntinel-vault", "asn": 65000}


class FakeKeyring:
    """An in-memory keyring backend, installed via sys.modules.

    Real enough for the vault's contract — get/set/delete by (service, user)
    — and hermetic, because CI has no Secret Service and a test that touched
    a real keyring would leave state behind on a developer box.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, value: str) -> None:
        self.store[(service, user)] = value

    def delete_password(self, service: str, user: str) -> None:
        self.store.pop((service, user), None)


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    ring = FakeKeyring()
    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", ring)
    return ring


# -- round trip ---------------------------------------------------------------


def test_seal_unseal_round_trips() -> None:
    blob = vault.seal(SECRET, purpose="candidate-intent")
    assert vault.is_sealed(blob)
    assert "s3ntinel-vault" not in str(blob)
    assert vault.unseal(blob, purpose="candidate-intent") == SECRET


def test_purpose_is_bound_in_not_labelled_on() -> None:
    """The purpose is GCM associated data, not a courtesy field: a snapshot
    blob pasted into a candidate file must refuse to open as intent even for
    an attacker who edits the label to match."""
    blob = vault.seal(SECRET, purpose="candidate-intent")
    with pytest.raises(vault.VaultOpenError, match="candidate-intent"):
        vault.unseal(blob, purpose="journal-snapshot")
    relabelled = dict(blob, purpose="journal-snapshot")
    with pytest.raises(vault.VaultOpenError, match="would not open"):
        vault.unseal(relabelled, purpose="journal-snapshot")


def test_a_tampered_blob_refuses_rather_than_returning_garbage() -> None:
    blob = vault.seal(SECRET, purpose="p")
    raw = bytearray(base64.b64decode(blob["data"]))
    raw[-1] ^= 0xFF
    blob["data"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(vault.VaultOpenError):
        vault.unseal(blob, purpose="p")


def test_a_future_blob_version_is_refused_not_guessed() -> None:
    blob = vault.seal(SECRET, purpose="p")
    blob[vault.SEALED_FIELD] = vault.SEALED_VERSION + 1
    with pytest.raises(vault.VaultOpenError, match="version"):
        vault.unseal(blob, purpose="p")


# -- key sourcing -------------------------------------------------------------


def test_a_malformed_env_key_is_a_loud_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(vault.ENV_ENVELOPE_KEY, "not-base64!!")
    with pytest.raises(vault.VaultUnavailable, match="base64"):
        vault.seal(SECRET, purpose="p")
    monkeypatch.setenv(vault.ENV_ENVELOPE_KEY, base64.b64encode(b"short").decode())
    with pytest.raises(vault.VaultUnavailable, match="32"):
        vault.seal(SECRET, purpose="p")


def test_no_key_source_at_all_fails_closed_with_both_remedies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The message must carry its own fix: which keyring entry, or which
    variable — an operator hitting this on a headless box gets one shot at
    reading it before they start exporting things to make it go away."""
    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)

    class Broken:
        @staticmethod
        def get_password(service: str, user: str) -> str:
            raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", Broken)
    with pytest.raises(vault.VaultUnavailable) as exc:
        vault.seal(SECRET, purpose="p")
    message = str(exc.value)
    assert vault.ENV_ENVELOPE_KEY in message
    assert vault.KEYRING_SERVICE in message


def test_first_seal_creates_a_keyring_key_and_later_seals_reuse_it(
    fake_keyring: FakeKeyring,
) -> None:
    blob = vault.seal(SECRET, purpose="p")
    stored = fake_keyring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)]
    assert len(base64.b64decode(stored)) == vault.KEY_BYTES
    # A second seal must not mint a second key, or the first blob dies.
    vault.seal({"password": "other"}, purpose="p")
    assert fake_keyring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)] == stored
    assert vault.unseal(blob, purpose="p") == SECRET


def test_unseal_never_creates_a_key(fake_keyring: FakeKeyring) -> None:
    """A read path that minted a fresh key would turn "key lost" into
    "key lost, plus a decoy that opens nothing"."""
    data = base64.b64encode(b"x" * 40).decode()  # long enough to pass the shape checks
    blob_from_elsewhere = {vault.SEALED_FIELD: 1, "purpose": "p", "data": data}
    with pytest.raises(vault.VaultOpenError, match="no envelope key"):
        vault.unseal(blob_from_elsewhere, purpose="p")
    assert fake_keyring.store == {}


# -- tree sealing -------------------------------------------------------------


def test_seal_secrets_touches_only_secret_named_values() -> None:
    tree = {
        "asn": 65000,
        "neighbors": [{"ip": "10.0.0.1", "password": "s3ntinel-tree"}],
    }
    sealed = vault.seal_secrets(tree, "p")
    assert sealed["asn"] == 65000
    assert sealed["neighbors"][0]["ip"] == "10.0.0.1"
    assert vault.is_sealed(sealed["neighbors"][0]["password"])
    assert "s3ntinel-tree" not in str(sealed)
    assert vault.unseal_secrets(sealed, "p") == tree


def test_a_tree_without_secrets_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole cost model: a box with no keyring pays nothing until it
    stages something the detector recognises."""
    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)

    class Broken:
        @staticmethod
        def get_password(service: str, user: str) -> str:
            raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", Broken)
    tree = {"asn": 65000, "neighbors": [{"ip": "10.0.0.1"}]}
    assert vault.seal_secrets(tree, "p") == tree
    assert vault.unseal_secrets(tree, "p") == tree


# -- rotation ----------------------------------------------------------------


def test_rotation_is_refused_under_an_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(vault.ENV_ENVELOPE_KEY, base64.b64encode(b"k" * 32).decode())
    with pytest.raises(vault.VaultUnavailable, match=vault.ENV_ENVELOPE_KEY):
        vault.rotate_key()


def test_rotation_with_no_stored_key_says_there_is_nothing_to_rotate(
    fake_keyring: FakeKeyring,
) -> None:
    with pytest.raises(vault.VaultUnavailable, match="nothing to rotate"):
        vault.rotate_key()


def test_rotation_reseals_and_retires_the_old_key(
    fake_keyring: FakeKeyring, state_home: Any
) -> None:
    """The receipt-level rotation properties: a new key, the old one gone, and
    every previously sealed blob opening under the new one. The file-walking
    half is exercised end to end in test_secret_state.py."""
    blob = vault.seal(SECRET, purpose="candidate-intent")
    key_before = fake_keyring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)]

    report = vault.rotate_key()

    key_after = fake_keyring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)]
    assert key_after != key_before
    assert (vault.KEYRING_SERVICE, vault.KEYRING_USER_PREVIOUS) not in fake_keyring.store
    assert report.blobs == 0  # nothing on disk in this test
    # The old blob is now orphaned by design: rotation re-seals what is *on
    # disk*, and this one never was. It no longer opens — which is exactly
    # why rotate_key walks the state directory rather than trusting callers.
    with pytest.raises(vault.VaultOpenError):
        vault.unseal(blob, purpose="candidate-intent")
