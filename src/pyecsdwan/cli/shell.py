"""Interactive Junos-flavored shell (``ec-cli shell``).

Two modes over one persistent candidate changeset:

* OPERATIONAL (``pyecsdwan> ``) — read-only inspection: appliances, the
  transaction journal, pending (orphaned) transactions, plugin coverage, and
  generic ``show <kind> [<name>]`` for any registered resource kind.
* CONFIG (``pyecsdwan(config)# ``) — ``set``/``delete`` accumulate into the
  on-disk candidate store; ``show | compare``, ``commit [confirm <minutes>]``,
  ``rollback`` and ``discard`` drive the transaction engine in
  :mod:`pyecsdwan.txn`.

Command dispatch is factored into :func:`dispatch_operational` /
:func:`dispatch_config` so the whole command surface is testable without a
TTY; :func:`run_shell` only owns the PromptSession loop. Rendering helpers
from ``pyecsdwan.cli.render`` are imported inside functions so this module
imports standalone.
"""

from __future__ import annotations

import dataclasses
import re
import shlex
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.table import Table

from pyecsdwan import __version__, config, journal, locking, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import outcomes
from pyecsdwan.contract import Ctx, Ref, Resource, Scope
from pyecsdwan.registry import Registry, UnknownKind, scoped_instances
from pyecsdwan.reports import versions

if TYPE_CHECKING:
    from pyecsdwan.reports.applianceconfig import ApplianceConfig

MODE_OPERATIONAL = "operational"
MODE_CONFIG = "config"

PROMPT_OPERATIONAL = "pyecsdwan> "
PROMPT_CONFIG = "pyecsdwan(config)# "

_OPERATIONAL_COMMANDS: tuple[str, ...] = ("configure", "exit", "quit", "show")
_CONFIG_COMMANDS: tuple[str, ...] = (
    "commit",
    "compare",
    "delete",
    "discard",
    "exit",
    "quit",
    "rollback",
    "set",
    "show",
    "top",
)
#: `show <token>` where the subject is the tool, not the fabric: transaction
#: and audit state. Deliberately left under bare `show` (grammar.md §2) — it is
#: neither network-operational nor configuration, and the corpus has no
#: precedent for a fifth category.
_SHOW_CLI_STATE: tuple[str, ...] = (
    "appliances",
    "commands",
    "coverage",
    "journal",
    "locks",
    "transactions",
)
#: Scope nouns (grammar.md §3). Outermost-first and mandatory: EdgeConnect
#: always has a subject, so there is no implicit "this device".
_SCOPE_NOUNS: tuple[str, ...] = ("appliance", "fabric")
#: Datastore qualifiers under `show configuration`. `running` is the default
#: and may always be written explicitly; `candidate` is never implicit, so an
#: operator cannot be shown staged intent believing it is the device.
_DATASTORES: tuple[str, ...] = ("running", "candidate")
#: Operational domains under a scope noun.
_FABRIC_DOMAINS: tuple[str, ...] = ("flow", "flows", "version")
_APPLIANCE_DOMAINS: tuple[str, ...] = ("bgp",)
#: `bgp` leaves. `routes` is listed and answers `unsupported`: no BGP
#: route-table endpoint exists in either vendored baseline (specs/002 finding
#: 2), and hiding it would make the CLI look like it never considered the
#: question an operator is certain to ask.
_BGP_VIEWS: tuple[str, ...] = ("summary", "neighbors", "routes")
#: CLI token -> txn.commit() keyword argument.
_COMMIT_FLAGS: dict[str, str] = {
    "force": "force",
    "override-template": "override_template",
    "allow-untransactional": "allow_untransactional",
    "rebase": "rebase",
}
_INT_RE = re.compile(r"^[+-]?\d+$")

_SET_USAGE = (
    "usage: set <kind> <name> <path...> <value>  |  "
    "set appliance <appliance-name> <kind> <name> <path...> <value>"
)
_DELETE_USAGE = (
    "usage: delete <kind> <name> [<path...>]  |  "
    "delete appliance <appliance-name> <kind> <name> [<path...>]"
)
# `show flow` and `show flows` differ by one character and mean different
# things, so each insists on its own shape rather than guessing at the other.
_SHOW_FLOWS_USAGE = "usage: show fabric flows summary"
_SHOW_FLOW_USAGE = "usage: show fabric flow <ip>[/<prefix>]"
_ROLLBACK_USAGE = "usage: rollback <n>  |  rollback pending"
_SHOW_GENERIC_USAGE = (
    "usage: show configuration [running] [appliance <name>] <kind> [<instance>]"
)
#: The per-appliance vendor text (#56). `--format native` is what selects it;
#: without the flag the same path renders normalized configuration.
_SHOW_NATIVE_USAGE = (
    "usage: show configuration [running] appliance <name> [<name>...] --format native"
)
#: The fabric-wide configuration breakdown (#55), optionally scoped to one
#: section.
_SHOW_FABRIC_CONFIG_USAGE = "usage: show configuration [running] fabric [<section>]"


class Nonterminal(Exception):
    """A valid prefix that names its continuations — not an error (D-NSO-2).

    `show appliance BR1-EC bgp` is the canonical case: three views live under
    it and the shell must never pick one. Carried as an exception so any depth
    of the tree can answer "you are here, these are the next tokens" without
    every intermediate frame having to return a sentinel; the dispatcher
    renders it at exit 0, in the ordinary style rather than in red.
    """

    def __init__(self, path: str, options: Sequence[str], note: str = "") -> None:
        super().__init__(path)
        self.path = path
        self.options = list(options)
        self.note = note


@dataclasses.dataclass
class ShellState:
    """Mutable session state shared by the prompt loop, dispatchers, completer."""

    ctx: Ctx
    registry: Registry
    settings: config.Settings
    console: Console
    candidate: CandidateStore
    mode: str = MODE_OPERATIONAL
    running: bool = True
    exit_code: int = 0


def build_state(
    ctx: Ctx,
    registry: Registry,
    settings: config.Settings,
    console: Console | None = None,
) -> ShellState:
    """Assemble a ShellState (candidate store keyed by the Orchestrator host)."""
    return ShellState(
        ctx=ctx,
        registry=registry,
        settings=settings,
        console=console if console is not None else Console(),
        candidate=CandidateStore(settings.host),
    )


# -- output helpers ----------------------------------------------------------


def _error(console: Console, message: str) -> None:
    # soft_wrap for the same reason running-config output uses it: these
    # messages name the command to type instead, and a remedy rich has broken
    # across two lines cannot be pasted. The terminal still wraps it visually;
    # what it no longer does is put a newline inside the command.
    console.print(message, style="bold red", markup=False, highlight=False, soft_wrap=True)


