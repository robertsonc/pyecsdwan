"""``ec-cli`` — Typer entrypoint over the transactional core.

Command surface (Junos-flavored): ``set``/``delete``/``load`` stage into the
candidate store, ``diff`` plans, ``commit`` applies with optional
commit-confirm, ``rollback`` restores from the journal, ``show`` inspects,
``api`` is the journaled Tier-0 passthrough.

Bootstrap is lazy: the Orchestrator client is only built when a command needs
it, so ``--help`` and pure-local commands stay fast and offline-safe.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import logging
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import structlog
import typer
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

from pyecsdwan import (
    audit,
    config,
    desired,
    evidence,
    locking,
    redaction,
    runtime,
    specs,
    txn,
    vault,
)
from pyecsdwan import journal as journal_mod
from pyecsdwan import registry as registry_mod
from pyecsdwan import retry as retry_mod
from pyecsdwan.candidate import (
    CandidateCorruptError,
    CandidateFormatError,
    CandidateStore,
    IntentSource,
)
from pyecsdwan.cli import fanout, outcomes, reference, render
from pyecsdwan.cli.outcomes import Outcome
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import Ctx, Ref, Resource, Reversibility, Scope, Tier
from pyecsdwan.journal import TxnJournal, TxnState, list_txns
from pyecsdwan.registry import Registry, UnknownKind, default_registry
from pyecsdwan.reports import DEFAULT_CONCURRENCY, applianceconfig, drift, fabric, versions
from pyecsdwan.reports import flows as flows_report
from pyecsdwan.resolver import ResolveError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="Transactional CLI for HPE Aruba EdgeConnect SD-WAN.",
    pretty_exceptions_show_locals=False,
)
class _KindFallbackGroup(TyperGroup):
    """A Typer group whose unmatched subcommand name is a resource noun.

    `show configuration <kind> [<instance>]` puts the kind where Click expects
    a fixed subcommand, and there are 43 of them. Registering one command each
    would make `--help` a wall and startup a registry walk; refusing the form
    would mean the scriptable CLI does not speak the approved grammar. So an
    unrecognised name falls through to the hidden `_kind` command with the name
    pushed back onto its arguments.

    Real subcommands still win — `fabric`, `appliance` and `candidate` are
    matched first — which is exactly why those words are reserved and cannot be
    a kind's CLI name (`contract.RESERVED_CLI_WORDS`).
    """

    # Typed as Any: Typer 0.27 vendors Click's Context/Command under a private
    # module, so naming them here would either import that private module or
    # fail --strict against Click's own types, which are a different class.
    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        name, command, rest = super().resolve_command(ctx, args)
        if command is not None and command.name == "_kind" and name not in self.commands:
            return name, command, [name, *rest]
        return name, command, rest

    def get_command(self, ctx: Any, name: str) -> Any:
        return super().get_command(ctx, name) or super().get_command(ctx, "_kind")


show_app = typer.Typer(help="Read-only views of fabric and CLI state.")
cache_app = typer.Typer(help="Resolver cache maintenance.")
plugin_app = typer.Typer(help="Resource-plugin tooling (promotion checklist).")
flows_app = typer.Typer(help="Active-flow reports (see also: show fabric flow <ip>).")
#: Operational state (grammar.md §2): live reads across the fabric.
fabric_app = typer.Typer(help="Operational state of the whole fabric.")
#: Configuration, at a named datastore. `running` is the default and need not
#: be typed; `candidate` is never implicit.
configuration_app = typer.Typer(
    cls=_KindFallbackGroup,
    help="Configuration — running by default, candidate when named.",
    invoke_without_command=True,
)
#: `running` written explicitly. The same subtree, registered from the same
#: functions rather than defined twice, because Decision 1 says the two
#: spellings mean exactly the same thing and a parallel definition is a place
#: for them to stop meaning it.
running_app = typer.Typer(
    cls=_KindFallbackGroup,
    help="The live datastore — the default, written out.",
    invoke_without_command=True,
)
app.add_typer(show_app, name="show")
app.add_typer(cache_app, name="cache")
app.add_typer(plugin_app, name="plugin")
show_app.add_typer(fabric_app, name="fabric")
show_app.add_typer(configuration_app, name="configuration")
fabric_app.add_typer(flows_app, name="flows")

_API_METHODS = ("get", "post", "put", "delete")

#: Set by the app callback; controls traceback visibility in main().
_DEBUG = False


@dataclasses.dataclass
class _State:
    """Per-invocation global options plus the lazily built runtime bundle."""

    orch_url: str | None = None
    insecure: bool = False
    debug: bool = False
    mock: int | None = None
    booted: tuple[Ctx, Registry, config.Settings] | None = None


def _state(ctx: typer.Context) -> _State:
    return cast(_State, ctx.obj)


def _fail(message: str) -> NoReturn:
    err_console.print(Text(f"error: {message}", style="bold red"))
    raise typer.Exit(2)


def _configure_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def _startup_scan(origin: str) -> None:
    """Warn (never block) about orphaned unconfirmed transactions for ORIGIN."""
    orphans = txn.pending_rollbacks(origin=origin)
    if orphans:
        err_console.print(
            Text(
                f"warning: {len(orphans)} orphaned unconfirmed transaction(s) found — "
                f"run 'ec-cli rollback --pending' to restore",
                style="yellow",
            )
        )


def _bootstrap(state: _State) -> tuple[Ctx, Registry, config.Settings]:
    if state.booted is None:
        try:
            ctx, registry, settings = runtime.bootstrap(
                orch_url=state.orch_url, insecure=state.insecure, mock=state.mock is not None
            )
        except RuntimeError as exc:
            _fail(str(exc))
        state.booted = (ctx, registry, settings)
        _startup_scan(settings.origin)
    return state.booted


def _registry_only() -> Registry:
    """Populated registry without an Orchestrator connection (offline-safe)."""
    import pyecsdwan.resources  # noqa: F401 - importing registers the built-in plugins

    return default_registry


def _run_shell(state: _State) -> None:
    try:
        shell_module = importlib.import_module("pyecsdwan.cli.shell")
    except ImportError:
        _fail("shell not available yet")
    run_shell = getattr(shell_module, "run_shell", None)
    if run_shell is None:
        _fail("shell not available yet (pyecsdwan.cli.shell.run_shell missing)")
    ctx, registry, settings = _bootstrap(state)
    result = run_shell(ctx, registry, settings)
    code = result if isinstance(result, int) else 0
    if code:
        raise typer.Exit(code)


# -- staging helpers ----------------------------------------------------------


def _coerce_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    # Only plain base-10 integers; avoid int()'s acceptance of '1_000',
    # surrounding whitespace, and unicode digits, so the CLI and shell coerce
    # identically and a stored candidate value is predictable.
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    return raw


#: Operational domains under `show appliance NAME`. One so far (specs/002).
APPLIANCE_DOMAINS: tuple[str, ...] = ("bgp",)
#: `bgp` leaves. `routes` is listed and answers `unsupported`: no BGP
#: route-table endpoint exists in either vendored baseline, and hiding it
#: would make the CLI look like it never considered the question.
BGP_VIEWS: tuple[str, ...] = ("summary", "neighbors", "routes")


def _resolve_kind(registry: Registry, token: str, appliance: str | None) -> str:
    """User-facing noun -> internal registry kind (issue #77), as the shell does it.

    The shell learned this and the scriptable CLI did not, so `banners` worked
    at the prompt and `plugin promote banners` answered "unknown resource
    kind" — then listed the registry keys, which is the leak #77 is about.
    Principle IV is one grammar across interfaces, so both surfaces resolve a
    token the same way, including the scope rule: naming an appliance selects
    appliance scope, naming none selects the Orchestrator. A token that does
    not resolve there is retried in the other scope so the caller can say
    "that is appliance-scope, pass --appliance" — or its mirror — rather than
    "unknown kind".

    The unknown-token error lists *every* noun, unlike the shell's, which lists
    the position's. Here scope is a flag rather than a position, so both scopes
    are reachable without moving the token, and narrowing the list would hide
    the noun the operator meant behind a flag they have not typed yet.
    """
    scope = Scope.APPLIANCE if appliance is not None else Scope.ORCHESTRATOR
    other = Scope.ORCHESTRATOR if scope is Scope.APPLIANCE else Scope.APPLIANCE
    try:
        return registry.resolve_cli(token, scope)
    except UnknownKind:
        try:
            return registry.resolve_cli(token, other)
        except UnknownKind:
            known = ", ".join(sorted(set(registry.cli_names()))) or "(none)"
            _fail(f"unknown resource kind {token!r}; known kinds: {known}")


_YES_OPTION = typer.Option(
    "--yes", "-y", help="Skip the fan-out cost prompt (grammar.md Decision 7)."
)


def _gate_fanout(rt_ctx: Ctx, assume_yes: bool, calls_each: int = 1) -> None:
    """Ask, or warn, before a command that calls every appliance.

    Declining is not a failure: the operator was asked and said no, so it
    exits 0 with a line saying nothing ran. Exiting non-zero would make a
    deliberate "not now" indistinguishable from the command breaking.
    """
    try:
        fanout.confirm(
            rt_ctx, console=console, err_console=err_console, assume_yes=assume_yes,
            calls_each=calls_each,
        )
    except fanout.FanoutDeclined:
        console.print(Text("cancelled: nothing was queried", style="dim"))
        raise typer.Exit(0) from None


def _resource_for(registry: Registry, token: str, appliance: str | None = None) -> Resource:
    return registry.get(_resolve_kind(registry, token, appliance))


def _validated_ref(registry: Registry, kind: str, name: str, appliance: str | None) -> Ref:
    """Scope-check an already-resolved kind and build the ref."""
    resource = registry.get(kind)
    noun = registry.cli_name(kind)
    if resource.scope is Scope.APPLIANCE and appliance is None:
        _fail(f"{noun} is appliance-scoped; pass --appliance NAME")
    if resource.scope is Scope.ORCHESTRATOR and appliance is not None:
        _fail(f"{noun} is orchestrator-scoped; --appliance does not apply")
    return Ref(kind=kind, name=name, appliance=appliance)


def _make_ref(registry: Registry, token: str, name: str, appliance: str | None) -> Ref:
    return _validated_ref(registry, _resolve_kind(registry, token, appliance), name, appliance)


# -- global callback ----------------------------------------------------------


@app.callback(invoke_without_command=True)
def cli(
    ctx: typer.Context,
    orch_url: Annotated[
        str | None,
        typer.Option("--orch-url", envvar=config.ENV_ORCH_URL, help="Orchestrator base URL."),
    ] = None,
    insecure: Annotated[
        bool,
        typer.Option("--insecure", help="Skip TLS certificate verification (lab gear only)."),
    ] = False,
    mock: Annotated[
        int | None,
        typer.Option(
            "--mock",
            metavar="PORT",
            help="Connect to a locally running mock orchestrator at http://127.0.0.1:PORT.",
        ),
    ] = None,
    debug: Annotated[
        bool, typer.Option("--debug", help="Verbose structlog output to stderr.")
    ] = False,
) -> None:
    """Transactional CLI for HPE Aruba EdgeConnect SD-WAN."""
    global _DEBUG
    _DEBUG = debug
    _configure_logging(debug)
    state = _State(orch_url=orch_url, insecure=insecure, debug=debug, mock=mock)
    if mock is not None:
        # Plain-http to loopback; runtime.bootstrap supplies a sentinel key so
        # the demo path works without a real credential.
        state.orch_url = f"http://127.0.0.1:{mock}"
    ctx.obj = state
    if ctx.invoked_subcommand is None:
        _run_shell(state)


# -- commands -----------------------------------------------------------------


@app.command()
def shell(ctx: typer.Context) -> None:
    """Interactive shell (also the default when no subcommand is given)."""
    _run_shell(_state(ctx))


@app.command("set")
def set_(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Resource kind (see 'show coverage').")],
    name: Annotated[str, typer.Argument(help="Resource instance name.")],
    args: Annotated[
        list[str],
        typer.Argument(metavar="PATH... VALUE", help="Path segments followed by the value."),
    ],
    appliance: Annotated[
        str | None,
        typer.Option("--appliance", help="Appliance name for appliance-scoped kinds."),
    ] = None,
) -> None:
    """Stage one value into the candidate changeset (merge semantics)."""
    state = _state(ctx)
    _rt, registry, settings = _bootstrap(state)
    if len(args) < 2:
        _fail("need at least one PATH segment and a VALUE (usage: set KIND NAME PATH... VALUE)")
    ref = _make_ref(registry, kind, name, appliance)
    path, raw_value = list(args[:-1]), args[-1]
    value = _coerce_value(raw_value)
    candidate = CandidateStore(settings.origin)
    candidate.set_path(ref, path, value)
    console.print(f"staged: {ref.key()} {'.'.join(path)} = {value!r}")


@app.command()
def delete(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Resource kind (see 'show coverage').")],
    name: Annotated[str, typer.Argument(help="Resource instance name.")],
    path: Annotated[
        list[str] | None,
        typer.Argument(metavar="[PATH...]", help="Subtree to delete; omit for whole resource."),
    ] = None,
    appliance: Annotated[
        str | None,
        typer.Option("--appliance", help="Appliance name for appliance-scoped kinds."),
    ] = None,
) -> None:
    """Stage a delete: the whole resource, or one subtree when PATH is given."""
    state = _state(ctx)
    _rt, registry, settings = _bootstrap(state)
    ref = _make_ref(registry, kind, name, appliance)
    candidate = CandidateStore(settings.origin)
    candidate.delete(ref, list(path) if path else None)
    suffix = f" {'.'.join(path)}" if path else ""
    console.print(f"staged delete: {ref.key()}{suffix}")


@app.command()
def load(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Resource kind (see 'show coverage').")],
    name: Annotated[str, typer.Argument(help="Resource instance name.")],
    file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True, help="YAML desired-state file."),
    ],
    appliance: Annotated[
        str | None,
        typer.Option("--appliance", help="Appliance name for appliance-scoped kinds."),
    ] = None,
    merge: Annotated[
        bool,
        typer.Option("--merge", help="Deep-merge over current state instead of full replace."),
    ] = False,
) -> None:
    """Load desired state for one resource from a YAML file into the candidate."""
    state = _state(ctx)
    _rt, registry, settings = _bootstrap(state)
    ref = _make_ref(registry, kind, name, appliance)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"cannot read {file}: {exc}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        _fail(f"invalid YAML in {file}: {exc}")
    if not isinstance(data, dict):
        _fail(f"{file}: top-level YAML value must be a mapping")
    candidate = CandidateStore(settings.origin)
    if merge:
        for key, value in data.items():
            candidate.set_path(ref, [str(key)], value)
    else:
        # Replace mode is a full overwrite: a section the file omits will be
        # normalized to empty and DELETED on apply. Warn if the file is a
        # strict subset of the resource's known top-level sections so the
        # operator isn't surprised by a silent wipe (they still see it in
        # `show | compare`, but a heads-up here is cheap).
        resource = registry.get(ref.kind)
        try:
            template = resource.normalize(None)
        except Exception:  # noqa: BLE001 - a normalize() that needs real state just skips the hint
            template = None
        if isinstance(template, dict):
            missing = [k for k in template if k not in data]
            if missing:
                err_console.print(
                    Text(
                        f"warning: {file} omits section(s) {', '.join(missing)}; replace mode "
                        f"will remove them. Use --merge, or add them explicitly to keep them.",
                        style="yellow",
                    )
                )
        candidate.set_desired(ref, data)
    mode = "merge" if merge else "replace"
    console.print(f"loaded {ref.key()} from {file} ({mode} mode, {len(data)} top-level key(s))")


@app.command("diff")
def diff_(ctx: typer.Context) -> None:
    """Compare candidate against live state; exit 1 when changes exist (CI drift check)."""
    state = _state(ctx)
    rt_ctx, registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.origin)
    plan = txn.build_plan(rt_ctx, registry, candidate)
    render.render_plan(console, plan)
    raise typer.Exit(0 if plan.empty else 1)


app.command("compare", help="Alias for 'diff'.")(diff_)


@app.command("apply")
def apply_(
    ctx: typer.Context,
    from_dir: Annotated[
        Path,
        typer.Option("--from", help="Desired-state directory to apply."),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show the plan and exit 1 if it would change anything. Writes nothing.",
        ),
    ] = False,
    confirm_minutes: Annotated[
        float | None,
        typer.Option(
            "--confirm-minutes",
            min=0.02,
            help="Auto-rollback unless confirmed within this many minutes (fractions allowed).",
        ),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Allow IRREVERSIBLE changes.")] = False,
    override_template: Annotated[
        bool,
        typer.Option("--override-template", help="Allow changes to template-managed sections."),
    ] = False,
    allow_untransactional: Annotated[
        bool,
        typer.Option(
            "--allow-untransactional",
            help="Allow sub-curated resources inside a commit-confirm window.",
        ),
    ] = False,
    rebase: Annotated[
        bool,
        typer.Option(
            "--rebase",
            help="Re-merge declared intent over current state when it moved, instead of refusing.",
        ),
    ] = False,
) -> None:
    """Apply a desired-state directory as one transaction (epic #8).

    The same planner, the same guards and the same journal as `commit` — only
    the intent comes from git instead of the candidate. Ownership, shared
    write targets, drift-since-compare and reversibility are all enforced
    exactly as they are for a hand-staged change, because it is literally the
    same code path.

    `--dry-run` is the CI form: it writes nothing and exits 1 if applying would
    change anything.
    """
    state = _state(ctx)
    # Read and validate the directory *before* a client exists (R2). Invalid
    # or empty input is an offline error: it should cost no credentials, no
    # resolver call, and no round trip to an Orchestrator to find out that a
    # path was mistyped.
    try:
        declared = desired.load(_registry_only(), from_dir)
    except desired.DesiredError as exc:
        _fail(str(exc))

    # The loader is done (T7); safe materialization is not (T8). A declaration
    # is *typed partial intent* under the ratified spec (D7), and no resource
    # has yet proved it can build a complete target without erasing unknown,
    # unmodeled or write-only fields (D8/R12). Until it has, this path would
    # send the declaration as a full replacement — so it refuses rather than
    # writing under semantics the spec does not sanction. `--dry-run` is left
    # open: it writes nothing, and seeing the plan is how you find out whether
    # the directory says what you meant.
    if not dry_run:
        _fail(
            "apply --from is not enabled yet: the declaration format is ratified "
            "(T7) but per-resource safe materialization is not (T8), so writing a "
            "partial declaration would replace fields nobody declared. Use "
            "`--dry-run` to see the plan, or `ec-cli drift --from` to compare"
        )
    commit_only = [
        name
        for name, given in (
            ("--confirm-minutes", confirm_minutes is not None),
            ("--force", force),
            ("--override-template", override_template),
            ("--allow-untransactional", allow_untransactional),
            ("--rebase", rebase),
        )
        if given
    ]
    if commit_only:
        # Accepting a flag that cannot do anything is the lie this project
        # keeps removing: an operator who passed --confirm-minutes and saw a
        # plan would reasonably believe a confirm window was armed.
        _fail(
            f"{', '.join(commit_only)} only affect committing, and this command "
            f"can only preview until T8 lands"
        )
    rt_ctx, registry, settings = _bootstrap(state)

    # Refuse to mix two sources of intent in one transaction. A non-empty
    # candidate is someone's in-progress work; folding it into a declarative
    # apply would commit changes the directory never declared, and the operator
    # would have no way to see which came from where. Clearing it is their
    # call, not this command's.
    staged = CandidateStore(settings.origin)
    if len(staged):
        _fail(
            f"refusing: {len(staged)} item(s) staged in the candidate. A declarative "
            f"apply commits the directory, not your staged work, and mixing the two "
            f"in one transaction would hide which change came from where. "
            f"Run `ec-cli commit` first, or `ec-cli discard` to drop them."
        )

    console.print(
        Text(
            f"declared: {len(declared)} instance(s) from {from_dir} "
            f"({declared.digest[:12]})",
            style="dim",
        )
    )
    plan = txn.build_plan(rt_ctx, registry, declared)
    render.render_plan(console, plan)
    # Only the preview exists today. The commit half was removed rather than
    # left unreachable behind the T8 guard above: dead code that looks like a
    # working write path is how someone re-enables it without the per-resource
    # materialization proof it is waiting for. T13 restores it.
    #
    # Same exit convention as `diff`: nonzero means "this would change things",
    # which is what a CI gate keys on.
    raise typer.Exit(0 if plan.empty else 1)


@app.command("drift")
def drift_(
    ctx: typer.Context,
    from_dir: Annotated[
        Path | None,
        typer.Option(
            "--from",
            help="Desired-state directory to compare against, instead of the candidate.",
        ),
    ] = None,
    kind: Annotated[
        list[str] | None,
        typer.Option("--kind", help="Limit to these kinds (repeatable). Default: every kind."),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Overall deadline (seconds) for the fan-out."),
    ] = None,
    max_concurrency: Annotated[
        int,
        typer.Option("--max-concurrency", help="Reads in flight at once."),
    ] = DEFAULT_CONCURRENCY,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    assume_yes: Annotated[bool, _YES_OPTION] = False,
) -> None:
    """Fabric-wide drift: every instance of every kind, against staged intent.

    `diff` compares only what you staged, so an empty candidate reports no
    changes. This enumerates instead, and reports what `diff` had no reason to
    mention: instances nobody has declared, instances that could not be read,
    and kinds no curated resource can compare.

    `--from <dir>` compares against a desired-state directory in git rather
    than the local candidate, which is what a CI drift check wants: the same
    answer on every run, from a declaration someone reviewed.

    Exit 0 clean, 1 drift found, 8 the run was incomplete — and 8 outranks 1,
    because "no drift" from a report that skipped part of the fabric is a claim
    it has not earned.
    """
    state = _state(ctx)
    # Same ordering as `apply` (R2): the directory is validated offline, so a
    # wrong path fails before any credential or connection is needed.
    declared: desired.Declared | None = None
    if from_dir is not None:
        try:
            declared = desired.load(_registry_only(), from_dir)
        except desired.DesiredError as exc:
            # Fatal, never a warning: a partially-read declaration would report
            # the rest of the fabric as `undeclared` and look like a smaller
            # fabric rather than a broken input.
            _fail(str(exc))
    rt_ctx, registry, settings = _bootstrap(state)
    # Every kind, every instance: the heaviest read this CLI has. `list_refs`
    # for most appliance-scope kinds is itself a per-appliance call, so the
    # estimate counts the curated kinds rather than pretending it is one.
    intent: IntentSource
    if declared is not None:
        console.print(
            Text(
                f"declared: {len(declared)} instance(s) from {from_dir} "
                f"({declared.digest[:12]})",
                style="dim",
            )
        )
        intent = declared
    else:
        intent = CandidateStore(settings.origin)

    _gate_fanout(rt_ctx, assume_yes, calls_each=_drift_calls_each(registry, kind))
    report = drift.collect(
        rt_ctx,
        registry,
        intent,
        kinds=kind or None,
        concurrency=max_concurrency,
        timeout=timeout,
    )
    if as_json:
        console.print_json(json.dumps(report.as_json(), default=str))
    else:
        render.render_drift(console, report)
    raise typer.Exit(report.exit_code)


def _drift_calls_each(registry: Registry, kinds: list[str] | None) -> int:
    """Roughly how many calls this makes per appliance, for the cost prompt.

    Appliance-scope kinds only: an orchestrator-scope read is one call for the
    whole fabric, so counting it per appliance would inflate the estimate and
    train operators to ignore the warning.
    """
    wanted = kinds or list(registry.kinds())
    return max(
        1,
        sum(
            1
            for k in wanted
            if k in registry.kinds() and registry.get(k).scope is Scope.APPLIANCE
        ),
    )


@app.command()
def commit(
    ctx: typer.Context,
    confirm_minutes: Annotated[
        float | None,
        typer.Option(
            "--confirm-minutes",
            min=0.02,
            help="Auto-rollback unless confirmed within this many minutes (fractions allowed).",
        ),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Allow IRREVERSIBLE changes.")] = False,
    override_template: Annotated[
        bool,
        typer.Option("--override-template", help="Allow changes to template-managed sections."),
    ] = False,
    allow_untransactional: Annotated[
        bool,
        typer.Option(
            "--allow-untransactional",
            help=(
                "Allow sub-curated resources inside a commit-confirm window. "
                "No shipped tier reaches this: a Tier-1 stub's normalize() "
                "raises first, so the guard is belt-and-braces (#68)."
            ),
        ),
    ] = False,
    rebase: Annotated[
        bool,
        typer.Option(
            "--rebase",
            help=(
                "Re-merge staged intent over the server's current state when it "
                "moved since compare, instead of refusing."
            ),
        ),
    ] = False,
) -> None:
    """Apply the candidate changeset (a bare commit inside a confirm window confirms)."""
    state = _state(ctx)
    rt_ctx, registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.origin)
    unconfirmed = [
        t
        for t in list_txns()
        if t.meta.state == TxnState.APPLIED_UNCONFIRMED
        and journal_mod.targets(t, settings.origin)
    ]
    if unconfirmed:
        # A bare commit confirms the pending window. But if the user also
        # staged new changes or passed options, they meant a fresh commit —
        # refuse rather than silently confirming and dropping the new work.
        options_passed = bool(
            confirm_minutes or force or override_template or allow_untransactional or rebase
        )
        if len(candidate) or options_passed:
            _fail(
                f"transaction {unconfirmed[0].meta.txn_id} is awaiting confirmation; "
                f"run 'ec-cli confirm' (or 'ec-cli rollback --pending') before "
                f"committing new changes"
            )
        report = txn.confirm_pending(settings)
        render.render_report(console, report)
        raise typer.Exit(0 if report.ok else 2)
    # One shared cycle with the shell: snapshot before planning, acknowledge
    # per item afterwards. Duplicating it is what left the shell deleting
    # another writer's staged work after this path was fixed (#63).
    outcome = txn.commit_candidate(
        rt_ctx,
        registry,
        candidate,
        settings,
        on_plan=lambda plan: render.render_plan(console, plan),
        confirm_minutes=confirm_minutes,
        force=force,
        override_template=override_template,
        allow_untransactional=allow_untransactional,
        rebase=rebase,
    )
    if outcome.report is None:
        console.print("no changes")
        raise typer.Exit(0)
    _report_kept(outcome.kept)
    render.render_report(console, outcome.report)
    raise typer.Exit(0 if outcome.report.ok else 2)


def _report_kept(kept: Sequence[str]) -> None:
    """Name items another writer changed while this commit ran.

    Kept rather than acknowledged, and said out loud: an operator who expects
    a clean candidate after a successful commit needs to know why it is not.
    """
    if not kept:
        return
    console.print(
        Text(
            f"kept {len(kept)} candidate item(s) changed since this commit was "
            f"planned: {', '.join(sorted(kept))}",
            style="yellow",
        )
    )


@app.command()
def confirm(ctx: typer.Context) -> None:
    """Confirm the pending commit-confirm transaction (stops the watchdog)."""
    state = _state(ctx)
    _rt, _registry, settings = _bootstrap(state)
    report = txn.confirm_pending(settings)
    render.render_report(console, report)
    raise typer.Exit(0 if report.ok else 2)


@app.command()
def discard(ctx: typer.Context) -> None:
    """Drop the entire candidate changeset."""
    state = _state(ctx)
    _rt, _registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.origin)
    count = len(candidate)
    candidate.clear()
    console.print(f"discarded {count} candidate item(s)")


@app.command()
def adopt(
    ctx: typer.Context,
    txn_id: Annotated[
        str | None,
        typer.Option("--txn", help="Bind one pre-#63 transaction to this Orchestrator."),
    ] = None,
    candidate: Annotated[
        bool,
        typer.Option("--candidate", help="Bind pre-#63 staged changes to this Orchestrator."),
    ] = False,
) -> None:
    """Bind local state written before origins were recorded to this Orchestrator.

    Older builds keyed state by a display hostname, which the http:// and
    https:// endpoints on one name — and every tenant path under it — share.
    Such state is still listed and still readable, but it cannot confirm,
    restore or commit anything, because nothing in it establishes which
    Orchestrator it belongs to and a hostname is not proof.

    You are the proof. Connect to the Orchestrator the state belongs to and
    run this; the choice is recorded in the journal's event log.

    With no options it reports what is adoptable and changes nothing.
    """
    state = _state(ctx)
    _rt, _registry, settings = _bootstrap(state)
    store = CandidateStore(settings.origin)
    unproven = [t for t in journal_mod.list_txns() if journal_mod.is_legacy(t)]

    if not txn_id and not candidate:
        _report_adoptable(settings, store, unproven)
        return

    if candidate:
        adopted = store.adopt_legacy()
        if adopted:
            console.print(
                f"adopted {len(adopted)} staged change(s) into {settings.origin}: "
                f"{', '.join(sorted(adopted))}"
            )
        else:
            console.print("no unadopted staged changes found")

    if txn_id:
        match = [t for t in unproven if t.meta.txn_id == txn_id]
        if not match:
            bound = [t for t in journal_mod.list_txns() if t.meta.txn_id == txn_id]
            if bound:
                _fail(
                    f"transaction {txn_id} is already bound to "
                    f"{bound[0].meta.orch_origin!r}; adoption cannot re-target a journal"
                )
            _fail(f"no unadopted transaction {txn_id!r} found")
        journal_mod.adopt(match[0], settings.origin)
        console.print(f"adopted transaction {txn_id} into {settings.origin}")


def _report_adoptable(
    settings: config.Settings, store: CandidateStore, unproven: list[Any]
) -> None:
    """The no-op form. Reporting before acting matters more here than usual:
    adoption is an assertion about provenance that cannot be checked, so the
    operator should see exactly what they would be claiming."""
    staged = store.legacy_pending()
    if not staged and not unproven:
        console.print("nothing to adopt: all local state records which Orchestrator it targets")
        return
    console.print(f"adoptable into {settings.origin}:")
    if staged:
        console.print(f"  staged changes ({len(staged)}): {', '.join(sorted(staged))}")
        console.print("    adopt with: ec-cli adopt --candidate")
    for t in unproven:
        console.print(
            f"  transaction {t.meta.txn_id} [{t.meta.state}] "
            f"recorded host {t.meta.orch_host!r}"
        )
        console.print(f"    adopt with: ec-cli adopt --txn {t.meta.txn_id}")
    console.print(
        "\nAdopt only what you know belongs to this Orchestrator: a hostname is "
        "shared by its http:// and https:// endpoints and by every tenant path under it."
    )


@app.command()
def rollback(
    ctx: typer.Context,
    n: Annotated[
        int,
        typer.Argument(min=1, help="History depth: 1 = most recent confirmed transaction."),
    ] = 1,
    pending: Annotated[
        bool,
        typer.Option("--pending", help="Restore orphaned unconfirmed transactions instead."),
    ] = False,
) -> None:
    """Restore prior state from the journal (Junos-style rollback)."""
    state = _state(ctx)
    rt_ctx, registry, settings = _bootstrap(state)
    if pending:
        orphans = txn.pending_rollbacks(origin=settings.origin)
        if not orphans:
            console.print("no orphaned transactions")
            return
        failures = 0
        for orphan in orphans:
            report = txn.revert_txn_dir(
                orphan.dir, reason="operator rollback --pending", ctx=rt_ctx, registry=registry
            )
            render.render_report(console, report)
            if not report.ok:
                failures += 1
        raise typer.Exit(2 if failures else 0)
    report = txn.rollback_history_txn(rt_ctx, registry, settings, n=n)
    render.render_report(console, report)
    raise typer.Exit(0 if report.ok else 2)


# -- show ---------------------------------------------------------------------


@show_app.command("appliances")
def show_appliances(ctx: typer.Context) -> None:
    """Appliance inventory (via the resolver cache)."""
    state = _state(ctx)
    rt_ctx, _registry, _settings = _bootstrap(state)
    rows = rt_ctx.resolver.appliances()
    extra = [key for key in ("site", "model") if any(key in row for row in rows)]
    table = Table(title=f"appliances ({len(rows)})")
    table.add_column("hostName")
    table.add_column("nePk")
    for key in extra:
        table.add_column(key)
    for row in rows:
        values = [str(row.get("hostName") or ""), str(row.get("nePk") or row.get("id") or "")]
        values += [str(row.get(key) or "") for key in extra]
        table.add_row(*values)
    console.print(table)


@show_app.command("commands")
def show_commands(
    intent: Annotated[
        str | None,
        typer.Option("--intent", help="Restrict to operational | configuration | cli-state."),
    ] = None,
    unsupported: Annotated[
        bool, typer.Option("--unsupported", help="Only the commands that cannot be answered.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Every command, with its intent, scope, mutability and support status.

    Offline: reads the plugin registry and the vendored baselines, never the
    Orchestrator. Deciding whether this tool can do a thing should not require
    credentials for a fabric.
    """
    registry = _registry_only()
    rows = reference.build(registry)
    if intent is not None:
        valid = {reference.OPERATIONAL, reference.CONFIGURATION, reference.CLI_STATE}
        if intent not in valid:
            _fail(f"unknown intent {intent!r}; valid: {', '.join(sorted(valid))}")
        rows = [r for r in rows if r.intent == intent]
    if unsupported:
        rows = [r for r in rows if r.support.startswith("unsupported")]
    if as_json:
        console.print_json(json.dumps([r.as_json() for r in rows]))
        return
    if not rows:
        # An empty filter result is an answer, and a different one from "there
        # are no commands" — say which filter emptied it (Principle II).
        console.print("(no commands match that filter)")
        return
    render.render_command_reference(console, rows)


@show_app.command("journal")
def show_journal(
    as_json: Annotated[
        bool, typer.Option("--json", help="Transaction summaries as a JSON document.")
    ] = False,
    as_events: Annotated[
        bool,
        typer.Option(
            "--events",
            help="Every journaled event as NDJSON — the audit-trail / SIEM export.",
        ),
    ] = False,
    txn_id: Annotated[
        str | None, typer.Option("--txn", help="Limit to one transaction id.")
    ] = None,
    include_snapshots: Annotated[
        bool,
        typer.Option(
            "--include-snapshots",
            help="Include snapshot bodies in --events, with secret-named "
            "fields masked (#106). Off by default: a snapshot is a whole "
            "server object and exporting is distribution.",
        ),
    ] = False,
) -> None:
    """All journaled transactions, newest first.

    `--events` streams the journal itself as NDJSON: one self-contained record
    per line, stamped with its transaction and Orchestrator, oldest first. That
    is the audit trail — who changed what, when, under which ownership
    decision, and whether it was confirmed or reverted.

    Snapshot bodies are redacted to a SHA-256 and a size unless
    `--include-snapshots` says otherwise, because the journal is 0600 for a
    reason and a log shipper is not.
    """
    # Flags first, before any disk walk: a rejected combination should not
    # depend on what happens to be in the journal.
    if as_events and as_json:
        _fail("--events is already newline-delimited JSON; drop --json")
    if include_snapshots and not as_events:
        # Silently ignoring it would be bad enough for any flag. For the one
        # flag whose whole job is to disclose config bodies, an operator who
        # typed it and saw no bodies would conclude they had been disclosed.
        _fail("--include-snapshots only applies to --events; a summary carries no bodies")

    # Journal directories too corrupt to open. `list_txns` drops them, so
    # without this an audit export would quietly describe a smaller journal
    # than the one on disk — the export equivalent of a torn page.
    corrupt = journal_mod.unreadable_txn_dirs()
    txns = list_txns()
    if txn_id is not None:
        txns = [t for t in txns if t.meta.txn_id == txn_id]
        if not txns:
            # Not an empty export: a pipeline asking for one transaction and
            # getting zero lines would record "nothing happened" for a typo.
            _fail(f"no journaled transaction {txn_id!r}")

    if as_events:
        if include_snapshots:
            # The one call here that distributes config bodies says so, the way
            # `api` announces Tier-0. On stderr: it must reach the operator's
            # terminal without reaching the file they are redirecting into.
            err_console.print(
                Text(
                    "including snapshot bodies — secret-named fields are "
                    "masked, but this stream carries whole appliance objects "
                    "and may contain credentials under names the redactor "
                    "does not recognise",
                    style="bold yellow",
                )
            )
        # Oldest first — it is a log. `list_txns` is newest-first for the table.
        records = audit.events(reversed(txns), include_snapshots=include_snapshots)
        for line in audit.to_ndjson(records):
            # soft_wrap: a wrapped NDJSON line is two records, neither of which
            # parses. Same reason `show configuration` prints raw.
            console.print(line, markup=False, highlight=False, soft_wrap=True)
        _warn_corrupt_journal(corrupt)
        return

    if as_json:
        console.print_json(
            json.dumps(
                {"transactions": audit.summaries(txns), "unreadable": corrupt},
                default=str,
            )
        )
        return

    render.render_journal_table(console, txns, corrupt)


def _warn_corrupt_journal(names: list[str]) -> None:
    """Same fact as the table's own warning, but for the NDJSON path — on
    stderr, so it can never land in a stream being piped to a log shipper."""
    if not names:
        return
    err_console.print(
        Text(
            f"warning: {len(names)} journal director(ies) could not be read and are "
            f"absent from this export: {', '.join(names)}",
            style="yellow",
        )
    )


def render_locks_table(out: Console, rows: list[locking.LockState]) -> None:
    """Render origin-scoped lock state (#63). Shared by the subcommand and the shell."""
    if not rows:
        out.print("no locks")
        return
    table = Table(title=f"locks ({len(rows)})")
    table.add_column("orchestrator")
    table.add_column("scope")
    table.add_column("state")
    table.add_column("holder")
    table.add_column("since")
    for row in rows:
        state_cell = Text("HELD", style="bold yellow") if row.held else Text("free", style="dim")
        owner = row.owner
        if owner is None:
            holder = _ABSENT
        elif row.held:
            holder = Text(owner.describe(), style="dim")
        else:
            # The file outlives the lock, so this is the *last* holder, not a
            # current one. Labelled, because an unlabelled pid reads as truth.
            holder = Text(f"(last: {owner.describe()})", style="dim")
        since = Text(owner.acquired_utc, style="dim") if owner is not None else _ABSENT
        # The file name carries a one-way digest, so it cannot be read back as
        # an identity. Shown only when no owner record says which target it is.
        which = row.origin if row.origin else Text(row.name, style="dim")
        if row.legacy:
            which = Text.assemble(which, ("  (pre-#63 name)", "dim yellow"))
        table.add_row(which, row.scope, state_cell, holder, since)
    out.print(table)
    if any(row.legacy for row in rows):
        out.print(
            Text(
                "Locks marked (pre-#63 name) are also taken by this build, so a process "
                "from before origin-keyed locks cannot run alongside one from after. "
                "Once no such process can still be running, deleting those files ends "
                "the barrier.",
                style="dim",
            )
        )


@show_app.command("locks")
def show_locks() -> None:
    """Host-scoped candidate/commit locks, and who holds them (#63).

    The lock file outlives the lock on the flock path, so "a file exists" is
    not an answer. Each row is probed by trying to take the lock.
    """
    render_locks_table(console, locking.active_locks())


@show_app.command("pending")
def show_pending(ctx: typer.Context) -> None:
    """Orphaned/unconfirmed transactions needing operator attention."""
    _rt, _registry, settings = _bootstrap(_state(ctx))
    # Read before the early return: "no pending transactions" is the most
    # dangerous place for a corrupt directory to hide, because it is the one
    # answer an operator acts on by going home.
    corrupt = journal_mod.unreadable_txn_dirs()
    orphans = txn.pending_rollbacks(origin=settings.origin)
    if not orphans:
        console.print("no pending transactions")
        _warn_corrupt_journal(corrupt)
        return
    render.render_journal_table(console, orphans, corrupt)


# -- show version: fabric version report (#57) --------------------------------
#
# Rendering only; the report itself is built in `pyecsdwan.reports.versions`.
# It lives here rather than in `cli/render.py` for the same reason
# `coverage_summary_line` does: the shell imports it back from this module, so
# the CLI and the prompt cannot drift into showing different things.

#: An appliance off the fleet baseline.
_SKEW_STYLE = "yellow"
#: A next boot that would move the appliance off its running partition.
_NEXT_BOOT_STYLE = "bold yellow"
_ABSENT = Text("-", style="dim")


def _partition_cell(partition: versions.Partition | None, style: str = "") -> Text:
    return Text(partition.label, style=style) if partition is not None else _ABSENT.copy()


def _version_table(report: versions.FabricVersions) -> Table:
    """One row per appliance: active partition, backup partition, next boot."""
    table = Table(title=f"appliance versions ({len(report.appliances)})")
    table.add_column("appliance")
    table.add_column("active")
    table.add_column("backup (fallback)")
    table.add_column("next boot")
    table.add_column("notes")
    for appliance in report.appliances:
        name = Text(appliance.hostname)
        if appliance.unreachable:
            # One dead branch box never costs the operator the rest of the
            # table — it degrades to a row carrying the reason.
            table.add_row(
                name,
                Text("unreachable", style="red"),
                _ABSENT.copy(),
                _ABSENT.copy(),
                Text(appliance.error, style="red"),
            )
            continue
        if not appliance.partitions:
            table.add_row(
                name,
                Text(versions.UNKNOWN, style="yellow"),
                _ABSENT.copy(),
                _ABSENT.copy(),
                Text("appliance reported no partitions", style="yellow"),
            )
            continue
        notes: list[str] = []
        skewed = report.is_outlier(appliance)
        if skewed:
            notes.append(f"version skew (fleet baseline {report.baseline_version})")
        next_boot = appliance.next_boot
        if next_boot is not None and appliance.next_boot_diverges:
            next_cell = Text(f"! {next_boot.label}", style=_NEXT_BOOT_STYLE)
            notes.append("reload changes the running partition")
        else:
            next_cell = _partition_cell(next_boot)
        table.add_row(
            name,
            _partition_cell(appliance.active, style=_SKEW_STYLE if skewed else ""),
            _partition_cell(appliance.backup),
            next_cell,
            Text("; ".join(notes), style=_SKEW_STYLE if notes else ""),
        )
    return table


def render_version_report(console: Console, report: versions.FabricVersions) -> None:
    """Orchestrator header, per-appliance table, then the two conditions this
    report exists to surface: fleet version skew and a divergent next boot."""
    if report.orchestrator_error:
        console.print(
            Text(f"Orchestrator version unknown: {report.orchestrator_error}", style="red")
        )
    else:
        # `current`, never `installed` — the latter is what is available to
        # upgrade to, not what is running.
        console.print(Text(f"Orchestrator {report.orchestrator}", style="bold"))
    if report.orchestrator_available:
        console.print(
            Text(
                f"orchestrator versions available: {', '.join(report.orchestrator_available)}",
                style="dim",
            )
        )
    if not report.appliances:
        console.print("no appliances")
        return
    console.print(_version_table(report))

    if report.skewed:
        outliers = ", ".join(a.hostname for a in report.appliances if report.is_outlier(a))
        console.print(
            Text(
                f"version skew: {len(report.active_versions)} active versions across the fleet "
                f"({', '.join(report.active_versions)}); baseline {report.baseline_version}, "
                f"off-baseline: {outliers}",
                style=_SKEW_STYLE,
            )
        )
    elif report.reachable:
        console.print(Text(f"fleet is uniform on {report.baseline_version}", style="green"))
    for appliance in report.divergent_next_boot:
        upcoming, active = appliance.next_boot, appliance.active
        if upcoming is None or active is None:
            continue
        console.print(
            Text(
                f"! {appliance.hostname}: next reload boots {upcoming.label}, "
                f"not the running {active.label}",
                style=_NEXT_BOOT_STYLE,
            )
        )
    if report.unreachable:
        console.print(
            Text(f"{len(report.unreachable)} appliance(s) unreachable", style="red")
        )
    if not report.cached:
        console.print(Text("read live from each appliance (--no-cache)", style="dim"))
    # #66: this is the one command that knows both versions, so it is where
    # "your fabric is outside the verified support matrix" belongs. Printed
    # once, from the versions the fabric just reported — not guessed from the
    # vendored spec baseline, which says what the code was written against
    # rather than what anyone has run it on.
    warning = evidence.version_warning(
        "" if report.orchestrator_error else report.orchestrator,
        report.baseline_version if report.reachable else "",
    )
    if warning:
        console.print(Text(warning, style="yellow"))


@fabric_app.command("version")
def show_version(
    ctx: typer.Context,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Read versions live from each appliance instead of the Orchestrator cache.",
        ),
    ] = False,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Overall deadline (seconds) for the appliance fan-out."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    assume_yes: Annotated[bool, _YES_OPTION] = False,
) -> None:
    """Orchestrator version, then every appliance's active and backup partitions.

    Read-only: two GETs and a bounded fan-out. Never touches the candidate
    config, the journal, or the transaction engine.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
    if no_cache:
        # The cached read is one Orchestrator call; --no-cache is what turns
        # this into a per-appliance fan-out, so only that spelling is gated.
        _gate_fanout(rt_ctx, assume_yes)
    report = versions.collect(rt_ctx, cached=not no_cache, timeout=timeout)
    if as_json:
        console.print_json(json.dumps(versions.to_payload(report), default=str))
        return
    render_version_report(console, report)


def _coverage_notes(resource: Resource) -> str:
    if resource.tier is Tier.RAW:
        return "raw passthrough; journaled for audit only"
    if resource.tier is Tier.GENERATED:
        return "generated; best-effort snapshot, no full guarantees"
    if resource.reversibility is Reversibility.REVERSIBLE:
        return "full commit-confirm"
    if resource.reversibility is Reversibility.COMPENSABLE:
        return "compensating rollback; commit-confirm supported"
    return "no rollback; commit requires --force"


def _evidence_label(kind: str) -> str:
    """What has been *observed* of this kind, as `show coverage` prints it.

    Distinct from tier, which says how carefully the resource was written.
    "unrecorded" is not "no evidence": it means the ledger has no row at all,
    which is a bookkeeping failure rather than an answer, and the gate in
    `tests/test_evidence.py` exists so it never ships.
    """
    record = evidence.ledger().get(kind)
    return record.level.label if record else "unrecorded"


def _unverified_writes(registry: Registry) -> list[str]:
    """Curated kinds whose write path no one has watched run on real gear.

    #66's "demote or visibly warn". Demotion is wrong here — the code is
    genuinely curated, and pretending otherwise would misreport tier to make a
    point about evidence — so `show coverage` warns instead, and the roadmap
    stops calling these writes shipped.
    """
    led = evidence.ledger()
    if not led.available:
        return []
    return sorted(
        kind
        for kind in registry.kinds()
        if registry.get(kind).tier is Tier.CURATED
        and (led.level(kind) or evidence.Evidence.IMPLEMENTED) < evidence.WRITE_SUPPORTED_FLOOR
    )


def _evidence_rollup(registry: Registry) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in registry.kinds():
        label = _evidence_label(kind)
        counts[label] = counts.get(label, 0) + 1
    return counts


def _evidence_line(registry: Registry, *, pointer: bool = True) -> str:
    """The one-line warning #66 asks for, in the language of what is missing.

    ``pointer`` is off inside ``--evidence`` itself: pointing a reader at the
    view they are already looking at is the kind of small dishonesty that
    makes people stop reading warnings.
    """
    led = evidence.ledger()
    if not led.available:
        return "evidence ledger not vendored; per-resource evidence unavailable"
    curated = [k for k in registry.kinds() if registry.get(k).tier is Tier.CURATED]
    unverified = _unverified_writes(registry)
    if not unverified:
        return f"all {len(curated)} curated resource(s) carry live change-and-rollback evidence"
    tail = " `show coverage --evidence` for the detail." if pointer else ""
    return (
        f"{len(unverified)} of {len(curated)} curated resource(s) have no live "
        f"change-and-rollback evidence: their write paths have never been run against "
        f"a real fabric.{tail}"
    )


def _support_matrix_lines(registry: Registry) -> list[str]:
    led = evidence.ledger()
    matrix = led.support
    return [
        f"spec baseline:         {matrix.spec_baseline or 'unknown'}",
        f"Orchestrator verified: {', '.join(matrix.orchestrator) or 'none'}",
        f"ECOS verified:         {', '.join(matrix.ecos) or 'none'}",
        f"auth modes verified:   {', '.join(matrix.auth_modes) or 'none'}",
    ]


def _observed_against(record: evidence.Record | None) -> str:
    """The versions and date behind a record, as one cell.

    Three columns of em-dashes is a table that has learned nothing; one cell
    that says "—" says the same thing and leaves room for the notes, which is
    where the actual information is while every row reads mock-verified.
    """
    if record is None:
        return "—"
    parts = [p for p in (record.orchestrator, record.ecos) if p]
    if record.auth_mode:
        parts.append(record.auth_mode)
    if record.observed:
        parts.append(record.observed)
    return " / ".join(parts) if parts else "—"


def _evidence_table(registry: Registry, kinds: list[str]) -> Table:
    led = evidence.ledger()
    table = Table(title="resource evidence")
    table.add_column("kind", overflow="fold")
    table.add_column("tier", justify="right")
    table.add_column("evidence")
    table.add_column("observed against", overflow="fold")
    table.add_column("notes", overflow="fold")
    for kind in kinds:
        record = led.get(kind)
        table.add_row(
            kind,
            str(int(registry.get(kind).tier)),
            record.level.label if record else "unrecorded",
            _observed_against(record),
            (record.notes if record else "") or "",
        )
    return table


# -- coverage: registered kinds joined onto the spec endpoint universe (#28) ---
#
# A resource declares the operations it covers via ``Resource.endpoints``
# (``"<scope> <METHOD> <path>"``, see :func:`pyecsdwan.specs.endpoint_key`).
# Those declarations are the *only* link between a kind and the API surface,
# so this join is exactly as good as they are — ``tests/test_coverage.py``
# asserts every one of them resolves to a real spec operation.
#
# Deliberately NOT attributed to any kind: the appliance proxy transport
# (``/appliance/rest``) and the engine's own plumbing
# (``/appliance/saveChanges``, ``/action/status``, ``/appliance``). Every
# appliance-scope transaction drives those, so charging them to one kind
# would be arbitrary; they sit at tier 0 with the rest of the passthrough
# surface.

#: Endpoints with no covering resource are still reachable — this is the
#: floor of the tool, not a hole in it.
_TIER0_NOTE = "tier 0 = reachable today via `ec-cli api` passthrough (journaled), not a gap"


@dataclasses.dataclass(frozen=True)
class _EndpointCoverage:
    """One spec operation with the tier attribution derived from the registry."""

    scope: str
    method: str
    path: str
    tier: int
    #: Every kind declaring this endpoint, sorted; empty at tier 0.
    kinds: tuple[str, ...]
    #: Reversibility of the highest-tier covering kinds; "" at tier 0.
    reversibility: str
    summary: str
    deprecated: bool
    #: A Postman-derived payload example exists for this endpoint (issue #51).
    example: bool


def _declared_coverage(registry: Registry) -> dict[str, list[Resource]]:
    """``endpoint_key()`` -> the resources declaring it (may be more than one)."""
    covering: dict[str, list[Resource]] = {}
    for kind in registry.kinds():
        resource = registry.get(kind)
        for declared in resource.endpoints:
            scope, _, rest = declared.partition(" ")
            method, _, path = rest.partition(" ")
            covering.setdefault(specs.endpoint_key(scope, method, path), []).append(resource)
    return covering


def _undeclared_keys(registry: Registry) -> list[str]:
    """Declared endpoints absent from the spec universe — drift or a typo.

    Normally empty (a test enforces it), but the report says so out loud
    rather than silently dropping the declaration.
    """
    universe = specs.endpoint_index()
    if not universe:
        # No baselines vendored: the universe is unknown, not empty. Judging
        # declarations against it would report every one of them as drift.
        return []
    return sorted(k for k in _declared_coverage(registry) if k not in universe)


def _endpoint_coverage(registry: Registry) -> list[_EndpointCoverage]:
    """The whole endpoint universe, each row tiered by its covering kinds."""
    covering = _declared_coverage(registry)
    examples = specs.payload_examples()
    rows: list[_EndpointCoverage] = []
    for key, endpoint in specs.endpoint_index().items():
        resources = covering.get(key, [])
        tier = max((int(r.tier) for r in resources), default=int(Tier.RAW))
        # Two kinds can legitimately declare one endpoint (appliance/deployment
        # and appliance/dhcp share the deployment object). Take the highest
        # tier and name every kind rather than picking one.
        top = [r for r in resources if int(r.tier) == tier]
        rows.append(
            _EndpointCoverage(
                scope=endpoint.scope,
                method=endpoint.method,
                path=endpoint.path,
                tier=tier,
                kinds=tuple(sorted(r.kind for r in top)),
                reversibility="/".join(sorted({r.reversibility.value for r in top})),
                summary=endpoint.summary,
                deprecated=endpoint.deprecated,
                example=key in examples,
            )
        )
    rows.sort(key=lambda r: (r.scope, r.path, r.method))
    return rows


def _coverage_rollup(rows: list[_EndpointCoverage]) -> dict[str, int]:
    counts = {"endpoints": len(rows), "curated": 0, "generated": 0, "raw": 0}
    for row in rows:
        counts[{2: "curated", 1: "generated"}.get(row.tier, "raw")] += 1
    return counts


def _rollup_line(counts: dict[str, int]) -> str:
    return (
        f"{counts['curated']} of {counts['endpoints']} endpoints curated, "
        f"{counts['generated']} generated, {counts['raw']} raw-only"
    )


def coverage_summary_line(registry: Registry) -> str:
    """One-line roll-up, shared by ``ec-cli show coverage`` and the shell.

    Returns an explanation instead of a fake zero when no baselines are
    vendored — an empty universe is "unknown", not "nothing is covered".
    """
    rows = _endpoint_coverage(registry)
    if not rows:
        return "no API specs vendored; per-endpoint coverage unavailable"
    return _rollup_line(_coverage_rollup(rows))


def _kind_rows(registry: Registry, kinds: list[str]) -> Table:
    table = Table(title="resource coverage")
    table.add_column("kind")
    table.add_column("scope")
    table.add_column("reversibility")
    table.add_column("tier", justify="right")
    # Tier is how it was written; evidence is what anyone has seen it do.
    # Side by side, because reading one without the other is how "shipped"
    # came to mean five different things (#66).
    table.add_column("evidence")
    table.add_column("endpoints", justify="right")
    table.add_column("notes")
    for kind in kinds:
        resource = registry.get(kind)
        table.add_row(
            kind,
            resource.scope.value,
            resource.reversibility.value,
            str(int(resource.tier)),
            _evidence_label(kind),
            str(len(resource.endpoints)),
            _coverage_notes(resource),
        )
    return table


def _endpoint_table(rows: list[_EndpointCoverage]) -> Table:
    # The example column only appears once payload examples are vendored
    # (issue #51); before that it would be a column of blanks.
    with_examples = any(row.example for row in rows)
    table = Table(title=f"endpoint coverage ({len(rows)})")
    table.add_column("scope")
    table.add_column("method")
    table.add_column("path", overflow="fold")
    table.add_column("tier", justify="right")
    table.add_column("kind(s)", overflow="fold")
    table.add_column("reversibility")
    if with_examples:
        table.add_column("example")
    for row in rows:
        cells = [
            row.scope,
            row.method,
            row.path + (" (deprecated)" if row.deprecated else ""),
            str(row.tier),
            ", ".join(row.kinds),
            row.reversibility,
        ]
        if with_examples:
            cells.append("yes" if row.example else "")
        table.add_row(*cells)
    return table


@show_app.command("coverage")
def show_coverage(
    endpoints: Annotated[
        bool,
        typer.Option(
            "--endpoints",
            help="One row per known API endpoint instead of per resource kind.",
        ),
    ] = False,
    kind: Annotated[
        str | None,
        typer.Option("--kind", help="Restrict to one resource kind and what it covers."),
    ] = None,
    tier: Annotated[
        int | None,
        typer.Option("--tier", min=0, max=2, help="Restrict to tier 0, 1 or 2."),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Restrict to 'orchestrator' or 'appliance'."),
    ] = None,
    show_evidence: Annotated[
        bool,
        typer.Option(
            "--evidence",
            help="What has been observed of each kind, and against which versions.",
        ),
    ] = False,
    level: Annotated[
        str | None,
        typer.Option(
            "--level",
            help="Restrict to one evidence level, e.g. 'mock-verified'.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Resource kinds, the endpoints they cover, the tier, and the evidence.

    Tier and evidence answer different questions and are both here on purpose:
    tier is how carefully the resource was *written*, evidence is what anyone
    has *seen it do*, and against which Orchestrator (#66).

    Offline: reads the vendored ``_specs/`` baselines, the vendored evidence
    ledger and the plugin registry, never the Orchestrator.
    """
    registry = _registry_only()
    if kind is not None and kind not in registry:
        _fail(f"unknown resource kind {kind!r}; known kinds: {', '.join(registry.kinds())}")
    if scope is not None and scope not in specs.SCOPES:
        _fail(f"unknown scope {scope!r}; expected one of: {', '.join(specs.SCOPES)}")
    if level is not None:
        try:
            evidence.Evidence.from_label(level)
        except ValueError as exc:
            _fail(str(exc))

    kinds = [
        k
        for k in registry.kinds()
        if (kind is None or k == kind)
        and (tier is None or int(registry.get(k).tier) == tier)
        and (scope is None or registry.get(k).scope.value == scope)
        and (level is None or _evidence_label(k) == level)
    ]
    all_rows = _endpoint_coverage(registry)
    # Join through endpoint_key(), never raw strings: a declaration writes an
    # ECOS path relative while the spec writes it absolute.
    covered_by_kind = (
        {specs.endpoint_key(*e.split(" ", 2)) for e in registry.get(kind).endpoints}
        if kind is not None
        else set()
    )
    rows = [
        row
        for row in all_rows
        if (tier is None or row.tier == tier)
        and (scope is None or row.scope == scope)
        and (kind is None or specs.endpoint_key(row.scope, row.method, row.path) in covered_by_kind)
    ]
    counts = _coverage_rollup(all_rows)
    unknown = _undeclared_keys(registry)

    led = evidence.ledger()

    if as_json:
        payload: dict[str, Any] = {
            "spec_versions": {s: specs.spec_version(s) for s in specs.SCOPES},
            "totals": counts,
            "note": _TIER0_NOTE,
            "kinds": [
                {
                    "kind": k,
                    "scope": registry.get(k).scope.value,
                    "reversibility": registry.get(k).reversibility.value,
                    "tier": int(registry.get(k).tier),
                    "evidence": _evidence_label(k),
                    "notes": _coverage_notes(registry.get(k)),
                    "endpoints": list(registry.get(k).endpoints),
                }
                for k in kinds
            ],
            "undeclared_in_spec": unknown,
            # Evidence rides in the same payload as tier so a script asking
            # "can I use this?" cannot read one without seeing the other.
            "evidence": {
                "available": led.available,
                "note": led.note,
                "support": led.support.as_json(),
                "totals": _evidence_rollup(registry),
                "unverified_writes": _unverified_writes(registry),
                "records": [r.as_json() for r in (led.get(k) for k in kinds) if r],
            },
        }
        if endpoints:
            payload["endpoints"] = [dataclasses.asdict(row) for row in rows]
        console.print_json(json.dumps(payload, default=str))
        return

    if show_evidence:
        if not led.available:
            console.print(Text(_evidence_line(registry), style="yellow"))
            return
        if not kinds:
            console.print("no resource kinds match those filters")
            return
        console.print(_evidence_table(registry, kinds))
        for line in _support_matrix_lines(registry):
            console.print(Text(line, style="dim"))
        console.print(Text(_evidence_line(registry, pointer=False), style="yellow"))
        if led.note:
            console.print(Text(led.note, style="dim"))
        return

    if endpoints:
        if not all_rows:
            console.print(Text(_no_specs_message(), style="yellow"))
            return
        if not rows:
            console.print("no endpoints match those filters")
            return
        console.print(_endpoint_table(rows))
    elif kinds:
        console.print(_kind_rows(registry, kinds))
    else:
        # `--tier 0` is the common case here: no *registered kind* is tier 0,
        # but 1700-odd endpoints are, so point at the view that shows them.
        console.print("no resource kinds match those filters; try --endpoints")

    if all_rows:
        console.print(_rollup_line(counts))
        console.print(Text(_TIER0_NOTE, style="dim"))
    else:
        console.print(Text(_no_specs_message(), style="yellow"))
    console.print(Text(_evidence_line(registry), style="yellow"))
    if unknown:
        err_console.print(
            Text(
                f"warning: {len(unknown)} declared endpoint(s) are absent from the "
                f"vendored specs: {', '.join(unknown)}",
                style="yellow",
            )
        )


