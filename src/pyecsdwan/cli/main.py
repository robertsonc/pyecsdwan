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

from pyecsdwan import config, runtime, specs, txn
from pyecsdwan import registry as registry_mod
from pyecsdwan.candidate import CandidateCorruptError, CandidateStore
from pyecsdwan.cli import render
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import Ctx, Ref, Resource, Reversibility, Scope, Tier
from pyecsdwan.journal import TxnJournal, TxnState, list_txns
from pyecsdwan.registry import Registry, UnknownKind, default_registry
from pyecsdwan.reports import DEFAULT_CONCURRENCY, applianceconfig, versions
from pyecsdwan.reports import flows as flows_report
from pyecsdwan.resolver import ResolveError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="Transactional CLI for HPE Aruba EdgeConnect SD-WAN.",
    pretty_exceptions_show_locals=False,
)
show_app = typer.Typer(help="Read-only views of fabric and CLI state.")
cache_app = typer.Typer(help="Resolver cache maintenance.")
plugin_app = typer.Typer(help="Resource-plugin tooling (promotion checklist).")
flows_app = typer.Typer(help="Active-flow reports (see also: show flow <ip>).")
app.add_typer(show_app, name="show")
app.add_typer(cache_app, name="cache")
app.add_typer(plugin_app, name="plugin")
show_app.add_typer(flows_app, name="flows")

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


def _startup_scan(host: str) -> None:
    """Warn (never block) about orphaned unconfirmed transactions for HOST."""
    orphans = txn.pending_rollbacks(host=host)
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
        _startup_scan(settings.host)
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


def _resource_for(registry: Registry, kind: str) -> Resource:
    if kind not in registry:
        known = ", ".join(registry.kinds()) or "(none)"
        _fail(f"unknown resource kind {kind!r}; known kinds: {known}")
    return registry.get(kind)


def _make_ref(registry: Registry, kind: str, name: str, appliance: str | None) -> Ref:
    resource = _resource_for(registry, kind)
    if resource.scope is Scope.APPLIANCE and appliance is None:
        _fail(f"kind {kind!r} is appliance-scoped; pass --appliance NAME")
    if resource.scope is Scope.ORCHESTRATOR and appliance is not None:
        _fail(f"kind {kind!r} is orchestrator-scoped; --appliance does not apply")
    return Ref(kind=kind, name=name, appliance=appliance)


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
    candidate = CandidateStore(settings.host)
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
    candidate = CandidateStore(settings.host)
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
    candidate = CandidateStore(settings.host)
    if merge:
        for key, value in data.items():
            candidate.set_path(ref, [str(key)], value)
    else:
        # Replace mode is a full overwrite: a section the file omits will be
        # normalized to empty and DELETED on apply. Warn if the file is a
        # strict subset of the resource's known top-level sections so the
        # operator isn't surprised by a silent wipe (they still see it in
        # `show | compare`, but a heads-up here is cheap).
        resource = _resource_for(registry, kind)
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
    candidate = CandidateStore(settings.host)
    plan = txn.build_plan(rt_ctx, registry, candidate)
    render.render_plan(console, plan)
    raise typer.Exit(0 if plan.empty else 1)


