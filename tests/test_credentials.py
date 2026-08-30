"""Credential handling: nothing emits the key, and a broken keyring says so.

Epic #9's definition of done contains one clause about secrets — "No secret is
written or emitted unredacted" — and one about credentials — OS keyring
integration. Both were half true.

`client._scrub` already removed the key from error text, which shows the
project was thinking about the right hazard. What it missed is that
`Settings` is a dataclass, so its *generated* repr printed the key in full.
Nothing logged a whole `Settings` today, but that is a fact about the current
call sites, not about the type: one `log.debug(..., settings=settings)` or one
traceback rendered with locals puts the key in a file. A type that refuses to
emit its secret is a property; every caller remembering is a hope.

The keyring half is the same shape as the rest of this project's recurring
bug. A keyring that is installed but will not open — locked session, no D-Bus
— returned None exactly like a keyring holding nothing, so an operator who had
stored a key was told they had not, and went looking in the wrong place.
"""

from __future__ import annotations

import dataclasses
from typing import Any, ClassVar

import pytest

from pyecsdwan import config

SECRET = "SUPER-SECRET-KEY-8f3a2b"


def _settings(**kw: Any) -> config.Settings:
    return config.Settings(orch_url="https://orch.example.com", **kw)


# -- the key is never emitted -------------------------------------------------


def test_repr_does_not_contain_the_key() -> None:
    settings = _settings(api_key=SECRET)
    assert SECRET not in repr(settings)
    assert SECRET not in str(settings)
    assert SECRET not in f"{settings}"


def test_repr_still_says_whether_a_key_is_configured() -> None:
    """Redaction, not deletion — the same rule the journal export follows.

    "A key is configured" and "no key is configured" are different answers, and
    an operator debugging auth needs to tell them apart. Dropping the field
    entirely would have passed the test above while making this one impossible.
    """
    assert config.SECRET_FIELDS == {"api_key"}, "update this test with the new field"
    assert config.REDACTED in repr(_settings(api_key=SECRET))
    assert "api_key=None" in repr(_settings())


def test_the_key_is_still_readable_by_the_code_that_needs_it() -> None:
    """Guards the guard: a repr fix that also broke the attribute would leave
    every test above green and every request unauthenticated."""
    assert _settings(api_key=SECRET).api_key == SECRET


def test_no_field_of_settings_leaks_through_the_generated_repr() -> None:
    """Stated over the fields rather than the one field, so a credential added
    later is caught by this test instead of by an incident.

    A future `password` or `refresh_token` field would be printed in full by
    the same mechanism that printed `api_key`, and nothing else in the suite
    would notice.
    """
    secretish = {"key", "secret", "password", "token", "credential"}
    # Split on `_` rather than matching substrings: `keyring_error` contains
    # "key" and is not a credential, and a check that cried wolf about it would
    # be silenced rather than fixed.
    unexpected = {
        f.name
        for f in dataclasses.fields(config.Settings)
        if secretish & set(f.name.split("_")) and f.name not in config.SECRET_FIELDS
    }
    assert not unexpected, (
        f"new credential-looking field(s) {unexpected} — add them to "
        f"config.SECRET_FIELDS before this ships"
    )


# -- a keyring that will not open is not a keyring that is empty --------------


def test_an_empty_keyring_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)
    monkeypatch.setattr(config, "_keyring_api_key", lambda url: (None, None))

    settings = config.settings_from_env("https://orch.example.com")

    assert settings.api_key is None
    assert settings.keyring_error is None


def test_a_broken_keyring_is_reported_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: both states used to arrive as a bare None."""
    monkeypatch.delenv(config.ENV_API_KEY, raising=False)

    class Boom:
        @staticmethod
        def get_password(service: str, username: str) -> str:
            raise RuntimeError("no D-Bus session bus")

    monkeypatch.setitem(__import__("sys").modules, "keyring", Boom)

    key, error = config._keyring_api_key("https://orch.example.com")

    assert key is None
    assert error is not None
    assert "no D-Bus session bus" in error


def _keyring_holding(entries: dict[str, str]) -> Any:
    class Stored:
        asked: ClassVar[list[str]] = []

        @staticmethod
        def get_password(service: str, username: str) -> str | None:
            assert service == config.KEYRING_SERVICE
            Stored.asked.append(username)
            return entries.get(username)

    return Stored


def test_a_stored_key_is_returned_with_no_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the guard: a lookup wired to always fail would satisfy the test
    above and quietly break every keyring user."""
    stored = _keyring_holding({"https://orch.example.com": SECRET})
    monkeypatch.setitem(__import__("sys").modules, "keyring", stored)

    assert config._keyring_api_key("https://orch.example.com") == (SECRET, None)
    # Asked for the canonical origin, not the display host (#63).
    assert stored.asked == ["https://orch.example.com"]


def test_two_tenants_can_hold_separate_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The display host made these one entry, so a key for one tenant
    authenticated against the other (#63)."""
    stored = _keyring_holding(
        {
            "https://orch.example.com/tenant-a": "key-a",
            "https://orch.example.com/tenant-b": "key-b",
        }
    )
    monkeypatch.setitem(__import__("sys").modules, "keyring", stored)

    assert config._keyring_api_key("https://orch.example.com/tenant-a")[0] == "key-a"
    assert config._keyring_api_key("https://orch.example.com/tenant-b")[0] == "key-b"


def test_a_key_stored_by_an_older_build_is_still_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older builds keyed the entry by the display host. Reading only the new
    key would tell an operator who *has* stored a key that they have not —
    which is the failure this whole function exists to avoid."""
    stored = _keyring_holding({"orch.example.com": SECRET})
    monkeypatch.setitem(__import__("sys").modules, "keyring", stored)

    assert config._keyring_api_key("https://orch.example.com") == (SECRET, None)
    assert stored.asked == ["https://orch.example.com", "orch.example.com"]


def test_the_origin_key_wins_over_the_legacy_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the fallback would quietly undo the fix for anyone who has
    both, which is everyone mid-migration."""
    stored = _keyring_holding(
        {"https://orch.example.com": "new", "orch.example.com": "old"}
    )
    monkeypatch.setitem(__import__("sys").modules, "keyring", stored)

    assert config._keyring_api_key("https://orch.example.com")[0] == "new"


def test_a_missing_keyring_package_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not installed is a deployment choice, not a fault. Reporting it on every
    invocation would train operators to ignore the line that matters."""
    import builtins

    real_import = builtins.__import__

    def no_keyring(name: str, *args: Any, **kw: Any) -> Any:
        if name == "keyring":
            raise ImportError("no module named keyring")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", no_keyring)

    assert config._keyring_api_key("https://orch.example.com") == (None, None)


def test_the_commit_confirm_refusal_names_a_broken_keyring() -> None:
    """Where the distinction actually reaches a human.

    `commit --confirm-minutes` refuses without an API key, because the detached
    watchdog cannot replay a login session. Told only that, an operator who did
    store a key concludes the store failed and re-stores it — into the keyring
    that is not opening.
    """
    from pyecsdwan import txn

    settings = _settings(keyring_error="RuntimeError: no D-Bus session bus")
    with pytest.raises(txn.CommitError) as caught:
        txn._guard_confirm_auth(settings, confirm_minutes=10)

    assert "no D-Bus session bus" in str(caught.value)


def test_the_refusal_stays_short_when_the_keyring_was_fine() -> None:
    from pyecsdwan import txn

    with pytest.raises(txn.CommitError) as caught:
        txn._guard_confirm_auth(_settings(), confirm_minutes=10)

    assert "keyring" not in str(caught.value).lower()
