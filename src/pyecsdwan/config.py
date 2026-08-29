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
import hashlib
import os
import re
import urllib.parse
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
        """Short form, for display only.

        Lossy on purpose — it drops the scheme and everything after the first
        slash — which is why it must never key persisted state. Use
        :attr:`origin`.
        """
        return self.orch_url.replace("https://", "").replace("http://", "").split("/")[0]

    @property
    def origin(self) -> str:
        """Canonical identity of the Orchestrator this session targets.

        `host` collapsed distinct targets onto one key: two tenants under
        different paths, and a plaintext and a TLS endpoint on one name, all
        reduced to the same string — and then shared one candidate store, one
        lock, one journal and one rollback history (#63). A snapshot from one
        restored into another is the worst outcome this project has.
        """
        return canonical_origin(self.orch_url)

    @property
    def origin_digest(self) -> str:
        return origin_digest(self.origin)

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


#: Default ports, so `https://x` and `https://x:443` are one identity rather
#: than two. Anything else is kept explicitly.
DEFAULT_PORTS = {"https": 443, "http": 80}


def canonical_origin(url: str) -> str:
    """One stable identity per Orchestrator, as ``scheme://host[:port][/path]``.

    Everything that distinguishes two targets is kept — scheme, host, port and
    base path — and everything that does not is normalized away: case in the
    scheme and host, a default port written out or omitted, a trailing slash.
    So `HTTPS://Orch.Example.COM:443/` and `https://orch.example.com` are one
    identity, and `https://orch/tenant-a` and `https://orch/tenant-b` are two.
    """
    parsed = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(f"cannot derive an Orchestrator identity from {url!r}")
    port = parsed.port
    authority = host if port in (None, DEFAULT_PORTS.get(scheme)) else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return f"{scheme}://{authority}{path}"


def display_host(origin: str) -> str:
    """Short form of a canonical origin, for showing a human at a glance.

    Deliberately lossy and deliberately never an identity: two tenants under
    one hostname share a display host, which is the whole of #63. Derived
    rather than stored, so it can never disagree with the origin it came from.
    """
    return origin.split("://", 1)[-1].split("/")[0]


def origin_digest(origin: str) -> str:
    """Short, collision-free key for a canonical origin.

    File names used to be the identity run through a lossy sanitizer, so
    ``orch:443`` and ``orch_443`` produced the same file. The digest is taken
    over the *unsanitized* origin, so distinct targets cannot share a name
    however they are spelled.
    """
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()[:16]


def origin_slug(origin: str) -> str:
    """Filename stem: readable enough to recognise, keyed by the digest.

    The readable half is a convenience for anyone looking in the directory;
    the digest is what makes it unique, so a collision in the readable half
    costs nothing.
    """
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", origin)[-48:].strip("_") or "orch"
    return f"{readable}-{origin_digest(origin)}"


def as_origin(value: str) -> str:
    """Canonicalize whatever identity a caller passed, for keying state.

    Normalizing rather than rejecting a bare hostname is deliberate. The
    collision #63 is about is between two *distinct* targets sharing one key,
    and that is fixed at the source: production paths pass
    :attr:`Settings.origin`, which keeps the scheme and path that tell them
    apart. Rejecting the short form as well would buy no extra safety and
    would make every caller — including a fixture that only ever names one
    Orchestrator — carry a scheme it does not care about.

    What it does buy: `orch.example.com`, `https://orch.example.com` and
    `HTTPS://Orch.Example.COM:443/` are one key rather than three, so state
    does not fragment when a URL is written a different way.

    The rule that production must key state by `origin` and never by the
    lossy `host` is held by a test over the source
    (`tests/test_origin_identity.py`), which is where a convention belongs
    once it cannot be a type.
    """
    return canonical_origin(value)


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
    try:
        import keyring
    except ImportError:
        # Not installed at all. That is a deployment choice, not a fault, and
        # saying so on every invocation would be noise.
        return None, None
    # Keyed by the canonical origin, so two tenants under one hostname can hold
    # separate credentials (#63). The bare host is still read when the origin
    # holds nothing: entries stored by an older build are keyed that way, and
    # silently telling an operator they have no key is the failure this whole
    # function exists to avoid. Prompting them to re-store under the new key
    # belongs with the rest of the credential work (#106).
    origin = as_origin(url)
    try:
        for username in (origin, display_host(origin)):
            found = keyring.get_password(KEYRING_SERVICE, username)
            if found:
                return found, None
        return None, None
    except Exception as exc:  # noqa: BLE001 - backends fail in exotic ways; none of them are ours to enumerate
        return None, f"{type(exc).__name__}: {exc}"
