"""``show fabric flows summary`` (#58) and ``show fabric flow <ip>`` (#59).

Both commands read ``GET /flow`` — the real endpoint; the parent issue's
``GET /flow/{neId}/q`` exists in neither vendored spec — and share one row
parser, so they share one test module.

The load-bearing tests here are the two that would let a plausible-looking
implementation ship a lie:

* :func:`test_matching_is_server_side_via_ip_either_flag` — the mock only
  filters when the flag is sent, so a client that pulled every flow and
  filtered locally would return the *same rows* and pass a naive assertion.
  This one asserts on the request.
* :func:`test_one_conversation_seen_from_both_ends_is_reported_once` — the
  fixture seeds ``10.1.1.5`` on two appliances as the two ends of one
  conversation. Reporting it twice is the failure mode #59 exists to prevent.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, journal
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.shell import ShellCompleter, ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import flows as flows_report
from pyecsdwan.resolver import Resolver

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """CliRunner's ``_configure_logging`` binds structlog process-wide to a
    capture stream that is closed on exit; leaving it configured makes the
    *next* test that logs die on a closed file."""
    yield
    structlog.reset_defaults()


class RecordingClient(OrchClient):
    """An ``OrchClient`` that remembers every request it made.

    Needed because the interesting properties of these reports are properties
    of the *requests* — that the address filter went to the server, that
    ``maxFlows`` was always sent, that nothing wrote — and none of those are
    visible in the response.
    """

    def __init__(self, settings: config.Settings) -> None:
        super().__init__(settings)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail_for_ne_pk: str | None = None

    def request(  # type: ignore[override]
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expected: Any = None,
        # #67 added these to OrchClient.request. Accepted and ignored: this
        # double exists to record *what was asked for*, and it never retries
        # because it never fails at the transport layer. The `type: ignore`
        # above is why mypy did not catch the mismatch — a narrowed override on
        # a test double is invisible until it is called.
        retry_policy: Any = None,
        scope: str = "orchestrator",
    ) -> Any:
        self.calls.append((method, path, dict(params or {})))
        if (
            self.fail_for_ne_pk is not None
            and path == flows_report.FLOWS_ENDPOINT
            and (params or {}).get("nePk") == self.fail_for_ne_pk
        ):
            raise ConnectionError("connection refused")
        return super().request(method, path, json_body=json_body, params=params, expected=expected)

    def flow_calls(self) -> list[dict[str, Any]]:
        return [p for _m, path, p in self.calls if path == flows_report.FLOWS_ENDPOINT]

    def paths(self) -> list[str]:
        return [path for _m, path, _p in self.calls]


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = RecordingClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    return {
        "ctx": ctx,
        "client": client,
        "settings": settings,
        "state": mstate,
        "port": str(urlsplit(base_url).port),
    }


def _shell_state(world: dict[str, Any]) -> ShellState:
    return ShellState(
        ctx=world["ctx"],
        registry=default_registry,
        settings=world["settings"],
        console=Console(record=True, width=220),
        candidate=CandidateStore(world["settings"].host),
    )


def _shell(world: dict[str, Any], line: str) -> str:
    state = _shell_state(world)
    dispatch_operational(line, state)
    return state.console.export_text()


def _cli(world: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", world["port"], *args], env={"COLUMNS": "300"})


# -- the endpoint contract ----------------------------------------------------


def test_both_required_parameters_are_always_sent(world: dict[str, Any]) -> None:
    """``nePk`` and ``maxFlows`` are required: omitting either is a 400 live
    and a 422 against the mock. Every call must carry both."""
    flows_report.build_flows_summary(world["ctx"])
    calls = world["client"].flow_calls()
    assert calls, "the summary made no /flow calls at all"
    for params in calls:
        assert params["nePk"]
        assert isinstance(params["maxFlows"], int)


def test_omitting_max_flows_really_is_rejected(world: dict[str, Any]) -> None:
    """Guards the assumption above: if the mock ever stopped requiring
    maxFlows, the test above would keep passing while meaning nothing."""
    from pyecsdwan.client import OrchApiError

    with pytest.raises(OrchApiError):
        world["ctx"].client.get(flows_report.FLOWS_ENDPOINT, params={"nePk": "1.NE"})


def test_the_report_uses_one_request_per_appliance(world: dict[str, Any]) -> None:
    """The summary counts rows from a single per-appliance read rather than
    re-reading once per overlay: ``active`` carries no per-overlay breakdown,
    so per-overlay summary reads would be N_appliances x N_overlays requests
    against a low-QPS control plane to answer what one read already contains."""
    summary = flows_report.build_flows_summary(world["ctx"])
    assert len(world["client"].flow_calls()) == len(summary.rows) == 3


# -- #58: the matrix ----------------------------------------------------------


def test_summary_counts_per_appliance_per_overlay_with_both_totals(
    world: dict[str, Any],
) -> None:
    summary = flows_report.build_flows_summary(world["ctx"])
    by_name = {row.target.name: row for row in summary.rows}
    assert by_name["HUB1-EC"].counts == {"RealTime": 1, "CriticalApps": 1, "Passthrough": 1}
    assert by_name["BR1-EC"].counts == {"RealTime": 1, "CriticalApps": 1}
    assert by_name["BR2-EC"].counts == {"CriticalApps": 1}
    assert summary.column_totals == {"CriticalApps": 3, "RealTime": 2, "Passthrough": 1}
    assert summary.grand_total == 6
    # Column totals must reconcile with row totals: one flow, one cell.
    assert sum(summary.column_totals.values()) == sum(row.total for row in summary.rows)


def test_built_in_traffic_is_a_column_not_a_dropped_row(world: dict[str, Any]) -> None:
    """The Passthrough row on 1.NE is real traffic. Dropping it would make the
    grand total quietly wrong."""
    summary = flows_report.build_flows_summary(world["ctx"])
    assert flows_report.PASSTHROUGH in summary.overlays
    assert summary.column_totals[flows_report.PASSTHROUGH] == 1
    # ...and it sorts last, after the named overlays.
    assert summary.overlays[-1] == flows_report.PASSTHROUGH


def test_overlay_names_are_used_as_they_arrive_with_no_lookup(
    world: dict[str, Any],
) -> None:
    """``overlayName`` is already a resolved string. An ID-to-name lookup
    would be both unnecessary and wrong: the Orchestrator's overlay inventory
    does not enumerate the overlays flows actually appear on."""
    summary = flows_report.build_flows_summary(world["ctx"])
    assert "RealTime" in summary.overlays
    assert "/gms/overlays/config" not in world["client"].paths()


def test_an_inbound_outbound_overlay_pair_is_one_cell(world: dict[str, Any]) -> None:
    """``overlayName`` may name two overlays ('a | b'). Counting it under both
    would make the columns sum past the appliance total."""
    assert flows_report.normalize_overlay("RealTime | CriticalApps") == ("RealTime | CriticalApps")
    assert flows_report.normalize_overlay("RealTime | RealTime") == "RealTime"
    assert flows_report.normalize_overlay("") == flows_report.PASSTHROUGH
    assert flows_report.normalize_overlay(None) == flows_report.PASSTHROUGH


def test_an_unreachable_appliance_is_a_marked_row_not_a_failed_report(
    world: dict[str, Any],
) -> None:
    world["client"].fail_for_ne_pk = "3.NE"
    summary = flows_report.build_flows_summary(world["ctx"])
    assert len(summary.rows) == 3, "the dead appliance lost its row"
    dead = next(row for row in summary.rows if row.target.ne_pk == "3.NE")
    assert not dead.reachable
    assert "connection refused" in dead.error
    assert dead.counts == {}
    # The rest of the fabric still counted.
    assert summary.grand_total == 4
    assert [row.target.name for row in summary.unreachable] == ["BR1-EC"]


def test_a_cell_bounded_by_max_flows_says_so(world: dict[str, Any]) -> None:
    """A truncated tally presented as a total is worse than no report."""
    summary = flows_report.build_flows_summary(world["ctx"], max_flows=2)
    assert summary.bounded
    assert set(summary.bounded_appliances) == {"HUB1-EC", "BR1-EC"}
    assert not flows_report.build_flows_summary(world["ctx"], max_flows=50).bounded


def test_summary_json_emits_the_matrix(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flows", "summary", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["overlays"][-1] == flows_report.PASSTHROUGH
    assert payload["column_totals"] == {"CriticalApps": 3, "RealTime": 2, "Passthrough": 1}
    assert payload["grand_total"] == 6
    assert payload["bounded_by_max_flows"] is False
    hub = next(a for a in payload["appliances"] if a["appliance"] == "HUB1-EC")
    assert hub["counts"]["Passthrough"] == 1
    assert hub["nePk"] == "1.NE"


def test_summary_table_renders_names_counts_and_totals(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flows", "summary")
    assert result.exit_code == 0, result.output
    for expected in ("HUB1-EC", "RealTime", "CriticalApps", "Passthrough", "total"):
        assert expected in result.output


def test_summary_table_flags_a_bounded_run(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flows", "summary", "--max-flows", "2")
    assert result.exit_code == 0, result.output
    assert "--max-flows 2" in result.output
    assert "ceiling, not a total" in result.output


# -- #59: one address, fabric-wide -------------------------------------------


def test_matching_is_server_side_via_ip_either_flag(world: dict[str, Any]) -> None:
    """The whole of #59. The mock only applies the address filter when
    ``ipEitherFlag`` is sent, so an implementation that pulled every flow and
    filtered in Python would produce the same rows — this asserts on the
    request instead of the result."""
    flows_report.find_flows(world["ctx"], "10.1.1.5")
    calls = world["client"].flow_calls()
    assert len(calls) == 3
    for params in calls:
        assert params["ip1"] == "10.1.1.5"
        assert params["ipEitherFlag"] is True
    # ...and nothing was fetched unfiltered alongside it, which is how a
    # "filter locally, then also query the server" implementation would look.
    assert all("ip1" in params for params in calls)


def test_the_flag_is_what_makes_either_end_match(world: dict[str, Any]) -> None:
    """Guards the test above. Without the flag the mock matches ``ip1`` only,
    so 1.NE — which sees 10.1.1.5 as ``ip2`` — returns nothing."""
    client = world["client"]
    without = client.get(
        flows_report.FLOWS_ENDPOINT,
        params={"nePk": "1.NE", "maxFlows": 100, "ip1": "10.1.1.5"},
    )
    with_flag = client.get(
        flows_report.FLOWS_ENDPOINT,
        params={"nePk": "1.NE", "maxFlows": 100, "ip1": "10.1.1.5", "ipEitherFlag": True},
    )
    assert without["flows"] == []
    assert len(with_flag["flows"]) == 1


def test_one_conversation_seen_from_both_ends_is_reported_once(
    world: dict[str, Any],
) -> None:
    """10.1.1.5 is seeded on 1.NE as ``ip2`` and on 3.NE as ``ip1`` — one
    conversation, two observers. Two rows would be a lie about the fabric."""
    search = flows_report.find_flows(world["ctx"], "10.1.1.5")
    assert search.match_count == 1
    match = search.matches[0]
    assert set(match.appliances) == {"HUB1-EC", "BR1-EC"}, "an end was dropped"
    assert len(match.observations) == 2
    assert match.seen_on_both_ends


def test_the_dedupe_identity_is_direction_free_and_vrf_aware() -> None:
    """The documented identity: ``(protocol, sorted[(ip, port, vrf) x2])``.

    Direction-free so ``A:p -> B:q`` and ``B:q -> A:p`` collapse; VRF-aware so
    the same 5-tuple in two segments stays two flows.
    """
    target = flows_report.Target(name="HUB1-EC", ne_pk="1.NE")
    forward = flows_report.parse_row(
        {"ip1": "10.0.0.1", "port1": 80, "ip2": "10.0.0.2", "port2": 9, "protocol": "tcp"},
        target=target,
    )
    reverse = flows_report.parse_row(
        {"ip1": "10.0.0.2", "port1": 9, "ip2": "10.0.0.1", "port2": 80, "protocol": "tcp"},
        target=flows_report.Target(name="BR1-EC", ne_pk="3.NE"),
    )
    other_vrf = flows_report.parse_row(
        {
            "ip1": "10.0.0.1",
            "port1": 80,
            "ip2": "10.0.0.2",
            "port2": 9,
            "protocol": "tcp",
            "vrf1": 7,
        },
        target=target,
    )
    other_proto = flows_report.parse_row(
        {"ip1": "10.0.0.1", "port1": 80, "ip2": "10.0.0.2", "port2": 9, "protocol": "udp"},
        target=target,
    )
    assert forward.key == reverse.key
    assert other_vrf.key != forward.key
    assert other_proto.key != forward.key
    assert len(flows_report.dedupe([forward, reverse, other_vrf, other_proto])) == 3
    # Neither observer is dropped, and the first one seen stays primary.
    collapsed = flows_report.dedupe([forward, reverse])[0]
    assert collapsed.appliances == ("HUB1-EC", "BR1-EC")
    assert collapsed.primary is forward


def test_flow_id_is_not_part_of_the_identity(world: dict[str, Any]) -> None:
    """The two observations of the seeded conversation carry different
    ``flowId`` values — per-appliance internal keys cannot identify a flow
    fabric-wide, which is exactly why they are excluded."""
    search = flows_report.find_flows(world["ctx"], "10.1.1.5")
    ids = {o.flow_id for o in search.matches[0].observations}
    assert ids == {"1001", "2001"}


def test_byte_counters_are_not_summed_across_observers(world: dict[str, Any]) -> None:
    """Each appliance reports its own view of the same traffic; adding them
    double-counts. The rendered row is the primary observation's."""
    search = flows_report.find_flows(world["ctx"], "10.1.1.5")
    match = search.matches[0]
    assert match.primary.outbound_tx_bytes == 918_244
    assert all(o.outbound_tx_bytes == 918_244 for o in match.observations)


