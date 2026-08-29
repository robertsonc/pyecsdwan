"""The offline command reference (#77, taxonomy T4).

Two properties carry the weight, and neither is "the table looks right":

1. **Every row is a command the parser accepts.** A reference that documents
   commands which do not exist is worse than no reference — it sends an
   operator to type something that fails.
2. **Every offerable noun appears in a row.** A kind the CLI will happily
   accept but never mentions is undiscoverable, which is the complaint #77
   opens with.

Together they close the loop in both directions, which is what makes this a
generated view rather than a hand-maintained list that drifts. The first
version of `build()` hand-added a `show commands` row *and* generated one from
the parser's table, producing a duplicate — so the drift it guards against is
not hypothetical.

And it all runs offline: no Orchestrator, no credentials, no network.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.cli import reference
from pyecsdwan.cli.main import app
from pyecsdwan.contract import Scope, Tier
from pyecsdwan.registry import default_registry

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture
def rows() -> list[reference.CommandRow]:
    return reference.build(default_registry)


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, Any]]:
    from pyecsdwan.mock.server import run_in_thread

    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def shell_state(state_home: Any, mock_server: tuple[str, Any]) -> Any:
    """A shell pointed at the bundled mock, for the round-trip test below.

    The reference itself is offline; checking that what it documents actually
    parses is not, because the parser reaches a fabric.
    """
    from pyecsdwan import config
    from pyecsdwan.candidate import CandidateStore
    from pyecsdwan.cli.shell import ShellState
    from pyecsdwan.client import OrchClient
    from pyecsdwan.contract import Ctx
    from pyecsdwan.resolver import Resolver

    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(
        orch_url=base_url, api_key="test-key",
        job_timeout=5.0, job_poll_initial=0.01, job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=default_registry,
        settings=settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(settings.origin),
    )


# -- the two properties -----------------------------------------------------


def _runnable(command: str) -> str:
    """Substitute the reference's placeholders for values the mock fabric has.

    Optional positions are dropped rather than filled: the reference documents
    them as optional, so the shortest accepted form is the one to check.
    """
    line = re.sub(r"\[[^\]]*\]", "", command)  # [<instance>], [<ip>], [a|b|c]
    line = line.replace("<name>", "BR1-EC").replace("<ip>", "10.1.2.3")
    return " ".join(line.split())


def test_every_row_is_a_command_the_parser_accepts(
    rows: list[reference.CommandRow], shell_state: Any
) -> None:
    """The reference's central claim, checked by running it.

    Placeholders are substituted and every line goes through the real
    dispatcher. A reference that documents commands which do not exist is
    worse than no reference: it sends an operator to type something that
    fails, and they have no way to tell whose fault that is.

    Nonterminals and `unsupported` are fine — those are answers. The one
    outcome that must never occur is `invalid`, which is by definition the
    parser saying it does not recognise what was typed.

    Asserted on the *outcome*, not on message text: the first version of this
    test looked for the phrases "unknown command" / "unknown resource kind",
    and a deliberately broken row (`show fabric versions`) sailed past it
    because that branch says "unknown domain". Exit codes do not have
    synonyms.
    """
    from pyecsdwan.cli.outcomes import Outcome
    from pyecsdwan.cli.shell import dispatch_operational

    for row in rows:
        line = _runnable(row.command)
        shell_state.console = Console(record=True, width=200)
        shell_state.exit_code = 0
        dispatch_operational(line, shell_state)
        out = shell_state.console.export_text()
        assert out.strip(), f"no output at all for a documented command: {line}"
        assert shell_state.exit_code != Outcome.INVALID.exit_code, (line, out)


def test_every_offerable_noun_appears_in_a_row(rows: list[reference.CommandRow]) -> None:
    """A kind the CLI accepts but never mentions is undiscoverable — #77's
    opening complaint."""
    text = " ".join(r.command for r in rows)
    for scope in (Scope.ORCHESTRATOR, Scope.APPLIANCE):
        for noun in default_registry.cli_names(scope):
            assert f" {noun} " in f" {text} ", noun


def test_no_command_is_listed_twice(rows: list[reference.CommandRow]) -> None:
    """The first version of `build()` hand-added a `show commands` row that
    the parser's own table already generated. Nothing here is hand-listed
    precisely so that cannot happen; this asserts it."""
    duplicates = [cmd for cmd, n in Counter(r.command for r in rows).items() if n > 1]
    assert not duplicates, duplicates


def test_the_registry_nouns_drive_the_rows(rows: list[reference.CommandRow]) -> None:
    """Guards the guard: a `build()` that returned only the hand-written rows
    would still satisfy "every row is valid", so the count has to move with
    the registry."""
    curated = sum(
        len(default_registry.cli_names(scope))
        for scope in (Scope.ORCHESTRATOR, Scope.APPLIANCE)
    )
    assert curated > 20, curated
    assert len(rows) > curated


# -- what each column claims -------------------------------------------------


def test_a_stub_is_listed_as_unsupported_rather_than_hidden() -> None:
    """A Tier-1 stub's `normalize()` raises by design. Omitting it would make
    the CLI look like the kind does not exist, which is a different and worse
    answer than "there, but not curated"."""
    stubs = [k for k in default_registry.kinds() if default_registry.get(k).tier < Tier.CURATED]
    assert stubs, "expected at least one generated stub"
    rows = reference.build(default_registry)
    text = " ".join(r.command for r in rows)

    # Named, by the only spelling they have. `cli_names()` excludes them from
    # *completion* on purpose — offering a name whose normalize() raises is a
    # dead end — and an earlier version of build() inherited that exclusion,
    # so the reference silently omitted them and this test passed anyway on
    # the strength of `bgp routes`. Asserting the stubs by name is what closes
    # that gap.
    for kind in stubs:
        noun = default_registry.cli_name(kind)
        assert noun in text, noun
        row = next(r for r in rows if f" {noun} " in f"{r.command} ")
        assert row.support.startswith("unsupported"), row
        assert "not curated" in row.mutability, row

    unsupported = [r for r in rows if r.support.startswith("unsupported")]
    assert all(":" in r.support for r in unsupported), unsupported


def test_the_route_view_with_no_source_is_listed_and_explained(
    rows: list[reference.CommandRow],
) -> None:
    """#72 finding 2, carried into the reference: hiding it would make the CLI
    look like it never considered the question."""
    routes = [r for r in rows if r.command.endswith("bgp routes")]
    assert len(routes) == 1, routes
    assert "no BGP route-table endpoint" in routes[0].support


def test_address_comes_from_the_declared_contract_not_a_guess() -> None:
    """`Resource.deletable` is documented as False for singleton tables with
    no absent state, so it is the existing per-kind declaration of exactly
    this — rather than something inferred from a name."""
    rows = reference.build(default_registry)
    # Keyed by (scope, noun): `zones` exists in both scopes as two different
    # objects, and keying on the noun alone silently collapses them — the same
    # collision that forced the per-scope alias namespace in #77.
    by_key = {
        (r.scope, r.command.split()[-2]): r
        for r in rows
        if r.command.endswith("[<instance>]")
    }
    for scope in (Scope.ORCHESTRATOR, Scope.APPLIANCE):
        for noun in default_registry.cli_names(scope):
            resource = default_registry.get(default_registry.resolve_cli(noun, scope))
            expected = "named" if resource.deletable else "singleton"
            row = by_key[(scope.value, noun)]
            assert row.address == expected, (scope.value, noun, row)


def test_both_addressing_shapes_actually_occur() -> None:
    """A column where every row says the same thing is a column that is not
    being computed — this fails if `deletable` stops discriminating."""
    addresses = {r.address for r in reference.build(default_registry)}
    assert {"named", "singleton"} <= addresses, addresses


def test_the_intent_split_is_visible(rows: list[reference.CommandRow]) -> None:
    """The taxonomy's whole point: configuration and operational state are
    different commands over different sources."""
    intents = {r.intent for r in rows}
    assert intents == {reference.CLI_STATE, reference.OPERATIONAL, reference.CONFIGURATION}
    # And the same noun can appear under two scopes without colliding.
    zones = sorted(r.command for r in rows if r.command.endswith(" zones [<instance>]"))
    assert len(zones) == 2, zones


# -- offline is the requirement ---------------------------------------------


def test_it_runs_with_no_orchestrator_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """T4's acceptance criterion. Any HTTP call at all fails this."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the command reference must not touch the network")

    monkeypatch.setattr("pyecsdwan.client.OrchClient.request", boom)
    monkeypatch.setenv("ECSDWAN_API_KEY", "unused")
    result = runner.invoke(app, ["--orch-url", "https://nowhere.invalid", "show", "commands"])
    assert result.exit_code == 0, result.output
    assert "supported" in result.output


