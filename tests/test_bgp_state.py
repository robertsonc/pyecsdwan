"""BGP operational views (#72), and the four traps the spec found before code.

`specs/002-appliance-operational-views/spec.md` was written by reading the
vendored baselines rather than by assuming, and it named four things an
obvious implementation gets wrong. Each has a test here, and each is written so
that the *obvious* implementation fails it — a fixture where every appliance
answers the same way would let all four through.

The mock fabric is seeded accordingly: one appliance with BGP off, one with the
two peers the schema documents, and one with three peers and a `neighborCount`
that disagrees with its rows.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.outcomes import Outcome
from pyecsdwan.cli.shell import ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import bgpstate
from pyecsdwan.resolver import Resolver

runner = CliRunner()


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
def ctx(state_home: Any, mock_server: tuple[str, MockState]) -> Ctx:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(
        orch_url=base_url, api_key="test-key",
        job_timeout=5.0, job_poll_initial=0.01, job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return Ctx(client=client, resolver=Resolver(client))


@pytest.fixture
def shell_state(ctx: Ctx) -> ShellState:
    return ShellState(
        ctx=ctx,
        registry=default_registry,
        settings=ctx.client.settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(ctx.client.settings.origin),
    )


def _shell(state: ShellState, line: str) -> str:
    state.console = Console(record=True, width=200)
    state.exit_code = 0
    dispatch_operational(line, state)
    return state.console.export_text()


def _cli(ctx: Ctx, *args: str) -> Any:
    port = str(ctx.client.settings.orch_url).rsplit(":", 1)[1]
    return runner.invoke(cli_main.app, ["--mock", port, *args])


# -- the fixture preconditions, asserted rather than assumed ----------------


def test_the_three_appliances_answer_differently(ctx: Ctx) -> None:
    """A fixture where every appliance says the same thing lets a wrong
    implementation pass every assertion below."""
    off = bgpstate.collect(ctx, "HUB1-EC")
    two = bgpstate.collect(ctx, "BR1-EC")
    three = bgpstate.collect(ctx, "BR2-EC")
    assert off.summary.bgp_state == 0
    assert len(two.neighbors) == 2
    assert len([n for n in three.neighbors if not n.configured_only]) == 3
    assert three.neighbor_count == 4


# -- trap 1: neighborState is an object, and not limited to two keys --------


def test_every_numeric_key_is_read_not_just_the_two_the_schema_documents(
    ctx: Ctx,
) -> None:
    """The schema documents keys "0" and "1", described as "the first/second
    neighbor". That is a documentation artifact, not a limit.

    BR2-EC has three peers. An implementation that stops at two, or that
    treats `neighborState` as an array, loses the third — and no assertion
    about the two-peer appliance would notice.
    """
    state = bgpstate.collect(ctx, "BR2-EC")
    observed = [n.peer_ip for n in state.neighbors if not n.configured_only]
    assert observed == ["10.200.0.1", "10.200.0.2", "10.200.0.3"], observed


def test_peers_come_back_in_numeric_key_order_not_string_order() -> None:
    """The keys are strings, so sorting them as strings puts "10" before "2".

    The mock's three peers are keyed "0".."2", where both orderings agree —
    so this needs a case with a two-digit key, which is any appliance with ten
    or more peers. Without it the sort is untested and a string sort passes.
    """
    states = {str(i): {"peer_ip": f"10.0.0.{i}"} for i in (0, 1, 2, 10, 11)}
    peers, _count = bgpstate._neighbors({"neighborCount": 5, "neighborState": states})
    assert [p.peer_ip for p in peers] == [
        "10.0.0.0",
        "10.0.0.1",
        "10.0.0.2",
        "10.0.0.10",
        "10.0.0.11",
    ]


def test_a_list_shaped_response_is_still_read(ctx: Ctx) -> None:
    """Not the documented shape, but the natural thing for a future version to
    switch to — and dropping every peer on that day would be a silent wrong
    answer rather than a visible break."""
    peers, count = bgpstate._neighbors(
        {"neighborCount": 2, "neighborState": [{"peer_ip": "1.1.1.1"}, {"peer_ip": "2.2.2.2"}]}
    )
    assert [p.peer_ip for p in peers] == ["1.1.1.1", "2.2.2.2"]
    assert count == 2


# -- trap 2: neighborCount is authoritative ---------------------------------


def test_a_count_that_disagrees_with_the_rows_is_partial_not_ok(ctx: Ctx) -> None:
    """A response that dropped a peer has not answered the question."""
    state = bgpstate.collect(ctx, "BR2-EC")
    assert not state.rows_match_count
    assert bgpstate.collect(ctx, "BR1-EC").rows_match_count


def test_configured_only_rows_do_not_mask_the_mismatch(ctx: Ctx) -> None:
    """BR2-EC has three observed peers, one configured-only row, and claims
    four. Counting the configured-only row would make 4 == 4 and hide exactly
    the mismatch this exists to surface."""
    state = bgpstate.collect(ctx, "BR2-EC")
    assert len(state.neighbors) == 4  # including the configured-only row
    assert state.neighbor_count == 4
    assert not state.rows_match_count


def test_the_shell_reports_partial_with_its_exit_code(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show appliance BR2-EC bgp neighbors")
    assert "partial" in out, out
    assert shell_state.exit_code == Outcome.PARTIAL.exit_code
    # The rows are still shown: partial means incomplete, not unavailable.
    assert "10.200.0.1" in out, out


def test_the_cli_reports_partial_with_its_exit_code(ctx: Ctx) -> None:
    result = _cli(ctx, "show", "appliance", "BR2-EC", "bgp", "neighbors")
    assert result.exit_code == Outcome.PARTIAL.exit_code, result.output
    assert "10.200.0.1" in result.output


# -- trap 3: bgp_state 0 and 1 are different answers, and neither is an error


def test_bgp_not_enabled_is_a_successful_answer(shell_state: ShellState) -> None:
    """An appliance that does not run BGP is not a failure to report BGP.

    Rendering it as one sends an operator to debug a healthy device — and it
    is a *different* answer from state 1, which is BGP enabled and down.
    """
    out = _shell(shell_state, "show appliance HUB1-EC bgp summary")
    assert shell_state.exit_code == 0, out
    assert "error" not in out.lower(), out
    # The sentence, not a table row reading "not enabled (0)" — eighteen
    # counters that are all zero because the protocol is off is a worse answer
    # than saying the protocol is off. (Asserting the substring alone passes
    # either way, which is how this nearly went untested.)
    assert "BGP is not enabled on this appliance." in out, out
    assert "routes received" not in out, out
    assert "subnets advertised" not in out, out


def test_not_enabled_and_down_are_different_answers() -> None:
    from pyecsdwan.reports.bgpstate import _summary

    off = _summary({"bgp_state": 0})
    down = _summary({"bgp_state": 1})
    assert off.state_name != down.state_name
    assert not off.enabled
    assert down.enabled, "state 1 is BGP enabled and down, not BGP switched off"


def test_an_unknown_state_code_renders_as_the_number(ctx: Ctx) -> None:
    """Never guessed at the nearest name: an operator acting on a wrong state
    word is worse off than one who has to look the number up."""
    from pyecsdwan.reports.bgpstate import _summary

    assert _summary({"bgp_state": 99}).state_name == "code 99"
    assert _summary({}).state_name == "unknown"


def test_a_missing_counter_is_not_zero() -> None:
    """`num_ebgp_rtes` absent and `num_ebgp_rtes: 0` are different facts."""
    from pyecsdwan.reports.bgpstate import _summary

    assert _summary({}).num_ebgp_rtes is None
    assert _summary({"num_ebgp_rtes": 0}).num_ebgp_rtes == 0


# -- trap 4: bgp_state_str contradicts its own type -------------------------


def test_the_contradictory_field_is_carried_not_parsed(ctx: Ctx) -> None:
    """`bgp_state_str` is typed integer while described as a string
    representation. Neither reading has been observed live, so nothing here
    picks one — it is passed through as it arrived."""
    state = bgpstate.collect(ctx, "BR1-EC")
    assert state.summary.bgp_state_str == 6  # exactly what the fixture sent


# -- #72's first guardrail: configured but not observed ---------------------


def test_a_configured_peer_missing_from_state_is_shown_as_such(ctx: Ctx) -> None:
    """Never inferred to be established, and never silently dropped — the
    reason this view cannot be built on the config object."""
    state = bgpstate.collect(ctx, "BR2-EC")
    extra = [n for n in state.neighbors if n.configured_only]
    assert [n.peer_ip for n in extra] == ["10.200.0.9"], extra
    only = extra[0]
    assert only.peer_state is None and only.rcvd_pfxs is None, "no counters were invented"
    assert "not observed" in only.peer_state_str


def test_the_rendering_marks_the_unobserved_row(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show appliance BR2-EC bgp neighbors")
    assert "10.200.0.9" in out, out
    assert "configured but not observed" in out, out


def test_losing_the_correlation_does_not_fail_the_read(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state view is the answer being asked for; an annotation is not
    worth failing a read that succeeded."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("config endpoint unavailable")

    monkeypatch.setattr(type(ctx.client), "appliance_request", boom)
    state = bgpstate.collect(ctx, "BR2-EC")
    assert len(state.neighbors) == 3
    assert not any(n.configured_only for n in state.neighbors)


# -- routes: unsupported, and said so --------------------------------------


def test_routes_is_unsupported_not_an_error(shell_state: ShellState) -> None:
    """No BGP route-table endpoint exists in either baseline. That is a
    statement about the product, not about the appliance."""
    out = _shell(shell_state, "show appliance BR1-EC bgp routes")
    assert shell_state.exit_code == Outcome.UNSUPPORTED.exit_code
    assert "unsupported" in out, out
    assert "no BGP route-table endpoint" in out, out
    # Points at what does exist rather than leaving the operator to guess.
    assert "num_bgp_rtes_rcvd" in out, out
    assert "ec-cli api" in out, out


def test_routes_stays_listed_rather_than_hidden(shell_state: ShellState) -> None:
    """Dropping it from the listing would make the CLI look like it never
    considered the question an operator is certain to ask."""
    out = _shell(shell_state, "show appliance BR1-EC bgp")
    assert "routes" in out and "unsupported" in out, out


def test_routes_makes_no_api_call(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    """It cannot be answered, so asking the Orchestrator is pure cost."""
    calls: list[str] = []
    original = ctx.client.request

    def counting(method: str, path: str, **kwargs: Any) -> Any:
        calls.append(path)
        return original(method, path, **kwargs)

    monkeypatch.setattr(ctx.client, "request", counting)
    result = _cli(ctx, "show", "appliance", "BR1-EC", "bgp", "routes")
    assert result.exit_code == Outcome.UNSUPPORTED.exit_code
    assert not [p for p in calls if "bgp" in p], calls


# -- the bare nonterminal costs nothing -------------------------------------


def test_the_shell_bare_domain_lists_leaves_without_fetching(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guardrail on the other surface: contextual help is free."""
    calls: list[str] = []
    original = shell_state.ctx.client.request

    def counting(method: str, path: str, **kwargs: Any) -> Any:
        calls.append(path)
        return original(method, path, **kwargs)

    monkeypatch.setattr(shell_state.ctx.client, "request", counting)
    out = _shell(shell_state, "show appliance BR1-EC bgp")
    assert "summary" in out and "neighbors" in out, out
    assert shell_state.exit_code == 0
    assert not [p for p in calls if "bgp" in p], calls