def test_a_prefix_query_maps_to_mask1(world: dict[str, Any]) -> None:
    search = flows_report.find_flows(world["ctx"], "10.1.1.0/24")
    assert (search.ip, search.mask) == ("10.1.1.0", 24)
    for params in world["client"].flow_calls():
        assert params["mask1"] == 24
        assert params["ip1"] == "10.1.1.0"
    # The prefix widens the match: 10.1.1.5 (both ends) plus 10.1.1.7.
    assert search.match_count == 2
    assert {m.primary.source.ip for m in search.matches} == {"10.1.1.5", "10.1.1.7"}


def test_a_bare_address_is_a_host_query(world: dict[str, Any]) -> None:
    assert flows_report.parse_query_ip("10.1.1.5") == ("10.1.1.5", 32)
    assert flows_report.parse_query_ip("2001:db8::1") == ("2001:db8::1", 128)
    assert flows_report.parse_query_ip("2001:db8::/64") == ("2001:db8::", 64)


@pytest.mark.parametrize("bad", ["", "nope", "10.1.1.5/33", "10.1.1.5/x", "999.1.1.1"])
def test_a_malformed_address_is_rejected_before_any_request(
    world: dict[str, Any], bad: str
) -> None:
    with pytest.raises(ValueError):
        flows_report.find_flows(world["ctx"], bad)
    assert world["client"].flow_calls() == []