def _no_specs_message() -> str:
    return (
        "no API specs vendored (looked for _specs/*-openapi-*.json inside the "
        "package, or "
        f"${specs.ENV_SPECS_DIR}); per-endpoint coverage is unavailable — "
        "the resource-kind table above is complete on its own"
    )


@configuration_app.command("candidate")
def show_candidate(ctx: typer.Context) -> None:
    """Dump the staged candidate changeset (intent as YAML)."""
    state = _state(ctx)
    _rt, _registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.origin)
    items = candidate.ordered_items()
    if not items:
        console.print("candidate is empty")
        return
    for item in items:
        console.print(Text(f"{item.ref_key} (mode: {item.mode})", style="bold"))
        if item.intent:
            # Redacted at the render, not in the store: commit needs the real
            # values, a terminal never does (#106).
            shown = redaction.redact_tree(item.intent)
            dumped = yaml.safe_dump(shown, sort_keys=True, default_flow_style=False)
            console.print(dumped.rstrip("\n"))
        for path in item.delete_paths:
            console.print(Text(f"  delete: {'.'.join(path)}", style="red"))


# -- flows: active-flow reports (#58 summary matrix, #59 one address) ---------
#
# Both read GET /flow through `pyecsdwan.reports.flows`; everything below is
# rendering and option plumbing. `show flows summary` and `show flow <ip>`
# differ by one character, so they are deliberately two separate commands
# (a sub-Typer and a leaf) rather than one command with a mode argument —
# `show flow` with no address is then a usage error from the parser itself,
# never mistaken for the summary.


