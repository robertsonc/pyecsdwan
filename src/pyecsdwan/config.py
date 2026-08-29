"""Settings, paths, and credential sourcing.

Credentials never live in argv or in files under the repo. Sources, in
precedence order:

1. ``ECSDWAN_ORCH_URL`` / ``ECSDWAN_API_KEY`` environment variables
2. OS keyring (service ``pyecsdwan``, username = orchestrator host)
3. Interactive session login (user/password prompt) — ``client.OrchClient.login``

State lives under ``~/.pyecsdwan/`` (override root with ``ECSDWAN_HOME`` for
tests): ``journal/`` transaction journals, ``candidate/`` candidate
changesets, ``cache/`` resolver caches, ``locks/`` host-scoped advisory
locks.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

ENV_ORCH_URL = "ECSDWAN_ORCH_URL"
ENV_API_KEY = "ECSDWAN_API_KEY"
ENV_HOME = "ECSDWAN_HOME"
ENV_INSECURE = "ECSDWAN_INSECURE"
KEYRING_SERVICE = "pyecsdwan"


def state_root() -> Path:
    root = os.environ.get(ENV_HOME)
    if root:
        return Path(root)
    return Path.home() / ".pyecsdwan"


def journal_root() -> Path:
    return state_root() / "journal"


def candidate_root() -> Path:
    return state_root() / "candidate"


def cache_root() -> Path:
    return state_root() / "cache"


def lock_root() -> Path:
    return state_root() / "locks"


def ensure_dirs() -> None:
    # chmod after mkdir: mkdir(mode=) only applies to the leaf and is subject
    # to umask, and a pre-existing dir (backup restore, older layout) keeps
    # its old mode — so re-assert 0o700 on every run. These trees hold config
    # snapshots that can embed secrets.
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for d in (journal_root(), candidate_root(), cache_root(), lock_root()):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)


#: What a redacted credential shows as. Present rather than absent, because
#: "a key is configured" and "no key is configured" are different answers and
#: an operator debugging auth needs to tell them apart.
REDACTED = "<redacted>"

#: Fields of :class:`Settings` never shown in its repr. Named here rather than
#: inline so a test can assert the rule covers every credential-looking field,
#: instead of re-listing them and drifting.
SECRET_FIELDS = frozenset({"api_key"})


@dataclasses.dataclass(repr=False)
class Settings:
    orch_url: str
    api_key: str | None = None
    verify_tls: bool = True
    connect_timeout: float = 9.0
    read_timeout: float = 30.0
    #: Bounded retries on 5xx / connection errors only. Non-idempotent
    #: writes are never blindly retried (see client.py).
    max_retries: int = 3
    #: Async job (action key) polling.
    job_timeout: float = 600.0
    job_poll_initial: float = 1.0
    job_poll_max: float = 15.0
    #: Journal history depth for `rollback <n>` (Junos keeps 50; we default 10).
    rollback_history: int = 10
    #: Why the keyring lookup failed, if it did. Not a secret, and the
    #: difference between "no key stored" and "the keyring would not open"
    #: is the whole reason it is carried: see :func:`_keyring_api_key`.
    keyring_error: str | None = None

    @property
    def host(self) -> str:
        return self.orch_url.replace("https://", "").replace("http://", "").split("/")[0]

    def __repr__(self) -> str:
        """Every field except the credential, which is shown as present or absent.

        The generated dataclass repr printed the API key in full. Nothing in
        the tree logged a whole ``Settings`` today, but that is a property of
        the current call sites rather than of the type: one ``log.debug(...,
        settings=settings)``, one traceback rendered with locals, and the key
        is in a log file. Epic #9's definition of done says no secret is
        written or emitted unredacted, so the type refuses to emit it rather
        than every caller having to remember.
        """
        parts = []
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if field.name in SECRET_FIELDS and value is not None:
                value = REDACTED
            parts.append(f"{field.name}={value!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


def settings_from_env(orch_url: str | None = None, insecure: bool | None = None) -> Settings:
    """Build Settings from environment; explicit args (CLI flags) win."""
    url = orch_url or os.environ.get(ENV_ORCH_URL, "")
    if not url:
        raise RuntimeError(
            f"No Orchestrator URL configured. Set {ENV_ORCH_URL} or pass --orch-url."
        )
    api_key = os.environ.get(ENV_API_KEY) or None
    keyring_error = None
    if api_key is None:
        api_key, keyring_error = _keyring_api_key(url)
    verify = not (
        insecure if insecure is not None else os.environ.get(ENV_INSECURE, "") == "1"
    )
    return Settings(
        orch_url=url, api_key=api_key, verify_tls=verify, keyring_error=keyring_error
    )


def _keyring_api_key(url: str) -> tuple[str | None, str | None]:
    """Look up the stored key, returning ``(key, error)``.

    Both halves are None when the keyring simply holds no key for this host —
    an ordinary state, not a problem, and the caller falls through to session
    login. The error is the point of the tuple: a keyring that is *installed
    but would not open* (locked session, no D-Bus, a backend that raises)
    used to be indistinguishable from one holding nothing, so an operator who
    had stored a key was told they had not. Absence of evidence, read as
    evidence of absence.

    Still never fatal. A broken keyring must not take down a CLI that can
    still authenticate another way; it only has to stop lying about why.
    """
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        import keyring
    except ImportError:
        # Not installed at all. That is a deployment choice, not a fault, and
        # saying so on every invocation would be noise.
        return None, None
    try:
        return keyring.get_password(KEYRING_SERVICE, host), None
    except Exception as exc:  # noqa: BLE001 - backends fail in exotic ways; none of them are ours to enumerate
        return None, f"{type(exc).__name__}: {exc}"
