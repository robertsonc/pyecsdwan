"""The outcome taxonomy, pinned to the spec it was copied from (R7).

`cli/outcomes.py` is a table transcribed by hand from `grammar.md` §5, and a
hand-copied table drifts from its source without anyone noticing — the code
keeps working, the spec keeps saying something else, and the disagreement only
surfaces when a script branches on an exit code that moved.

So the assertion here is not "these are the right codes" (that is the spec's
job) but "these are the spec's codes". The spec table is parsed and compared
whole: every outcome, every exit code, both directions, so neither a row added
to the markdown nor a member added to the enum can pass unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from pyecsdwan.cli.outcomes import CommandOutcome, Outcome, classify
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import NotCurated
from pyecsdwan.resolver import ResolveError

SPEC = Path(__file__).resolve().parents[1] / "specs/001-cli-command-taxonomy/grammar.md"

#: | `ok` | Result, non-empty | 0 | the result | `ok` |
_ROW = re.compile(
    r"^\|\s*`(?P<name>\w+)`\s*\|[^|]*\|\s*(?P<exit>\d+)\s*\|[^|]*\|\s*`(?P<status>\w+)`\s*\|$"
)


def _spec_table() -> dict[str, tuple[int, str]]:
    """Parse §5's outcome table out of the grammar."""
    section = SPEC.read_text(encoding="utf-8").split("## 5. Outcomes", 1)
    assert len(section) == 2, "grammar.md has no '## 5. Outcomes' section"
    body = section[1].split("\n## ", 1)[0]
    rows = {}
    for line in body.splitlines():
        match = _ROW.match(line.strip())
        if match:
            rows[match["name"]] = (int(match["exit"]), match["status"])
    return rows


def test_the_spec_table_actually_parsed() -> None:
    """Guards the guard: a regex that matches nothing would make every
    assertion below vacuously true."""
    table = _spec_table()
    assert len(table) == 11, table
    assert table["ok"] == (0, "ok")
    assert table["partial"] == (8, "partial")


def test_every_spec_outcome_exists_in_code() -> None:
    missing = set(_spec_table()) - {o.value for o in Outcome}
    assert not missing, f"grammar.md §5 names outcomes the code does not have: {missing}"


def test_every_code_outcome_exists_in_the_spec() -> None:
    """The other direction, so an invented outcome cannot slip in unspecified."""
    extra = {o.value for o in Outcome} - set(_spec_table())
    assert not extra, f"code has outcomes the spec does not define: {extra}"


@pytest.mark.parametrize("name", sorted(_spec_table()))
def test_the_exit_code_matches_the_spec(name: str) -> None:
    expected_exit, expected_status = _spec_table()[name]
    outcome = Outcome(name)
    assert outcome.exit_code == expected_exit
    assert outcome.value == expected_status


# -- the distinctions the table exists to preserve --------------------------


def test_an_answer_is_not_a_failure() -> None:
    """Principle II: `empty` and `stale` are answers, so they exit 0.

    An empty configuration reported as a failure sends an operator to debug a
    healthy appliance; cached data reported as a failure makes `--stale-ok`
    useless, since it was asked for.
    """
    assert Outcome.OK.is_success
    assert Outcome.EMPTY.is_success
    assert Outcome.STALE.is_success


def test_every_other_outcome_is_distinguishable_from_success() -> None:
    for outcome in Outcome:
        if outcome in (Outcome.OK, Outcome.EMPTY, Outcome.STALE):
            continue
        assert not outcome.is_success, outcome
        assert outcome.exit_code != 0, outcome


def test_the_pairs_that_must_not_collapse() -> None:
    """Each of these is a distinction a careless implementation loses, and
    each one changes what the operator does next."""
    # "no configuration here" vs "no such object"
    assert Outcome.EMPTY.exit_code != Outcome.NOT_FOUND.exit_code
    # "this API has no endpoint for that" vs "the appliance failed"
    assert Outcome.UNSUPPORTED.exit_code != Outcome.ERROR.exit_code
    assert Outcome.UNSUPPORTED.exit_code != Outcome.UNREACHABLE.exit_code
    # "reached some targets" vs "answered the question"
    assert Outcome.PARTIAL.exit_code != Outcome.OK.exit_code
    # "you typed it wrong" vs "it is not there"
    assert Outcome.INVALID.exit_code != Outcome.NOT_FOUND.exit_code