def _human_bytes(count: int) -> str:
    """Byte counters run to the gigabytes; a raw integer column is unreadable."""
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _human_uptime(milliseconds: int) -> str:
    seconds, ms = divmod(max(0, milliseconds), 1000)
    minutes, secs = divmod(seconds, 60)
    hours, mins = divmod(minutes, 60)
    if hours:
        return f"{hours}h{mins:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}.{ms // 100}s"


def _bounded_note(appliances: Sequence[str], max_flows: int) -> str:
    """The line that keeps a truncated tally from reading as a total."""
    return (
        f"bounded by --max-flows {max_flows} on {', '.join(appliances)}: "
        "those figures are a ceiling, not a total — re-run with a higher "
        "--max-flows for a complete answer"
    )


def render_flows_summary(console_out: Console, summary: flows_report.FlowsSummary) -> None:
    """The #58 matrix: appliances down, overlays across, totals both ways."""
    reachable = [row for row in summary.rows if row.reachable]
    title = f"active flows ({summary.grand_total}) across {len(reachable)} appliance(s)"
    table = Table(title=title)
    table.add_column("appliance")
    for overlay in summary.overlays:
        table.add_column(overlay, justify="right")
    table.add_column("total", justify="right")
    for row in summary.rows:
        if not row.reachable:
            table.add_row(
                Text(row.target.name, style="red"),
                *["-" for _ in summary.overlays],
                Text("unreachable", style="red"),
            )
            continue
        total = Text(str(row.total) + ("+" if row.bounded else ""))
        table.add_row(
            row.target.name,
            *[str(row.counts.get(overlay, 0)) for overlay in summary.overlays],
            total,
        )
    if summary.overlays:
        table.add_section()
        column_totals = summary.column_totals
        table.add_row(
            Text("total", style="bold"),
            *[Text(str(column_totals[overlay]), style="bold") for overlay in summary.overlays],
            Text(str(summary.grand_total), style="bold"),
        )
    console_out.print(table)
    if flows_report.PASSTHROUGH in summary.overlays:
        console_out.print(
            Text(
                f"{flows_report.PASSTHROUGH}: built-in and passthrough traffic "
                "(not on a named SD-WAN overlay)",
                style="dim",
            )
        )
    if summary.bounded:
        console_out.print(
            Text(_bounded_note(summary.bounded_appliances, summary.max_flows), style="yellow")
        )
    for row in summary.unreachable:
        err_console.print(Text(f"unreachable: {row.target.name}: {row.error}", style="red"))


