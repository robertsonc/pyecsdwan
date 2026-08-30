"""`?` asks what may come next, at any position (#89).

The shell answers "what can I type here" in two ways already: omit a token and
the parser raises a :class:`Nonterminal` listing the continuations, or press
Tab and the completer offers them. The one spelling an operator coming from
Junos actually reaches for — `?` — was not among them. It was parsed as an
ordinary token, so:

    pyecsdwan> show configuration fabric deployment ?
    invalid: usage: show configuration [running] fabric [<section>]

which is the shell answering a question with a syntax error, and a usage line
on its own reads as "everything you typed was wrong" when in fact everything
before the `?` was accepted.

`?` is now routed through the completer's own tree rather than a second table,
so it and Tab are the same function and cannot drift apart. The test that
matters most here is the one asserting exactly that.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from prompt_toolkit.document import Document
from rich.console import Console

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli.shell import ShellCompleter, ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver


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
def shell(state_home: Any, mock_server: tuple[str, MockState]) -> Any:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)

    def _run(line: str) -> ShellState:
        client = OrchClient(settings)
        state = ShellState(
            ctx=Ctx(client=client, resolver=Resolver(client)),
            registry=default_registry,
            settings=settings,
            console=Console(record=True, width=200),
            candidate=CandidateStore(settings.origin),
        )
        dispatch_operational(line, state)
        return state

    return _run


#: Every position the issue exercised, plus the root.
POSITIONS = [
    ("?", "configure"),
    ("show ?", "configuration"),
    ("show configuration ?", "fabric"),
    ("show configuration fabric ?", "deployment"),
    ("show appliance BR1-EC bgp ?", "summary"),
]


@pytest.mark.parametrize("line,expected", POSITIONS, ids=[p[0] for p in POSITIONS])
def test_the_question_mark_answers_at_every_position(
    shell: Any, line: str, expected: str
) -> None:
    state = shell(line)
    out = state.console.export_text()
    assert expected in out, out
    assert "invalid" not in out, out


@pytest.mark.parametrize("line,_expected", POSITIONS, ids=[p[0] for p in POSITIONS])
def test_asking_is_not_an_error(shell: Any, line: str, _expected: str) -> None:
    """Exit 0. A question answered is not a command that failed, and a script
    that checks exit codes should not see one."""
    assert shell(line).exit_code == 0, line


def test_a_complete_command_says_that_nothing_follows(shell: Any) -> None:
    """The literal line from the issue. `deployment` is a whole view, not a
    path into one, and "nothing follows" is the answer — not silence, and not
    a usage line implying the command was wrong."""
    out = shell("show configuration fabric deployment ?").console.export_text()
    assert "nothing follows this" in out
    assert "invalid" not in out


def test_the_question_mark_and_tab_give_the_same_answer(
    state_home: Any, mock_server: tuple[str, MockState]
) -> None:
    """The property the fix is built on, and the reason `?` reuses the
    completer instead of getting its own table.

    Two grammars that must agree eventually disagree — that is what #74 set out
    to stop, and a second help table would have reintroduced it inside a single
    interface. Here they are the same function, and this asserts it stays that
    way rather than trusting the comment that says so.
    """
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    client = OrchClient(settings)
    state = ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=default_registry,
        settings=settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(settings.origin),
    )
    completer = ShellCompleter(state)

    for prefix in ("show", "show configuration", "show configuration fabric"):
        from_tab = {
            c.text
            for c in completer.get_completions(Document(prefix + " "), None)  # type: ignore[arg-type]
        }
        state.console = Console(record=True, width=200)
        dispatch_operational(f"{prefix} ?", state)
        rendered = state.console.export_text()
        assert from_tab, prefix
        for option in from_tab:
            assert option in rendered, (prefix, option, rendered)


def test_a_surplus_token_names_itself(shell: Any) -> None:
    """The other half of #89: `fabric deployment interfaces` raised the bare
    usage string, which says the shape of the command but not which token was
    the problem — nor that everything before it had been accepted."""
    state = shell("show configuration fabric deployment interfaces")
    out = state.console.export_text()
    assert "'deployment' takes no further tokens" in out
    assert "interfaces" in out
    assert state.exit_code != 0, "this one really is a mistake, unlike `?`"


def test_a_question_mark_that_is_not_last_is_still_a_token(shell: Any) -> None:
    """Guards the trigger from over-reach. Only a *trailing* `?` is the ask;
    mid-line it is an ordinary token and must still be rejected, or a typo in
    the middle of a command would silently print help instead."""
    state = shell("show ? fabric")
    assert state.exit_code != 0
    assert "invalid" in state.console.export_text()