def test_unreachable_and_timeout_share_a_code_deliberately() -> None:
    """Both mean "the target did not answer within the budget". Splitting them
    would imply a distinction a caller cannot act on differently — but the
    JSON status still tells them apart, for a human reading a log."""
    assert Outcome.UNREACHABLE.exit_code == Outcome.TIMEOUT.exit_code
    assert Outcome.UNREACHABLE.value != Outcome.TIMEOUT.value


def test_a_command_outcome_carries_what_to_do_instead() -> None:
    """"unsupported" on its own sends the operator to guess."""
    exc = CommandOutcome(
        Outcome.UNSUPPORTED,
        "no BGP route-table endpoint exists in the supported API",
        remedy="the route counts in `show appliance BR1-EC bgp summary`",
    )
    assert exc.outcome is Outcome.UNSUPPORTED
    assert "route-table" in str(exc)
    assert "summary" in exc.remedy


# -- classification: which outcome a raised exception represents ------------
#
# One table, applied at both dispatch boundaries. Before it, every failure was
# exit 2 in the scriptable CLI and exit 0 in the shell — so "permission
# denied", "appliance unreachable" and "you typed it wrong" were the same
# answer to a script. That is #78's complaint at the level of the process
# contract rather than the rendering.

def _api_error(status: int | None, cause: BaseException | None = None) -> OrchApiError:
    exc = OrchApiError("GET", "/bgp/state", status, "detail")
    if cause is not None:
        exc.__cause__ = cause
    return exc


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, Outcome.DENIED),
        (403, Outcome.DENIED),
        (404, Outcome.NOT_FOUND),
        (408, Outcome.TIMEOUT),
        (504, Outcome.TIMEOUT),
        (501, Outcome.UNSUPPORTED),
        (400, Outcome.ERROR),
        (500, Outcome.ERROR),
        (502, Outcome.ERROR),
    ],
)
def test_an_api_status_maps_to_its_outcome(status: int, expected: Outcome) -> None:
    assert classify(_api_error(status)) is expected


def test_denied_is_not_unreachable() -> None:
    """A 403 means the credential lacks the right. Sending the operator to
    check the network wastes their time on a reachable Orchestrator."""
    assert classify(_api_error(403)) is Outcome.DENIED
    assert classify(_api_error(None)) is Outcome.UNREACHABLE
    assert Outcome.DENIED.exit_code != Outcome.UNREACHABLE.exit_code


def test_no_response_at_all_is_unreachable_or_timeout_by_cause() -> None:
    """`OrchApiError` carries no status for a transport failure, so the only
    thing separating "did not answer in time" from "was not reachable" is the
    exception it was raised from — which the client preserves with `from`."""
    assert classify(_api_error(None, httpx.ConnectError("refused"))) is Outcome.UNREACHABLE
    assert classify(_api_error(None, httpx.ReadTimeout("slow"))) is Outcome.TIMEOUT
    assert classify(_api_error(None, httpx.ConnectTimeout("slow"))) is Outcome.TIMEOUT


def test_a_bare_transport_error_is_classified_too() -> None:
    """Not everything reaches the boundary wrapped: the resolver and the
    reports call httpx paths that can raise directly."""
    assert classify(httpx.ConnectError("refused")) is Outcome.UNREACHABLE
    assert classify(httpx.ReadTimeout("slow")) is Outcome.TIMEOUT


def test_timeout_is_ordered_before_the_general_transport_error() -> None:
    """`TimeoutException` is a subclass of `HTTPError`, so a check in the
    wrong order would classify every timeout as unreachable and the JSON
    status would lose a distinction the exit code cannot carry."""
    assert isinstance(httpx.ReadTimeout("x"), httpx.HTTPError)
    assert classify(httpx.ReadTimeout("x")) is Outcome.TIMEOUT


def test_a_tier_one_stub_is_unsupported_not_an_error() -> None:
    """This tool declining to guess, not the appliance failing."""
    assert classify(NotCurated("generated/whatever has no curated normalize()")) is (
        Outcome.UNSUPPORTED
    )


def test_an_unknown_name_is_not_found_not_invalid() -> None:
    """The path was well-formed; the object named by it does not exist."""
    assert classify(ResolveError("unknown appliance 'S1-ecv-01'")) is Outcome.NOT_FOUND


def test_a_usage_error_is_invalid() -> None:
    assert classify(ValueError("usage: show configuration fabric [<section>]")) is (
        Outcome.INVALID
    )


