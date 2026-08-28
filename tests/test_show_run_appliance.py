"""`show configuration appliance <name>` — appliance CLI running-config (issue #56).

The deliverable this file guards is the **allowlist**, not the printing. Every
smuggling shape gets its own case: a second command appended with `;`, `&&`,
`|`, a newline, a carriage return, a tab, leading/trailing/repeated whitespace,
unicode whitespace posing as a separator, and a `show`-prefixed word that is
not the `show` verb. The rule under test is deny-by-default: a positive
character alphabet plus an exact-match verb set, so a shape nobody thought of
is refused by construction rather than by having been listed.

Non-ASCII smuggling cases are written as `\\u....` escapes on purpose — a
literal NBSP in a test file is invisible in review, which is exactly the
property that makes it a good attack and a bad test.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, journal
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.shell import ShellCompleter, ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, JobOutcome
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import applianceconfig
from pyecsdwan.reports.applianceconfig import CommandRefused, validate_command
from pyecsdwan.resolver import ResolveError, Resolver

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    # The app callback binds structlog process-wide to CliRunner's capture
    # stream, which is closed on exit; leaving it bound kills the next test
    # that logs. (Same teardown as tests/test_coverage.py.)
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def fabric(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "settings": settings,
        "state": mstate,
        "port": base_url.rsplit(":", 1)[1],
    }


class _ExplodingClient:
    """Any network call is a test failure: a refused command must never be sent."""

    settings = None

    def appliance_request(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused command reached the appliance proxy")

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a refused command reached the Orchestrator")


class _ExplodingResolver:
    def ne_pk_for(self, name: str) -> str:
        raise AssertionError("a refused command got as far as name resolution")


def _sealed_ctx() -> Ctx:
    return Ctx(client=_ExplodingClient(), resolver=_ExplodingResolver())  # type: ignore[arg-type]


# -- the allowlist -----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "show running-config",
        "show fabric version",
        "display running-config",
        "show interfaces gigabit0/1",
        "show ip route 10.0.0.0/8",
        "show tunnel to_HUB1-EC",
        "show ipv6 neighbors fe80::1",
    ],
)
def test_read_commands_are_accepted_unchanged(command: str) -> None:
    """Accepted commands come back byte-identical — the validator never
    normalizes, so it and the appliance can never disagree about what ran."""
    assert validate_command(command) == command


def test_the_only_command_this_module_issues_passes_its_own_gate() -> None:
    """Defence in depth: the constant is vetted like anything else, so the
    allowlist can never drift away from the one command that depends on it."""
    assert validate_command(applianceconfig.RUNNING_CONFIG_COMMAND) == "show running-config"


@pytest.mark.parametrize(
    ("command", "why"),
    [
        # -- a second command smuggled behind a separator --
        ("show running-config; reload", "semicolon"),
        ("show running-config && reload", "double ampersand"),
        ("show running-config & reload", "single ampersand"),
        ("show running-config | reload", "pipe"),
        ("show running-config\nreload", "newline"),
        ("show running-config\r\nreload", "crlf"),
        ("show running-config\rreload", "bare carriage return"),
        ("show running-config\treload", "tab"),
        ("show running-config\x00reload", "nul"),
        ("show running-config\x1b[2Kreload", "ansi escape"),
        ("show running-config\x0breload", "vertical tab"),
        ("show running-config\x0creload", "form feed"),
        ("show running-config`reload`", "backtick substitution"),
        ("show running-config $(reload)", "dollar-paren substitution"),
        ("show running-config > /etc/passwd", "redirect"),
        ("show running-config\\; reload", "escaped separator"),
        ('show "running-config"; reload', "quotes"),
        ("show running-config ! reload", "bang"),
        ("show running-config ?", "context-help question mark"),
        # -- whitespace games --
        (" show running-config", "leading space"),
        ("show running-config ", "trailing space"),
        ("show  running-config", "repeated space"),
        ("\u00a0show running-config", "leading non-breaking space"),
        ("show\u00a0running-config", "non-breaking space as separator"),
        ("show running-config\u2028reload", "unicode line separator"),
        ("show running-config\u2029reload", "unicode paragraph separator"),
        ("show\u3000running-config", "ideographic space"),
        ("show running-config\u0085reload", "unicode next-line"),
        ("\ufeffshow running-config", "byte-order mark"),
        ("show running-config\u200breload", "zero-width space"),
        # -- a `show`-prefixed word that is not the `show` verb --
        ("showtech-and-reload", "show-prefixed single token"),
        ("show_running", "underscore, still one token"),
        ("showrunning-config", "no separator at all"),
        ("show-tech reload", "hyphenated pseudo-verb"),
        ("SHOW running-config", "uppercase verb is not the verb"),
        ("Show running-config", "title-case verb is not the verb"),
        # -- plainly not reads --
        ("reload", "reload"),
        ("configure terminal", "config mode"),
        ("write memory", "write"),
        ("delete running-config", "delete"),
        ("debug generic all", "debug is not allowlisted"),
        ("no shutdown", "no-form"),
        # -- degenerate --
        ("", "empty"),
        (" ", "one space"),
        ("show", "bare verb, no argument"),
        ("display", "bare verb, no argument"),
    ],
)
def test_smuggling_and_non_read_commands_are_refused(command: str, why: str) -> None:
    with pytest.raises(CommandRefused):
        validate_command(command)


def test_an_overlong_command_is_refused() -> None:
    with pytest.raises(CommandRefused):
        validate_command("show " + "a" * applianceconfig.MAX_COMMAND_LENGTH)


def test_refusal_names_what_is_permitted() -> None:
    """An operator who is refused must learn the rule, not just the verdict."""
    with pytest.raises(CommandRefused) as excinfo:
        validate_command("reload")
    message = str(excinfo.value)
    assert "show" in message and "display" in message
    assert "read-only" in message
    # The rejected text is echoed as a repr so control characters cannot move
    # the operator's cursor on the way out.
    with pytest.raises(CommandRefused) as ctrl:
        validate_command("show running-config\r\x1b[2Kall clear")
    assert "\r" not in str(ctrl.value)
    assert "\\r" in str(ctrl.value)


def test_a_refused_command_never_reaches_the_network() -> None:
    """The point of the allowlist: refusal happens before a request exists."""
    ctx = _sealed_ctx()
    for command in ("reload", "show running-config; reload", "configure terminal"):
        with pytest.raises(CommandRefused):
            applianceconfig.run_read_command(ctx, "BR1-EC", command)
        with pytest.raises(CommandRefused):
            applianceconfig.broadcast_read_command(ctx, ["BR1-EC"], command)


def test_command_refused_is_a_value_error() -> None:
    """`ec-cli`'s top-level handler renders ValueError cleanly; a refusal must
    land there rather than as a traceback."""
    assert issubclass(CommandRefused, ValueError)


def test_the_allowlist_is_stricter_than_the_mock() -> None:
    """The mock refuses anything not starting with `show`/`display`. Calibrating
    to it would wave through every one of these, which is why the gate is
    tested against the rule and not against the fixture."""
    mock_would_accept = [
        "show running-config; reload",
        "show running-config\nreload",
        "showtech-and-reload",
        "display foo | reload",
    ]
    for command in mock_would_accept:
        assert command.strip().startswith(("show", "display")), command
        with pytest.raises(CommandRefused):
            validate_command(command)


# -- fetching one appliance ---------------------------------------------------


def test_running_config_comes_back_attributed_to_its_own_appliance(
    fabric: dict[str, Any],
) -> None:
    """The mock puts the hostname in the banner precisely so a report that
    attributes output to the wrong appliance fails here rather than looking
    plausible."""
    br1 = applianceconfig.fetch_running_config(fabric["ctx"], "BR1-EC")
    br2 = applianceconfig.fetch_running_config(fabric["ctx"], "BR2-EC")
    assert br1.appliance == "BR1-EC"
    assert br1.ne_pk == "3.NE"
    assert "# BR1-EC running-config" in br1.text
    assert "BR2-EC" not in br1.text
    assert br2.appliance == "BR2-EC"
    assert br2.ne_pk == "5.NE"
    assert "# BR2-EC running-config" in br2.text
    assert br1.command == "show running-config"


def test_unknown_appliance_gives_a_resolver_error_with_a_suggestion(
    fabric: dict[str, Any],
) -> None:
    with pytest.raises(ResolveError) as excinfo:
        applianceconfig.fetch_running_config(fabric["ctx"], "BR1-ECX")
    assert "unknown appliance" in str(excinfo.value)
    assert "BR1-EC" in str(excinfo.value)


def test_several_appliances_fan_out_in_order_and_isolate_failures(
    fabric: dict[str, Any],
) -> None:
    outcomes = applianceconfig.fetch_running_configs(
        fabric["ctx"], ["BR1-EC", "BR1-ECX", "BR2-EC"]
    )
    assert [o.item for o in outcomes] == ["BR1-EC", "BR1-ECX", "BR2-EC"]
    assert [o.ok for o in outcomes] == [True, False, True]
    assert outcomes[0].value is not None
    assert "# BR1-EC running-config" in outcomes[0].value.text
    assert "unknown appliance" in outcomes[1].error
    assert outcomes[2].value is not None
    assert "# BR2-EC running-config" in outcomes[2].value.text


def test_the_report_is_read_only(fabric: dict[str, Any]) -> None:
    """No candidate, no journal, no transaction, no async job, and nothing on
    the appliance left needing a save."""
    mstate: MockState = fabric["state"]
    settings = fabric["settings"]
    assert journal.list_txns() == []
    applianceconfig.fetch_running_configs(fabric["ctx"], ["BR1-EC", "BR2-EC"])
    assert journal.list_txns() == []
    assert len(CandidateStore(settings.host)) == 0
    # The per-appliance CLI read starts no Orchestrator job at all — a
    # saveChanges (or any other write) would have registered an action key.
    assert mstate.actions == {}
    assert not any(a.get("hasUnsavedChanges") for a in mstate.appliances)


def test_output_is_never_logged(fabric: dict[str, Any]) -> None:
    """A running-config is configuration and some of it is sensitive: the log
    line carries its size, never its content."""
    captured: list[dict[str, Any]] = []

    def _capture(logger: Any, name: str, event: dict[str, Any]) -> dict[str, Any]:
        captured.append(dict(event))
        raise structlog.DropEvent  # capture only; nothing reaches a real sink

    structlog.configure(processors=[_capture], cache_logger_on_first_use=False)
    try:
        result = applianceconfig.fetch_running_config(fabric["ctx"], "BR1-EC")
    finally:
        structlog.reset_defaults()
    assert "hostname BR1-EC" in result.text
    assert captured, "the read should log something"
    for event in captured:
        assert "hostname BR1-EC" not in json.dumps(event, default=str)
    read_events = [e for e in captured if e.get("event") == "appliance_cli_read"]
    assert read_events
    assert read_events[0]["chars"] == len(result.text)


# -- broadcast ----------------------------------------------------------------


def test_broadcast_posts_one_call_and_polls_the_bare_guid(fabric: dict[str, Any]) -> None:
    """`/broadcastCli` answers with a bare JSON string, not an object; it must
    still be polled to a terminal state like any other action key."""
    mstate: MockState = fabric["state"]
    result = applianceconfig.broadcast_running_config(fabric["ctx"], ["BR1-EC", "BR2-EC"])
    assert result.ok
    assert result.outcome.state == "SUCCESS"
    assert result.action_key
    assert result.action_key in mstate.actions
    assert mstate.actions[result.action_key]["ne_pks"] == ["3.NE", "5.NE"]
    assert result.targets == (("BR1-EC", "3.NE"), ("BR2-EC", "5.NE"))
    payload = result.as_json()
    assert payload["mode"] == "broadcast"
    assert [a["appliance"] for a in payload["appliances"]] == ["BR1-EC", "BR2-EC"]


def test_a_failed_broadcast_is_not_a_success(fabric: dict[str, Any]) -> None:
    mstate: MockState = fabric["state"]
    mstate.fail_next_action = True
    result = applianceconfig.broadcast_running_config(fabric["ctx"], ["BR1-EC"])
    assert not result.ok
    assert result.outcome.state == "FAILED"


def test_a_broadcast_timeout_is_a_failure_never_a_success(fabric: dict[str, Any]) -> None:
    """The one way this command could lie: stop waiting and call it done."""
    mstate: MockState = fabric["state"]
    mstate.action_delay_polls = 10_000  # never finishes inside the deadline
    ctx: Ctx = fabric["ctx"]
    ctx.client.settings.job_timeout = 0.05
    result = applianceconfig.broadcast_running_config(ctx, ["BR1-EC", "BR2-EC"])
    assert result.outcome.state == "TIMEOUT"
    assert not result.ok
    assert result.as_json()["ok"] is False


def test_broadcast_needs_at_least_one_appliance() -> None:
    with pytest.raises(ValueError, match="at least one appliance"):
        applianceconfig.broadcast_read_command(_sealed_ctx(), [], "show running-config")


# -- ec-cli -------------------------------------------------------------------


def _cli(fabric: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", fabric["port"], "show", "run", *args])


def test_cli_prints_the_appliances_running_config(fabric: dict[str, Any]) -> None:
    result = _cli(fabric, "appliance", "BR1-EC")
    assert result.exit_code == 0, result.output
    assert "# BR1-EC running-config" in result.output
    assert "hostname BR1-EC" in result.output


def test_cli_json_wraps_the_text_with_the_appliance_it_came_from(
    fabric: dict[str, Any],
) -> None:
    result = _cli(fabric, "appliance", "BR1-EC", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "show running-config"
    assert payload["unreachable"] == []
    entry = payload["appliances"][0]
    assert entry["appliance"] == "BR1-EC"
    assert entry["nePk"] == "3.NE"
    assert "hostname BR1-EC" in entry["text"]


def test_cli_json_attributes_each_appliance_to_its_own_text(fabric: dict[str, Any]) -> None:
    result = _cli(fabric, "appliance", "BR1-EC", "BR2-EC", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    by_name = {a["appliance"]: a["text"] for a in payload["appliances"]}
    assert "# BR1-EC running-config" in by_name["BR1-EC"]
    assert "# BR2-EC running-config" in by_name["BR2-EC"]
    assert "BR2-EC" not in by_name["BR1-EC"]


def test_cli_unknown_appliance_is_a_clean_error_not_a_traceback(
    fabric: dict[str, Any],
) -> None:
    result = _cli(fabric, "appliance", "BR1-ECX")
    assert result.exit_code != 0
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "unknown appliance" in combined
    assert "Traceback" not in result.output


def test_cli_partial_failure_is_reported_and_exits_nonzero(fabric: dict[str, Any]) -> None:
    result = _cli(fabric, "appliance", "BR1-EC", "BR1-ECX", "--json")
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert [a["appliance"] for a in payload["appliances"]] == ["BR1-EC"]
    assert payload["unreachable"][0]["appliance"] == "BR1-ECX"


def test_cli_broadcast_goes_through_broadcastcli(fabric: dict[str, Any]) -> None:
    mstate: MockState = fabric["state"]
    result = _cli(fabric, "appliance", "BR1-EC", "BR2-EC", "--broadcast", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "broadcast"
    assert payload["state"] == "SUCCESS"
    assert payload["actionKey"] in mstate.actions
    assert mstate.actions[payload["actionKey"]]["name"] == "broadcast cli"


def test_cli_broadcast_failure_exits_nonzero(fabric: dict[str, Any]) -> None:
    fabric["state"].fail_next_action = True
    result = _cli(fabric, "appliance", "BR1-EC", "--broadcast")
    assert result.exit_code == 2, result.output


def test_cli_broadcast_timeout_exits_nonzero(
    fabric: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deadline that expired is a failure at the CLI boundary too."""
    timed_out = applianceconfig.BroadcastResult(
        command="show running-config",
        targets=(("BR1-EC", "3.NE"),),
        action_key="abc-123",
        outcome=JobOutcome(key="abc-123", state="TIMEOUT", detail="did not finish"),
    )
    monkeypatch.setattr(
        cli_main.applianceconfig, "broadcast_running_config", lambda *a, **k: timed_out
    )
    result = _cli(fabric, "appliance", "BR1-EC", "--broadcast")
    assert result.exit_code == 2, result.output
    assert "TIMEOUT" in result.output