def _warn(console: Console, message: str) -> None:
    console.print(message, style="yellow", markup=False, highlight=False)


def _info(console: Console, message: str) -> None:
    console.print(message, markup=False, highlight=False)


def _format_error(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str):
        # UnknownKind (and friends) carry a clean operator message; plain
        # str() on a KeyError would wrap it in quotes.
        return exc.args[0]
    text = str(exc).strip()
    return text if text else type(exc).__name__


# -- parsing helpers ---------------------------------------------------------


def coerce_value(token: str) -> bool | int | str:
    """``set`` value coercion: true/false -> bool, integer-looking -> int, else str."""
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT_RE.match(token):
        return int(token)
    return token


def _tokenize(line: str, console: Console) -> list[str] | None:
    try:
        return shlex.split(line, comments=False)
    except ValueError as exc:
        _error(console, f"parse error: {exc}")
        return None


def _resolve_kind(token: str, appliance: str | None, state: ShellState) -> str:
    """User-facing noun -> internal registry kind (issue #77).

    Scope comes from the command, not from the operator: naming an appliance
    selects appliance scope, and naming none selects the Orchestrator — which
    is exactly what `grammar.md` §3 says the absent scope noun means. So
    `zones` is unambiguous in either position even though it names two
    different objects.

    A token that does not resolve in the position's scope is retried in the
    *other* one, so the caller can answer "that is appliance-scope, use
    `appliance <name> ...`" — or the mirror image, `bio` named after an
    appliance (#48) — instead of the much less useful "unknown resource kind".
    The caller's own scope check then rejects it with that message.

    Both directions matter, and only one of them used to work: until #74
    withdrew the `appliance/<kind>` alias, an orchestrator noun in appliance
    position was rescued by the registry-key fallback rather than by this
    retry, which hid the asymmetry.
    """
    scope = Scope.APPLIANCE if appliance is not None else Scope.ORCHESTRATOR
    other = Scope.ORCHESTRATOR if scope is Scope.APPLIANCE else Scope.APPLIANCE
    try:
        return state.registry.resolve_cli(token, scope)
    except UnknownKind as unknown_here:
        try:
            return state.registry.resolve_cli(token, other)
        except UnknownKind:
            # Report the nouns valid where the operator typed, not where the
            # retry looked: in the shell, position *is* scope.
            raise unknown_here from None


def _parse_ref(args: list[str], state: ShellState, usage: str) -> tuple[Ref, list[str]]:
    """Split ``[appliance <name>] <kind> <name> <rest...>`` into (Ref, rest)."""
    appliance: str | None = None
    if args and args[0] == "appliance":
        if len(args) < 4:
            raise ValueError(usage)
        appliance = args[1]
        args = args[2:]
    if len(args) < 2:
        raise ValueError(usage)
    token, name, rest = args[0], args[1], args[2:]
    kind = _resolve_kind(token, appliance, state)
    resource = state.registry.get(kind)
    noun = state.registry.cli_name(kind)
    if appliance is not None and resource.scope is not Scope.APPLIANCE:
        raise ValueError(
            f"{noun} is {resource.scope.value}-scope; omit the 'appliance' form: "
            f"{noun} <name> ..."
        )
    if appliance is None and resource.scope is Scope.APPLIANCE:
        raise ValueError(
            f"{noun} is appliance-scope; use: appliance <appliance-name> {noun} <name> ..."
        )
    return Ref(kind=kind, name=name, appliance=appliance), rest


def _parse_commit_args(tokens: list[str]) -> tuple[int | None, dict[str, bool]]:
    """Parse ``commit [confirm <minutes>] [force] [override-template] [...]``."""
    confirm_minutes: int | None = None
    flags = {name: False for name in _COMMIT_FLAGS.values()}
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if word == "confirm":
            if i + 1 >= len(tokens) or not _INT_RE.match(tokens[i + 1]):
                raise ValueError("usage: commit confirm <minutes>")
            confirm_minutes = int(tokens[i + 1])
            if confirm_minutes < 1:
                raise ValueError("commit confirm: minutes must be >= 1")
            i += 2
        elif word in _COMMIT_FLAGS:
            flags[_COMMIT_FLAGS[word]] = True
            i += 1
        else:
            raise ValueError(
                f"unknown commit option {word!r} — expected: confirm <minutes>, "
                f"force, override-template, allow-untransactional, rebase"
            )
    return confirm_minutes, flags


# -- dispatch ----------------------------------------------------------------


def dispatch(line: str, state: ShellState) -> None:
    """Route one command line to the dispatcher for the current mode."""
    if state.mode == MODE_CONFIG:
        dispatch_config(line, state)
    else:
        dispatch_operational(line, state)


#: The token an operator types to ask what may come next.
HELP_TOKEN = "?"  # noqa: S105 - a question mark, not a credential


def _help_for(prior: list[str], state: ShellState) -> Nonterminal:
    """What may follow ``prior``, as the Nonterminal the renderer already knows.

    Deliberately not a new answer: a Nonterminal is what the parser raises when
    a token is *omitted*, so `show appliance A bgp` and `show appliance A bgp ?`
    now print the same thing. Two spellings of one question deserve one answer.
    """
    options = ShellCompleter(state).next_tokens(prior)
    path = " ".join(prior)
    if not options:
        note = (
            "nothing follows this — it is a complete command"
            if path
            else "no commands are available here"
        )
        return Nonterminal(path=path or "(start)", options=(), note=note)
    return Nonterminal(path=path or "(start)", options=options)


def dispatch_operational(line: str, state: ShellState) -> None:
    """Execute one operational-mode line; command errors print red, never raise."""
    tokens = _tokenize(line, state.console)
    if not tokens:
        return
    if tokens[-1] == HELP_TOKEN:
        # `?` is how a Junos operator asks "what now", and this shell answers
        # every other way of asking (#89): omit the token and the parser raises
        # a Nonterminal listing the continuations. Typing `?` produced
        # "unknown command '?'" instead — the one spelling that reads as a
        # question got treated as a noun.
        #
        # Routed through the completer's tree rather than a second table, so
        # `?` and Tab cannot drift apart: they are the same function. That is
        # Principle IV inside one interface.
        _render_nonterminal(state.console, _help_for(tokens[:-1], state))
        return
    try:
        _run_operational(tokens, state)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Nonterminal as nonterminal:
        _render_nonterminal(state.console, nonterminal)
    except outcomes.CommandOutcome as outcome:
        _render_outcome(state, outcome)
    except Exception as exc:  # noqa: BLE001 - dispatch boundary: any command failure becomes a red line; the shell survives
        # Classified through the same table the scriptable CLI uses, so a
        # failure reported at the prompt and the same failure in a script are
        # the same outcome with the same exit code (grammar.md §5, R7).
        # Distinct name: `outcome` is bound by the CommandOutcome clause above
        # and unbound at its end, so reusing it here reads a deleted variable.
        classified = outcomes.classify(exc)
        _error(state.console, f"{classified.value}: {_format_error(exc)}")
        state.exit_code = classified.exit_code


