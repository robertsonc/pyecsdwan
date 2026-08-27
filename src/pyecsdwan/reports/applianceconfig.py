"""Appliance CLI running-config over the Orchestrator proxy (issue #56).

Every other report in epic #54 reads a structured endpoint. This one opens a
path to an appliance's *command interpreter*, so the interesting part of this
module is not the fetching — it is :func:`validate_command`, the deny-by-default
allowlist that stands between a caller and ``reload``.

Two channels, deliberately different:

* **Text, per appliance** — ``POST /cli`` through the proxy
  (``/appliance/rest?nePk=&url=cli``), body ``{"command": "<string>"}``
  (``specs/payload-examples-9.6.json``: ``appliance POST /cli``). Returns the
  command output, so this is the only channel that can render a running-config.
  Several appliances go through :func:`~pyecsdwan.reports.fanout.fan_out`:
  bounded, ordered, and one unreachable box degrades to a row rather than
  killing the report.
* **Broadcast, status only** — ``POST /broadcastCli``, body
  ``{"cmdList": [...], "nePks": [...]}``, whose response is a **bare JSON
  string** (the GUID), not an object; it is polled with the ordinary action-key
  machinery in :mod:`pyecsdwan.jobs`. This channel fans out server-side in one
  control-plane call, but it returns **no command output** — neither the
  vendored Orchestrator OpenAPI (``/broadcastCli`` -> ``text/plain`` string) nor
  the payload examples document any way to retrieve it, and
  ``docs/research/appliance-config.md`` records the same finding ("Text
  response, no per-appliance status from broadcastCli"). It is therefore
  exposed as an explicit opt-in for "run this read across these appliances and
  confirm it ran", never as the way to obtain config text. A TIMEOUT is a
  failure, never a success.

Read-only by construction: nothing here touches the candidate store, the
journal, or the transaction engine, and no code path calls ``saveChanges``.

Why no ``debug`` verb
---------------------
The brief for this issue asked for "the ``debug``-style read commands the
vendor tool permits". The vendored 9.6 payload examples show the appliance's
``debug`` namespace is *not* read-only — ``DELETE /debug/generic/{}`` ("Deletes
data of a generic module") and, worse, ``GET /oro/debug/closeGrpcConnection``
("Close ORO grpc link"), a read-shaped verb that mutates. On the ECOS CLI the
``debug`` verb likewise arms debug logging, which is appliance state and a
load hazard on a busy box. Deny-by-default says: without evidence of which
``debug`` commands are inert, none of them are allowlisted. The read-only
inspection forms operators actually want (``show debug ...``) already pass
under the ``show`` verb.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any, Final

import structlog

from pyecsdwan.contract import Ctx, JobOutcome
from pyecsdwan.jobs import extract_action_key, wait_for_action
from pyecsdwan.reports.fanout import DEFAULT_CONCURRENCY, Outcome, fan_out

log = structlog.get_logger(__name__)

#: ECOS path handed to the proxy as its ``url`` parameter. Relative, no leading
#: slash: the proxy appends it to ``rest/json/`` on the appliance, so a leading
#: slash resolves to ``rest/json//cli``.
CLI_ECOS_PATH: Final[str] = "cli"

BROADCAST_PATH: Final[str] = "/broadcastCli"

#: The one command this module's reports issue. A module constant, not a
#: format string: nothing user-supplied is ever concatenated into a command.
RUNNING_CONFIG_COMMAND: Final[str] = "show running-config"


# -- the allowlist -----------------------------------------------------------
#
# Deny by default, and stated as a *positive* character set plus a *positive*
# verb set. A blocklist of dangerous words is a list of the attacks its author
# thought of; this instead enumerates the small alphabet legitimate ECOS read
# commands need and refuses every codepoint outside it — which takes out `;`,
# `&&`, `|`, newlines, carriage returns, tabs, NUL, backticks, `$(`, quotes,
# backslashes, `?`, and every non-ASCII codepoint (so U+00A0 NBSP, U+2028 line
# separator and U+3000 ideographic space cannot pose as the space that
# separates a verb from its argument).
#
# The point is not what this regex accepts, it is what the *appliance* would do
# with the string. Anything ECOS might plausibly treat as a command boundary,
# a redirect, a substitution or an escape is outside the set — where there was
# doubt the character was left out, because refusing a legitimate read is a
# nuisance and permitting a second command is an outage.

#: Characters a legitimate read command needs: identifiers, and the punctuation
#: in ``running-config``, ``gigabit0/1``, ``10.0.0.0/8``, ``fe80::1``,
#: ``some_name``. Nothing else.
_SAFE_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_./:"
    " "
)

#: Read verbs. Matched with ``==`` against the first whitespace-free token —
#: never ``startswith``, which would wave through ``showtech-and-reload``.
#: ``show_running`` is likewise one token and is not ``show``.
ALLOWED_VERBS: Final[frozenset[str]] = frozenset({"show", "display"})

#: Long enough for any real read command, short enough that nothing novel can
#: be smuggled in by sheer length.
MAX_COMMAND_LENGTH: Final[int] = 200


class CommandRefused(ValueError):
    """A CLI command failed the read-only allowlist and was not sent.

    A ``ValueError`` so ``ec-cli``'s top-level handler renders it as a clean
    error rather than a traceback.
    """


def permitted_summary() -> str:
    """One line naming what the allowlist permits, for a refusal message."""
    verbs = ", ".join(sorted(ALLOWED_VERBS))
    return (
        f"only read-only commands are permitted: a single command beginning with "
        f"one of [{verbs}], built from letters, digits, spaces and '-_./:' only, "
        f"single-spaced, at most {MAX_COMMAND_LENGTH} characters"
    )


def _refuse(command: str, reason: str) -> CommandRefused:
    # The rejected command is echoed back with repr() so control characters
    # show as escapes rather than actually moving the terminal cursor.
    return CommandRefused(f"refused CLI command {command!r}: {reason}; {permitted_summary()}")


def validate_command(command: str) -> str:
    """Return *command* unchanged if the read-only allowlist accepts it.

    Raises :class:`CommandRefused` otherwise. Never normalizes: a command that
    needs stripping or de-duplicating of spaces to pass is refused, because
    silently rewriting an operator's input is how a validator and the thing it
    guards end up disagreeing about what was actually run.
    """
    if not isinstance(command, str):  # pragma: no cover - typing guard
        raise _refuse(str(command), "not a string")
    if not command:
        raise _refuse(command, "empty command")
    if len(command) > MAX_COMMAND_LENGTH:
        raise _refuse(command[:60], f"longer than {MAX_COMMAND_LENGTH} characters")
    bad = sorted({c for c in command if c not in _SAFE_CHARS})
    if bad:
        raise _refuse(command, f"contains disallowed character(s) {bad!r}")
    tokens = command.split(" ")
    if any(not token for token in tokens):
        raise _refuse(
            command, "leading, trailing or repeated spaces (tokens must be single-spaced)"
        )
    if tokens[0] not in ALLOWED_VERBS:
        raise _refuse(command, f"{tokens[0]!r} is not a permitted read verb")
    if len(tokens) < 2:
        raise _refuse(command, f"{tokens[0]!r} needs an argument (a bare verb is not a read)")
    return command


# -- results -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ApplianceConfig:
    """One appliance's CLI output, tied to the appliance it came from."""

    appliance: str
    ne_pk: str
    command: str
    text: str

    def as_json(self) -> dict[str, Any]:
        return {
            "appliance": self.appliance,
            "nePk": self.ne_pk,
            "command": self.command,
            "text": self.text,
        }