def test_no_matches_is_a_sentence_not_an_empty_table(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flow", "192.0.2.9")
    assert result.exit_code == 0, result.output
    assert "no flows found for 192.0.2.9" in result.output
    assert "appliance" not in result.output.lower().split("no flows found")[0]


def test_flow_json_emits_the_rows_with_both_appliances(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flow", "10.1.1.5", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["match_count"] == 1
    assert payload["ip"] == "10.1.1.5"
    assert payload["mask"] == 32
    row = payload["flows"][0]
    assert sorted(row["appliances"]) == ["BR1-EC", "HUB1-EC"]
    assert len(row["observations"]) == 2
    assert row["protocol"] == "tcp"
    assert row["application"] == "https"
    assert row["overlay"] == "RealTime"
    assert row["bytes"]["total"] == 918_244 + 44_120 + 51_002 + 883_910
    assert payload["bounded_by_max_flows"] is False


def test_flow_table_attributes_both_appliances(world: dict[str, Any]) -> None:
    result = _cli(world, "show", "fabric", "flow", "10.1.1.5")
    assert result.exit_code == 0, result.output
    assert "HUB1-EC" in result.output
    assert "BR1-EC" in result.output
    assert "10.1.1.5:443" in result.output


def test_flow_search_reports_a_max_flows_ceiling(world: dict[str, Any]) -> None:
    """Bounded results must not imply completeness — 'every flow touching this
    address' is exactly the claim maxFlows can silently break."""
    search = flows_report.find_flows(world["ctx"], "10.1.1.0/24", max_flows=1)
    assert search.bounded
    # Both appliances that answered returned exactly the cap they were given,
    # so neither can claim its answer was complete.
    assert set(search.bounded_appliances) == {"BR1-EC", "HUB1-EC"}
    result = _cli(world, "show", "fabric", "flow", "10.1.1.0/24", "--max-flows", "1")
    assert "ceiling, not a total" in result.output


def test_an_unreachable_appliance_degrades_the_flow_search(
    world: dict[str, Any],
) -> None:
    world["client"].fail_for_ne_pk = "1.NE"
    search = flows_report.find_flows(world["ctx"], "10.1.1.5")
    assert search.match_count == 1
    assert search.matches[0].appliances == ("BR1-EC",)
    assert [t.name for t, _err in search.unreachable] == ["HUB1-EC"]


# -- shared: read-only, scoping, cache ---------------------------------------


def test_neither_report_writes_anything(world: dict[str, Any], state_home: Any) -> None:
    """Read-only means read-only: no candidate entry, no journal record, no
    transaction, and not a single non-GET request."""
    candidate = CandidateStore(world["settings"].host)
    assert len(candidate) == 0
    assert journal.list_txns() == []

    flows_report.build_flows_summary(world["ctx"])
    flows_report.find_flows(world["ctx"], "10.1.1.5")

    methods = {method for method, _path, _params in world["client"].calls}
    assert methods == {"GET"}, f"a report issued a write: {methods}"
    assert len(CandidateStore(world["settings"].host)) == 0
    assert journal.list_txns() == []
    assert not any(row.get("hasUnsavedChanges") for row in world["state"].appliances), (
        "a report dirtied appliance running config"
    )


def test_no_cache_refreshes_the_appliance_inventory(world: dict[str, Any]) -> None:
    """``GET /flow`` has no cached/live switch of its own; the cached read
    behind these reports is the resolver's appliance inventory."""
    assert len(flows_report.targets(world["ctx"])) == 3
    world["state"].appliances.append(
        {"nePk": "7.NE", "id": "7.NE", "hostName": "BR3-EC", "site": "Branch-3"}
    )
    assert len(flows_report.targets(world["ctx"])) == 3, "cache was not consulted"
    assert len(flows_report.targets(world["ctx"], no_cache=True)) == 4


def test_appliance_scoping_narrows_the_fan_out(world: dict[str, Any]) -> None:
    summary = flows_report.build_flows_summary(world["ctx"], appliances=["BR1-EC"])
    assert [row.target.name for row in summary.rows] == ["BR1-EC"]
    assert len(world["client"].flow_calls()) == 1


def test_rows_are_sorted_by_appliance_for_a_stable_table(world: dict[str, Any]) -> None:
    summary = flows_report.build_flows_summary(world["ctx"])
    names = [row.target.name for row in summary.rows]
    assert names == sorted(names)


# -- shell dispatch: `show fabric flow` and `show fabric flows` are one character apart -----


def test_shell_show_flows_summary_renders_the_matrix(world: dict[str, Any]) -> None:
    output = _shell(world, "show fabric flows summary")
    assert "HUB1-EC" in output
    assert "RealTime" in output
    assert "total" in output


def test_shell_show_flow_renders_the_search(world: dict[str, Any]) -> None:
    output = _shell(world, "show fabric flow 10.1.1.5")
    assert "10.1.1.5:443" in output
    assert "HUB1-EC" in output


def test_shell_bare_show_flow_is_a_usage_error_not_the_summary(
    world: dict[str, Any],
) -> None:
    """One character apart, entirely different reports."""
    output = _shell(world, "show fabric flow")
    assert "usage: show fabric flow <ip>" in output
    assert "RealTime" not in output


def test_shell_bare_show_flows_demands_the_subcommand(world: dict[str, Any]) -> None:
    assert "usage: show fabric flows summary" in _shell(world, "show fabric flows")
    assert "usage: show fabric flows summary" in _shell(world, "show fabric flows 10.1.1.5")


def test_shell_show_flow_rejects_extra_arguments(world: dict[str, Any]) -> None:
    assert "usage: show fabric flow <ip>" in _shell(world, "show fabric flow 10.1.1.5 extra")


def test_shell_show_flow_reports_a_bad_address_without_a_traceback(
    world: dict[str, Any],
) -> None:
    assert "not an IP address" in _shell(world, "show fabric flow nope")


def test_completer_offers_both_and_distinguishes_them(world: dict[str, Any]) -> None:
    completer = ShellCompleter(_shell_state(world))

    def complete(text: str) -> list[str]:
        from prompt_toolkit.completion import CompleteEvent
        from prompt_toolkit.document import Document

        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    # Both live under the `fabric` scope noun now: they are fabric-wide reads,
    # and the old grammar put them at top level where nothing said so.
    assert {"flow", "flows"} <= set(complete("show fabric "))
    assert set(complete("show fabric flow")) == {"flow", "flows"}
    assert complete("show fabric flows ") == ["summary"]
    # `show fabric flow` takes a free-form address, so it suggests nothing rather
    # than offering `summary` and inviting the two commands to be confused.
    assert complete("show fabric flow ") == []
    # And neither is reachable at the top level any more (#74).
    assert {"flow", "flows"} & set(complete("show ")) == set()


def test_naming_an_appliance_twice_does_not_double_count_it(
    world: dict[str, Any],
) -> None:
    """A repeated ``--appliance`` must not inflate the grand total."""
    summary = flows_report.build_flows_summary(
        world["ctx"], appliances=["BR1-EC", "BR1-EC", "3.NE"]
    )
    assert [row.target.name for row in summary.rows] == ["BR1-EC"]
    assert summary.grand_total == 2