def _render_outcome(state: ShellState, outcome: outcomes.CommandOutcome) -> None:
    """A terminal state that is not success, rendered with its exit code.

    Not printed in red unless it is a failure: `unsupported` is a statement
    about the product, not about the appliance, and colouring it like an error
    sends someone to debug a healthy device (grammar.md §5).
    """
    style = "yellow" if outcome.outcome is outcomes.Outcome.UNSUPPORTED else "bold red"
    state.console.print(
        f"{outcome.outcome.value}: {outcome.detail}",
        style=style,
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    if outcome.remedy:
        _info(state.console, outcome.remedy)
    state.exit_code = outcome.outcome.exit_code


def _render_nonterminal(console: Console, nonterminal: Nonterminal) -> None:
    """Print a prefix's valid continuations. Exit 0: this answered the question
    that was asked, which was "what can follow this?"."""
    console.print(f"{nonterminal.path} — valid next tokens:", markup=False, highlight=False)
    if nonterminal.options:
        for option in nonterminal.options:
            console.print(f"  {option}", markup=False, highlight=False)
    else:
        _info(console, "  (none)")
    if nonterminal.note:
        console.print(nonterminal.note, markup=False, highlight=False, soft_wrap=True)


def dispatch_config(line: str, state: ShellState) -> None:
    """Execute one config-mode line; command errors print red, never raise."""
    tokens = _tokenize(line, state.console)
    if not tokens:
        return
    try:
        _run_config(tokens, state)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - dispatch boundary: any command failure becomes a red line; the shell survives
        _error(state.console, _format_error(exc))


def _run_operational(tokens: list[str], state: ShellState) -> None:
    cmd = tokens[0]
    if cmd == "configure":
        state.mode = MODE_CONFIG
        return
    if cmd in ("exit", "quit"):
        state.running = False
        return
    if cmd == "show":
        _show_operational(tokens[1:], state)
        return
    _error(state.console, f"unknown command {cmd!r} — try: configure, show <...>, exit")


def _run_config(tokens: list[str], state: ShellState) -> None:
    cmd = tokens[0]
    if cmd == "set":
        _cmd_set(tokens[1:], state)
    elif cmd == "delete":
        _cmd_delete(tokens[1:], state)
    elif cmd == "show":
        _cmd_show_config(tokens[1:], state)
    elif cmd == "compare":
        if tokens[1:]:
            raise ValueError("usage: compare")
        _cmd_compare(state)
    elif cmd == "commit":
        _cmd_commit(tokens[1:], state)
    elif cmd == "rollback":
        _cmd_rollback(tokens[1:], state)
    elif cmd == "discard":
        state.candidate.clear()
        _info(state.console, "candidate changes discarded")
    elif cmd in ("exit", "top", "quit"):
        _leave_config(state)
    else:
        _error(
            state.console,
            f"unknown command {cmd!r} — try: set, delete, show, compare, commit, "
            f"rollback, discard, exit",
        )


def _leave_config(state: ShellState) -> None:
    state.mode = MODE_OPERATIONAL
    if len(state.candidate):
        _warn(state.console, "candidate changes retained; 'discard' to drop")


# -- operational commands ----------------------------------------------------


def _show_operational(args: list[str], state: ShellState) -> None:
    """The read tree (grammar.md §2).

    Three branches, one per intent, and no token sequence resolves to two —
    that is Principle I made structural rather than documented:

    * ``show <cli-state>``    — the tool's own state
    * ``show <scope> ...``    — operational state of the network
    * ``show configuration``  — configuration, at a named datastore

    The pre-#74 tree had no such split: ``show appliance BR1-EC bgp`` returned
    modeled configuration and ``show version`` returned live state, from the
    same position, distinguished only by which noun happened to be typed.
    """
    if not args:
        raise Nonterminal(
            "show", [*_SHOW_CLI_STATE, *_SCOPE_NOUNS, "configuration"]
        )
    head = args[0]
    if head == "appliances":
        _show_appliances(state)
    elif head == "journal":
        from pyecsdwan.cli.render import render_journal_table

        render_journal_table(state.console, journal.list_txns())
    elif head == "locks":
        from pyecsdwan.cli.main import render_locks_table

        if args[1:]:
            raise ValueError("usage: show locks")
        render_locks_table(state.console, locking.active_locks())
    elif head == "transactions":
        if args[1:] != ["pending"]:
            raise ValueError("usage: show transactions pending")
        _show_pending(state)
    elif head == "coverage":
        _show_coverage(state)
    elif head == "commands":
        if args[1:]:
            raise ValueError("usage: show commands")
        from pyecsdwan.cli import reference
        from pyecsdwan.cli.render import render_command_reference

        render_command_reference(state.console, reference.build(state.registry))
    elif head == "configuration":
        _show_configuration(args[1:], state)
    elif head == "fabric":
        _show_fabric_operational(args[1:], state)
    elif head == "appliance":
        _show_appliance_operational(args[1:], state)
    else:
        raise ValueError(
            f"unknown command 'show {head}' — valid next tokens: "
            f"{', '.join([*_SHOW_CLI_STATE, *_SCOPE_NOUNS, 'configuration'])}"
        )


# -- operational state -------------------------------------------------------


def _take_flag(args: list[str], *spellings: str) -> tuple[list[str], bool]:
    """Strip a boolean flag from anywhere in the token list, reporting whether
    it was there. Position-independent because the shell's grammar is
    positional and a flag is not part of that grammar."""
    kept = [a for a in args if a not in spellings]
    return kept, len(kept) != len(args)


def _take_yes_flag(args: list[str]) -> tuple[list[str], bool]:
    """Strip ``--yes``/``-y`` (grammar.md §6)."""
    return _take_flag(args, "--yes", "-y")


def _gate_fanout(state: ShellState, assume_yes: bool, calls_each: int = 1) -> None:
    """Ask before a command that calls every appliance (Decision 7).

    Declining returns quietly: the operator was asked and said no, so there is
    nothing to report except that nothing ran.
    """
    from pyecsdwan.cli import fanout

    fanout.confirm(
        state.ctx,
        console=state.console,
        err_console=state.console,
        assume_yes=assume_yes,
        calls_each=calls_each,
    )


def _show_fabric_operational(args: list[str], state: ShellState) -> None:
    """``show fabric <domain> ...`` — live state across every appliance.

    Every domain here is a fan-out by definition of the scope noun, so the
    cost gate sits at the branch rather than in each leaf.
    """
    from pyecsdwan.cli.fanout import FanoutDeclined

    args, assume_yes = _take_yes_flag(args)
    if not args:
        raise Nonterminal("show fabric", list(_FABRIC_DOMAINS))
    head = args[0]
    try:
        _gate_fanout(state, assume_yes)
    except FanoutDeclined:
        _info(state.console, "cancelled: nothing was queried")
        return
    if head == "version":
        if args[1:]:
            raise ValueError("usage: show fabric version")
        _show_version(state)
    elif head == "flows":
        _show_flows_summary(args[1:], state)
    elif head == "flow":
        _show_flow(args[1:], state)
    else:
        raise ValueError(
            f"unknown domain {head!r} under 'show fabric' — valid next tokens: "
            f"{', '.join(_FABRIC_DOMAINS)}"
        )


def _show_appliance_operational(args: list[str], state: ShellState) -> None:
    """``show appliance <name> <domain> ...`` — live state of one appliance.

    One domain exists so far (`bgp`, specs/002). Everything else that resolves
    as a configuration kind is the *renamed* form, and gets the one refusal in
    this migration that could otherwise have hurt someone: before #74 these
    tokens returned modeled configuration, and they now name operational
    state. Answering with either silently is what Principle II exists to
    prevent, so it is refused and the refusal says where the configuration
    went.
    """
    if not args:
        raise ValueError("usage: show appliance <name> <domain>")
    name, rest = args[0], args[1:]
    if not rest:
        raise Nonterminal(f"show appliance {name}", list(_APPLIANCE_DOMAINS))
    domain, tail = rest[0], rest[1:]
    if domain == "bgp":
        _show_bgp(name, tail, state)
        return
    try:
        kind = _resolve_kind(domain, name, state)
    except UnknownKind:
        raise ValueError(
            f"unknown operational domain {domain!r} for appliance {name!r} — "
            f"valid next tokens: {', '.join(_APPLIANCE_DOMAINS)}"
        ) from None
    noun = state.registry.cli_name(kind)
    raise ValueError(
        f"{noun} is configuration, not operational state — use:\n"
        f"    show configuration appliance {name} {noun}"
        f"{' ' + tail[0] if tail else ''}\n"
        f"'show appliance <name> {noun}' meant that before #74 and now names "
        f"operational state, so it is refused rather than answered with "
        f"different data than it used to return."
    )


def _show_bgp(appliance: str, args: list[str], state: ShellState) -> None:
    """``show appliance <name> bgp [summary | neighbors [<ip>] | routes]``.

    A bare ``bgp`` lists the leaves and makes **no API call** — #72's guardrail
    that a nonterminal is contextual help, not an implicit expensive fetch.
    """
    # Decision 7: cached data is served only when asked for. The API has its
    # own `cached` parameter, so this is honoured at the source rather than
    # inferred here (#72 finding 3).
    args, stale_ok = _take_flag(args, "--stale-ok")
    if not args:
        raise Nonterminal(
            f"show appliance {appliance} bgp",
            ["summary", "neighbors [<ip>]", "routes (unsupported)", "--stale-ok"],
        )
    view, rest = args[0], args[1:]
    if view == "routes":
        # Not a failure of the appliance, and not something a retry fixes.
        raise outcomes.CommandOutcome(
            outcomes.Outcome.UNSUPPORTED,
            "no BGP route-table endpoint exists in the supported Orchestrator "
            "or ECOS API, so this view cannot be built from a verified source.",
            remedy=(
                f"route counts are in 'show appliance {appliance} bgp summary' "
                f"(num_bgp_rtes_rcvd, num_ebgp_rtes, num_ibgp_rtes, "
                f"num_subs_installed); per-peer counts are in 'neighbors'. "
                f"If you have a source this missed, 'ec-cli api' reaches it raw."
            ),
        )
    if view not in ("summary", "neighbors"):
        raise ValueError(
            f"unknown bgp view {view!r} — valid next tokens: {', '.join(_BGP_VIEWS)}"
        )
    if view == "summary" and rest:
        raise ValueError(f"usage: show appliance {appliance} bgp summary")
    if len(rest) > 1:
        raise ValueError(f"usage: show appliance {appliance} bgp neighbors [<ip>]")

    from pyecsdwan.cli.render import render_bgp_neighbors, render_bgp_summary
    from pyecsdwan.reports import bgpstate

    result = bgpstate.collect(state.ctx, appliance, cached=stale_ok)
    if view == "summary":
        render_bgp_summary(state.console, result)
        return
    peer = rest[0] if rest else None
    render_bgp_neighbors(state.console, result, peer=peer)
    if peer is not None and not any(n.peer_ip == peer for n in result.neighbors):
        raise outcomes.CommandOutcome(
            outcomes.Outcome.NOT_FOUND,
            f"{appliance} has no BGP neighbor {peer}",
            remedy=f"'show appliance {appliance} bgp neighbors' lists the peers it has",
        )
    if not result.rows_match_count:
        raise outcomes.CommandOutcome(
            outcomes.Outcome.PARTIAL,
            f"{appliance} reports neighborCount={result.neighbor_count} but returned "
            f"{len([n for n in result.neighbors if not n.configured_only])} peer rows; "
            f"the table above is incomplete.",
        )


# -- configuration -----------------------------------------------------------


def _show_configuration(args: list[str], state: ShellState) -> None:
    """``show configuration [running|candidate] ...`` (grammar.md §2).

    The datastore token is optional and defaults to ``running`` (Decision 1),
    which makes the common read short at the cost that it does not name its
    datastore. The mitigation is the asymmetry: ``candidate`` is never
    implicit, so the only unnamed datastore is the live one and an operator
    can never be shown staged intent while believing they are looking at the
    device.

    That optionality is why ``running``, ``candidate``, ``appliance``,
    ``fabric`` and ``configuration`` are reserved kind names — this position
    has to decide whether a token is a datastore, a scope noun, or a kind, and
    it cannot if a kind may be spelled like one (enforced in
    ``contract.RESERVED_CLI_WORDS``).
    """
    datastore = "running"
    if args and args[0] in _DATASTORES:
        datastore, args = args[0], args[1:]
    if datastore == "candidate":
        if args:
            raise ValueError("usage: show configuration candidate")
        _render_candidate(state)
        return
    if not args:
        raise Nonterminal(
            "show configuration",
            [*_DATASTORES, *_SCOPE_NOUNS, *state.registry.cli_names(Scope.ORCHESTRATOR)],
        )
    head = args[0]
    if head == "fabric":
        _show_fabric_config(args[1:], state)
    elif head == "appliance":
        _show_appliance_config(args[1:], state)
    else:
        _show_generic(args, None, state)


def _show_appliance_config(args: list[str], state: ShellState) -> None:
    """``show configuration [running] appliance <name> [<kind> [<instance>]]``
    and the ``--format native`` form that replaced ``show run appliance``."""
    if not args:
        raise ValueError(_SHOW_GENERIC_USAGE)
    rest, native = _take_native_flag(args)
    if native:
        if not rest:
            raise ValueError(_SHOW_NATIVE_USAGE)
        _show_native_config(rest, state)
        return
    name, tail = rest[0], rest[1:]
    if not tail:
        raise Nonterminal(
            f"show configuration appliance {name}",
            [*state.registry.cli_names(Scope.APPLIANCE), "--format native"],
        )
    _show_generic(tail, name, state)


def _take_native_flag(args: list[str]) -> tuple[list[str], bool]:
    """Split ``--format native`` off the token list.

    Only ``native`` is accepted here: it selects a different *source* (the
    appliance's own command interpreter) rather than a different rendering, so
    it cannot be handled with the yaml/json formats that apply to every read.
    """
    if "--format" not in args:
        return args, False
    index = args.index("--format")
    if index + 1 >= len(args):
        raise ValueError("--format needs a value: native")
    value = args[index + 1]
    if value != "native":
        raise ValueError(
            f"--format {value!r} is not valid here; 'native' selects the "
            f"appliance's own configuration text"
        )
    return args[:index] + args[index + 2 :], True


def _show_appliances(state: ShellState) -> None:
    appliances = state.ctx.resolver.appliances()
    if not appliances:
        _info(state.console, "no appliances")
        return
    table = Table("hostName", "nePk", "site", "model")
    for appliance in appliances:
        table.add_row(
            str(appliance.get("hostName") or ""),
            str(appliance.get("nePk") or appliance.get("id") or ""),
            str(appliance.get("site") or ""),
            str(appliance.get("model") or ""),
        )
    state.console.print(table)


def _show_pending(state: ShellState) -> None:
    pending = txn.pending_rollbacks(host=state.settings.host)
    if not pending:
        _info(state.console, "none")
        return
    table = Table("id", "state", "deadline")
    for orphan in pending:
        table.add_row(
            orphan.meta.txn_id, orphan.meta.state, orphan.meta.confirm_deadline or "-"
        )
    state.console.print(table)


def _show_coverage(state: ShellState) -> None:
    """Summary only: kinds, their tier, and the endpoint-universe roll-up.

    Deliberately narrower than ``ec-cli show coverage`` — the per-endpoint
    view is 1800-odd rows and belongs in the scriptable CLI, not a prompt —
    but the numbers come from the same helper so the two cannot disagree.
    """
    # Imported here (not at module scope) for the same reason `render` is:
    # `pyecsdwan.cli.main` imports this module lazily, so a top-level import
    # back into it would be circular.
    from pyecsdwan.cli.main import coverage_summary_line

    table = Table("kind", "scope", "reversibility", "tier", "endpoints")
    for kind in state.registry.kinds():
        resource = state.registry.get(kind)
        table.add_row(
            kind,
            resource.scope.value,
            resource.reversibility.value,
            str(int(resource.tier)),
            str(len(resource.endpoints)),
        )
    state.console.print(table)
    state.console.print(coverage_summary_line(state.registry))
    _info(state.console, "`ec-cli show coverage --endpoints` lists every known endpoint")


def _show_version(state: ShellState) -> None:
    """Orchestrator version plus per-appliance partition versions (#57).

    Read-only, and identical to ``ec-cli show version`` with no flags: the
    renderer is imported from :mod:`pyecsdwan.cli.main` — for the same reason
    ``coverage_summary_line`` is — so the prompt and the scriptable CLI cannot
    drift into reporting different versions.
    """
    from pyecsdwan.cli.main import render_version_report

    report = versions.collect(state.ctx)
    render_version_report(state.console, report)
    _info(
        state.console,
        "`ec-cli show version --no-cache` re-reads each appliance instead of the cache",
    )
def _show_flows_summary(args: list[str], state: ShellState) -> None:
    """``show flows summary`` (#58) — the per-appliance x per-overlay matrix.

    The literal ``summary`` subcommand is required: a bare ``show flows``
    would otherwise be one keystroke away from ``show flow`` and quietly do
    the wrong thing.
    """
    if args != ["summary"]:
        raise ValueError(_SHOW_FLOWS_USAGE)
    from pyecsdwan.cli.main import render_flows_summary
    from pyecsdwan.reports import flows as flows_report

    render_flows_summary(state.console, flows_report.build_flows_summary(state.ctx))


def _show_flow(args: list[str], state: ShellState) -> None:
    """``show flow <ip>`` (#59) — every flow touching one address, fabric-wide.

    A bare ``show flow`` is a usage error, never silently the summary.
    """
    if len(args) != 1:
        raise ValueError(_SHOW_FLOW_USAGE)
    from pyecsdwan.cli.main import render_flow_search
    from pyecsdwan.reports import flows as flows_report

    render_flow_search(state.console, flows_report.find_flows(state.ctx, args[0]))
def _show_native_config(names: list[str], state: ShellState) -> None:
    """``show configuration [running] appliance <name>... --format native``.

    The appliance's own configuration text, read through its command
    interpreter (#56) — the one surface that reaches an ECOS CLI at all. Still
    read-only: the command sent is the vetted constant in
    :mod:`pyecsdwan.reports.applianceconfig`, whose deny-by-default validator
    is unchanged by the rename (compatibility.md rule 5).

    Several names are accepted because the pre-#74 spelling accepted them and
    the migration table does not remove that; the flag is what selects the
    format, so everything before it is a name.
    """
    from pyecsdwan.reports import applianceconfig

    if len(names) == 1:
        _print_appliance_config(state, applianceconfig.fetch_running_config(state.ctx, names[0]))
        return
    outcomes = applianceconfig.fetch_running_configs(state.ctx, names)
    for index, outcome in enumerate(outcomes):
        if index:
            state.console.print()
        if outcome.value is not None:
            _print_appliance_config(state, outcome.value)
        else:
            _error(state.console, f"{outcome.item}: unreachable — {outcome.error}")
    if any(o.unreachable for o in outcomes):
        state.exit_code = 2


def _show_fabric_config(args: list[str], state: ShellState) -> None:
    """``show configuration [running] fabric [<section>]`` (#55).

    An unknown section names the valid ones and then the usage line, so a
    typo is answered with what to type rather than with an empty report.
    """
    from pyecsdwan.cli.fanout import FanoutDeclined
    from pyecsdwan.cli.main import render_fabric_config
    from pyecsdwan.reports import fabric

    args, assume_yes = _take_yes_flag(args)
    if len(args) > 1:
        # Name what was unexpected (#89). This used to raise the bare usage
        # line, so an operator drilling into `fabric deployment interfaces`
        # was told the shape of the command but not that `deployment` had been
        # accepted and `interfaces` was the surplus — and a usage string on its
        # own reads as "everything you typed was wrong".
        extra = " ".join(args[1:])
        raise ValueError(
            f"'{args[0]}' takes no further tokens, but got '{extra}'. "
            f"Sections are a whole view, not a path into one.\n"
            f"{_SHOW_FABRIC_CONFIG_USAGE}"
        )
    section = args[0] if args else None
    try:
        sections = fabric.resolve_sections(section)
    except fabric.UnknownSection as exc:
        raise ValueError(f"{exc}\n{_SHOW_FABRIC_CONFIG_USAGE}") from None
    if "deployment" in sections:
        # Only the deployment section reads every appliance; the others are
        # Orchestrator-level GETs, so scoping to one of those is not a fan-out
        # and must not be gated as though it were.
        try:
            _gate_fanout(state, assume_yes)
        except FanoutDeclined:
            _info(state.console, "cancelled: nothing was queried")
            return
    render_fabric_config(state.console, fabric.collect(state.ctx, section=section))


def _print_appliance_config(state: ShellState, config: ApplianceConfig) -> None:
    state.console.print(
        f"# {config.appliance} ({config.ne_pk}) — {config.command}",
        style="bold",
        markup=False,
        highlight=False,
    )
    # soft_wrap: a wrapped running-config line is a corrupted one.
    state.console.print(
        config.text.rstrip("\n"), markup=False, highlight=False, soft_wrap=True
    )


def _show_generic(args: list[str], appliance: str | None, state: ShellState) -> None:
    """``<kind> [<instance>]`` — fetch + normalize one resource and print it as
    YAML. With no instance name, resolve the kind's instances in the caller's
    scope (a lone instance is shown directly).

    Scope arrives as an argument rather than being re-parsed here: the tree
    above has already decided it, and parsing it twice is how the two surfaces
    came to disagree in the first place (#76).
    """
    if not args or len(args) > 2:
        raise ValueError(_SHOW_GENERIC_USAGE)
    kind = _resolve_kind(args[0], appliance, state)
    resource = state.registry.get(kind)
    noun = state.registry.cli_name(kind)
    if appliance is not None and resource.scope.value != "appliance":
        # Mirrors the symmetric check in _parse_ref (set/delete): an
        # orchestrator-scoped kind silently ignoring an `appliance <name>`
        # prefix produced confusing "(not present)"-style results instead of
        # a clear rejection (#48).
        raise ValueError(
            f"{noun} is {resource.scope.value}-scope; omit the 'appliance' form: "
            f"show configuration {noun} [<instance>]"
        )

    # Ordered before enumeration deliberately: an appliance-scoped kind with no
    # appliance used to enumerate the whole fabric first and report *that*
    # confusion, burying the one message that actually tells the operator what
    # to type.
    if resource.scope.value == "appliance" and appliance is None:
        raise ValueError(
            f"{noun} is appliance-scoped; use "
            f"'show configuration appliance <name> {noun} ...'"
        )

    if len(args) == 2:
        instance = args[1]
    else:
        instance, appliance = _resolve_instance(resource, noun, appliance, state)

    ref = Ref(kind=kind, name=instance, appliance=appliance)
    canonical = resource.normalize(resource.fetch(state.ctx, ref))
    state.console.print(f"# {ref}", style="dim", markup=False, highlight=False)
    if canonical is None:
        _info(state.console, "(not present)")
        return
    if not canonical:
        # `CanonicalState` is dict | list | None, and None is handled above, so
        # this is an empty container: a real answer — the appliance holds no
        # configuration for this kind — and a different answer from "(not
        # present)". Rendered as YAML it would be a bare `{}`, which in a
        # scrollback is indistinguishable from the command having done nothing.
        _info(state.console, "(empty — no configuration for this kind)")
        return
    text = yaml.safe_dump(canonical, sort_keys=True, default_flow_style=False)
    state.console.print(text.rstrip("\n"), markup=False, highlight=False)


def _resolve_instance(
    resource: Resource, kind: str, appliance: str | None, state: ShellState
) -> tuple[str, str | None]:
    """Pick the instance when the operator gave a kind but no name.

    Scoping itself lives in :func:`pyecsdwan.registry.scoped_instances` — one
    implementation, shared with completion and with ``plugin promote`` (#76),
    because three private copies of "filter to the named appliance" is how
    those surfaces came to disagree about what an instance list even is.

    What stays here is the shell's half: what to say when the scoped answer is
    not exactly one instance.
    """
    refs = scoped_instances(resource, state.ctx, appliance)

    if len(refs) == 1:
        only = refs[0]
        return only.name, appliance or only.appliance
    if not refs:
        if appliance is not None:
            # Distinguish "this appliance has none" from "there is no such
            # appliance" (#78). ne_pk_for raises the resolver's own
            # `unknown appliance 'X'` — with its did-you-mean suggestion —
            # which is a better answer than reporting zero instances on a
            # target that does not exist.
            state.ctx.resolver.ne_pk_for(appliance)
        where = f" on {appliance}" if appliance else ""
        # `not_found`, not `invalid`: the path the operator typed is valid and
        # the object it names does not exist (grammar.md §5). Reporting it as
        # a malformed command told a script the caller had a bug.
        raise outcomes.CommandOutcome(
            outcomes.Outcome.NOT_FOUND,
            f"{kind}: no instances found{where}",
            remedy=f"name one explicitly: ... {kind} <instance>",
        )
    # Several instances is a *nonterminal*, not an invalid command: the
    # operator typed a valid prefix and the answer is the list of names that
    # may follow it (D-NSO-2). Raising a usage error here exited 2 and told a
    # script the command was malformed, while the scriptable CLI's own
    # `_render_resource` had always treated the same case as a nonterminal —
    # a Principle IV divergence the command reference's round-trip found.
    where = f" on {appliance}" if appliance else ""
    raise Nonterminal(f"{kind}{where}", [r.name for r in refs])


# -- config commands ---------------------------------------------------------


def _cmd_set(args: list[str], state: ShellState) -> None:
    ref, rest = _parse_ref(args, state, _SET_USAGE)
    if len(rest) < 2:
        raise ValueError(_SET_USAGE)
    state.candidate.set_path(ref, list(rest[:-1]), coerce_value(rest[-1]))


def _cmd_delete(args: list[str], state: ShellState) -> None:
    ref, rest = _parse_ref(args, state, _DELETE_USAGE)
    state.candidate.delete(ref, list(rest) or None)


def _cmd_show_config(args: list[str], state: ShellState) -> None:
    rest = [t for t in args if t != "|"]
    if rest == ["compare"]:
        _cmd_compare(state)
        return
    if rest:
        raise ValueError("usage: 'show' or 'show | compare'")
    _render_candidate(state)


def _render_candidate(state: ShellState) -> None:
    """The staged changeset. Reached two ways, and deliberately one renderer:
    bare ``show`` in config mode, where the mode carries the intent (D-JUN-1),
    and ``show configuration candidate`` in operational mode, where it has to
    be named because ``candidate`` is never the default datastore."""
    if not len(state.candidate):
        _info(state.console, "(candidate is empty)")
        return
    doc: dict[str, Any] = {}
    for item in state.candidate.ordered_items():
        entry: dict[str, Any] = {"mode": item.mode}
        if item.intent:
            entry["intent"] = item.intent
        if item.delete_paths:
            entry["delete_paths"] = item.delete_paths
        doc[item.ref_key] = entry
    text = yaml.safe_dump(doc, sort_keys=True, default_flow_style=False)
    state.console.print(text.rstrip("\n"), markup=False, highlight=False)


def _cmd_compare(state: ShellState) -> None:
    from pyecsdwan.cli.render import render_plan

    plan = txn.build_plan(state.ctx, state.registry, state.candidate)
    render_plan(state.console, plan)


def _cmd_commit(tokens: list[str], state: ShellState) -> None:
    from pyecsdwan.cli.render import render_report

    confirm_minutes, flags = _parse_commit_args(tokens)

    # A bare `commit` inside a confirm window confirms the pending transaction
    # — but only for THIS Orchestrator, and only when there's nothing new
    # staged and no options were passed (otherwise the user meant a fresh
    # commit and we'd silently confirm + drop their new work).
    unconfirmed = [
        t
        for t in journal.list_txns()
        if t.meta.state == journal.TxnState.APPLIED_UNCONFIRMED
        and t.meta.orch_host == state.settings.host
    ]
    if unconfirmed:
        options_passed = bool(
            confirm_minutes
            or flags["force"]
            or flags["override_template"]
            or flags["allow_untransactional"]
            or flags["rebase"]
        )
        if len(state.candidate) or options_passed:
            _error(
                state.console,
                f"transaction {unconfirmed[0].meta.txn_id} is awaiting confirmation; "
                f"run 'commit' with no args (or 'rollback pending') before committing new changes",
            )
            state.exit_code = 2
            return
        report = txn.confirm_pending(state.settings)
        render_report(state.console, report)
        if not report.ok:
            state.exit_code = 2
        return

    plan = txn.build_plan(state.ctx, state.registry, state.candidate)
    if plan.empty:
        _info(state.console, "no changes")
        return
    try:
        report = txn.commit(
            state.ctx,
            state.registry,
            plan,
            state.settings,
            confirm_minutes=confirm_minutes,
            force=flags["force"],
            override_template=flags["override_template"],
            allow_untransactional=flags["allow_untransactional"],
        )
    except txn.CommitError as exc:
        _error(state.console, str(exc))
        state.exit_code = 2
        return
    if report.ok:
        state.candidate.clear()
    else:
        state.exit_code = 2
    render_report(state.console, report)
    if report.ok and confirm_minutes is not None:
        _warn(state.console, f"commit within {confirm_minutes} minute(s) to keep changes")


def _cmd_rollback(tokens: list[str], state: ShellState) -> None:
    from pyecsdwan.cli.render import render_report

    if len(tokens) != 1:
        raise ValueError(_ROLLBACK_USAGE)
    if tokens[0] == "pending":
        pending = txn.pending_rollbacks(host=state.settings.host)
        if not pending:
            _info(state.console, "no pending transactions")
            return
        for orphan in pending:
            report = txn.revert_txn_dir(
                orphan.dir,
                reason="operator rollback pending",
                ctx=state.ctx,
                registry=state.registry,
            )
            render_report(state.console, report)
            if not report.ok:
                state.exit_code = 2
        return
    if not _INT_RE.match(tokens[0]):
        raise ValueError(_ROLLBACK_USAGE)
    report = txn.rollback_history_txn(state.ctx, state.registry, state.settings, int(tokens[0]))
    if not report.ok:
        state.exit_code = 2
    render_report(state.console, report)


# -- completion --------------------------------------------------------------


class ShellCompleter(Completer):
    """Mode-aware completion: first word, resource kinds, appliance names."""

    def __init__(self, state: ShellState) -> None:
        self.state = state

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterator[Completion]:
        text = document.text_before_cursor
        words = text.split()
        if text and not text[-1].isspace():
            current, prior = words[-1], words[:-1]
        else:
            current, prior = "", words
        try:
            options = self.next_tokens(prior)
        except Exception:  # noqa: BLE001 - completion must never break the prompt
            return
        for option in sorted(set(options)):
            if option.startswith(current):
                yield Completion(option, start_position=-len(current))

    def next_tokens(self, prior: list[str]) -> list[str]:
        """Valid next tokens after ``prior``.

        Walks the same tree the dispatcher does, one frame per token, so a
        position that offers something is a position the parser accepts —
        #74's "drive parsing, completion and help from one command definition"
        as far as two functions that must agree can get without a table.
        """
        mode = self.state.mode
        if not prior:
            return list(_CONFIG_COMMANDS if mode == MODE_CONFIG else _OPERATIONAL_COMMANDS)
        first, rest = prior[0], prior[1:]
        if first == "show":
            # Mode carries the intent: in config mode bare `show` is the
            # candidate and takes only `compare` (D-JUN-1), so offering kind
            # nouns there would complete a command that does not exist.
            if mode == MODE_CONFIG:
                return ["compare"] if not rest else []
            return self._show_options(rest)
        if first in ("set", "delete"):
            return self._ref_options(rest)
        if first == "commit" and mode == MODE_CONFIG:
            return [*_COMMIT_FLAGS, "confirm"]
        if first == "rollback" and mode == MODE_CONFIG and not rest:
            return ["pending"]
        return []

    def _show_options(self, rest: list[str]) -> list[str]:
        """Operational-mode ``show`` (grammar.md §2)."""
        if not rest:
            return [*_SHOW_CLI_STATE, *_SCOPE_NOUNS, "configuration"]
        head, tail = rest[0], rest[1:]
        if head == "configuration":
            return self._configuration_options(tail)
        if head == "fabric":
            if not tail:
                return list(_FABRIC_DOMAINS)
            # `flows` completes to its one subcommand; `flow` takes a free-form
            # address, so it deliberately offers nothing rather than suggesting
            # `summary` and inviting the two to be confused.
            if tail == ["flows"]:
                return ["summary"]
            return []
        if head == "appliance":
            if not tail:
                return self._appliance_names()
            if len(tail) == 1:
                return list(_APPLIANCE_DOMAINS)
            if tail[1] == "bgp":
                if len(tail) == 2:
                    return [*_BGP_VIEWS, "--stale-ok"]
                # Peer addresses are free-form, and offering `summary` after
                # `neighbors` would suggest a command that does not exist.
                return []
            return []
        if head == "transactions" and not tail:
            return ["pending"]
        return []

    def _configuration_options(self, rest: list[str]) -> list[str]:
        """``show configuration [running|candidate] ...``."""
        registry = self.state.registry
        if rest and rest[0] == "candidate":
            return []
        if rest and rest[0] == "running":
            rest = rest[1:]
            here = [*_SCOPE_NOUNS, *registry.cli_names(Scope.ORCHESTRATOR)]
        else:
            here = [*_DATASTORES, *_SCOPE_NOUNS, *registry.cli_names(Scope.ORCHESTRATOR)]
        if not rest:
            return here
        head, tail = rest[0], rest[1:]
        if head == "fabric":
            from pyecsdwan.reports import fabric

            return list(fabric.SECTIONS) if not tail else []
        if head == "appliance":
            if not tail:
                return self._appliance_names()
            if tail[-1] == "--format":
                return ["native"]
            if len(tail) == 1:
                return [*registry.cli_names(Scope.APPLIANCE), "--format"]
            if len(tail) == 2:
                return self._instance_names(tail[1], Scope.APPLIANCE, tail[0])
            return []
        return self._instance_names(head, Scope.ORCHESTRATOR, None) if not tail else []

    def _ref_options(self, rest: list[str]) -> list[str]:
        """``set``/``delete``: ``[appliance <name>] <kind> <instance> ...``.

        Scope is a position here exactly as it is in `show`, which is
        Principle IV's "one grammar across interfaces" at the token level.
        """
        registry = self.state.registry
        if not rest:
            return [*registry.cli_names(Scope.ORCHESTRATOR), "appliance"]
        head, tail = rest[0], rest[1:]
        if head == "appliance":
            if not tail:
                return self._appliance_names()
            if len(tail) == 1:
                return registry.cli_names(Scope.APPLIANCE)
            if len(tail) == 2:
                return self._instance_names(tail[1], Scope.APPLIANCE, tail[0])
            return []
        return self._instance_names(head, Scope.ORCHESTRATOR, None) if not tail else []

    def _instance_names(self, noun: str, scope: Scope, appliance: str | None) -> list[str]:
        """Instance names for a kind the operator has already named (#49, #76).

        Completion used to stop at the kind noun, so the only way to learn an
        instance name was to run the command without one and read the names out
        of the error message — which for an appliance-scoped singleton listed
        the same name once per appliance and so named nothing at all (#78).

        Scoped through the same :func:`scoped_instances` the command itself
        uses, so TAB offers exactly the set the command will accept.
        """
        registry = self.state.registry
        try:
            resource = registry.get(registry.resolve_cli(noun, scope))
        except UnknownKind:
            # Includes an appliance-scoped noun at the bare position: it does
            # not resolve in Orchestrator scope, and the command would reject
            # that form anyway, so there is nothing to offer.
            return []
        # A kind whose list_refs() reaches the Orchestrator costs one GET per
        # completion. That is the same call the command itself is about to
        # make, and completion here is explicit (complete_while_typing=False),
        # so the operator asked for it.
        try:
            refs = scoped_instances(resource, self.state.ctx, appliance)
        except Exception:  # noqa: BLE001 - see _appliance_names: TAB must never break the prompt
            # Deliberately local rather than leaning on get_completions'
            # catch-all, so `_options` stays total for every caller — the
            # contextual `?` help of #49 reads the same tree. An unreachable
            # Orchestrator and a resource that cannot address its instances
            # both land here; the operator gets the real message from the
            # command, where there is somewhere to print it.
            return []
        return [r.name for r in refs]

    def _appliance_names(self) -> list[str]:
        try:
            return self.state.ctx.resolver.appliance_names()
        except Exception:  # noqa: BLE001 - resolver/cache/API may be offline; degrade to no completions
            return []


# -- entry point -------------------------------------------------------------


def print_banner(state: ShellState) -> None:
    console = state.console
    console.print(
        f"pyecsdwan {__version__} — EdgeConnect SD-WAN transactional shell",
        style="bold",
        markup=False,
        highlight=False,
    )
    _info(console, f"orchestrator: {state.settings.host}")
    try:
        pending = txn.pending_rollbacks()
    except Exception:  # noqa: BLE001 - a corrupt journal must not block shell startup
        pending = []
    if pending:
        _warn(
            console,
            f"{len(pending)} orphaned unconfirmed transaction(s) — run 'rollback pending'",
        )


def run_shell(ctx: Ctx, registry: Registry, settings: config.Settings) -> int:
    """Interactive REPL entry point (called by ``cli.main``). Returns exit code."""
    state = build_state(ctx, registry, settings)
    print_banner(state)

    state_root = config.state_root()
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(state_root / "shell_history")),
        completer=ShellCompleter(state),
        complete_while_typing=False,
    )

    while state.running:
        prompt = PROMPT_CONFIG if state.mode == MODE_CONFIG else PROMPT_OPERATIONAL
        try:
            line = session.prompt(prompt)
        except KeyboardInterrupt:
            continue  # Ctrl-C drops the current line; the shell stays up
        except EOFError:
            if state.mode == MODE_CONFIG:
                _leave_config(state)  # Ctrl-D in config mode == `exit`
                continue
            state.running = False
            break
        dispatch(line, state)
    return state.exit_code


__all__ = [
    "ShellCompleter",
    "ShellState",
    "build_state",
    "coerce_value",
    "dispatch",
    "dispatch_config",
    "dispatch_operational",
    "print_banner",
    "run_shell",
]