@dataclasses.dataclass(frozen=True)
class BroadcastResult:
    """Terminal outcome of one ``/broadcastCli`` dispatch.

    Carries no command output — see the module docstring. ``ok`` is true only
    for a job that reached SUCCESS; FAILED and TIMEOUT are both failures.
    """

    command: str
    #: (appliance name, nePk), in the order the caller named them.
    targets: tuple[tuple[str, str], ...]
    action_key: str
    outcome: JobOutcome

    @property
    def ok(self) -> bool:
        return self.outcome.state == "SUCCESS"

    def as_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "mode": "broadcast",
            "actionKey": self.action_key,
            "state": self.outcome.state,
            "detail": self.outcome.detail,
            "ok": self.ok,
            "appliances": [
                {
                    "appliance": name,
                    "nePk": ne_pk,
                    "status": self.outcome.per_appliance.get(ne_pk, ""),
                }
                for name, ne_pk in self.targets
            ],
        }


# -- fetching ----------------------------------------------------------------


def _as_text(response: Any) -> str:
    """Coerce whatever ``POST /cli`` answered into command output text.

    The appliance answers ``text/plain`` (the client hands back ``str`` when
    the body will not parse as JSON), but the vendored spec declares no
    response schema at all, so a JSON-wrapped variant on some release is
    plausible. Tolerated rather than trusted: unknown shapes stringify instead
    of raising, because a report must not die on a cosmetic difference.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for field in ("result", "output", "text", "data", "response"):
            value = response.get(field)
            if isinstance(value, str):
                return value
        return str(response)
    if isinstance(response, list):
        parts = []
        for entry in response:
            if isinstance(entry, dict):
                value = entry.get("result") or entry.get("output") or ""
                parts.append(str(value))
            else:
                parts.append(str(entry))
        return "\n".join(parts)
    return str(response)


def run_read_command(ctx: Ctx, appliance: str, command: str) -> ApplianceConfig:
    """Run one allowlisted read command on one appliance and return its output.

    *appliance* is a hostname (or a raw nePk); an unknown name raises
    :class:`~pyecsdwan.resolver.ResolveError` with a suggestion. *command* is
    validated before any request is built — a refused command never reaches
    the network.
    """
    validated = validate_command(command)
    ne_pk = ctx.resolver.ne_pk_for(appliance)
    response = ctx.client.appliance_request(
        "POST", ne_pk, CLI_ECOS_PATH, json_body={"command": validated}
    )
    text = _as_text(response)
    # Never the output itself, at any level: a running-config is configuration
    # and some of it is sensitive. Only its size is loggable.
    log.debug(
        "appliance_cli_read", appliance=appliance, ne_pk=ne_pk, command=validated, chars=len(text)
    )
    return ApplianceConfig(appliance=appliance, ne_pk=ne_pk, command=validated, text=text)


def fetch_running_config(ctx: Ctx, appliance: str) -> ApplianceConfig:
    """``show running-config`` from one appliance."""
    return run_read_command(ctx, appliance, RUNNING_CONFIG_COMMAND)


def fetch_running_configs(
    ctx: Ctx,
    appliances: Sequence[str],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> list[Outcome[str, ApplianceConfig]]:
    """``show running-config`` from several appliances, one outcome each.

    Bounded and failure-isolating: an unreachable appliance becomes an
    ``unreachable`` outcome carrying the reason, in the order the caller named
    them, rather than losing the whole report.
    """
    # Validated once up front so a bad command fails before a single
    # connection is opened, rather than N times inside the pool.
    validate_command(RUNNING_CONFIG_COMMAND)
    return fan_out(
        list(appliances),
        lambda name: fetch_running_config(ctx, name),
        concurrency=concurrency,
        timeout=timeout,
    )


def broadcast_read_command(
    ctx: Ctx, appliances: Sequence[str], command: str
) -> BroadcastResult:
    """Dispatch one allowlisted read to several appliances via ``/broadcastCli``.

    One control-plane call fans the command out server-side; the response is a
    bare JSON string (the GUID), which is polled to a terminal state with the
    shared action-key poller. FAILED and TIMEOUT are both failures — a report
    must never present "we stopped waiting" as "it worked".

    Returns execution status only. The broadcast channel carries no command
    output (module docstring); callers that need the text use
    :func:`fetch_running_configs`.
    """
    validated = validate_command(command)
    if not appliances:
        raise ValueError("broadcast needs at least one appliance")
    targets = tuple((name, ctx.resolver.ne_pk_for(name)) for name in appliances)
    response = ctx.client.post(
        BROADCAST_PATH,
        {"cmdList": [validated], "nePks": [ne_pk for _name, ne_pk in targets]},
    )
    # The response is a bare JSON string, not an object; extract_action_key
    # already handles that shape (appliance_resync returns one too).
    action_key = extract_action_key(response)
    if not action_key:
        outcome = JobOutcome(
            key="",
            state="FAILED",
            detail="broadcastCli returned no action key; completion cannot be confirmed",
        )
        log.debug("broadcast_cli_keyless", command=validated)
        return BroadcastResult(
            command=validated, targets=targets, action_key="", outcome=outcome
        )
    log.debug("broadcast_cli_started", key=action_key, command=validated, count=len(targets))
    outcome = wait_for_action(
        ctx.client, action_key, ctx.client.settings, f"broadcast cli {validated!r}"
    )
    return BroadcastResult(
        command=validated, targets=targets, action_key=action_key, outcome=outcome
    )


def broadcast_running_config(ctx: Ctx, appliances: Sequence[str]) -> BroadcastResult:
    """``show running-config`` broadcast to several appliances (status only)."""
    return broadcast_read_command(ctx, appliances, RUNNING_CONFIG_COMMAND)


__all__ = [
    "ALLOWED_VERBS",
    "BROADCAST_PATH",
    "CLI_ECOS_PATH",
    "MAX_COMMAND_LENGTH",
    "RUNNING_CONFIG_COMMAND",
    "ApplianceConfig",
    "BroadcastResult",
    "CommandRefused",
    "broadcast_read_command",
    "broadcast_running_config",
    "fetch_running_config",
    "fetch_running_configs",
    "permitted_summary",
    "run_read_command",
    "validate_command",
]
