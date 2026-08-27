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
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.table import Table

from pyecsdwan import __version__, config, journal, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ctx, Ref, Scope
from pyecsdwan.registry import Registry
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
_SHOW_SPECIALS: tuple[str, ...] = (
    "appliances",
    "coverage",
    "flow",
    "flows",
    "journal",
    "run",
    "transactions",
    "version",
)
#: CLI token -> txn.commit() keyword argument.
_COMMIT_FLAGS: dict[str, str] = {
    "force": "force",
    "override-template": "override_template",
    "allow-untransactional": "allow_untransactional",
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
_SHOW_OPERATIONAL_USAGE = (
    "usage: show <appliances | journal | coverage | version | transactions pending | "
    "run appliance <name> | flows summary | flow <ip> | <kind> [<name>]>"
)
# `show flow` and `show flows` differ by one character and mean different
# things, so each insists on its own shape rather than guessing at the other.
_SHOW_FLOWS_USAGE = "usage: show flows summary"
_SHOW_FLOW_USAGE = "usage: show flow <ip>[/<prefix>]"
_ROLLBACK_USAGE = "usage: rollback <n>  |  rollback pending"
_SHOW_GENERIC_USAGE = "usage: show [appliance <name>] <kind> [<instance>]"
#: #55 adds the fabric-wide bare `show run`; until then the only form is the
#: per-appliance one, and this string is what names it.
_SHOW_RUN_USAGE = "usage: show run appliance <name> [<name>...]"


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
    console.print(message, style="bold red", markup=False, highlight=False)


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
    kind, name, rest = args[0], args[1], args[2:]
    resource = state.registry.get(kind)  # raises UnknownKind with a known-kinds hint
    if appliance is not None and resource.scope is not Scope.APPLIANCE:
        raise ValueError(
            f"{kind} is {resource.scope.value}-scope; omit the 'appliance' form: "
            f"{kind} <name> ..."
        )
    if appliance is None and resource.scope is Scope.APPLIANCE:
        raise ValueError(
            f"{kind} is appliance-scope; use: appliance <appliance-name> {kind} <name> ..."
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
                f"force, override-template, allow-untransactional"
            )
    return confirm_minutes, flags


# -- dispatch ----------------------------------------------------------------


def dispatch(line: str, state: ShellState) -> None:
    """Route one command line to the dispatcher for the current mode."""
    if state.mode == MODE_CONFIG:
        dispatch_config(line, state)
    else:
        dispatch_operational(line, state)


def dispatch_operational(line: str, state: ShellState) -> None:
    """Execute one operational-mode line; command errors print red, never raise."""
    tokens = _tokenize(line, state.console)
    if not tokens:
        return
    try:
        _run_operational(tokens, state)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - dispatch boundary: any command failure becomes a red line; the shell survives
        _error(state.console, _format_error(exc))


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
    if not args:
        raise ValueError(_SHOW_OPERATIONAL_USAGE)
    head = args[0]
    if head == "appliances":
        _show_appliances(state)
    elif head == "journal":
        from pyecsdwan.cli.render import render_journal_table

        render_journal_table(state.console, journal.list_txns())
    elif head == "transactions":
        if args[1:] != ["pending"]:
            raise ValueError("usage: show transactions pending")
        _show_pending(state)
    elif head == "coverage":
        _show_coverage(state)
    elif head == "version":
        _show_version(state)
    elif head == "flows":
        _show_flows_summary(args[1:], state)
    elif head == "flow":
        _show_flow(args[1:], state)
    elif head == "run":
        _show_run(args[1:], state)
    else:
        _show_generic(args, state)


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
def _show_run(args: list[str], state: ShellState) -> None:
    """``show run appliance <name> [<name>...]`` — appliance CLI running-config.

    Read-only: the command sent to each appliance is the vetted constant in
    :mod:`pyecsdwan.reports.applianceconfig`; nothing here touches the
    candidate, the journal or the transaction engine.

    **Seam for #55.** The fabric-wide bare ``show run`` lands in the ``if not
    args`` branch below: replace the usage error with the fabric report and no
    other line in this module changes.
    """
    from pyecsdwan.reports import applianceconfig

    if not args:
        # <- #55: fabric-wide `show run` goes here.
        raise ValueError(_SHOW_RUN_USAGE)
    if args[0] != "appliance" or len(args) < 2:
        raise ValueError(_SHOW_RUN_USAGE)
    names = args[1:]
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


def _show_generic(args: list[str], state: ShellState) -> None:
    """``show [appliance <name>] <kind> [<instance>]`` — fetch + normalize one
    resource and print it as YAML. With no instance name, enumerate the kind's
    known instances via list_refs() (a lone instance is shown directly)."""
    appliance: str | None = None
    if args and args[0] == "appliance":
        # Need at least `appliance <name> <kind>` — an incomplete prefix (just
        # "appliance", or "appliance <name>") falls through to the generic
        # usage error below rather than being silently mistaken for a kind or
        # swallowing the next token as an appliance name.
        if len(args) < 3:
            raise ValueError(_SHOW_GENERIC_USAGE)
        appliance = args[1]
        args = args[2:]
    if not args or len(args) > 2:
        raise ValueError(_SHOW_GENERIC_USAGE)
    kind = args[0]
    resource = state.registry.get(kind)
    if appliance is not None and resource.scope.value != "appliance":
        # Mirrors the symmetric check in _parse_ref (set/delete): an
        # orchestrator-scoped kind silently ignoring an `appliance <name>`
        # prefix produced confusing "(not present)"-style results instead of
        # a clear rejection (#48).
        raise ValueError(
            f"{kind} is {resource.scope.value}-scope; omit the 'appliance' form: "
            f"show {kind} [<instance>]"
        )

    if len(args) == 2:
        instance = args[1]
    else:
        refs = list(resource.list_refs(state.ctx))
        if len(refs) == 1:
            instance = refs[0].name
            appliance = appliance or refs[0].appliance
        elif not refs:
            raise ValueError(f"{kind}: name required (no instances to enumerate)")
        else:
            names = ", ".join(r.name for r in refs)
            raise ValueError(f"{kind}: name required; instances: {names}")

    if resource.scope.value == "appliance" and appliance is None:
        raise ValueError(f"{kind} is appliance-scoped; use 'show appliance <name> {kind} ...'")
    ref = Ref(kind=kind, name=instance, appliance=appliance)
    canonical = resource.normalize(resource.fetch(state.ctx, ref))
    state.console.print(f"# {ref}", style="dim", markup=False, highlight=False)
    if canonical is None:
        _info(state.console, "(not present)")
        return
    text = yaml.safe_dump(canonical, sort_keys=True, default_flow_style=False)
    state.console.print(text.rstrip("\n"), markup=False, highlight=False)


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
            options = self._options(prior)
        except Exception:  # noqa: BLE001 - completion must never break the prompt
            return
        for option in sorted(set(options)):
            if option.startswith(current):
                yield Completion(option, start_position=-len(current))

    def _options(self, prior: list[str]) -> list[str]:
        mode = self.state.mode
        if not prior:
            return list(_CONFIG_COMMANDS if mode == MODE_CONFIG else _OPERATIONAL_COMMANDS)
        first = prior[0]
        if first in ("set", "delete", "show"):
            return self._target_options(first, prior)
        if first == "commit" and mode == MODE_CONFIG:
            return [*_COMMIT_FLAGS, "confirm"]
        if first == "rollback" and mode == MODE_CONFIG and len(prior) == 1:
            return ["pending"]
        return []

    def _target_options(self, first: str, prior: list[str]) -> list[str]:
        kinds = self.state.registry.kinds()
        if len(prior) == 1:
            options = [*kinds, "appliance"]
            if first == "show" and self.state.mode == MODE_OPERATIONAL:
                options.extend(_SHOW_SPECIALS)
            return options
        if prior[1] == "appliance" and first in ("set", "delete", "show"):
            if len(prior) == 2:
                return self._appliance_names()
            if len(prior) == 3:
                return kinds
        if first == "show" and prior[1] == "transactions" and len(prior) == 2:
            return ["pending"]
        # `flows` completes to its one subcommand; `flow` takes a free-form
        # address, so it deliberately offers nothing rather than suggesting
        # `summary` and inviting the two to be confused.
        if first == "show" and prior[1] == "flows" and len(prior) == 2:
            return ["summary"]
        if first == "show" and prior[1] == "run":
            # `show run appliance <name>` (#56); #55's bare `show run` adds no
            # completions of its own.
            if len(prior) == 2:
                return ["appliance"]
            if prior[2] == "appliance":
                return self._appliance_names()
        return []

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
