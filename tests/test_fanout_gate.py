"""The fan-out cost gate (`grammar.md` §6, Decision 7).

The thing being guarded against is *elapsed time*, not danger: every fan-out
command here is read-only, so this is not a safety prompt and must not behave
like one. What it owes the operator is the two figures they cannot see for
themselves before the command starts — how many appliances, and roughly how
long.

Two branches, and the second is the one that matters most: piped or scripted,
a prompt cannot be answered and would hang the pipeline, which is exactly the
failure class #78 exists to remove. So it warns and proceeds. Both are tested
here, because getting the TTY detection right while only exercising the
interactive path would leave the hanging case unexercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console

from pyecsdwan import config
from pyecsdwan.cli import fanout
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.resolver import Resolver

#: Enough appliances that the estimate clears QUIET_THRESHOLD_SECONDS at the
#: default latency — the mock's three never would, so a test written against
#: the seeded fabric would pass with the whole gate deleted.
BIG_FABRIC = 40


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def ctx(state_home: Any, mock_server: tuple[str, MockState], wide_fabric: Any) -> Ctx:
    """A Ctx over a fabric big enough for the gate to have something to say.

    `state_home` is not incidental: without a per-test cache directory the
    resolver reads a cache another test persisted, makes no HTTP call, and so
    records no latency — which made the estimate depend on test order.
    """
    base_url, mstate = mock_server
    mstate.reset()
    wide_fabric(mstate, BIG_FABRIC)
    settings = config.Settings(
        orch_url=base_url, api_key="test-key",
        job_timeout=5.0, job_poll_initial=0.01, job_poll_max=0.02,
    )
    client = OrchClient(settings)
    rt_ctx = Ctx(client=client, resolver=Resolver(client))
    rt_ctx.resolver.appliance_names()  # warm the cache, as a real session would
    return rt_ctx


@pytest.fixture
def slow_links(monkeypatch: pytest.MonkeyPatch) -> float:
    """Pin the observed per-call latency instead of measuring it.

    The mock answers in single-digit milliseconds, so a 40-appliance fan-out
    against it is estimated at well under a second and the gate correctly stays
    quiet. Any test of what the gate *says* therefore has to state the link
    speed rather than measure it — otherwise it passes or fails on how fast the
    test machine happened to be that run, which is not a property of the gate.
    (Found the hard way: the first version of these tests passed or failed
    depending on test order.)
    """
    monkeypatch.setattr(OrchClient, "observed_latency_ms", property(lambda _: 300.0))
    return 300.0


def test_the_mock_is_too_fast_to_trip_the_gate_on_its_own(ctx: Ctx) -> None:
    """Why `slow_links` exists, asserted rather than assumed.

    If the mock ever became slow enough to cross the threshold, every test
    below would start passing for the wrong reason — they would no longer be
    testing the pinned latency they claim to.
    """
    measured = ctx.client.observed_latency_ms
    assert measured is not None
    assert fanout.estimate_seconds(ctx, BIG_FABRIC) < fanout.QUIET_THRESHOLD_SECONDS


class _Consoles:
    def __init__(self) -> None:
        self.out = Console(record=True, width=200)
        self.err = Console(record=True, width=200)

    def text(self) -> str:
        return self.out.export_text() + self.err.export_text()


# -- the estimate ------------------------------------------------------------


def test_the_duration_is_coarse_and_hedged() -> None:
    """A figure like "1.7 minutes" claims a precision the input does not have.

    The estimate is appliance count times a latency sample; presenting that to
    one decimal place would invite an operator to plan against it.
    """
    assert fanout.describe_duration(3) == "a few seconds"
    assert fanout.describe_duration(42) == "around 40 seconds"
    assert fanout.describe_duration(45) == "around 50 seconds"
    assert fanout.describe_duration(150) == "around 3 minutes"
    assert fanout.describe_duration(61 * 60) == "around 60 minutes"

    # Rounds half *up*: an estimate that under-states the wait is the unsafe
    # direction, and `round()` is banker's rounding (2.5 -> 2).
    assert fanout.describe_duration(150) == "around 3 minutes"
    assert fanout.describe_duration(90) == "around 2 minutes"
    for seconds in (0.4, 9.9, 45, 200, 4000):
        rendered = fanout.describe_duration(seconds)
        assert "." not in rendered, rendered
        assert rendered.startswith(("a few", "around ")), rendered


def test_the_estimate_uses_the_latency_this_client_has_seen(ctx: Ctx) -> None:
    """A constant is wrong on every fabric, and wrong by different amounts."""
    assert ctx.client.observed_latency_ms is not None, "the fixture made calls"
    from_client = fanout.estimate_seconds(ctx, appliances=10)

    fresh = OrchClient(ctx.client.settings)
    assert fresh.observed_latency_ms is None
    from_default = 10 * fanout.DEFAULT_LATENCY_MS / 1000.0
    assert fanout.estimate_seconds(
        Ctx(client=fresh, resolver=Resolver(fresh)), appliances=10
    ) == pytest.approx(from_default)
    # And the mock is far faster than the placeholder, so the two differ.
    assert from_client < from_default


def test_more_calls_per_appliance_is_a_longer_wait(ctx: Ctx) -> None:
    one = fanout.estimate_seconds(ctx, appliances=10, calls_each=1)
    three = fanout.estimate_seconds(ctx, appliances=10, calls_each=3)
    assert three == pytest.approx(one * 3)


# -- when it stays quiet -----------------------------------------------------


def test_a_short_wait_is_not_worth_interrupting_anyone_over(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A three-appliance lab fabric answering in a second must not prompt.

    Same fixture, shrunk to the seeded size — the threshold is the only thing
    under test, so the fabric is what has to change.
    """
    monkeypatch.setattr(OrchClient, "observed_latency_ms", property(lambda _: 300.0))
    monkeypatch.setattr(
        ctx.resolver, "appliance_names", lambda: ["HUB1-EC", "BR1-EC", "BR2-EC"]
    )
    monkeypatch.setattr("builtins.input", _never_called)
    consoles = _Consoles()
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=True)
    assert consoles.text() == ""
    # And it is the threshold doing that, not the gate being inert: the same
    # fabric with a slow link crosses it.
    monkeypatch.setattr(OrchClient, "observed_latency_ms", property(lambda _: 5000.0))
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=False)
    assert "3 appliances" in consoles.text(), consoles.text()