def test_an_explicit_command_outcome_passes_through() -> None:
    """A command that already decided its terminal state is not re-guessed."""
    raised = CommandOutcome(Outcome.PARTIAL, "9 of 10 appliances answered")
    assert classify(raised) is Outcome.PARTIAL


def test_anything_unrecognised_is_error_not_success() -> None:
    """The default must never be an outcome that exits 0: an exception nobody
    classified is still a command that did not answer."""
    assert classify(RuntimeError("something nobody anticipated")) is Outcome.ERROR
    assert not Outcome.ERROR.is_success


# -- both dispatch boundaries -----------------------------------------------
#
# The classifier is only useful where it is actually reached, and the two
# surfaces reach it differently. `CliRunner.invoke(app)` calls the Typer app
# directly, so it never runs `cli_main.main()` — the very handler that turns an
# escaping exception into an exit code. Testing the scriptable side through
# CliRunner alone would leave that path unexercised, so these drive `main()`.


@pytest.fixture
def failing_orchestrator(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make every API call fail in a chosen way, from the client outward."""

    def _fail_with(exc: BaseException) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr("pyecsdwan.client.OrchClient.request", boom)

    return _fail_with


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OrchApiError("GET", "/gms/versions", 403, "forbidden"), Outcome.DENIED),
        (OrchApiError("GET", "/gms/versions", 500, "boom"), Outcome.ERROR),
        (OrchApiError("GET", "/gms/versions", None, "refused"), Outcome.UNREACHABLE),
    ],
)
def test_the_scriptable_cli_exits_with_the_classified_code(
    failing_orchestrator: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
    expected: Outcome,
) -> None:
    from pyecsdwan.cli import main as cli_main

    failing_orchestrator(failure)
    monkeypatch.setenv("ECSDWAN_HOME", str(tmp_path))
    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    monkeypatch.setattr(
        "sys.argv",
        ["ec-cli", "--orch-url", "https://nowhere.invalid", "show", "fabric", "version"],
    )
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == expected.exit_code, failure


def test_every_failure_used_to_be_exit_two(
    failing_orchestrator: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The point of the change, stated as a property: three different failures
    that a script must handle differently now exit differently."""
    from pyecsdwan.cli import main as cli_main

    codes = set()
    for failure in (
        OrchApiError("GET", "/gms/versions", 403, "forbidden"),
        OrchApiError("GET", "/gms/versions", 500, "boom"),
        OrchApiError("GET", "/gms/versions", None, "refused"),
    ):
        failing_orchestrator(failure)
        monkeypatch.setenv("ECSDWAN_HOME", str(tmp_path))
        monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
        monkeypatch.setattr(
            "sys.argv",
            ["ec-cli", "--orch-url", "https://nowhere.invalid", "show", "fabric", "version"],
        )
        with pytest.raises(SystemExit) as excinfo:
            cli_main.main()
        codes.add(excinfo.value.code)
    assert len(codes) == 3, codes
    assert 0 not in codes


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OrchApiError("GET", "/bgp/state", 403, "forbidden"), Outcome.DENIED),
        (OrchApiError("GET", "/bgp/state", 404, "no such"), Outcome.NOT_FOUND),
        (OrchApiError("GET", "/bgp/state", None, "refused"), Outcome.UNREACHABLE),
        (OrchApiError("GET", "/bgp/state", 500, "boom"), Outcome.ERROR),
    ],
)
def test_the_shell_sets_the_classified_exit_code(
    failing_orchestrator: Any,
    state_home: Any,
    failure: BaseException,
    expected: Outcome,
) -> None:
    """The shell's dispatch boundary catches everything so the prompt survives
    — which is right, and used to mean every failure looked identical."""
    from rich.console import Console

    from pyecsdwan.candidate import CandidateStore
    from pyecsdwan.cli.shell import ShellState, dispatch_operational
    from pyecsdwan.client import OrchClient
    from pyecsdwan.config import Settings
    from pyecsdwan.contract import Ctx
    from pyecsdwan.registry import default_registry
    from pyecsdwan.resolver import Resolver

    settings = Settings(orch_url="https://nowhere.invalid", api_key="test-key")
    client = OrchClient(settings)
    state = ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=default_registry,
        settings=settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(settings.origin),
    )
    failing_orchestrator(failure)
    dispatch_operational("show appliance BR1-EC bgp summary", state)

    assert state.exit_code == expected.exit_code
    out = state.console.export_text()
    assert expected.value in out, out
    # And the prompt survived it, which is the other half of #78.
    assert state.running