def render_flow_search(console_out: Console, search: flows_report.FlowSearch) -> None:
    """The #59 fabric-wide table. One row per *conversation*, not per
    per-appliance observation — see ``reports.flows`` for the identity."""
    if not search.matches:
        console_out.print(
            f"no flows found for {search.query} on "
            f"{len(search.searched)} appliance(s) searched"
        )
    else:
        table = Table(title=f"flows matching {search.query} ({search.match_count})")
        for name in ("appliance", "src", "dst", "proto/app", "overlay", "transport", "state"):
            table.add_column(name)
        table.add_column("uptime", justify="right")
        table.add_column("bytes out tx/rx", justify="right")
        table.add_column("bytes in tx/rx", justify="right")
        previous = ""
        for match in search.matches:
            flow = match.primary
            appliance = ", ".join(match.appliances)
            if previous and flow.appliance != previous:
                table.add_section()
            previous = flow.appliance
            table.add_row(
                appliance,
                str(flow.source),
                str(flow.destination),
                f"{flow.protocol}/{flow.application}" if flow.application else flow.protocol,
                flow.overlay,
                flow.transport,
                flow.status,
                _human_uptime(flow.uptime_ms),
                f"{_human_bytes(flow.outbound_tx_bytes)}/{_human_bytes(flow.outbound_rx_bytes)}",
                f"{_human_bytes(flow.inbound_tx_bytes)}/{_human_bytes(flow.inbound_rx_bytes)}",
            )
        console_out.print(table)
        console_out.print(
            Text(
                f"{search.match_count} flow(s); an entry naming two appliances is one "
                "conversation seen from both ends, counted once. Byte counters are the "
                "first-listed appliance's view — summing the two would double-count. "
                "'transport' is derived from the overlay; the API reports no such field.",
                style="dim",
            )
        )
    if search.bounded:
        console_out.print(
            Text(_bounded_note(search.bounded_appliances, search.max_flows), style="yellow")
        )
    for target, error in search.unreachable:
        err_console.print(Text(f"unreachable: {target.name}: {error}", style="red"))


