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

import pytest

from pyecsdwan.cli.outcomes import CommandOutcome, Outcome

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
