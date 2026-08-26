"""Settings, paths, and credential sourcing.

Credentials never live in argv or in files under the repo. Sources, in
precedence order:

1. ``ECSDWAN_ORCH_URL`` / ``ECSDWAN_API_KEY`` environment variables
2. OS keyring (service ``pyecsdwan``, username = orchestrator host)
3. Interactive session login (user/password prompt) — handled by auth.py

State lives under ``~/.pyecsdwan/`` (override root with ``ECSDWAN_HOME`` for
tests): ``journal/`` transaction journals, ``candidate/`` candidate
changesets, ``cache/`` resolver caches.
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


def ensure_dirs() -> None:
    # chmod after mkdir: mkdir(mode=) only applies to the leaf and is subject
    # to umask, and a pre-existing dir (backup restore, older layout) keeps
    # its old mode — so re-assert 0o700 on every run. These trees hold config
    # snapshots that can embed secrets.
    root = state_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    for d in (journal_root(), candidate_root(), cache_root()):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o700)


@dataclasses.dataclass
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

    @property
    def host(self) -> str:
        return self.orch_url.replace("https://", "").replace("http://", "").split("/")[0]


def settings_from_env(orch_url: str | None = None, insecure: bool | None = None) -> Settings:
    """Build Settings from environment; explicit args (CLI flags) win."""
    url = orch_url or os.environ.get(ENV_ORCH_URL, "")
    if not url:
        raise RuntimeError(
            f"No Orchestrator URL configured. Set {ENV_ORCH_URL} or pass --orch-url."
        )
    api_key = os.environ.get(ENV_API_KEY) or None
    if api_key is None:
        api_key = _keyring_api_key(url)
    verify = not (
        insecure if insecure is not None else os.environ.get(ENV_INSECURE, "") == "1"
    )
    return Settings(orch_url=url, api_key=api_key, verify_tls=verify)


def _keyring_api_key(url: str) -> str | None:
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, host)
    except Exception:  # noqa: BLE001 - keyring backends fail in exotic ways; absence of a stored key is never fatal
        return None