_MAX_FLOWS_OPTION = typer.Option(
    "--max-flows",
    min=1,
    help="Per-appliance cap sent as the required maxFlows parameter.",
)
_APPLIANCE_OPTION = typer.Option(
    "--appliance",
    help="Restrict to these appliances (hostname or nePk); repeatable.",
)
_CONCURRENCY_OPTION = typer.Option(
    "--concurrency", min=1, help="Appliances queried in parallel."
)
_TIMEOUT_OPTION = typer.Option(
    "--timeout", help="Overall report deadline in seconds; stragglers become unreachable rows."
)
_NO_CACHE_OPTION = typer.Option(
    "--no-cache",
    help="Refresh the cached appliance inventory first (flow data is always live).",
)
_JSON_OPTION = typer.Option("--json", help="Machine-readable output.")


@flows_app.command("summary")
def show_flows_summary(
    ctx: typer.Context,
    max_flows: Annotated[int, _MAX_FLOWS_OPTION] = flows_report.DEFAULT_MAX_FLOWS,
    appliance: Annotated[list[str] | None, _APPLIANCE_OPTION] = None,
    concurrency: Annotated[int, _CONCURRENCY_OPTION] = DEFAULT_CONCURRENCY,
    timeout: Annotated[float | None, _TIMEOUT_OPTION] = None,
    no_cache: Annotated[bool, _NO_CACHE_OPTION] = False,
    as_json: Annotated[bool, _JSON_OPTION] = False,
    assume_yes: Annotated[bool, _YES_OPTION] = False,
) -> None:
    """Active flow counts per appliance per overlay, with row and column totals.

    Read-only: one GET /flow per appliance and nothing else. Counts come from
    the returned rows because the response's computed summary carries no
    per-overlay breakdown -- see ``pyecsdwan.reports.flows``.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
    if not appliance:
        # --appliance narrows the fan-out to what the operator named, so the
        # cost they are being warned about is one they already bounded.
        _gate_fanout(rt_ctx, assume_yes)
    try:
        summary = flows_report.build_flows_summary(
            rt_ctx,
            appliances=appliance,
            max_flows=max_flows,
            concurrency=concurrency,
            timeout=timeout,
            no_cache=no_cache,
        )
    except ResolveError as exc:
        _fail(str(exc))
    if as_json:
        console.print_json(json.dumps(summary.as_dict(), default=str))
        return
    render_flows_summary(console, summary)


@fabric_app.command("flow")
def show_flow(
    ctx: typer.Context,
    ip: Annotated[
        str,
        typer.Argument(help="Address to search for: <ip> or <ip>/<prefix>."),
    ],
    max_flows: Annotated[int, _MAX_FLOWS_OPTION] = flows_report.DEFAULT_MAX_FLOWS,
    appliance: Annotated[list[str] | None, _APPLIANCE_OPTION] = None,
    concurrency: Annotated[int, _CONCURRENCY_OPTION] = DEFAULT_CONCURRENCY,
    timeout: Annotated[float | None, _TIMEOUT_OPTION] = None,
    no_cache: Annotated[bool, _NO_CACHE_OPTION] = False,
    as_json: Annotated[bool, _JSON_OPTION] = False,
    assume_yes: Annotated[bool, _YES_OPTION] = False,
) -> None:
    """Every flow touching an address, fabric-wide, deduped across appliances.

    Matching is done by the Orchestrator via ipEitherFlag -- the address is
    matched at either end of the flow server-side, never by pulling every flow
    and filtering here. Read-only.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
    if not appliance:
        _gate_fanout(rt_ctx, assume_yes)
    try:
        search = flows_report.find_flows(
            rt_ctx,
            ip,
            appliances=appliance,
            max_flows=max_flows,
            concurrency=concurrency,
            timeout=timeout,
            no_cache=no_cache,
        )
    except ValueError as exc:
        _fail(str(exc))
    except ResolveError as exc:
        _fail(str(exc))
    if as_json:
        console.print_json(json.dumps(search.as_dict(), default=str))
        return
    render_flow_search(console, search)