app.command("compare", help="Alias for 'diff'.")(diff_)


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
            help="Allow tier-0/1 resources inside a commit-confirm window.",
        ),
    ] = False,
) -> None:
    """Apply the candidate changeset (a bare commit inside a confirm window confirms)."""
    state = _state(ctx)
    rt_ctx, registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.host)
    unconfirmed = [
        t
        for t in list_txns()
        if t.meta.state == TxnState.APPLIED_UNCONFIRMED and t.meta.orch_host == settings.host
    ]
    if unconfirmed:
        # A bare commit confirms the pending window. But if the user also
        # staged new changes or passed options, they meant a fresh commit —
        # refuse rather than silently confirming and dropping the new work.
        options_passed = bool(
            confirm_minutes or force or override_template or allow_untransactional
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
    plan = txn.build_plan(rt_ctx, registry, candidate)
    if plan.empty:
        console.print("no changes")
        raise typer.Exit(0)
    render.render_plan(console, plan)
    report = txn.commit(
        rt_ctx,
        registry,
        plan,
        settings,
        confirm_minutes=confirm_minutes,
        force=force,
        override_template=override_template,
        allow_untransactional=allow_untransactional,
    )
    if report.ok:
        candidate.clear()
    render.render_report(console, report)
    raise typer.Exit(0 if report.ok else 2)


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
    candidate = CandidateStore(settings.host)
    count = len(candidate)
    candidate.clear()
    console.print(f"discarded {count} candidate item(s)")


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
        orphans = txn.pending_rollbacks(host=settings.host)
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


@show_app.command("journal")
def show_journal() -> None:
    """All journaled transactions, newest first."""
    render.render_journal_table(console, list_txns())


@show_app.command("pending")
def show_pending(ctx: typer.Context) -> None:
    """Orphaned/unconfirmed transactions needing operator attention."""
    _rt, _registry, settings = _bootstrap(_state(ctx))
    orphans = txn.pending_rollbacks(host=settings.host)
    if not orphans:
        console.print("no pending transactions")
        return
    render.render_journal_table(console, orphans)


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


@show_app.command("version")
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
) -> None:
    """Orchestrator version, then every appliance's active and backup partitions.

    Read-only: two GETs and a bounded fan-out. Never touches the candidate
    config, the journal, or the transaction engine.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
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
    table.add_column("endpoints", justify="right")
    table.add_column("notes")
    for kind in kinds:
        resource = registry.get(kind)
        table.add_row(
            kind,
            resource.scope.value,
            resource.reversibility.value,
            str(int(resource.tier)),
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
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Resource kinds, the endpoints they cover, and the transactional tier.

    Offline: reads the vendored ``specs/`` baselines and the plugin registry,
    never the Orchestrator.
    """
    registry = _registry_only()
    if kind is not None and kind not in registry:
        _fail(f"unknown resource kind {kind!r}; known kinds: {', '.join(registry.kinds())}")
    if scope is not None and scope not in specs.SCOPES:
        _fail(f"unknown scope {scope!r}; expected one of: {', '.join(specs.SCOPES)}")

    kinds = [
        k
        for k in registry.kinds()
        if (kind is None or k == kind)
        and (tier is None or int(registry.get(k).tier) == tier)
        and (scope is None or registry.get(k).scope.value == scope)
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
                    "notes": _coverage_notes(registry.get(k)),
                    "endpoints": list(registry.get(k).endpoints),
                }
                for k in kinds
            ],
            "undeclared_in_spec": unknown,
        }
        if endpoints:
            payload["endpoints"] = [dataclasses.asdict(row) for row in rows]
        console.print_json(json.dumps(payload, default=str))
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
        "no API specs vendored (looked for specs/*-openapi-*.json, or "
        f"${specs.ENV_SPECS_DIR}); per-endpoint coverage is unavailable — "
        "the resource-kind table above is complete on its own"
    )


