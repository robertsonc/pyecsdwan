"""One grammar across both interfaces (#74, Principle IV).

The shell and the scriptable CLI are two parsers over one command tree. They
were written at different times against different token sets, and the drift was
real: `banners` worked at the prompt and `plugin promote banners` answered
"unknown resource kind". Nothing structural prevents that recurring — the tree
lives in `shell._show_operational` and in Typer's command registry, and the two
have to be kept in step by hand.

So this module tests the *correspondence* rather than either surface: for each
row of `grammar.md` §7, the same command spelled both ways must reach the same
place. It is the test that fails when one surface is migrated and the other is
not, which is exactly what happened between the two commits that built this.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.shell import ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

runner = CliRunner()


@dataclasses.dataclass(frozen=True)
class Answer:
    """What one command produced on each surface."""

    shell: str
    cli: str
    cli_exit: int

    def both(self) -> Iterator[tuple[str, str]]:
        yield "shell", self.shell
        yield "cli", self.cli


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
def both(state_home: Any, mock_server: tuple[str, MockState]) -> Any:
    """Run one command through both surfaces; return (shell_text, cli_text)."""
    base_url, mstate = mock_server
    mstate.reset()
    port = base_url.rsplit(":", 1)[1]
    settings = config.Settings(
        orch_url=base_url, api_key="test-key",
        job_timeout=5.0, job_poll_initial=0.01, job_poll_max=0.02,
    )

    def _run(line: str) -> Answer:
        client = OrchClient(settings)
        state = ShellState(
            ctx=Ctx(client=client, resolver=Resolver(client)),
            registry=default_registry,
            settings=settings,
            console=Console(record=True, width=200),
            candidate=CandidateStore(settings.host),
        )
        dispatch_operational(line, state)
        result = runner.invoke(cli_main.app, ["--mock", port, *line.split()])
        return Answer(
            shell=state.console.export_text(),
            cli=result.output,
            cli_exit=result.exit_code,
        )

    return _run


def _normalize(text: str) -> str:
    """Compare content, not chrome.

    The two surfaces render through different consoles at different widths and
    one prefixes errors with `error:`, so a byte-for-byte match would fail on
    presentation and say nothing about the grammar.
    """
    text = text.replace("ec-cli ", "")
    text = re.sub(r"^\s*error:\s*", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


#: `grammar.md` §7, restricted to the rows both surfaces implement today. The
#: operational appliance domains are specified but not built (specs/002), so
#: their rows are the refusal, tested separately below.
WORKED_EXAMPLES = [
    "show configuration appliance BR1-EC banners",
    "show configuration running appliance BR1-EC banners",
    "show configuration appliance BR1-EC banners global",
    "show configuration candidate",
    "show configuration interface-labels",
    "show configuration zones",
    "show configuration fabric",
    "show configuration fabric security",
    # The first operational domain (#72): configuration and state now sit
    # under different tokens, and both surfaces must reach both.
    "show appliance BR1-EC bgp summary",
    "show appliance BR1-EC bgp neighbors",
    "show appliance BR1-EC bgp neighbors 10.127.1.1",
]


@pytest.mark.parametrize("line", WORKED_EXAMPLES)
def test_both_surfaces_accept_the_same_command(both: Any, line: str) -> None:
    answer = both(line)
    for surface, text in answer.both():
        assert text.strip(), f"{surface} produced nothing for: {line}"
        assert "unknown command" not in text.lower(), (surface, line, text)
        assert "no such command" not in text.lower(), (surface, line, text)
        assert "usage:" not in text.lower(), (surface, line, text)


@pytest.mark.parametrize(
    "line",
    [
        "show configuration appliance BR1-EC banners",
        "show configuration appliance BR1-EC banners global",
        "show configuration interface-labels",
        "show appliance BR1-EC bgp summary",
    ],
)
def test_both_surfaces_return_the_same_resource(both: Any, line: str) -> None:
    """Not merely "both accept it" — both must answer with the same object."""
    answer = both(line)
    assert _normalize(answer.shell) == _normalize(answer.cli), answer


REMOVED = [
    "show run",
    "show run security",
    "show run appliance BR1-EC",
    "show version",
    "show flows summary",
    "show flow 10.1.2.3",
    "show banners",
]


@pytest.mark.parametrize("line", REMOVED)
def test_neither_surface_accepts_a_removed_form(both: Any, line: str) -> None:
    """A form removed from one surface and left on the other is worse than
    leaving it on both: a script keeps working while the prompt says it does
    not exist, and nobody finds out until the second one is migrated."""
    answer = both(line)
    assert "unknown command" in answer.shell.lower(), (line, answer.shell)
    lowered = answer.cli.lower()
    assert "no such command" in lowered or "usage:" in lowered, (line, answer.cli)
    assert answer.cli_exit != 0, answer.cli


@pytest.mark.parametrize(
    "line",
    [
        "show appliance BR1-EC banners",
        "show appliance BR1-EC zones",
    ],
)
def test_both_surfaces_refuse_the_renamed_form_the_same_way(both: Any, line: str) -> None:
    """The dangerous rename: refused on both, and both say where the
    configuration went. One surface answering it would be the whole problem."""
    answer = both(line)
    for surface, text in answer.both():
        assert "configuration, not operational state" in text, (surface, text)
        assert "show configuration appliance BR1-EC" in text, (surface, text)
        # Refused, not answered: no ref header, so no configuration came back.
        assert "appliance/" not in text.replace("show configuration", ""), (surface, text)
    assert answer.cli_exit != 0, answer.cli


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("show configuration", ["running", "candidate", "appliance", "fabric"]),
        ("show appliance BR1-EC", ["bgp"]),
        ("show appliance BR1-EC bgp", ["summary", "neighbors", "routes"]),
    ],
)
def test_both_surfaces_list_the_same_continuations(
    both: Any, line: str, expected: list[str]
) -> None:
    """D-NSO-2 on both: a valid prefix names what may follow and exits 0."""
    answer = both(line)
    for surface, text in answer.both():
        assert "valid next tokens" in text, (surface, text)
        for token in expected:
            assert token in text, (surface, token, text)
    assert answer.cli_exit == 0, answer.cli


@pytest.mark.parametrize("line", ["show appliance BR1-EC bgp routes"])
def test_both_surfaces_report_an_unsupported_view_identically(both: Any, line: str) -> None:
    """#72 finding 2: no BGP route-table endpoint exists in either baseline.

    The failure this guards against is one surface quietly answering it — from
    the config object, or from parsed CLI text — while the other says it cannot
    be answered. That would make the CLI's honesty depend on which entry point
    the operator used.
    """
    answer = both(line)
    for surface, text in answer.both():
        assert "unsupported" in text, (surface, text)
        # Says why, and what does exist instead.
        assert "no BGP route-table endpoint" in text, (surface, text)
        assert "summary" in text, (surface, text)
        # Not reported as the appliance having failed.
        assert "unreachable" not in text.lower(), (surface, text)
    assert answer.cli_exit == 5, answer.cli


def test_the_datastore_default_agrees_across_surfaces(both: Any) -> None:
    """Decision 1 is a property of the grammar, not of one parser: if one
    surface defaulted to `candidate` the operator would be shown staged intent
    while believing they were looking at the device."""
    implicit = both("show configuration appliance BR1-EC banners")
    explicit = both("show configuration running appliance BR1-EC banners")
    assert _normalize(implicit.shell) == _normalize(explicit.shell)
    assert _normalize(implicit.cli) == _normalize(explicit.cli)
    assert _normalize(implicit.shell) == _normalize(implicit.cli)
    # And neither reaches the candidate, which has to be named to be read.
    candidate = both("show configuration candidate")
    for surface, text in candidate.both():
        assert "banners" not in text, (surface, text)