def test_yes_skips_the_gate_entirely(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes must not merely auto-answer: it must not ask, and must not warn."""
    monkeypatch.setattr("builtins.input", _never_called)
    consoles = _Consoles()
    fanout.confirm(
        ctx, console=consoles.out, err_console=consoles.err, assume_yes=True, interactive=True
    )
    assert consoles.text() == ""


def test_an_unreachable_resolver_proceeds_silently_rather_than_inventing_a_count(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate exists to inform; a made-up number misinforms, which is worse
    than saying nothing. The unreachable Orchestrator is the command's problem
    to report, not the gate's to pre-empt."""

    def boom() -> list[str]:
        raise ConnectionError("orchestrator unreachable")

    monkeypatch.setattr(ctx.resolver, "appliance_names", boom)
    monkeypatch.setattr("builtins.input", _never_called)
    consoles = _Consoles()
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=True)
    assert consoles.text() == ""


def test_the_warning_itself_costs_no_api_call(ctx: Ctx, slow_links: float) -> None:
    """A gate that spends a request to say a request is expensive is its own
    joke. The count comes from the resolver cache."""
    calls: list[str] = []
    original = ctx.client.request

    def counting(method: str, path: str, **kwargs: Any) -> Any:
        calls.append(f"{method} {path}")
        return original(method, path, **kwargs)

    ctx.client.request = counting  # type: ignore[method-assign]
    consoles = _Consoles()
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=False)
    assert calls == [], calls
    assert "appliances" in consoles.text(), consoles.text()


# -- the two branches --------------------------------------------------------


def _never_called(*args: Any, **kwargs: Any) -> str:
    raise AssertionError("prompted when it must not have")