@show_app.command("candidate")
def show_candidate(ctx: typer.Context) -> None:
    """Dump the staged candidate changeset (intent as YAML)."""
    state = _state(ctx)
    _rt, _registry, settings = _bootstrap(state)
    candidate = CandidateStore(settings.host)
    items = candidate.ordered_items()
    if not items:
        console.print("candidate is empty")
        return
    for item in items:
        console.print(Text(f"{item.ref_key} (mode: {item.mode})", style="bold"))
        if item.intent:
            dumped = yaml.safe_dump(item.intent, sort_keys=True, default_flow_style=False)
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
) -> None:
    """Active flow counts per appliance per overlay, with row and column totals.

    Read-only: one GET /flow per appliance and nothing else. Counts come from
    the returned rows because the response's computed summary carries no
    per-overlay breakdown -- see ``pyecsdwan.reports.flows``.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
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


@show_app.command("flow")
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
) -> None:
    """Every flow touching an address, fabric-wide, deduped across appliances.

    Matching is done by the Orchestrator via ipEitherFlag -- the address is
    matched at either end of the flow server-side, never by pulling every flow
    and filtering here. Read-only.
    """
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
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
    journal = TxnJournal.create(settings.host, [Ref(kind="api", name=f"{method_upper} {path}")])
    journal.append(
        "RAW_API",
        method=method_upper,
        path=path,
        params=params,
        body_sha256=body_sha,
        status="sent",
    )
    try:
        if appliance is not None:
            ne_pk = rt_ctx.resolver.ne_pk_for(appliance)
            response = rt_ctx.client.appliance_request(
                method_upper, ne_pk, _with_params(path, params), json_body=body_data
            )
        else:
            response = rt_ctx.client.request(
                method_upper, path, json_body=body_data, params=params or None
            )
    except (OrchApiError, ResolveError, ValueError) as exc:
        journal.set_state(TxnState.AUDIT_ONLY, response_summary=f"error: {exc}")
        if isinstance(exc, ValueError):
            _fail(str(exc))
        raise
    journal.set_state(TxnState.AUDIT_ONLY, response_summary=_summarize_response(response))
    _print_response(response)


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
    resource enumerates on this Orchestrator."""
    if name is not None:
        return _make_ref(registry, resource.kind, name, appliance)
    refs = list(resource.list_refs(rt_ctx))
    if not refs:
        _fail(
            f"{resource.kind} enumerates no instances on this Orchestrator; "
            f"name one with --name (and --appliance for appliance scope)"
        )
    if appliance is not None:
        scoped = [r for r in refs if r.appliance == appliance]
        if not scoped:
            _fail(f"{resource.kind} enumerates no instances on appliance {appliance!r}")
        return scoped[0]
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
    resource = _resource_for(registry, kind)
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
                    "checks": [dataclasses.asdict(c) for c in checks],
                },
                default=str,
            )
        )
    else:
        console.print(_check_table(kind, checks))
        if ref is not None:
            console.print(Text(f"sampled {ref}", style="dim"))

    if failed:
        if not as_json:
            err_console.print(
                Text(f"{len(failed)} checklist box(es) failed — not ready for Tier 2", style="red")
            )
        raise typer.Exit(1)
    if curated:
        if not as_json:
            console.print(Text(f"ok: {kind} still meets its Tier-2 obligations", style="green"))
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
            f"{kind} is tier {int(resource.tier)} (generated). It correctly refuses "
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


# -- show run: appliance CLI running-config (#56) ------------------------------
#
# `ec-cli show run appliance <name> [<name>...]`. Declared as its own Typer
# group under `show` so the fabric-wide bare `show run` (#55) is a one-line
# addition: give `run_app` a `@run_app.callback(invoke_without_command=True)`
# that renders the fabric report when no subcommand was given. Nothing here
# needs to move for that.
run_app = typer.Typer(help="Running-config views.")
show_app.add_typer(run_app, name="run")


@run_app.callback()
def show_run() -> None:
    """Running-config views.

    The fabric-wide bare `show run` (#55) plugs in here: add
    `invoke_without_command=True` to this callback and render the fabric
    report when `ctx.invoked_subcommand is None`.
    """


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
                "run 'ec-cli show run appliance <name>' for the config text.",
                style="dim",
            )
        )
    raise typer.Exit(0 if result.ok else 2)


@run_app.command("appliance")
def show_run_appliance(
    ctx: typer.Context,
    names: Annotated[
        list[str],
        typer.Argument(metavar="NAME...", help="Appliance hostname(s) (or raw nePk)."),
    ],
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
    """An appliance's CLI running-config, read over the Orchestrator proxy.

    Read-only: the command sent to the appliance is a module constant vetted
    by the read-only allowlist in `pyecsdwan.reports.applianceconfig`; nothing
    here stages a candidate, journals a transaction, or saves changes.
    """
    if not names:
        _fail("usage: ec-cli show run appliance NAME [NAME...]")
    rt_ctx, _registry, _settings = _bootstrap(_state(ctx))
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
        ValueError,
    ) as exc:
        if _DEBUG:
            raise
        message = str(exc.args[0]) if exc.args else str(exc)
        err_console.print(Text(f"error: {message}", style="bold red"))
        err_console.print(Text("(re-run with --debug for details)", style="dim"))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