# -- tier-0 passthrough -------------------------------------------------------


def _parse_params(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            _fail(f"malformed --param {pair!r}; expected KEY=VALUE")
        params[key] = value
    return params


def _load_body(body: Path | None) -> tuple[Any, str | None]:
    """Parse a JSON/YAML body file; returns (parsed, sha256-of-raw-bytes)."""
    if body is None:
        return None, None
    try:
        raw = body.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {body}: {exc}")
    sha = hashlib.sha256(raw).hexdigest()
    try:
        if body.suffix.lower() == ".json":
            return json.loads(raw.decode("utf-8")), sha
        return yaml.safe_load(raw.decode("utf-8")), sha
    except (ValueError, yaml.YAMLError) as exc:
        _fail(f"could not parse body file {body}: {exc}")


def _with_params(path: str, params: dict[str, str]) -> str:
    """Fold query params into an ECOS path (the appliance proxy takes no params kwarg)."""
    if not params:
        return path
    query = "&".join(f"{key}={value}" for key, value in params.items())
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}{query}"


def _summarize_response(response: Any) -> str:
    if response is None:
        return "empty response"
    if isinstance(response, dict):
        return f"dict ({len(response)} key(s))"
    if isinstance(response, list):
        return f"list ({len(response)} item(s))"
    return f"text ({len(str(response))} char(s))"


def _print_response(response: Any) -> None:
    if response is None:
        console.print(Text("(empty response)", style="dim"))
    elif isinstance(response, (dict, list)):
        console.print_json(json.dumps(response, default=str))
    else:
        console.print(str(response))


@app.command()
def api(
    ctx: typer.Context,
    method: Annotated[str, typer.Argument(metavar="METHOD", help="get | post | put | delete")],
    path: Annotated[str, typer.Argument(help="API path, e.g. /gms/interfaceLabels")],
    body: Annotated[
        Path | None,
        typer.Option(
            "--body", exists=True, dir_okay=False, help="Request body file (JSON or YAML)."
        ),
    ] = None,
    appliance: Annotated[
        str | None,
        typer.Option("--appliance", help="Send through the appliance proxy to this appliance."),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option("--param", metavar="KEY=VALUE", help="Query parameter (repeatable)."),
    ] = None,
) -> None:
    """Tier-0 raw API passthrough — journaled for audit, no rollback guarantees."""
    render.tier0_banner(console)
    method_upper = method.lower()
    if method_upper not in _API_METHODS:
        _fail(f"unsupported method {method!r}; use one of: {', '.join(_API_METHODS)}")
    method_upper = method_upper.upper()
    params = _parse_params(param or [])
    body_data, body_sha = _load_body(body)
    state = _state(ctx)
    rt_ctx, _registry, settings = _bootstrap(state)
    # Tier-0 never replays (#67). An operator typing an arbitrary path has not
    # told us the endpoint is read-only, and the API has GETs whose own spec
    # summaries say "Clear idle time", "Generate the Sys Dump file" and "Delete
    # specific/all segment BGP state". The policy and its reason are journaled
    # so an audit can answer "was this call sent once?" without re-deriving it.
    scope = "appliance" if appliance is not None else "orchestrator"
    policy, reason = retry_mod.effective_policy(
        method_upper, path, retry_mod.Retry.NEVER, scope=scope
    )
    # Secret-named query values keep their name and a change hint only:
    # `--param apiKey=...` must not land verbatim in an audit trail whose
    # whole point is to be exportable (#106). The request itself sends the
    # real values; only the record is masked — and the transaction Ref is
    # built from the masked path too, because the ref key lands in meta.json
    # and TXN_BEGIN, which is exactly the file nobody thinks of.
    safe_path = redaction.redact_query(path)
    journal = TxnJournal.create(
        settings.origin, [Ref(kind="api", name=f"{method_upper} {safe_path}")]
    )
    journal.append(
        "RAW_API",
        method=method_upper,
        path=safe_path,
        params=redaction.redact_params(params),
        body_sha256=body_sha,
        retry_policy=policy.value,
        retry_reason=reason,
        status="sent",
    )
    try:
        if appliance is not None:
            ne_pk = rt_ctx.resolver.ne_pk_for(appliance)
            response = rt_ctx.client.appliance_request(
                method_upper,
                ne_pk,
                _with_params(path, params),
                json_body=body_data,
                retry_policy=retry_mod.Retry.NEVER,
            )
        else:
            response = rt_ctx.client.request(
                method_upper,
                path,
                json_body=body_data,
                params=params or None,
                retry_policy=retry_mod.Retry.NEVER,
            )
    except (OrchApiError, ResolveError, ValueError) as exc:
        journal.set_state(TxnState.AUDIT_ONLY, response_summary=f"error: {exc}")
        if isinstance(exc, ValueError):
            _fail(str(exc))
        raise
    journal.set_state(TxnState.AUDIT_ONLY, response_summary=_summarize_response(response))
    _print_response(response)


# -- envelope-key rotation (#106) ---------------------------------------------


@app.command("rotate-key")
def rotate_key() -> None:
    """Retire the secrets envelope key and re-seal all encrypted state under a new one.

    Local-only: touches the OS keyring and the state directory, never the
    Orchestrator. Run it when no commit is in flight. Admissible as a
    top-level verb by grammar §8: it acts, nothing existing can carry it,
    and it is neither a read intent nor a transaction transition.
    """
    try:
        report = vault.rotate_key()
    except (vault.VaultUnavailable, vault.VaultOpenError) as exc:
        _fail(str(exc))
    console.print(Text(f"ok: envelope key rotated; {report.summary()}", style="green"))


# -- cache --------------------------------------------------------------------


@cache_app.command("refresh")
def cache_refresh(ctx: typer.Context) -> None:
    """Force-reload the name<->ID resolver cache."""
    state = _state(ctx)
    rt_ctx, _registry, _settings = _bootstrap(state)
    rt_ctx.resolver.refresh()
    console.print(Text("ok: resolver cache cleared (repopulates on next use)", style="green"))


# -- plugin promotion (#29) ---------------------------------------------------


def _promotion_sample_ref(
    rt_ctx: Ctx, registry: Registry, resource: Resource, name: str | None, appliance: str | None
) -> Ref:
    """Ref to run the checklist against: the operator's, or the first the
    resource enumerates in the operator's scope.

    Scoped through :func:`pyecsdwan.registry.scoped_instances` so this agrees
    with the shell about what ``--appliance`` selects (#76). It also decides
    which instance the checklist samples, so "the first one" has to be a
    stable choice: ``scoped_instances`` sorts by name, where the raw
    ``list_refs()`` order was whatever the resource happened to build.
    """
    if name is not None:
        return _validated_ref(registry, resource.kind, name, appliance)
    refs = registry_mod.scoped_instances(resource, rt_ctx, appliance)
    if not refs:
        noun = registry.cli_name(resource.kind)
        if appliance is not None:
            _fail(f"{noun} enumerates no instances on appliance {appliance!r}")
        _fail(
            f"{noun} enumerates no instances on this Orchestrator; "
            f"name one with --name (and --appliance for appliance scope)"
        )
    return refs[0]


def _check_table(kind: str, checks: list[registry_mod.Check]) -> Table:
    styles = {
        registry_mod.CheckStatus.OK: "green",
        registry_mod.CheckStatus.FAIL: "bold red",
        registry_mod.CheckStatus.MANUAL: "yellow",
    }
    table = Table(title=f"promotion checklist: {kind}")
    table.add_column("box")
    table.add_column("result")
    table.add_column("detail", overflow="fold")
    for check in checks:
        table.add_row(
            check.name,
            Text(check.status.value, style=styles[check.status]),
            check.detail,
        )
    return table