def test_a_pipeline_is_warned_and_not_asked(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that matters: a prompt nobody can answer is a hung pipeline."""
    monkeypatch.setattr("builtins.input", _never_called)
    consoles = _Consoles()
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=False)

    err = consoles.err.export_text()
    assert "warning" in err, err
    assert str(BIG_FABRIC) in err, err
    assert "around" in err, err
    # Warnings on stderr so piped output stays machine-parseable.
    assert consoles.out.export_text().strip() == ""


def test_the_prompt_names_both_figures(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Count and duration: the two things the operator cannot see for
    themselves before the command runs."""
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    consoles = _Consoles()
    fanout.confirm(ctx, console=consoles.out, err_console=consoles.err, interactive=True)
    out = consoles.out.export_text()
    assert str(BIG_FABRIC) in out and "appliances" in out, out
    assert "around" in out, out


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES"])
def test_yes_in_any_spelling_proceeds(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    fanout.confirm(ctx, console=Console(), err_console=Console(), interactive=True)


@pytest.mark.parametrize("answer", ["n", "N", "no", "", "  ", "maybe"])
def test_anything_but_yes_declines(
    ctx: Ctx, slow_links: float, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """Default-no: the prompt reads `[y/N]`, and an operator who hit return
    while reading did not agree to a two-minute wait."""
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    with pytest.raises(fanout.FanoutDeclined):
        fanout.confirm(ctx, console=Console(), err_console=Console(), interactive=True)


# -- reached from both surfaces ----------------------------------------------
#
# The gate is only worth having where the fan-out actually happens, and the two
# surfaces route to it separately. These check the wiring, not the gate.


def _shell_state(ctx: Ctx, state_home: Any) -> Any:
    from pyecsdwan.candidate import CandidateStore
    from pyecsdwan.cli.shell import ShellState
    from pyecsdwan.registry import default_registry

    return ShellState(
        ctx=ctx,
        registry=default_registry,
        settings=ctx.client.settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(ctx.client.settings.host),
    )


@pytest.mark.parametrize(
    "line",
    ["show fabric version", "show fabric flows summary", "show configuration fabric"],
)
def test_the_shell_warns_before_a_fabric_command(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    from pyecsdwan.cli.shell import dispatch_operational

    monkeypatch.setattr("builtins.input", _never_called)  # not a TTY under pytest
    state = _shell_state(ctx, state_home)
    dispatch_operational(line, state)
    out = state.console.export_text()
    assert "warning" in out and f"{BIG_FABRIC} appliances" in out, out


def test_the_shell_yes_flag_skips_the_warning(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyecsdwan.cli.shell import dispatch_operational

    monkeypatch.setattr("builtins.input", _never_called)
    state = _shell_state(ctx, state_home)
    dispatch_operational("show fabric version --yes", state)
    out = state.console.export_text()
    assert "warning" not in out, out
    # The flag was consumed, not mistaken for an argument.
    assert "usage:" not in out, out


def test_declining_is_not_a_failure(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exiting non-zero would make a deliberate "not now" indistinguishable
    from the command breaking."""
    from pyecsdwan.cli.shell import dispatch_operational

    monkeypatch.setattr("builtins.input", lambda *_: "n")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    state = _shell_state(ctx, state_home)
    dispatch_operational("show fabric version", state)
    out = state.console.export_text()
    assert "cancelled" in out, out
    assert state.exit_code == 0, out


def test_a_section_that_is_not_a_fan_out_is_not_gated(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `deployment` reads every appliance; the rest are Orchestrator GETs.

    Gating them would train the operator to skip a prompt that is usually
    wrong, which is how a prompt stops being read.
    """
    from pyecsdwan.cli.shell import dispatch_operational

    monkeypatch.setattr("builtins.input", _never_called)
    state = _shell_state(ctx, state_home)
    dispatch_operational("show configuration fabric overlays", state)
    assert "warning" not in state.console.export_text()
    # And the section that *is* a fan-out still warns, so the check above is
    # the section selection working rather than the gate being unreachable.
    state.console = Console(record=True, width=200)
    dispatch_operational("show configuration fabric deployment", state)
    assert "warning" in state.console.export_text()


def test_the_scriptable_cli_gates_only_the_fan_out_section(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both surfaces, because the section check is written out in each."""
    from typer.testing import CliRunner

    from pyecsdwan.cli import main as cli_main

    monkeypatch.setattr("builtins.input", _never_called)
    port = str(ctx.client.settings.orch_url).rsplit(":", 1)[1]
    runner = CliRunner()
    quiet = runner.invoke(
        cli_main.app, ["--mock", port, "show", "configuration", "fabric", "overlays"]
    )
    assert quiet.exit_code == 0, quiet.output
    assert "warning" not in quiet.output, quiet.output
    loud = runner.invoke(
        cli_main.app, ["--mock", port, "show", "configuration", "fabric", "deployment"]
    )
    assert loud.exit_code == 0, loud.output
    assert "warning" in loud.output, loud.output
    structlog.reset_defaults()


def test_the_scriptable_cli_warns_and_proceeds(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from pyecsdwan.cli import main as cli_main

    monkeypatch.setattr("builtins.input", _never_called)
    port = str(ctx.client.settings.orch_url).rsplit(":", 1)[1]
    result = CliRunner().invoke(
        cli_main.app, ["--mock", port, "show", "fabric", "flows", "summary"]
    )
    assert result.exit_code == 0, result.output
    assert "warning" in result.output and f"{BIG_FABRIC} appliances" in result.output
    structlog.reset_defaults()


def test_naming_appliances_bounds_the_cost_the_operator_is_warned_about(
    ctx: Ctx, slow_links: float, state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--appliance` narrows the fan-out, so the operator already bounded it."""
    from typer.testing import CliRunner

    from pyecsdwan.cli import main as cli_main

    monkeypatch.setattr("builtins.input", _never_called)
    port = str(ctx.client.settings.orch_url).rsplit(":", 1)[1]
    result = CliRunner().invoke(
        cli_main.app,
        ["--mock", port, "show", "fabric", "flows", "summary", "--appliance", "BR1-EC"],
    )
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output, result.output
    structlog.reset_defaults()