def test_a_bare_domain_lists_leaves_without_fetching(
    ctx: Ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#72's guardrail: a nonterminal is contextual help, not an implicit
    expensive fetch, and #74's criterion that static help never costs a
    request."""
    calls: list[str] = []
    original = ctx.client.request

    def counting(method: str, path: str, **kwargs: Any) -> Any:
        calls.append(path)
        return original(method, path, **kwargs)

    monkeypatch.setattr(ctx.client, "request", counting)
    result = _cli(ctx, "show", "appliance", "BR1-EC", "bgp")
    assert result.exit_code == 0, result.output
    assert "summary" in result.output
    assert not [p for p in calls if "bgp" in p], calls


# -- drill-down and JSON ----------------------------------------------------


def test_one_peer_can_be_named(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show appliance BR1-EC bgp neighbors 10.127.1.9")
    assert "10.127.1.9" in out and "10.127.1.1" not in out, out
    assert shell_state.exit_code == 0


def test_a_peer_that_does_not_exist_is_not_found_not_empty(
    shell_state: ShellState,
) -> None:
    """"no such peer" and "this appliance has no peers" are different facts."""
    out = _shell(shell_state, "show appliance BR1-EC bgp neighbors 9.9.9.9")
    assert shell_state.exit_code == Outcome.NOT_FOUND.exit_code
    assert "not_found" in out, out


def test_the_json_status_and_the_exit_code_agree(ctx: Ctx) -> None:
    """Both are derived from one decision. Two derivations is how they come to
    disagree, and a script branching on either would then get a different
    answer depending on which it read."""
    for args, expected in (
        (["show", "appliance", "BR1-EC", "bgp", "summary"], Outcome.OK),
        (["show", "appliance", "BR2-EC", "bgp", "neighbors"], Outcome.PARTIAL),
        (["show", "appliance", "BR1-EC", "bgp", "neighbors", "9.9.9.9"], Outcome.NOT_FOUND),
        (["show", "appliance", "BR1-EC", "bgp", "routes"], Outcome.UNSUPPORTED),
    ):
        result = _cli(ctx, *args, "--json")
        assert result.exit_code == expected.exit_code, (args, result.output)
        assert json.loads(result.stdout)["status"] == expected.value, (args, result.output)


def test_stale_ok_is_opt_in_and_reaches_the_api(ctx: Ctx) -> None:
    """Decision 7 / #72 finding 3: the endpoint has its own `cached`
    parameter, so freshness is honoured at the source rather than inferred."""
    seen: list[Any] = []
    original = ctx.client.request

    def capture(method: str, path: str, **kwargs: Any) -> Any:
        if path == bgpstate.BGP_STATE_PATH:
            seen.append(kwargs.get("params", {}).get("cached"))
        return original(method, path, **kwargs)

    ctx.client.request = capture  # type: ignore[method-assign]
    bgpstate.collect(ctx, "BR1-EC")
    bgpstate.collect(ctx, "BR1-EC", cached=True)
    assert seen == ["false", "true"], seen


def test_cached_data_reports_stale_and_still_exits_zero(ctx: Ctx) -> None:
    """Decision 7: cached data is served only under `--stale-ok`, so `stale`
    is honouring what was asked for, not degrading it — exit 0.

    The status still says so, because a log that cannot tell a cached answer
    from a live one is a log that cannot explain a stale reading later.
    """
    live = _cli(ctx, "show", "appliance", "BR1-EC", "bgp", "summary", "--json")
    assert live.exit_code == 0, live.output
    assert json.loads(live.stdout)["status"] == Outcome.OK.value

    cached = _cli(ctx, "show", "appliance", "BR1-EC", "bgp", "summary", "--stale-ok", "--json")
    assert cached.exit_code == 0, cached.output
    payload = json.loads(cached.stdout)
    assert payload["status"] == Outcome.STALE.value
    assert payload["cached"] is True


def test_the_stale_annotation_reaches_the_human_rendering(shell_state: ShellState) -> None:
    live = _shell(shell_state, "show appliance BR1-EC bgp summary")
    stale = _shell(shell_state, "show appliance BR1-EC bgp summary --stale-ok")
    assert "(cached)" not in live, live
    assert "(cached)" in stale, stale


def test_partial_outranks_stale(ctx: Ctx) -> None:
    """More than one outcome can hold at once, and only the most specific is
    worth acting on: an incomplete answer is the problem, and the staleness is
    what the operator already asked for."""
    result = _cli(ctx, "show", "appliance", "BR2-EC", "bgp", "neighbors", "--stale-ok", "--json")
    assert result.exit_code == Outcome.PARTIAL.exit_code, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == Outcome.PARTIAL.value
    assert payload["cached"] is True


# -- #87: what a live appliance actually sent --------------------------------
#
# The report in #87 is one run against a real fabric, and it contains its own
# contradiction: `show appliance S1-ecv-01 bgp summary` printed
#
#     state    code 11 (11)
#     peers    None active of None
#     routes received  0
#
# while `bgp neighbors`, parsed from the *same* `/bgp/state` response, listed
# two peers in Established with 10 and 11 prefixes received. Three separate
# defects, none of which any fixture here could reach: the mock always
# populates the peer counts, and always reports a state code the map knows.


def test_the_state_names_are_the_schemas_not_the_peer_state_machine() -> None:
    """The one that would have hurt someone.

    `bgp_state` is documented as "Overall state of the routerd & bgp
    processes", enumerated 0-10 in the appliance baseline. The first
    implementation filled 2-8 with the standard BGP *peer* FSM names, because
    "BGP state" means that almost everywhere else. So an appliance reporting 6
    — "RM Initializing" — was told its BGP was **established**.

    Asserted against the schema's words, and specifically that the peer-FSM
    vocabulary is gone: a rename back would be a confident wrong answer about
    whether BGP is up, which is the only question this view exists to answer.
    """
    assert bgpstate.BGP_STATE_NAMES[6] == "rm initializing"
    assert bgpstate.BGP_STATE_NAMES[2] == "mgmt stub initializing"
    assert bgpstate.BGP_STATE_NAMES[5] == "rtm active"
    peer_fsm = {"idle", "connect", "opensent", "openconfirm", "established", "clearing"}
    assert not (set(bgpstate.BGP_STATE_NAMES.values()) & peer_fsm), bgpstate.BGP_STATE_NAMES
    # 0, 1, 9 and 10 lined up by coincidence and must not have been disturbed.
    assert bgpstate.BGP_STATE_NAMES[0] == "not enabled"
    assert bgpstate.BGP_STATE_NAMES[9] == "active"


def _summary_with(**overrides: Any) -> bgpstate.BgpSummary:
    base: dict[str, Any] = {
        "bgp_state": 9,
        "bgp_state_str": 9,
        "local_asn": 64501,
        "local_ip": "10.254.1.1",
        "rtr_id": "10.254.1.1",
        "num_peers": 2,
        "num_peers_active": 2,
        "num_bgp_rtes_rcvd": 0,
        "num_ebgp_rtes": 0,
        "num_ibgp_rtes": 0,
        "num_rib_rtes": 0,
        "num_subs_installed": 0,
        "reject_mismatches": 0,
        "reject_unpreferred": 0,
        "mgmt_stub_tot_errors": 0,
        "mgmt_stub_last_err_str": "",
        "tunbgp_tot_errors": 0,
        "tunbgp_last_err_str": "",
    }
    base.update(overrides)
    return bgpstate.BgpSummary(**base)


def _peer(ip: str, state_str: str = "Established") -> bgpstate.BgpNeighbor:
    return bgpstate.BgpNeighbor(
        peer_ip=ip, asn=65001, peer_state=6, peer_state_str=state_str,
        local_ip="10.254.1.1", rtr_id="10.254.1.1", rcvd_pfxs=10, sent_pfxs=2,
        rcvd_updates=71, sent_updates=4, time_established=1, time_last_update=1,
        peer_caps="75008",
    )


def _state(summary: bgpstate.BgpSummary, *peers: bgpstate.BgpNeighbor) -> bgpstate.BgpState:
    return bgpstate.BgpState(
        appliance="S1-ecv-01", ne_pk="12.NE", summary=summary,
        neighbors=peers, neighbor_count=len(peers), cached=False,
    )


def test_an_unrecognised_state_code_says_so_once() -> None:
    """The live box reported code 11; the vendored 7.2.0 enumeration stops at
    10. Rendering was `f"{state_name} ({bgp_state})"`, which printed the number
    twice — "code 11 (11)" — and gave no hint the tool did not recognise it."""
    label = _summary_with(bgp_state=11).state_label
    assert "11" in label
    assert label.count("11") == 1, label
    assert "unrecognised" in label and "7.2.0" in label


def test_a_known_state_still_carries_its_number() -> None:
    """Guards the fix from over-reach: the code is what an operator reads back
    to support, so it must survive alongside the name."""
    assert _summary_with(bgp_state=9).state_label == "active (9)"


def test_peers_are_counted_from_the_neighbours_the_response_listed() -> None:
    """The live payload carried neither `num_peers` nor `num_peers_active`, so
    the row read "None active of None" — a Python repr in operator output —
    directly above a table of two Established sessions from the same response.

    What can be verified is the rows, so the rows are what the count comes
    from, and the peer's own `peer_state_str` decides "established" rather than
    a numeric code we would have to map (that mapping is what this issue found
    to be wrong).
    """
    state = _state(
        _summary_with(num_peers=None, num_peers_active=None),
        _peer("172.16.254.1"),
        _peer("172.16.254.2"),
    )
    assert state.observed_peers == 2
    assert state.observed_established == 2
    assert state.summary_peer_counts_missing


def test_a_peer_the_appliance_does_not_call_established_is_not_counted() -> None:
    state = _state(
        _summary_with(num_peers=None, num_peers_active=None),
        _peer("172.16.254.1"),
        _peer("172.16.254.2", state_str="Idle"),
    )
    assert state.observed_peers == 2
    assert state.observed_established == 1


def test_a_configured_only_peer_is_observed_by_nobody() -> None:
    """Configuration is not observation: a peer added from `/bgp/config` has no
    live state, and counting it would restate intent as fact."""
    configured = dataclasses.replace(_peer("10.200.0.9"), configured_only=True, peer_state_str="")
    state = _state(
        _summary_with(num_peers=None, num_peers_active=None), _peer("1.1.1.1"), configured
    )
    assert state.observed_peers == 1
    assert state.observed_established == 1


def test_a_summary_that_disagrees_with_its_own_rows_is_flagged() -> None:
    """Both numbers came from one response. The report shows both rather than
    quietly choosing which one the operator should have been told."""
    state = _state(_summary_with(num_peers=0, num_peers_active=0), _peer("1.1.1.1"))
    assert state.summary_peer_counts_disagree
    assert not state.summary_peer_counts_missing


def test_either_count_disagreeing_on_its_own_is_enough() -> None:
    """Two independent comparisons, so each needs its own case.

    A mutation sweep disabled the *total* comparison and the test above still
    passed, because its fixture disagreed on both counts and the *active*
    comparison caught it. These isolate them: one where only the total is
    wrong, one where only the active count is.
    """
    one_peer = (_peer("1.1.1.1"),)
    only_total_wrong = _state(_summary_with(num_peers=5, num_peers_active=1), *one_peer)
    assert only_total_wrong.summary_peer_counts_disagree, "5 configured against 1 listed"

    only_active_wrong = _state(_summary_with(num_peers=1, num_peers_active=0), *one_peer)
    assert only_active_wrong.summary_peer_counts_disagree, "0 active against 1 established"


def test_a_summary_that_agrees_is_not_flagged() -> None:
    state = _state(_summary_with(num_peers=1, num_peers_active=1), _peer("1.1.1.1"))
    assert not state.summary_peer_counts_disagree


def test_no_python_none_reaches_the_operator() -> None:
    """The literal defect in the report. Rendering a `None` tells the reader
    nothing and looks like a crash that did not happen."""
    from pyecsdwan.cli.render import render_bgp_summary

    console = Console(record=True, width=200)
    render_bgp_summary(
        console,
        _state(
            _summary_with(bgp_state=11, num_peers=None, num_peers_active=None),
            _peer("172.16.254.1"),
            _peer("172.16.254.2"),
        ),
    )
    out = console.export_text()
    assert "None" not in out, out
    assert "2 established of 2 observed" in out
    assert "unrecognised code 11" in out