def test_cli_show_run_alone_is_the_fabric_report(fabric: dict[str, Any]) -> None:
    """The #55/#56 split at the CLI: `show configuration fabric` is the group's
    `invoke_without_command=True` callback (the fabric configuration
    breakdown), `show configuration appliance <name>` is its subcommand. This file owns
    the subcommand half; `tests/test_show_run.py` owns the report itself.

    Was a "Missing command" assertion until #55 filled the callback in — the
    seam existed precisely so this test would have to be changed on purpose.
    """
    result = _cli(fabric)
    assert result.exit_code == 0, result.output
    # The fabric report, not this file's per-appliance running-config.
    assert "overlays" in result.output
    assert "running-config" not in result.output


# -- shell --------------------------------------------------------------------


@pytest.fixture
def shell_state(fabric: dict[str, Any]) -> ShellState:
    return ShellState(
        ctx=fabric["ctx"],
        registry=default_registry,
        settings=fabric["settings"],
        console=Console(record=True, width=200),
        candidate=CandidateStore(fabric["settings"].host),
    )


def _shell(state: ShellState, line: str) -> str:
    dispatch_operational(line, state)
    return state.console.export_text()


def test_shell_show_run_appliance_prints_the_config(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration appliance BR1-EC --format native")
    assert "# BR1-EC (3.NE)" in out
    assert "hostname BR1-EC" in out


def test_shell_show_run_appliance_handles_several_names(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration appliance BR1-EC BR2-EC --format native")
    assert "# BR1-EC running-config" in out
    assert "# BR2-EC running-config" in out


def test_shell_bare_show_run_is_the_fabric_report(shell_state: ShellState) -> None:
    """The same split in the shell: bare `show configuration fabric` is #55's fabric report,
    and it reaches it through the branch that used to raise the usage error.

    Asserted the usage line until #55 landed — changed on purpose, and kept
    here so the two halves of `show configuration fabric` stay pinned from both entry points.
    """
    out = _shell(shell_state, "show configuration fabric")
    assert "overlays" in out
    assert "usage:" not in out


def test_shell_show_run_garbage_is_a_usage_error(shell_state: ShellState) -> None:
    usage = "usage: show configuration [running] fabric [<section>]"
    assert usage in _shell(shell_state, "show configuration fabric wibble")
    assert usage in _shell(shell_state, "show configuration fabric appliance")


def test_shell_unknown_appliance_is_a_red_line_not_a_crash(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration appliance BR1-ECX --format native")
    assert "unknown appliance" in out


def test_shell_show_run_leaves_no_candidate_or_journal(shell_state: ShellState) -> None:
    _shell(shell_state, "show configuration appliance BR1-EC BR2-EC --format native")
    assert journal.list_txns() == []
    assert len(shell_state.candidate) == 0


def test_shell_completes_show_run_appliance_names(shell_state: ShellState) -> None:
    """A third seam test, following the command through the tree: the scope
    noun, then the appliance name, then the flag that selects vendor text."""
    completer = ShellCompleter(shell_state)
    assert "configuration" in completer._options(["show"])
    assert "appliance" in completer._options(["show", "configuration"])
    assert "BR1-EC" in completer._options(["show", "configuration", "appliance"])
    assert "--format" in completer._options(["show", "configuration", "appliance", "BR1-EC"])
    assert completer._options(
        ["show", "configuration", "appliance", "BR1-EC", "--format"]
    ) == ["native"]
