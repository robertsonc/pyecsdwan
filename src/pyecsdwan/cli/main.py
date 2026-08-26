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
import sys
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast

import structlog
import typer
import yaml
from rich.console import Console
from rich.table import Table
from rich.text import Text

from pyecsdwan import config, runtime, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import render
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import Ctx, Ref, Resource, Reversibility, Scope, Tier
from pyecsdwan.journal import TxnJournal, TxnState, list_txns
from pyecsdwan.registry import Registry, UnknownKind, default_registry
from pyecsdwan.resolver import ResolveError

console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    help="Transactional CLI for HPE Aruba EdgeConnect SD-WAN.",
    pretty_exceptions_show_locals=False,
)
show_app = typer.Typer(help="Read-only views of fabric and CLI state.")
cache_app = typer.Typer(help="Resolver cache maintenance.")
app.add_typer(show_app, name="show")
app.add_typer(cache_app, name="cache")

_API_METHODS = ("get", "post", "put", "delete")

#: Set by the app callback; controls traceback visibility in main().
_DEBUG = False


@dataclasses.dataclass
class _State:
    """Per-invocation global options plus the lazily built runtime bundle."""

    orch_url: str | None = None
    insecure: bool = False
    debug: bool = False
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


def _startup_scan() -> None:
    """Warn (never block) about orphaned unconfirmed transactions."""
    orphans = txn.pending_rollbacks()
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
                orch_url=state.orch_url, insecure=state.insecure
            )
        except RuntimeError as exc:
            _fail(str(exc))
        state.booted = (ctx, registry, settings)
        _startup_scan()
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
    try:
        return int(raw)
    except ValueError:
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
    state = _State(orch_url=orch_url, insecure=insecure, debug=debug)
    if mock is not None:
        # Plain-http URLs pass through OrchClient untouched, so http is allowed.
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
    unconfirmed = [
        t
        for t in list_txns()
        if t.meta.state == TxnState.APPLIED_UNCONFIRMED and t.meta.orch_host == settings.host
    ]
    if unconfirmed:
        report = txn.confirm_pending(settings)
        render.render_report(console, report)
        raise typer.Exit(0 if report.ok else 2)
    candidate = CandidateStore(settings.host)
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
        orphans = txn.pending_rollbacks()
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
def show_pending() -> None:
    """Orphaned/unconfirmed transactions needing operator attention."""
    orphans = txn.pending_rollbacks()
    if not orphans:
        console.print("no pending transactions")
        return
    render.render_journal_table(console, orphans)


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


@show_app.command("coverage")
def show_coverage() -> None:
    """Resource kinds and their transactional guarantees."""
    registry = _registry_only()
    table = Table(title="resource coverage")
    table.add_column("kind")
    table.add_column("scope")
    table.add_column("reversibility")
    table.add_column("tier", justify="right")
    table.add_column("notes")
    for kind in registry.kinds():
        resource = registry.get(kind)
        table.add_row(
            kind,
            resource.scope.value,
            resource.reversibility.value,
            str(int(resource.tier)),
            _coverage_notes(resource),
        )
    console.print(table)


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


# -- entrypoint ---------------------------------------------------------------


def main() -> None:
    """Console-script entrypoint (``ec-cli``)."""
    try:
        app()
    except (txn.CommitError, OrchApiError, ResolveError, UnknownKind) as exc:
        if _DEBUG:
            raise
        message = str(exc.args[0]) if exc.args else str(exc)
        err_console.print(Text(f"error: {message}", style="bold red"))
        err_console.print(Text("(re-run with --debug for details)", style="dim"))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
