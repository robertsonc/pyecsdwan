"""The README's commands are executed, not just written.

`README.md` documented `ec-cli plugin promote appliance/bgp` for as long as it
took anyone to notice — which was until this file existed. #74 withdrew
registry keys as command tokens and every test in the suite stayed green,
because no test read the README.

Documentation that is not executed drifts, and it drifts in the worst
direction: the first thing a new user types is the thing the README showed
them, so a stale example is a broken first impression that the authors never
see. This runs the read-only examples against the bundled mock.

Deliberately narrow: it checks that documented commands *parse and run*, not
what they print. Asserting on output would make the README a golden file and
every rendering tweak a doc change.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.outcomes import Outcome
from pyecsdwan.mock.server import MockState, run_in_thread

README = Path(__file__).resolve().parents[1] / "README.md"
runner = CliRunner()

#: Verbs skipped, with the reason. Mutating commands would need a transaction
#: and a candidate; `api` is Tier-0 passthrough whose whole point is that it is
#: not curated; `load` and `diff` need files or staged state. What is left is
#: every read, which is what a new user types first.
SKIP_VERBS = {
    "api": "Tier-0 passthrough, deliberately uncurated",
    "commit": "mutating",
    "diff": "needs staged state",
    "load": "needs a YAML file",
    "rollback": "mutating",
    "set": "mutating",
}


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


def _examples() -> list[str]:
    """Every `ec-cli ...` line the README shows, comments stripped."""
    found = []
    for line in README.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("ec-cli ") or stripped.startswith("./ec-cli "):
            found.append(re.sub(r"\s+#.*$", "", stripped))
    return found


def _read_only(examples: list[str]) -> list[list[str]]:
    out = []
    for line in examples:
        args = line.split()[1:]
        if not args or args[0] in SKIP_VERBS or args[0].startswith("-"):
            continue
        out.append(args)
    return out


def test_the_readme_actually_contains_examples() -> None:
    """Guards the guard: a parser that finds nothing would make every
    assertion below vacuously true, which is exactly how this class of test
    quietly stops working."""
    examples = _examples()
    assert len(examples) >= 10, examples
    assert len(_read_only(examples)) >= 5, examples


@pytest.mark.parametrize("args", _read_only(_examples()), ids=lambda a: " ".join(a))
def test_every_documented_read_command_runs(
    args: list[str], state_home: Any, mock_server: tuple[str, MockState]
) -> None:
    """The first thing a new user types is what the README showed them.

    Asserted on the exit code rather than the output: `invalid` means the
    parser rejected a documented command, which is the failure this exists to
    catch. What the command prints is not the README's business.
    """
    base_url, mstate = mock_server
    mstate.reset()
    port = base_url.rsplit(":", 1)[1]
    result = runner.invoke(cli_main.app, ["--mock", port, *args])
    assert result.exit_code != Outcome.INVALID.exit_code, (args, result.output)
    assert result.exit_code == 0, (args, result.output)


def test_no_example_spells_a_registry_key() -> None:
    """#74 withdrew `appliance/<kind>` as a command token, and the README kept
    showing `plugin promote appliance/bgp` until this test was written.

    Checked as text as well as by running, because a key that happens to be a
    valid *kind* argument somewhere would run and still teach the wrong thing.
    """
    for line in _examples():
        assert "appliance/" not in line, line
        assert "generated/" not in line, line
