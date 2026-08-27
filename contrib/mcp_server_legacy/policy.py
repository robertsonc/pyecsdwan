"""Trust-boundary policy for the quarantined legacy MCP server (issue #62).

Deliberately free of any ``mcp`` or ``pyedgeconnect`` import. The policy is
the security-relevant part of this component, so it has to be testable in the
normal test run — which means it cannot depend on packages the product does
not install. ``server.py`` imports this; nothing here imports ``server``.

What this file encodes
----------------------

The legacy server reflectively exposed every public method of the vendored
``pyedgeconnect`` SDK as an MCP tool: 641 methods on ``Orchestrator`` alone,
of which roughly 250 write or destroy. None of them went through
``pyecsdwan``'s candidate/plan/journal/ownership path, TLS verification was
off by default, and credentials were accepted as tool arguments.

That is a second product surface with materially weaker guarantees than the
CLI, living in the same repository. The rules below are what it takes to make
that surface fail closed:

1. **Disabled unless explicitly enabled.** No env var, no server.
2. **No direct-to-appliance access, at all.** Not a flag — the tools are gone.
   #10 defers direct appliance access until an RBAC broker exists, because the
   appliance's own REST API has no RBAC: anyone with the credential owns the
   box. A flag would be a way to turn that back on.
3. **TLS verification on by default**, with insecure transport a separate,
   explicit, lab-only opt-in.
4. **Credentials are never tool arguments.** They come from the environment or
   the keyring, the same sources ``pyecsdwan.config`` already uses. An argument
   is visible to the model, to the transcript, and to anything logging the
   call.
5. **Reads by default; writes need their own opt-in** and are labelled Tier 0
   (audit-journaled at best, never rolled back) so nobody mistakes them for a
   transaction.

Why classification is not a prefix match
----------------------------------------

The obvious rule — ``get_*`` is a read — is wrong on this SDK. 53 ``get_*``
methods issue a POST, and this repository has already found endpoints that
mutate behind a read-shaped verb: ``GET /oro/debug/closeGrpcConnection``
closes a live gRPC link (see ``reports/applianceconfig.py`` and issue #67).

So a method counts as a read only when its *name* looks like a read **and**
its body issues nothing but GETs. Everything else — including anything we
cannot see the verbs for — is treated as a write. Guessing wrong in the
read direction hands an LLM a destructive tool; guessing wrong in the write
direction costs an operator one environment variable.
"""

from __future__ import annotations

import enum
import os
import re
from collections.abc import Mapping

#: Turn the server on at all. Absent -> refuses to start.
ENABLE_ENV = "ECSDWAN_MCP_LEGACY_ENABLE"
#: Additionally expose write/destructive tools. Absent -> read-only surface.
ALLOW_WRITES_ENV = "ECSDWAN_MCP_LEGACY_ALLOW_WRITES"
#: Lab-only: disable TLS verification. Absent -> verification on.
INSECURE_ENV = "ECSDWAN_MCP_LEGACY_INSECURE"

#: Credentials, read from the environment — never from a tool argument.
URL_ENV = "ECSDWAN_ORCH_URL"
API_KEY_ENV = "ECSDWAN_API_KEY"

_TRUE = frozenset({"1", "true", "yes", "on"})

#: Name shapes that *may* be reads, if their verbs agree.
_READ_NAME = re.compile(r"^(get|list|find|show|search|fetch|read|is|has)_")
#: Name shapes that are destructive regardless of verb.
_DESTRUCTIVE_NAME = re.compile(
    r"^(delete|remove|destroy|purge|erase|reset|reboot|shutdown|deauthorize|"
    r"decommission|clear|wipe|revoke)_"
)
#: The only HTTP verb a read is allowed to issue.
_READ_VERBS = frozenset({"get"})


class Disabled(RuntimeError):
    """The legacy server is not enabled; message explains how and why."""