@plugin_app.command("promote")
def plugin_promote(
    ctx: typer.Context,
    kind: Annotated[str, typer.Argument(help="Resource kind to run the checklist for.")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Instance to sample; default: the first the kind enumerates."),
    ] = None,
    appliance: Annotated[
        str | None, typer.Option("--appliance", help="Appliance for an appliance-scope kind.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Run the Tier-1 -> Tier-2 promotion checklist for KIND against this fabric.

    Same checks ``make check`` runs (``pyecsdwan.registry``), pointed at a real
    Orchestrator instead of the bundled mock — worth running before you promote
    a plugin, because your fabric's state exercises shapes the fixtures do not.

    It reads only: one ``fetch()`` for the sampled instance, no writes. The tier
    itself is a source-level declaration a human reviews, so this command never
    edits your plugin — when every machine-checkable box is green it prints the
    one-line change to make and the boxes still left to human judgment.
    """
    state = _state(ctx)
    rt_ctx, registry, _settings = _bootstrap(state)
    resource = _resource_for(registry, kind, appliance)
    # What was typed is not necessarily what was checked: `zones` names two
    # different objects and --appliance picks between them. Reporting the token
    # back would label an `appliance/zones` result `zones`. Machine output gets
    # the resolved kind (stable, unambiguous); prose gets the noun (#77).
    kind, noun = resource.kind, registry.cli_name(resource.kind)
    curated = resource.tier >= Tier.CURATED

    # Only the Tier-2 boxes need a sample instance. An un-curated kind is
    # answerable without one — and must be, since generated stubs implement no
    # list_refs() and failing to resolve a ref would hide the real answer
    # ("still a stub") behind a ref-resolution error.
    checks = [registry_mod.check_untransactional_normalize(resource)]
    ref: Ref | None = None
    if curated:
        ref = _promotion_sample_ref(rt_ctx, registry, resource, name, appliance)
        raw = resource.fetch(rt_ctx, ref)
        checks.extend(registry_mod.check_idempotent(resource, ref, raw, rt_ctx))
    checks.extend(registry_mod.manual_checks())
    failed = [c for c in checks if c.failed]
    # An un-curated kind never counts as green: the Tier-2 boxes above were
    # skipped, and they *cannot* run while normalize() raises NotCurated.
    # Reporting green on one evaluated box would tell a curator to promote a
    # stub, producing a Tier-2 kind whose normalize() raises.
    green = not failed and curated

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "kind": kind,
                    "tier": int(resource.tier),
                    "ref": str(ref) if ref is not None else None,
                    "green": green,
                    "tier2_evaluated": curated,
                    "evidence": _evidence_label(kind),
                    "checks": [dataclasses.asdict(c) for c in checks],
                },
                default=str,
            )
        )
    else:
        console.print(_check_table(noun, checks))
        if ref is not None:
            console.print(Text(f"sampled {ref}", style="dim"))
        # Green here means the *machine-checkable* boxes pass, which is a
        # statement about the code and says nothing about whether anyone has
        # run this resource on a fabric. Printing the evidence level next to
        # the verdict is what stops "promote passed" from being read as
        # "ready for production" (#66).
        console.print(
            Text(f"evidence: {_evidence_label(kind)} (docs/live-validation.md)", style="dim")
        )

    if failed:
        if not as_json:
            err_console.print(
                Text(f"{len(failed)} checklist box(es) failed — not ready for Tier 2", style="red")
            )
        raise typer.Exit(1)
    if curated:
        if not as_json:
            console.print(Text(f"ok: {noun} still meets its Tier-2 obligations", style="green"))
            record = evidence.ledger().get(kind)
            if record is not None and record.level < evidence.WRITE_SUPPORTED_FLOOR:
                console.print(
                    Text(
                        f"...against the code. Its write path is {record.level.label}: no one "
                        f"has run a change and a rollback of {noun} on real gear at a recorded "
                        f"version. docs/live-validation.md is how that gets fixed.",
                        style="yellow",
                    )
                )
        return
    # An un-curated kind reaching here has passed exactly one box: "normalize()
    # refuses". The Tier-2 boxes were not evaluated and *cannot* be while
    # normalize() still raises — so this is not a green light, and saying
    # "every machine-checkable box is green" here would be advice that produces
    # a Tier-2 kind whose normalize() raises. Report the real state instead.
    if as_json:
        raise typer.Exit(1)
    err_console.print(
        Text(
            f"{noun} is tier {int(resource.tier)} (generated). It correctly refuses "
            f"to normalize, and that is the only box this command can evaluate — "
            f"the Tier-2 obligations above were NOT checked, because idempotency "
            f"cannot be proved while normalize() raises NotCurated.\n"
            f"Implement normalize() first (docs/plugin-promotion.md), set "
            f"`tier = Tier.CURATED`, then re-run this command to have the Tier-2 "
            f"boxes actually evaluated.",
            style="yellow",
        )
    )
    raise typer.Exit(1)


# -- show run: fabric configuration breakdown (#55) ----------------------------
#
# Rendering only; the report itself is built in `pyecsdwan.reports.fabric`.
# Lives here rather than in `cli/render.py` for the same reason
# `render_version_report` does: the shell imports it back from this module, so
# `ec-cli show run` and the shell's `show run` cannot drift into showing
# different things.

#: A section that could only be read in part.
_DEGRADED_STYLE = "yellow"


def _breakdown(counts: Sequence[tuple[str, int]]) -> str:
    """``hub 1, spoke 2`` — a count map on one line, in the report's order."""
    return ", ".join(f"{value} {count}" for value, count in counts) if counts else "-"


def _section_notes(console_out: Console, section: fabric.Section) -> None:
    """Partial data, said out loud under the section it affects.

    In-band and per-section on purpose: a footer would make the reader guess
    which table the missing data belonged to.
    """
    for note in section.notes:
        console_out.print(Text(f"  ! {note}", style=_DEGRADED_STYLE))


def _render_overlays(console_out: Console, section: fabric.OverlaySection) -> None:
    table = Table(title=f"overlays ({len(section.overlays)})")
    table.add_column("overlay")
    table.add_column("id")
    table.add_column("topology")
    table.add_column("members")
    table.add_column("appliances", overflow="fold")
    for overlay in section.overlays:
        table.add_row(
            Text(overlay.name),
            overlay.overlay_id or "-",
            overlay.topology or "-",
            str(overlay.member_count),
            ", ".join(overlay.members) or Text("none", style=_DEGRADED_STYLE),
        )
    console_out.print(table)
    _section_notes(console_out, section)
    for overlay in section.empty_overlays:
        console_out.print(
            Text(f"  {overlay.name}: no appliances associated", style=_DEGRADED_STYLE)
        )
    if section.unassociated:
        console_out.print(
            Text(
                f"  {len(section.unassociated)} appliance(s) in no overlay: "
                f"{', '.join(section.unassociated)}",
                style=_DEGRADED_STYLE,
            )
        )


def _render_templates(console_out: Console, section: fabric.TemplateSection) -> None:
    table = Table(title=f"template groups ({len(section.groups)})")
    table.add_column("group")
    table.add_column("sections")
    table.add_column("applied")
    table.add_column("appliances", overflow="fold")
    for group in section.groups:
        table.add_row(
            Text(group.name),
            str(len(group.sections)),
            str(group.applied_count),
            ", ".join(group.applied_to) or Text("nowhere", style=_DEGRADED_STYLE),
        )
    console_out.print(table)
    _section_notes(console_out, section)
    if section.unassigned:
        console_out.print(
            Text(
                f"  {len(section.unassigned)} appliance(s) with no template group: "
                f"{', '.join(section.unassigned)}",
                style=_DEGRADED_STYLE,
            )
        )


def _render_security(console_out: Console, section: fabric.SecuritySection) -> None:
    table = Table(
        title=f"security policy ({len(section.configured)} of "
        f"{len(section.policies)} segment pair(s) configured)"
    )
    table.add_column("map")
    table.add_column("rules")
    table.add_column("zone pairs")
    table.add_column("actions")
    for policy in section.policies:
        if policy.unreachable:
            table.add_row(
                Text(policy.pair),
                Text("unreadable", style="red"),
                _ABSENT.copy(),
                Text(policy.error, style="red"),
            )
            continue
        if not policy.present:
            # A 204 is a real answer: the Orchestrator orchestrates no policy
            # for this segment pair. Not the same as "could not be read".
            table.add_row(
                Text(policy.pair), Text("none", style="dim"), _ABSENT.copy(), _ABSENT.copy()
            )
            continue
        table.add_row(
            Text(policy.pair),
            str(policy.rule_count),
            str(policy.zone_pairs),
            _breakdown(policy.actions),
        )
    console_out.print(table)
    console_out.print(
        Text(f"  segments: {', '.join(section.segments) or 'unknown'}", style="dim")
    )
    _section_notes(console_out, section)


def _render_inventory(console_out: Console, section: fabric.InventorySection) -> None:
    table = Table(title=f"appliances ({section.total})")
    table.add_column("grouping")
    table.add_column("breakdown", overflow="fold")
    table.add_row("role", _breakdown(section.by_role))
    table.add_row("site", _breakdown(section.by_site))
    table.add_row("model", _breakdown(section.by_model))
    table.add_row("state", _breakdown(section.by_state))
    console_out.print(table)
    _section_notes(console_out, section)


def _render_deployment(console_out: Console, section: fabric.DeploymentSection) -> None:
    table = Table(title=f"deployment ({len(section.appliances)})")
    table.add_column("appliance")
    table.add_column("mode")
    table.add_column("license")
    table.add_column("interfaces")
    table.add_column("addresses")
    table.add_column("wan labels", overflow="fold")
    table.add_column("lan labels", overflow="fold")
    for appliance in section.appliances:
        if appliance.unreachable:
            # One appliance that will not answer is a marked row, never a
            # missing one — the fan-out contract, rendered.
            table.add_row(
                Text(appliance.hostname),
                Text("unreachable", style="red"),
                *([_ABSENT.copy()] * 4),
                Text(appliance.error, style="red"),
            )
            continue
        table.add_row(
            Text(appliance.hostname),
            appliance.mode,
            appliance.license or "-",
            str(appliance.interfaces),
            str(appliance.addresses),
            ", ".join(appliance.wan_labels) or "-",
            ", ".join(appliance.lan_labels) or "-",
        )
    console_out.print(table)
    _section_notes(console_out, section)
    if section.unreachable:
        console_out.print(
            Text(
                f"  {len(section.unreachable)} appliance(s) unreachable",
                style=_DEGRADED_STYLE,
            )
        )


def render_fabric_config(console_out: Console, report: fabric.FabricConfig) -> None:
    """The fabric configuration breakdown, one section at a time.

    Every section renders even when empty or degraded: a missing table would
    read as "the fabric has none of that", which is exactly the wrong thing to
    tell someone who is looking at a report because something is broken.
    """
    for index, section in enumerate(report.sections):
        if index:
            console_out.print()
        if isinstance(section, fabric.OverlaySection):
            _render_overlays(console_out, section)
        elif isinstance(section, fabric.TemplateSection):
            _render_templates(console_out, section)
        elif isinstance(section, fabric.SecuritySection):
            _render_security(console_out, section)
        elif isinstance(section, fabric.InventorySection):
            _render_inventory(console_out, section)
        elif isinstance(section, fabric.DeploymentSection):
            _render_deployment(console_out, section)
    if report.degraded:
        console_out.print()
        console_out.print(
            Text(
                "partial data in: "
                + ", ".join(s.name for s in report.degraded)
                + " — the sections above say what could not be read",
                style=_DEGRADED_STYLE,
            )
        )


def _render_resource(
    rt_ctx: Ctx,
    registry: Registry,
    token: str | None,
    instance: str | None,
    appliance: str | None,
    as_json: bool,
) -> None:
    """One resource, normalized — the scriptable half of the shell's `show`.

    Terminal states are kept distinct because Principle II says they are
    different answers, not degrees of failure: absent is not empty, and empty
    is not an error. Rendered as YAML a `{}` is a bare `{}`, which in a
    scrollback is indistinguishable from the command having done nothing.
    """
    if token is None:
        nouns = registry.cli_names(Scope.APPLIANCE if appliance else Scope.ORCHESTRATOR)
        _nonterminal(
            f"show configuration{' appliance ' + appliance if appliance else ''}",
            [*nouns, "--format native"] if appliance else nouns,
        )
    kind = _resolve_kind(registry, token, appliance)
    resource = registry.get(kind)
    noun = registry.cli_name(kind)
    if appliance is not None and resource.scope is not Scope.APPLIANCE:
        _fail(
            f"{noun} is {resource.scope.value}-scope; drop the appliance: "
            f"show configuration {noun} [INSTANCE]"
        )
    if appliance is None and resource.scope is Scope.APPLIANCE:
        _fail(
            f"{noun} is appliance-scoped; use "
            f"show configuration appliance NAME {noun} [INSTANCE]"
        )

    if instance is None:
        refs = registry_mod.scoped_instances(resource, rt_ctx, appliance)
        if not refs:
            if appliance is not None:
                rt_ctx.resolver.ne_pk_for(appliance)
            where = f" on {appliance}" if appliance else ""
            _fail(f"{noun}: no instances found{where}")
        if len(refs) > 1:
            _nonterminal(
                f"show configuration{' appliance ' + appliance if appliance else ''} {noun}",
                [r.name for r in refs],
            )
        ref = refs[0]
    else:
        ref = Ref(kind=kind, name=instance, appliance=appliance)

    canonical = resource.normalize(resource.fetch(rt_ctx, ref))
    # Reading config without knowing whether a template owns it is how an
    # operator ends up making a direct change that the next push reverts (#20).
    # Costs two round trips for one instance, and only the two states that
    # matter are printed — UNOWNED says nothing an operator needs to act on.
    owns = resource.managed_by(rt_ctx, ref)
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "ref": str(ref),
                    "config": canonical,
                    "ownership": {
                        "state": owns.state.value,
                        "owner": owns.owner,
                        "reason": owns.reason,
                    },
                },
                default=str,
            )
        )
        return
    console.print(Text(f"# {ref}", style="dim"))
    if owns.blocks_write:
        console.print(Text(f"# {owns.label}", style="bold yellow"))
    if canonical is None:
        console.print("(not present)")
        return
    if not canonical:
        console.print("(empty — no configuration for this kind)")
        return
    text = yaml.safe_dump(canonical, sort_keys=True, default_flow_style=False)
    console.print(text.rstrip("\n"), markup=False, highlight=False, soft_wrap=True)


def _nonterminal(path: str, options: Sequence[str]) -> NoReturn:
    """A valid prefix names its continuations and exits 0 (D-NSO-2).

    Exit 0 because the question asked — "what can follow this?" — was answered.
    Typer's own group help does this for fixed subcommands; this is the same
    thing where the continuations come from the registry or the fabric.
    """
    console.print(f"{path} — valid next tokens:")
    for option in options:
        console.print(f"  {option}")
    if not options:
        console.print("  (none)")
    raise typer.Exit(0)


@configuration_app.callback(invoke_without_command=True)
def show_configuration(ctx: typer.Context) -> None:
    """Configuration, at a named datastore.

    The datastore token is optional and means `running` (Decision 1). What
    makes that safe is the asymmetry: `candidate` is never implicit, so the
    only unnamed datastore is the live one.
    """
    if ctx.invoked_subcommand is not None:
        return
    state = _state(ctx)
    _rt, registry, _settings = _bootstrap(state)
    _nonterminal(
        "show configuration",
        ["running", "candidate", "appliance", "fabric", *registry.cli_names(Scope.ORCHESTRATOR)],
    )


@running_app.callback(invoke_without_command=True)
def show_configuration_running(ctx: typer.Context) -> None:
    """`running` written explicitly means exactly what omitting it means."""
    if ctx.invoked_subcommand is not None:
        return
    _rt, registry, _settings = _bootstrap(_state(ctx))
    _nonterminal(
        "show configuration running",
        ["appliance", "fabric", *registry.cli_names(Scope.ORCHESTRATOR)],
    )