def test_it_needs_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deciding whether this tool can do a thing should not require an
    account on a fabric."""
    monkeypatch.delenv("ECSDWAN_API_KEY", raising=False)
    result = runner.invoke(app, ["--orch-url", "https://nowhere.invalid", "show", "commands"])
    assert result.exit_code == 0, result.output


# -- filters and machine output ---------------------------------------------


def test_the_json_carries_every_column() -> None:
    result = runner.invoke(
        app, ["--orch-url", "https://nowhere.invalid", "show", "commands", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload
    assert set(payload[0]) == {"command", "intent", "scope", "address", "mutability", "support"}


def test_the_unsupported_filter_is_the_useful_one() -> None:
    """"What can this tool not do?" is the question an operator asks before
    they waste an afternoon."""
    result = runner.invoke(
        app,
        ["--orch-url", "https://nowhere.invalid", "show", "commands", "--unsupported", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload
    assert all(row["support"].startswith("unsupported") for row in payload)
    assert any("bgp routes" in row["command"] for row in payload)


def test_an_unknown_intent_names_the_valid_ones() -> None:
    result = runner.invoke(
        app, ["--orch-url", "https://nowhere.invalid", "show", "commands", "--intent", "wibble"]
    )
    assert result.exit_code != 0
    assert "cli-state" in result.output and "operational" in result.output


def test_an_empty_filter_result_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty table is indistinguishable from the command having done
    nothing (Principle II, #78)."""
    monkeypatch.setattr(reference, "build", lambda registry: [])
    result = runner.invoke(app, ["--orch-url", "https://nowhere.invalid", "show", "commands"])
    assert result.exit_code == 0, result.output
    assert "no commands match" in result.output


# -- the shell says the same thing ------------------------------------------


def test_the_shell_renders_the_same_reference() -> None:
    """Principle IV, and the reason the rows are built in one place."""
    from pyecsdwan.cli.render import render_command_reference

    console = Console(record=True, width=200)
    render_command_reference(console, reference.build(default_registry))
    out = console.export_text()
    assert "operational" in out and "configuration" in out
    assert "never the Orchestrator" in out