class OpClass(enum.Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in _TRUE


def enabled(env: Mapping[str, str] | None = None) -> bool:
    return _flag(os.environ if env is None else env, ENABLE_ENV)


def writes_allowed(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    # Writes are meaningless if the server itself is off; requiring both keeps
    # a stray ALLOW_WRITES in a shell profile from being half a decision.
    return _flag(env, ENABLE_ENV) and _flag(env, ALLOW_WRITES_ENV)


def verify_tls(env: Mapping[str, str] | None = None) -> bool:
    """TLS verification, on unless explicitly disabled for a lab."""
    return not _flag(os.environ if env is None else env, INSECURE_ENV)


def appliance_tools_enabled() -> bool:
    """Direct-to-appliance tools. Always False, by construction.

    Not a setting. #10 defers direct appliance access until an RBAC broker
    exists, and the appliance REST API has no RBAC of its own — a credential
    is total control of the box. A configurable version of this would be a
    way to switch that back on, so there isn't one.
    """
    return False


def check_enabled(env: Mapping[str, str] | None = None) -> None:
    """Raise unless the operator has explicitly turned this on."""
    if enabled(env):
        return
    raise Disabled(
        "The legacy MCP server is quarantined and disabled (issue #62).\n"
        "\n"
        "It reflectively exposed every public method of the vendored "
        "pyedgeconnect SDK — including ~250 write and destructive operations — "
        "with no transaction, journal, rollback, or ownership check, and with "
        "TLS verification off by default. It is not a front end over this "
        "product: it wraps the vendored reference SDK, not pyecsdwan.\n"
        "\n"
        f"To run it anyway, in a lab, set {ENABLE_ENV}=1. It will expose "
        "read-only Orchestrator tools; direct-to-appliance tools are removed "
        f"entirely (#10), and write tools additionally require "
        f"{ALLOW_WRITES_ENV}=1 and are Tier 0 — audit-journaled at best, never "
        "rolled back.\n"
        "\n"
        "For anything you would trust, use ec-cli: it plans, journals, "
        "verifies ownership, and can roll back."
    )


def classify(name: str, http_verbs: frozenset[str] | None) -> OpClass:
    """Classify one SDK method, failing closed.

    ``http_verbs`` is the set of HTTP verbs the method's body actually issues
    (lowercase), or ``None`` when that could not be determined. ``None`` is
    not "probably fine": an unreadable method is classified as a write.
    """
    if _DESTRUCTIVE_NAME.match(name):
        return OpClass.DESTRUCTIVE
    if http_verbs is not None and "delete" in http_verbs:
        return OpClass.DESTRUCTIVE
    if http_verbs and http_verbs <= _READ_VERBS and _READ_NAME.match(name):
        return OpClass.READ
    return OpClass.WRITE


def is_exposed(op: OpClass, env: Mapping[str, str] | None = None) -> bool:
    """Whether a tool of this class should be registered at all."""
    if op is OpClass.READ:
        return enabled(env)
    return writes_allowed(env)


def orchestrator_credentials(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """``(url, api_key)`` from the environment or the keyring.

    Never from a tool argument. A credential passed as an argument is visible
    to the model, lands in the conversation transcript, and is echoed by
    anything that logs tool calls — three places it should never be.
    """
    env = os.environ if env is None else env
    url = env.get(URL_ENV, "").strip()
    if not url:
        raise Disabled(
            f"No Orchestrator URL configured. Set {URL_ENV}. "
            f"Credentials are never accepted as MCP tool arguments (#62)."
        )
    api_key = env.get(API_KEY_ENV, "").strip() or None
    if api_key is None:
        api_key = _keyring_api_key(url)
    return url, api_key


def _keyring_api_key(url: str) -> str | None:
    host = url.replace("https://", "").replace("http://", "").split("/")[0]
    try:
        import keyring

        value = keyring.get_password("pyecsdwan", host)
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - keyring backends fail in exotic ways
        return None


def redact(text: str, secrets: object) -> str:
    """Blank out any known secret that made it into an outbound string.

    A backstop, not a design. The design is that secrets never enter a tool
    argument or a response in the first place; this catches the case where a
    response echoes back a key the SDK sent.
    """
    if not text:
        return text
    values = secrets if isinstance(secrets, (list, tuple, set)) else [secrets]
    for value in values:
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, "***redacted***")
    return text


def tier0_notice(op: OpClass) -> str:
    """The warning that rides along with every exposed write tool."""
    if op is OpClass.READ:
        return ""
    return (
        f"\n\nWARNING — Tier 0 {op.value} operation. This bypasses the "
        "pyecsdwan transaction engine entirely: no plan, no snapshot, no "
        "post-apply verification, no rollback, and no template-ownership "
        "check. A template push can silently revert it. Prefer `ec-cli` for "
        "anything you would need to undo."
    )
