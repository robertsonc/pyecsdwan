"""`ec-cli drift` — fabric-wide, and honest about what it could not see (epic #8).

The command's value is not the drift rows; `diff` already finds those. It is
the rows `diff` never had a reason to print:

* **undeclared** — the instance exists, was read, and nobody has said what it
  should be. Reporting that as "in sync" is how an unmanaged fabric passes a
  drift check;
* **unreadable** — the read failed, so this instance says nothing about drift
  either way;
* **unsupported** — a Tier-1 stub's `normalize()` raises, so there is no
  canonical form to compare.

And the exit code, which is where "explicit rather than collapsed into clean"
actually bites: an incomplete run exits `partial` (8) even when it also found
drift, because a report that skipped part of the fabric has not earned the
word "clean". Both codes fail a CI job; the code says which problem is first.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.client import OrchApiError, OrchClient
from pyecsdwan.contract import Ctx, Ref, Resource, Scope, Tier
from pyecsdwan.registry import Registry, default_registry
from pyecsdwan.reports import drift
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

runner = CliRunner()

#: One curated appliance-scope kind with a stable instance on the mock fabric.
KIND = "appliance/banners"
REF = Ref(KIND, "global", appliance="BR1-EC")


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "settings": settings,
        "candidate": CandidateStore(settings.host),
        "state": state,
        "port": base_url.rsplit(":", 1)[1],
    }


def _collect(world: dict[str, Any], **kw: Any) -> drift.Report:
    return drift.collect(
        world["ctx"], default_registry, drift.CandidateIntent(world["candidate"]), **kw
    )


# -- the statuses ------------------------------------------------------------


def test_an_unstaged_instance_is_undeclared_not_in_sync(world: dict[str, Any]) -> None:
    """The row this whole command exists for. `diff` never mentions this
    instance at all, and calling it "in sync" would mean an entirely unmanaged
    fabric passes a drift check with a clean bill of health."""
    report = _collect(world, kinds=[KIND])
    assert report.rows
    assert all(r.status is drift.Status.UNDECLARED for r in report.rows), report.counts
    assert not report.of(drift.Status.IN_SYNC)
    assert report.exit_code == drift.EXIT_OK


def test_staged_intent_that_matches_is_in_sync(world: dict[str, Any]) -> None:
    """Guards the guard above from the other side: if every row were
    `undeclared` regardless of what was staged, the test above would pass while
    the report meant nothing."""
    ctx, candidate = world["ctx"], world["candidate"]
    resource = default_registry.get(KIND)
    live = resource.normalize(resource.fetch(ctx, REF))
    assert isinstance(live, dict) and live, "expected the mock to seed banners"
    for key, value in live.items():
        candidate.set_path(REF, [key], value)

    row = next(r for r in _collect(world, kinds=[KIND]).rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.IN_SYNC, row


def test_staged_intent_that_differs_is_drift(world: dict[str, Any]) -> None:
    world["candidate"].set_path(REF, ["login"], "a banner nobody set on the box")
    report = _collect(world, kinds=[KIND])

    row = next(r for r in report.rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.DRIFT
    assert row.entries >= 1
    assert "login" in row.detail
    assert report.exit_code == drift.EXIT_DRIFT
    # The other appliances are still undeclared — drift on one instance must
    # not label its neighbours.
    assert {r.status for r in report.rows if r.appliance != "BR1-EC"} == {
        drift.Status.UNDECLARED
    }


def test_a_tier_1_kind_is_unsupported_not_missing(world: dict[str, Any]) -> None:
    """A kind absent from a fabric-wide report reads as "there is none of that
    here", which is a different and wrong claim. Stubs get a row saying why
    they cannot be compared."""
    stubs = [k for k in default_registry.kinds() if default_registry.get(k).tier < Tier.CURATED]
    assert stubs, "expected at least one generated stub in the registry"
    report = _collect(world, kinds=stubs)
    for row in report.rows:
        assert row.status is drift.Status.UNSUPPORTED
        assert "NotCurated" in row.detail or "tier-" in row.detail


def test_a_tier_1_kind_is_never_fetched(world: dict[str, Any]) -> None:
    """The tier check earns its place by saving the round trip, not by
    producing the right status — `normalize()` raising `NotCurated` reaches the
    same answer, which is why the mutation sweep reported the check as
    unprotected. On a fleet this is one wasted call per stub per appliance
    against a control plane the fan-out cap exists to protect.
    """
    fetched: list[str] = []

    class Stub(Resource):
        kind = "stubbed"
        scope = Scope.ORCHESTRATOR
        tier = Tier.GENERATED

        def list_refs(self, ctx: Ctx) -> list[Ref]:
            return [Ref("stubbed", "one")]

        def fetch(self, ctx: Ctx, ref: Ref) -> Any:  # pragma: no cover - must not run
            fetched.append(ref.key())
            return {}

    registry = Registry()
    registry.register(Stub())
    report = drift.collect(
        world["ctx"], registry, drift.CandidateIntent(world["candidate"]), kinds=["stubbed"]
    )

    assert not fetched, "a Tier-1 stub was fetched despite having no canonical form"
    assert report.rows[0].status is drift.Status.UNSUPPORTED
    assert "tier-1" in report.rows[0].detail


def test_an_unreadable_instance_is_a_row_not_a_silence(world: dict[str, Any]) -> None:
    """A read that fails must appear. Dropping the row would shrink the fabric
    to the part that answered and then call it clean."""

    class Broken(Resource):
        kind = "broken"
        scope = Scope.ORCHESTRATOR
        tier = Tier.CURATED

        def list_refs(self, ctx: Ctx) -> list[Ref]:
            return [Ref("broken", "one")]

        def fetch(self, ctx: Ctx, ref: Ref) -> Any:
            raise OrchApiError("GET", "/broken", 503, "service unavailable")

    registry = Registry()
    registry.register(Broken())
    report = drift.collect(
        world["ctx"], registry, drift.CandidateIntent(world["candidate"]), kinds=["broken"]
    )

    assert len(report.rows) == 1
    assert report.rows[0].status is drift.Status.UNREADABLE
    assert "503" in report.rows[0].detail


def test_a_kind_whose_instances_cannot_be_listed_becomes_a_note(
    world: dict[str, Any],
) -> None:
    """Enumeration failing is not the same as having no instances, and the
    report must not quietly report the second."""

    class Unlistable(Resource):
        kind = "unlistable"
        scope = Scope.ORCHESTRATOR
        tier = Tier.CURATED

        def list_refs(self, ctx: Ctx) -> list[Ref]:
            raise OrchApiError("GET", "/nope", 403, "forbidden")

    registry = Registry()
    registry.register(Unlistable())
    report = drift.collect(
        world["ctx"], registry, drift.CandidateIntent(world["candidate"]), kinds=["unlistable"]
    )

    assert not report.rows
    assert any("could not be listed" in n and "403" in n for n in report.notes)


# -- the exit code -----------------------------------------------------------


def _report(*statuses: drift.Status) -> drift.Report:
    return drift.Report(
        rows=tuple(
            drift.Row(noun="k", kind="k", name=str(i), appliance="", status=s)
            for i, s in enumerate(statuses)
        )
    )


def test_a_clean_complete_run_exits_zero() -> None:
    assert _report(drift.Status.IN_SYNC, drift.Status.UNDECLARED).exit_code == drift.EXIT_OK


def test_drift_exits_one() -> None:
    assert _report(drift.Status.IN_SYNC, drift.Status.DRIFT).exit_code == drift.EXIT_DRIFT


@pytest.mark.parametrize("blind", [drift.Status.UNREADABLE, drift.Status.UNSUPPORTED])
def test_an_incomplete_run_exits_partial(blind: drift.Status) -> None:
    assert _report(drift.Status.IN_SYNC, blind).exit_code == drift.EXIT_PARTIAL


@pytest.mark.parametrize("blind", [drift.Status.UNREADABLE, drift.Status.UNSUPPORTED])
def test_incompleteness_outranks_drift(blind: drift.Status) -> None:
    """The deliberate precedence, and the one worth pinning: a run that found
    drift *and* could not read part of the fabric reports the incompleteness
    first, because the drift count it produced is a floor, not a total."""
    report = _report(drift.Status.DRIFT, blind)
    assert report.exit_code == drift.EXIT_PARTIAL
    assert report.drifted  # ... and the drift is still in the report
    assert not report.complete


def test_completeness_is_about_comparison_not_success() -> None:
    """`undeclared` is not a failure and must not make a run incomplete —
    otherwise every fabric is permanently "partial" until someone declares
    every instance, and the exit code stops meaning anything."""
    assert _report(drift.Status.UNDECLARED, drift.Status.UNDECLARED).complete


# -- the CLI -----------------------------------------------------------------


def _cli(world: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", world["port"], "drift", "--yes", *args])


def test_the_command_reports_the_whole_fabric(world: dict[str, Any]) -> None:
    result = _cli(world)
    assert result.exit_code == drift.EXIT_OK, result.output
    assert "undeclared" in result.output
    # Every appliance in the fixture fabric is represented.
    for name in ("HUB1-EC", "BR1-EC", "BR2-EC"):
        assert name in result.output


def test_the_command_exits_one_on_drift(world: dict[str, Any]) -> None:
    world["candidate"].set_path(REF, ["login"], "changed")
    result = _cli(world, "--kind", KIND)
    assert result.exit_code == drift.EXIT_DRIFT, result.output
    assert "drift" in result.output


def test_the_json_payload_carries_the_verdict(world: dict[str, Any]) -> None:
    """A CI job reads the exit code; a dashboard reads this. They must agree,
    so the payload carries the same `exit_code` the process returned."""
    world["candidate"].set_path(REF, ["login"], "changed")
    result = _cli(world, "--kind", KIND, "--json")
    assert result.exit_code == drift.EXIT_DRIFT
    payload = json.loads(result.output)
    assert payload["exit_code"] == result.exit_code
    assert payload["complete"] is True
    assert payload["counts"]["drift"] == 1
    row = next(r for r in payload["rows"] if r["status"] == "drift")
    assert row["appliance"] == "BR1-EC"
    # The user-facing noun, never the registry key (#77).
    assert row["noun"] == "banners"
    assert "/" not in row["noun"]


def test_the_renderer_says_outright_that_a_run_was_incomplete() -> None:
    """The counts alone leave it to the reader to notice. This is the line
    that stops a partial report being read as a clean one."""
    import io

    from rich.console import Console

    from pyecsdwan.cli import render

    buf = io.StringIO()
    render.render_drift(
        Console(file=buf, width=200, no_color=True),
        drift.Report(
            rows=(
                drift.Row(noun="bgp", kind="appliance/bgp", name="config",
                          appliance="BR1-EC", status=drift.Status.UNREADABLE,
                          detail="OrchApiError: 503"),
            )
        ),
    )
    out = buf.getvalue()
    assert "incomplete" in out
    assert "not a claim this run can make" in out

    clean = io.StringIO()
    render.render_drift(
        Console(file=clean, width=200, no_color=True),
        drift.Report(
            rows=(
                drift.Row(noun="bgp", kind="appliance/bgp", name="config",
                          appliance="BR1-EC", status=drift.Status.IN_SYNC),
            )
        ),
    )
    assert "incomplete" not in clean.getvalue()


# -- the unsaved-changes note ------------------------------------------------


def test_unsaved_changes_are_noted_but_never_counted_as_drift(
    world: dict[str, Any],
) -> None:
    """A different axis: "differs from declared intent" and "differs from what
    is on flash" are two things, and folding the second into the first is the
    collapsing this module exists to avoid. Worth saying — a reboot discards
    it — so it is a note, outside the counts and outside the exit code."""
    world["state"].appliances[0]["hasUnsavedChanges"] = True
    report = _collect(world, kinds=[KIND])

    assert any("unsaved running-config" in n for n in report.notes)
    assert report.counts["drift"] == 0
    assert report.exit_code == drift.EXIT_OK


def test_no_note_when_nothing_is_unsaved(world: dict[str, Any]) -> None:
    """Guards the guard: a note that always fired would be noise, and the
    assertion above would pass without the probe working at all."""
    report = _collect(world, kinds=[KIND])
    assert not any("unsaved running-config" in n for n in report.notes)
