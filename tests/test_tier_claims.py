"""What the docs say a tier can do, checked against what it does (#68).

The README and the roadmap both claimed a Tier-1 kind could take a "plain
commit; confirm only with `--allow-untransactional`". It cannot, and never
could: `txn.build_plan()` calls `normalize()` on every candidate, and a
generated stub's raises `NotCurated`. A Tier-1 kind is stopped before a plan
exists — it never reaches the `--allow-untransactional` guard at all.

`docs/plugin-promotion.md` had it right the whole time ("a Tier-1 stub cannot
take part in a plan or a commit **at all**"), so the repository contradicted
itself, and the two documents a new user reads first were the wrong ones.

This is the same failure as the README's `plugin promote appliance/bgp`
example, which stayed broken until `tests/test_docs_examples.py` executed it:
a claim nobody runs is a claim that drifts. So the capability here is
*derived* — planned against the bundled mock — and the documents are checked
against the derivation rather than against a hardcoded expectation.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, NotCurated, Ref, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

#: Rich's help renderer styles flag names, splitting them across escape
#: sequences. Stripped before matching — see `test_the_flag_help_does_not_overpromise`.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
ROADMAP = REPO / "docs" / "ROADMAP.md"
PROMOTION = REPO / "docs" / "plugin-promotion.md"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


def _stubs() -> list[str]:
    return [k for k in default_registry.kinds() if default_registry.get(k).tier < Tier.CURATED]


# -- what a Tier-1 kind actually does ----------------------------------------


def test_there_is_a_tier_1_kind_to_check() -> None:
    """Guards the guard. With no sub-curated kind registered, every assertion
    below would pass vacuously — which is how this whole class of check
    quietly stops working."""
    assert _stubs(), "expected at least one generated stub in the registry"


def test_a_tier_1_kind_cannot_be_planned(
    state_home: Any, mock_server: tuple[str, MockState]
) -> None:
    """The derivation the documents are checked against.

    Run against a *working* fabric on purpose: a first attempt at this failed
    with a connection error instead, which would have "proved" the claim for
    entirely the wrong reason. `build_plan()` fetches before it normalizes, so
    the fetch has to succeed for the stop point to mean anything.
    """
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))

    for kind in _stubs():
        resource = default_registry.get(kind)
        ref = (
            Ref(kind, "x", appliance="BR1-EC")
            if resource.scope is Scope.APPLIANCE
            else Ref(kind, "x")
        )
        candidate = CandidateStore(settings.origin)
        candidate.set_path(ref, ["anything"], 1)
        with pytest.raises(NotCurated):
            txn.build_plan(ctx, default_registry, candidate)
        candidate.clear()


def test_a_curated_kind_plans_fine(
    state_home: Any, mock_server: tuple[str, MockState]
) -> None:
    """The other side, so the test above is not proving that planning is
    broken for everyone."""
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))

    candidate = CandidateStore(settings.origin)
    candidate.set_path(Ref("appliance/banners", "banners", appliance="BR1-EC"), ["login"], "hi")
    plan = txn.build_plan(ctx, default_registry, candidate)
    assert plan.items


# -- the documents say the same thing ----------------------------------------


def _tier_row(text: str, tier: str) -> str:
    """The `| ... | ... |` row of a tier table whose first cell names `tier`."""
    for line in text.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 4 and re.fullmatch(rf"\**{tier}\**\s*\w*", cells[1]):
            return line
    raise AssertionError(f"no tier-{tier} row found")


@pytest.mark.parametrize("path", [README, ROADMAP], ids=lambda p: p.name)
def test_the_tier_1_row_does_not_promise_a_commit(path: Path) -> None:
    """The specific wrong claim, in the two documents a new user reads first.

    Asserted on the row rather than on the whole file: "plain commit" appears
    legitimately elsewhere (the Tier-2 row, prose about curated resources), so
    a file-wide substring search would either miss the bug or fire on the fix.
    """
    row = _tier_row(path.read_text(encoding="utf-8"), "1")
    assert "never" in row.lower(), row
    assert "NotCurated" in row, row
    assert "plain commit" not in row.lower(), row


@pytest.mark.parametrize("path", [README, ROADMAP], ids=lambda p: p.name)
def test_the_tier_2_row_still_promises_full_commit_confirm(path: Path) -> None:
    """Guards the guard from the other direction: a fix that made every row say
    "never" would pass the test above and be just as wrong."""
    row = _tier_row(path.read_text(encoding="utf-8"), "2")
    assert "full commit-confirm" in row, row


def test_the_promotion_doc_and_the_readme_now_agree() -> None:
    """`plugin-promotion.md` had it right all along. The bug was that nothing
    made the other two documents match it."""
    # Whitespace-normalized: the sentence wraps across a newline in the
    # source, so a raw substring search misses it.
    promotion = " ".join(PROMOTION.read_text(encoding="utf-8").split())
    assert "cannot take part in a plan or a commit" in promotion
    for path in (README, ROADMAP):
        assert "NotCurated" in _tier_row(path.read_text(encoding="utf-8"), "1")


def test_the_deliberate_choice_is_recorded_not_just_the_behavior() -> None:
    """#68 asks for a decision, not only a correction: if best-effort writes
    are ever wanted they get their own explicit surface rather than being
    bought by weakening the normalization contract. A reader who thinks the
    raise is an oversight will helpfully remove it."""
    readme = README.read_text(encoding="utf-8")
    assert "developer scaffolding, not operator coverage" in readme
    assert "weakening the normalization contract" in readme


def test_the_flag_help_does_not_overpromise() -> None:
    """`--allow-untransactional` reads as the escape hatch that lets a Tier-1
    kind into a commit. It is not one — nothing shipped can reach it."""
    from typer.testing import CliRunner

    from pyecsdwan.cli.main import app

    # Two ways Rich's help renderer defeats a naive substring search, both of
    # which this test hit for real:
    #
    # * it truncates the options box to the terminal width, and the default is
    #   narrow enough to cut the flag out entirely;
    # * it *colourizes the flag name*, emitting `-`, `-allow` and
    #   `-untransactional` as three separately-styled spans, so the literal
    #   "--allow-untransactional" never appears in the output.
    #
    # The second one passed locally and failed in CI, because Rich only
    # colourizes when it thinks it has a terminal. Stripping the escapes is
    # environment-independent; setting NO_COLOR would only work for as long as
    # Rich keeps honouring it.
    result = CliRunner(env={"COLUMNS": "200"}).invoke(app, ["commit", "--help"])
    assert result.exit_code == 0, result.output
    flat = " ".join(_ANSI.sub("", result.output).split())
    assert "--allow-untransactional" in flat, flat
    assert "normalize() raises first" in flat, flat


def test_the_ansi_stripper_actually_strips() -> None:
    """Guards the guard. A stripper that silently did nothing would make the
    assertions above pass or fail on whether Rich felt like using colour —
    which is exactly the flakiness it exists to remove."""
    coloured = "\x1b[1;36m-\x1b[0m\x1b[1;36m-allow\x1b[0m\x1b[1;36m-untransactional\x1b[0m"
    assert "--allow-untransactional" not in coloured
    assert _ANSI.sub("", coloured) == "--allow-untransactional"