@configuration_app.command("_kind", hidden=True)
def show_configuration_kind(
    ctx: typer.Context,
    tokens: Annotated[list[str] | None, typer.Argument(metavar="KIND [INSTANCE]")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """`show configuration <kind> [<instance>]` — Orchestrator scope.

    Reached through `_KindFallbackGroup`, so `<kind>` can be any registered
    noun without one Typer command per kind.
    """
    args = list(tokens or [])
    if len(args) > 2:
        _fail("usage: ec-cli show configuration KIND [INSTANCE]")
    rt_ctx, registry, _settings = _bootstrap(_state(ctx))
    _render_resource(
        rt_ctx,
        registry,
        token=args[0] if args else None,
        instance=args[1] if len(args) > 1 else None,
        appliance=None,
        as_json=as_json,
    )


@show_app.command("appliance")
def show_appliance(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Appliance hostname (or raw nePk).")],
    domain: Annotated[
        str | None, typer.Argument(metavar="[DOMAIN]", help="Operational domain to read.")
    ] = None,
    view: Annotated[
        str | None,
        typer.Argument(metavar="[VIEW]", help="Domain view, e.g. summary | neighbors | routes."),
    ] = None,
    peer: Annotated[
        str | None, typer.Argument(metavar="[PEER]", help="Drill down to one neighbor.")
    ] = None,
    stale_ok: Annotated[
        bool,
        typer.Option("--stale-ok", help="Accept the Orchestrator's cached copy (Decision 7)."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Operational state of one appliance.

    `bgp` is the first domain (specs/002). Anything else that resolves as a
    configuration kind is the *renamed* form and is refused: it returned
    configuration before #74 and names operational state now — same tokens,
    different data — so answering with either would be exactly the failure
    Principle II exists to prevent.
    """
    if domain is None:
        _nonterminal(f"show appliance {name}", list(APPLIANCE_DOMAINS))
    rt_ctx, registry, _settings = _bootstrap(_state(ctx))
    if domain == "bgp":
        _show_bgp(rt_ctx, name, view, peer, stale_ok=stale_ok, as_json=as_json)
        return
    try:
        kind = _resolve_kind_quietly(registry, domain, name)
    except UnknownKind:
        _fail(
            f"unknown operational domain {domain!r} for appliance {name!r} — "
            f"valid next tokens: {', '.join(APPLIANCE_DOMAINS)}"
        )
    noun = registry.cli_name(kind)
    _fail(
        f"{noun} is configuration, not operational state — use:\n"
        f"    ec-cli show configuration appliance {name} {noun}\n"
        f"'show appliance NAME {noun}' meant that before #74 and now names "
        f"operational state, so it is refused rather than answered with "
        f"different data than it used to return."
    )


def _show_bgp(
    rt_ctx: Ctx,
    name: str,
    view: str | None,
    peer: str | None,
    *,
    stale_ok: bool,
    as_json: bool,
) -> None:
    """`ec-cli show appliance NAME bgp [summary|neighbors [PEER]|routes]`.

    A bare `bgp` lists the leaves and makes **no API call** — #72's guardrail
    that a nonterminal is contextual help, not an implicit expensive fetch.
    """
    from pyecsdwan.reports import bgpstate

    if view is None:
        _nonterminal(
            f"show appliance {name} bgp",
            ["summary", "neighbors [PEER]", "routes (unsupported)"],
        )
    if view == "routes":
        _terminal_outcome(
            Outcome.UNSUPPORTED,
            "no BGP route-table endpoint exists in the supported Orchestrator "
            "or ECOS API, so this view cannot be built from a verified source.",
            remedy=(
                f"route counts are in 'ec-cli show appliance {name} bgp summary' "
                f"(num_bgp_rtes_rcvd, num_ebgp_rtes, num_ibgp_rtes, "
                f"num_subs_installed); per-peer counts are in 'neighbors'. "
                f"If you have a source this missed, 'ec-cli api' reaches it raw."
            ),
            as_json=as_json,
        )
    if view not in ("summary", "neighbors"):
        _fail(f"unknown bgp view {view!r} — valid next tokens: {', '.join(BGP_VIEWS)}")
    if view == "summary" and peer is not None:
        _fail(f"usage: ec-cli show appliance {name} bgp summary")

    result = bgpstate.collect(rt_ctx, name, cached=stale_ok)
    if as_json:
        payload = result.as_json()
        if view == "neighbors" and peer is not None:
            payload["neighbors"] = [n for n in payload["neighbors"] if n["peer_ip"] == peer]
        payload["view"] = view
        payload["status"] = _bgp_status(result, view, peer).value
        console.print_json(json.dumps(payload, default=str))
    elif view == "summary":
        render.render_bgp_summary(console, result)
    else:
        render.render_bgp_neighbors(console, result, peer=peer)

    status = _bgp_status(result, view, peer)
    if status is Outcome.OK:
        return
    if not as_json:
        detail = (
            f"{name} has no BGP neighbor {peer}"
            if status is Outcome.NOT_FOUND
            else (
                f"{name} reports neighborCount={result.neighbor_count} but returned "
                f"{len([n for n in result.neighbors if not n.configured_only])} peer rows; "
                f"the table above is incomplete."
            )
        )
        err_console.print(Text(f"{status.value}: {detail}", style="bold red"))
    raise typer.Exit(status.exit_code)


def _bgp_status(result: Any, view: str | None, peer: str | None) -> Outcome:
    """Which outcome this read reached.

    Computed once and used for both the exit code and the JSON `status`, so a
    script branching on either gets the same answer — two derivations is how
    they come to disagree.

    Ordered most-specific first, because more than one can hold at once and
    only the most specific is worth acting on: a cached response that is also
    incomplete is `partial`, since the incompleteness is the problem and the
    staleness is what the operator already asked for with `--stale-ok`.
    """
    if view == "neighbors" and peer is not None:
        if not any(n.peer_ip == peer for n in result.neighbors):
            return Outcome.NOT_FOUND
    if not result.rows_match_count:
        return Outcome.PARTIAL
    if result.cached:
        # Exit 0: cached data is served only under `--stale-ok`, so this is
        # honouring what was asked for, not degrading it (Decision 7). The
        # status still says so, and the rendering still annotates it.
        return Outcome.STALE
    return Outcome.OK


def _terminal_outcome(
    outcome: Outcome, detail: str, *, remedy: str = "", as_json: bool = False
) -> NoReturn:
    """Report a terminal state and exit with its code (`grammar.md` §5).

    `unsupported` is not printed in red: it is a statement about the product,
    not about the appliance, and colouring it like a failure sends someone to
    debug a healthy device.
    """
    if as_json:
        console.print_json(
            json.dumps({"status": outcome.value, "detail": detail, "remedy": remedy})
        )
    else:
        style = "yellow" if outcome is Outcome.UNSUPPORTED else "bold red"
        err_console.print(Text(f"{outcome.value}: {detail}", style=style))
        if remedy:
            err_console.print(Text(remedy, style="dim"))
    raise typer.Exit(outcome.exit_code)


def _resolve_kind_quietly(registry: Registry, token: str, appliance: str | None) -> str:
    """`_resolve_kind` without the `_fail` on miss — the caller has its own."""
    scope = Scope.APPLIANCE if appliance is not None else Scope.ORCHESTRATOR
    other = Scope.ORCHESTRATOR if scope is Scope.APPLIANCE else Scope.APPLIANCE
    try:
        return registry.resolve_cli(token, scope)
    except UnknownKind:
        return registry.resolve_cli(token, other)


# -- show configuration: the fabric breakdown (#55) and appliance text (#56) ---


@configuration_app.command("fabric")
def show_configuration_fabric(
    ctx: typer.Context,
    section: Annotated[
        str | None,
        typer.Argument(
            metavar="[SECTION]",
            help=f"Scope to one section: {', '.join(fabric.SECTIONS)}.",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Overall deadline (seconds) for the deployment fan-out."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    assume_yes: Annotated[bool, _YES_OPTION] = False,
) -> None:
    """The fabric's Orchestrator-managed configuration, by section.

    With no subcommand this renders the fabric breakdown — overlays and their
    members, template groups and where they are applied, orchestrated security
    policy, the appliance inventory, and each appliance's deployment. With
    `appliance <name>` it reads that appliance's own CLI running-config
    instead.

    Read-only: every call is a GET, and nothing here touches the candidate
    config, the journal, or the transaction engine.

    Partial data is never fatal and never silent — an endpoint that will not
    answer costs its section, which still renders carrying the reason, and the
    command still exits 0. It is an orientation report: the unreachable rows
    are part of the answer, not a failure to produce one.
    """
    try:
        sections = fabric.resolve_sections(section)
    except fabric.UnknownSection as exc:
        # Names the valid sections; an unknown section must not be answered
        # with an empty report.
        _fail(str(exc))
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
    if "deployment" in sections:
        # Only the deployment section reads every appliance; the rest are
        # Orchestrator-level GETs, so scoping to one of those is not a fan-out
        # and must not be gated as though it were.
        _gate_fanout(rt_ctx, assume_yes)
    report = fabric.collect(rt_ctx, section=section, timeout=timeout)
    if as_json:
        console.print_json(json.dumps(fabric.to_payload(report), default=str))
        return
    render_fabric_config(console, report)


def _render_appliance_config(config: applianceconfig.ApplianceConfig) -> None:
    console.print(
        Text(f"# {config.appliance} ({config.ne_pk}) — {config.command}", style="bold")
    )
    # soft_wrap: a running-config line that rich wrapped is a corrupted
    # running-config line. Print it exactly as the appliance sent it.
    console.print(config.text.rstrip("\n"), markup=False, highlight=False, soft_wrap=True)


def _show_run_broadcast(rt_ctx: Ctx, names: list[str], as_json: bool) -> NoReturn:
    """`--broadcast`: one server-side fan-out, execution status only.

    `/broadcastCli` returns a bare GUID and no command output (see
    `reports/applianceconfig`), so this confirms the read ran everywhere it
    was sent. TIMEOUT is reported as failure, never as success.
    """
    result = applianceconfig.broadcast_running_config(rt_ctx, names)
    if as_json:
        console.print_json(json.dumps(result.as_json()))
    else:
        style = "green" if result.ok else "bold red"
        console.print(
            Text(
                f"broadcast {result.command!r} -> {result.outcome.state}"
                f" (action key: {result.action_key or 'none'})",
                style=style,
            )
        )
        if result.outcome.detail:
            console.print(Text(f"  {result.outcome.detail}", style=style))
        for name, ne_pk in result.targets:
            status = result.outcome.per_appliance.get(ne_pk, "")
            line = f"  {name} ({ne_pk}): {status or result.outcome.state}"
            console.print(Text(line, style="dim"))
        console.print(
            Text(
                "note: broadcast reports execution status only, not command output; "
                "run 'ec-cli show configuration appliance <name> --format native' "
                "for the config text.",
                style="dim",
            )
        )
    raise typer.Exit(0 if result.ok else 2)


@configuration_app.command("appliance")
def show_configuration_appliance(
    ctx: typer.Context,
    names: Annotated[
        list[str],
        typer.Argument(
            metavar="NAME [KIND [INSTANCE]]",
            help=(
                "Appliance, then the resource to read. With --format native, "
                "every positional is an appliance name instead."
            ),
        ),
    ],
    format_: Annotated[
        str | None,
        typer.Option(
            "--format",
            help="'native' reads the appliance's own configuration text instead.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    broadcast: Annotated[
        bool,
        typer.Option(
            "--broadcast",
            help=(
                "Dispatch the read through /broadcastCli in one server-side fan-out "
                "and report execution status (no command output)."
            ),
        ),
    ] = False,
) -> None:
    """One appliance's configuration: normalized by kind, or its own text.

    Two shapes behind one command, split by the flag exactly as the shell
    splits them (Principle IV): with `--format native` every positional is an
    appliance name and the answer is the vendor's own configuration text;
    without it the first positional is the appliance and the rest address one
    resource.

    Read-only either way: the native path sends a module constant vetted by
    the read-only allowlist in `pyecsdwan.reports.applianceconfig`, and the
    resource path is one GET. Neither stages a candidate, journals a
    transaction, or saves changes.
    """
    if not names:
        _fail("usage: ec-cli show configuration appliance NAME [KIND [INSTANCE]]")
    rt_ctx, registry, _settings = _bootstrap(_state(ctx))
    if format_ is not None and format_ != "native":
        _fail(
            f"--format {format_!r} is not valid here; 'native' selects the "
            f"appliance's own configuration text"
        )
    if format_ is None:
        if broadcast:
            _fail("--broadcast applies to --format native only")
        if len(names) > 3:
            _fail("usage: ec-cli show configuration appliance NAME [KIND [INSTANCE]]")
        _render_resource(
            rt_ctx,
            registry,
            token=names[1] if len(names) > 1 else None,
            instance=names[2] if len(names) > 2 else None,
            appliance=names[0],
            as_json=as_json,
        )
        return
    if broadcast:
        _show_run_broadcast(rt_ctx, names, as_json)

    if len(names) == 1:
        # One appliance: no pool, and a ResolveError/OrchApiError surfaces as
        # the clean top-level error rather than an "unreachable" row.
        config = applianceconfig.fetch_running_config(rt_ctx, names[0])
        if as_json:
            console.print_json(
                json.dumps(
                    {
                        "command": config.command,
                        "appliances": [config.as_json()],
                        "unreachable": [],
                    }
                )
            )
        else:
            _render_appliance_config(config)
        return

    outcomes = applianceconfig.fetch_running_configs(rt_ctx, names)
    good = [o.value for o in outcomes if o.done and o.value is not None]
    bad = [(o.item, o.error) for o in outcomes if o.unreachable]
    if as_json:
        console.print_json(
            json.dumps(
                {
                    "command": applianceconfig.RUNNING_CONFIG_COMMAND,
                    "appliances": [c.as_json() for c in good],
                    "unreachable": [{"appliance": n, "error": e} for n, e in bad],
                }
            )
        )
    else:
        for index, config in enumerate(good):
            if index:
                console.print()
            _render_appliance_config(config)
        for name, error in bad:
            err_console.print(Text(f"{name}: unreachable — {error}", style="yellow"))
    if bad:
        # A partial report is not a success: a script diffing configs must not
        # read a missing appliance as an unchanged one.
        raise typer.Exit(2)


# -- entrypoint ---------------------------------------------------------------


# `running` is the same three commands, registered from the same functions.
# Not `candidate`: that is a different datastore, and it is never implicit.
configuration_app.add_typer(running_app, name="running")
running_app.command("fabric")(show_configuration_fabric)
running_app.command("appliance")(show_configuration_appliance)
running_app.command("_kind", hidden=True)(show_configuration_kind)


def main() -> None:
    """Console-script entrypoint (``ec-cli``)."""
    try:
        app()
    except (
        txn.CommitError,
        OrchApiError,
        ResolveError,
        UnknownKind,
        CandidateCorruptError,
        CandidateFormatError,
        vault.VaultUnavailable,
        vault.VaultOpenError,
        ValueError,
    ) as exc:
        if _DEBUG:
            raise
        # One classifier, shared with the shell, so the same failure exits with
        # the same code on both surfaces (grammar.md §5, R7). Everything here
        # used to be exit 2, which made "permission denied", "appliance
        # unreachable" and "you typed it wrong" indistinguishable to a script.
        outcome = outcomes.classify(exc)
        message = str(exc.args[0]) if exc.args else str(exc)
        style = "yellow" if outcome is outcomes.Outcome.UNSUPPORTED else "bold red"
        err_console.print(Text(f"{outcome.value}: {message}", style=style))
        err_console.print(Text("(re-run with --debug for details)", style="dim"))
        raise SystemExit(outcome.exit_code) from None


if __name__ == "__main__":
    main()
