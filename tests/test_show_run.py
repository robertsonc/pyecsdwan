"""``show configuration fabric``: the fabric configuration breakdown (issue #55).

The report's job is orientation, so what these tests mostly pin is that it
stays *honest* while the fabric misbehaves: a section whose endpoint is dead
degrades to itself carrying the reason, an appliance that will not answer the
deployment fan-out is a marked row rather than a missing one, an inventory
that cannot be read costs the hostnames and not the report, and none of it is
ever fatal. The happy path is checked against the bundled mock; the failure
paths need a client that can be told to fail, so they use a stand-in.

Two mock-fixture notes:

* The mock seeds exactly one overlay (``CorpFabric``) and **no** template
  groups. Where a section needs more than that to be interesting, the extra
  data is created through the mock's own POST endpoints — never by editing the
  seed, which several other test files depend on.
* ``GET /vrf/config/securityPolicies`` answers 204 for a segment pair with no
  orchestrated policy. That is a real answer ("none configured"), not a
  failure, and this file has a test that says so, because rendering it as an
  error would make every unconfigured fabric look broken.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console
from typer.testing import CliRunner

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, journal, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.main import render_fabric_config
from pyecsdwan.cli.shell import ShellCompleter, ShellState, dispatch_operational
from pyecsdwan.client import OrchApiError, OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import fabric
from pyecsdwan.resolver import Resolver

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    # The app callback binds structlog process-wide to click's capture stream,
    # which CliRunner closes on exit; leaving it bound kills the next test that
    # logs anything on a closed file. (Same teardown as tests/test_coverage.py.)
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "client": client,
        "settings": settings,
        "state": mstate,
        "port": base_url.rsplit(":", 1)[1],
    }


def _rendered(report: fabric.FabricConfig) -> str:
    console = Console(record=True, width=200)
    render_fabric_config(console, report)
    return console.export_text()


def _section(report: fabric.FabricConfig, name: str) -> Any:
    section = report.section(name)
    assert section is not None, f"{name} missing from the report"
    return section


# -- fabric fixtures built through the mock's own write endpoints -------------


def _add_overlay(world: dict[str, Any], name: str, members: list[str]) -> str:
    """Create a second overlay the way the Orchestrator would.

    Through ``POST /gms/overlays/config`` rather than by editing the seed:
    ``_seed_overlays`` is deliberately a one-overlay fixture that several
    other test files depend on, and ``next_overlay_id`` is the mock's own
    allocator.
    """
    client = world["client"]
    response = client.post("/gms/overlays/config", {"name": name, "topology": "mesh"})
    overlay_id = str(response["id"])
    client.post("/gms/overlays/association", {overlay_id: members})
    world["ctx"].resolver.refresh("overlays")
    return overlay_id


def _add_template_group(world: dict[str, Any], name: str, sections: list[str]) -> None:
    client = world["client"]
    client.post(
        "/template/templateCreate",
        {"name": name, "templates": [{"name": s, "valObject": {}} for s in sections]},
        expected=(200, 204),
    )
    world["ctx"].resolver.refresh("template_groups")


def _apply_template_group(world: dict[str, Any], ne_pk: str, groups: list[str]) -> None:
    world["client"].post(
        "/template/applianceAssociation", {"templateIds": groups}, params={"nePk": ne_pk}
    )


#: Two rules over one zone pair, carrying the ``self`` echoes and the
#: ``gms_marked`` flag a real response carries — the summary must count rules,
#: not bookkeeping keys.
_POLICY_BODY: dict[str, Any] = {
    "data": {
        "0_0": {
            "self": "0_0",
            "gms_marked": True,
            "0_0": {
                "self": "0_0",
                "prio": {
                    "1000": {"self": 1000, "match": {}, "set": {"action": "allow"}},
                    "2000": {"self": 2000, "match": {}, "set": {"action": "deny"}},
                },
            },
        }
    },
    "options": {"merge": False, "templateApply": False},
}


def _add_security_policy(world: dict[str, Any], pair: str = "0_0") -> None:
    world["client"].post("/vrf/config/securityPolicies", _POLICY_BODY, params={"map": pair})


# -- stand-in client for the failure paths the mock cannot produce -----------


class _FakeClient:
    """Answers the report's GETs from a dict; fails the paths it is told to.

    ``fail`` maps a path (or ``"appliance:<nePk>"`` for a proxy read) to the
    error message it should raise, so each degradation test can kill exactly
    one endpoint and assert the rest of the report survived.
    """

    settings = None

    def __init__(
        self,
        *,
        responses: dict[str, Any] | None = None,
        fail: dict[str, str] | None = None,
    ) -> None:
        self.responses = responses or {}
        self.fail = fail or {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        if path in self.fail:
            raise OrchApiError("GET", path, None, self.fail[path])
        return self.responses.get(path)

    def appliance_request(
        self, method: str, ne_pk: str, ecos_path: str, **kwargs: Any
    ) -> Any:
        self.calls.append((method, f"appliance:{ne_pk}/{ecos_path}", None))
        key = f"appliance:{ne_pk}"
        if key in self.fail:
            raise OrchApiError(method, ecos_path, None, self.fail[key])
        return self.responses.get(key, {})


class _FakeResolver:
    """Inventory the tests can shape, with an option to make it fail."""

    def __init__(
        self, appliances: list[dict[str, Any]], *, error: str = "", overlays: Any = None
    ) -> None:
        self._appliances = appliances
        self._overlays = overlays if overlays is not None else []
        self.error = error

    def appliances(self) -> list[dict[str, Any]]:
        if self.error:
            raise OrchApiError("GET", "/appliance", None, self.error)
        return self._appliances

    def overlays(self) -> list[dict[str, Any]]:
        return list(self._overlays)


def _fake_ctx(
    client: _FakeClient, appliances: list[dict[str, Any]], **kwargs: Any
) -> Ctx:
    return Ctx(client=client, resolver=_FakeResolver(appliances, **kwargs))  # type: ignore[arg-type]


# -- the shape of the report --------------------------------------------------


def test_every_section_is_collected_in_canonical_order(world: dict[str, Any]) -> None:
    report = fabric.collect(world["ctx"])
    assert [s.name for s in report.sections] == list(fabric.SECTIONS)
    assert report.requested == fabric.SECTIONS
    assert report.degraded == ()


def test_overlays_carry_their_members_as_hostnames(world: dict[str, Any]) -> None:
    section = _section(fabric.collect(world["ctx"]), fabric.OVERLAYS)
    corp = next(o for o in section.overlays if o.name == "CorpFabric")
    assert corp.member_ne_pks == ("1.NE",)
    assert corp.members == ("HUB1-EC",)
    # The condition this section exists to surface: who is in nothing.
    assert section.unassociated == ("BR1-EC", "BR2-EC")


def test_a_second_overlay_shows_up_with_its_own_members(world: dict[str, Any]) -> None:
    _add_overlay(world, "Guest", ["3.NE", "5.NE"])
    section = _section(fabric.collect(world["ctx"]), fabric.OVERLAYS)
    assert [o.name for o in section.overlays] == ["CorpFabric", "Guest"]
    guest = section.overlays[1]
    assert guest.topology == "mesh"
    assert guest.members == ("BR1-EC", "BR2-EC")
    assert guest.member_count == 2
    # Every appliance is now in some overlay.
    assert section.unassociated == ()


def test_an_overlay_with_no_members_is_called_out(world: dict[str, Any]) -> None:
    _add_overlay(world, "Empty", [])
    section = _section(fabric.collect(world["ctx"], section=fabric.OVERLAYS), fabric.OVERLAYS)
    assert [o.name for o in section.empty_overlays] == ["Empty"]


def test_template_groups_report_their_sections_and_where_applied(
    world: dict[str, Any],
) -> None:
    _add_template_group(world, "Branch-Base", ["SNMP", "Firewall Zones"])
    _add_template_group(world, "Unused", ["NTP"])
    _apply_template_group(world, "3.NE", ["Branch-Base"])
    _apply_template_group(world, "5.NE", ["Branch-Base"])

    section = _section(fabric.collect(world["ctx"]), fabric.TEMPLATES)
    base = next(g for g in section.groups if g.name == "Branch-Base")
    assert base.sections == ("Firewall Zones", "SNMP")
    assert base.applied_to == ("BR1-EC", "BR2-EC")
    assert [g.name for g in section.unapplied_groups] == ["Unused"]
    assert section.unassigned == ("HUB1-EC",)


def test_security_policy_summary_counts_rules_not_bookkeeping(
    world: dict[str, Any],
) -> None:
    _add_security_policy(world)
    section = _section(fabric.collect(world["ctx"], section=fabric.SECURITY), fabric.SECURITY)
    assert section.segments == ("0",)
    policy = section.policies[0]
    assert policy.pair == "0_0"
    assert policy.present
    # Two rules over one zone pair — `self` and `gms_marked` are echoes that
    # SecurityPolicy.normalize strips, and must not be counted as content.
    assert policy.rule_count == 2
    assert policy.zone_pairs == 1
    assert dict(policy.actions) == {"allow": 1, "deny": 1}
    assert section.total_rules == 2


def test_a_segment_pair_with_no_policy_is_not_an_error(world: dict[str, Any]) -> None:
    """A 204 is 'nothing orchestrated here', which is a real answer."""
    section = _section(fabric.collect(world["ctx"], section=fabric.SECURITY), fabric.SECURITY)
    policy = section.policies[0]
    assert not policy.present
    assert policy.error == ""
    assert section.configured == ()
    assert not section.degraded


def test_inventory_counts_by_role_site_model_and_state(world: dict[str, Any]) -> None:
    section = _section(fabric.collect(world["ctx"]), fabric.INVENTORY)
    assert section.total == 3
    assert dict(section.by_site) == {"HQ": 1, "Branch-1": 1, "Branch-2": 1}
    assert dict(section.by_model) == {"EC-S": 3}
    assert dict(section.by_state) == {"normal": 1 * 3}
    # The mock's inventory sets no networkRole; "unspecified" is the honest
    # rendering of a field the Orchestrator did not fill in.
    assert dict(section.by_role) == {fabric.UNSPECIFIED: 3}


def test_network_role_codes_are_named() -> None:
    """`networkRole` is spoke=0, hub=1, nrhub=3 — and an unknown code passes
    through as sent rather than being flattened into 'unspecified'."""
    section = fabric.collect_inventory(
        [
            {"nePk": "1.NE", "hostName": "HUB1-EC", "networkRole": "1", "state": 1},
            {"nePk": "3.NE", "hostName": "BR1-EC", "networkRole": "0", "state": 2},
            {"nePk": "5.NE", "hostName": "BR2-EC", "networkRole": "9", "state": 4},
        ]
    )
    assert dict(section.by_role) == {"hub": 1, "spoke": 1, "9": 1}
    assert dict(section.by_state) == {"normal": 1, "unreachable": 1, "out of sync": 1}


def test_deployment_reads_every_appliance(world: dict[str, Any]) -> None:
    section = _section(fabric.collect(world["ctx"]), fabric.DEPLOYMENT)
    assert [a.hostname for a in section.appliances] == ["HUB1-EC", "BR1-EC", "BR2-EC"]
    assert dict(section.by_mode) == {"router": 3}
    hub = section.appliances[0]
    assert hub.license == "boost"
    assert hub.wan_labels == ("MPLS1", "INET1")
    assert hub.lan_labels == ("Data", "Voice")
    assert hub.interfaces == 6
    assert hub.addresses == 4
    assert section.unreachable == ()


# -- partial data is flagged in-band, never fatal -----------------------------


def test_an_unreachable_appliance_is_a_marked_row(state_home: Any) -> None:
    client = _FakeClient(
        responses={"appliance:1.NE": {"sysConfig": {"mode": "router"}}},
        fail={"appliance:3.NE": "connection refused"},
    )
    inventory = [
        {"nePk": "1.NE", "hostName": "HUB1-EC"},
        {"nePk": "3.NE", "hostName": "BR1-EC"},
    ]
    section = fabric.collect_deployment(_fake_ctx(client, inventory), inventory)
    assert [a.hostname for a in section.appliances] == ["HUB1-EC", "BR1-EC"]
    dead = section.appliances[1]
    assert dead.unreachable
    assert "connection refused" in dead.error
    assert [a.hostname for a in section.reachable] == ["HUB1-EC"]
    # The row renders; nothing was raised and nothing was dropped.
    assert "unreachable" in _rendered(fabric.FabricConfig(sections=(section,)))


def test_a_dead_endpoint_costs_its_section_and_nothing_else(state_home: Any) -> None:
    inventory = [{"nePk": "1.NE", "hostName": "HUB1-EC"}]
    client = _FakeClient(
        responses={
            "/gms/overlays/association": {"1": ["1.NE"]},
            "/zones/vrfZonesMap": {"0": {}},
        },
        fail={"/template/templateGroups": "502 Bad Gateway"},
    )
    ctx = _fake_ctx(client, inventory, overlays=[{"id": 1, "name": "CorpFabric"}])
    report = fabric.collect(ctx)

    templates = _section(report, fabric.TEMPLATES)
    assert templates.degraded
    assert any("502 Bad Gateway" in note for note in templates.notes)
    assert templates.groups == ()
    # Everything else still rendered.
    assert not _section(report, fabric.OVERLAYS).degraded
    assert _section(report, fabric.OVERLAYS).overlays[0].members == ("HUB1-EC",)
    assert [s.name for s in report.degraded] == [fabric.TEMPLATES]
    assert "502 Bad Gateway" in _rendered(report)


def test_unreadable_membership_still_lists_the_overlays(state_home: Any) -> None:
    client = _FakeClient(fail={"/gms/overlays/association": "500 Internal Server Error"})
    ctx = _fake_ctx(
        client, [{"nePk": "1.NE", "hostName": "HUB1-EC"}], overlays=[{"id": 1, "name": "Corp"}]
    )
    section = fabric.collect_overlays(ctx, ctx.resolver.appliances())
    assert [o.name for o in section.overlays] == ["Corp"]
    assert section.overlays[0].member_count == 0
    assert any("membership unreadable" in note for note in section.notes)


def test_unreadable_template_association_does_not_claim_everything_is_untemplated(
    state_home: Any,
) -> None:
    """The most alarming possible way to be wrong: reporting every appliance
    as having no template group because the association read failed."""
    client = _FakeClient(
        responses={"/template/templateGroups": [{"name": "Branch-Base", "templates": []}]},
        fail={"/template/applianceAssociation": "503 Service Unavailable"},
    )
    inventory = [{"nePk": "1.NE", "hostName": "HUB1-EC"}]
    section = fabric.collect_templates(_fake_ctx(client, inventory), inventory)
    assert [g.name for g in section.groups] == ["Branch-Base"]
    assert section.unassigned == ()
    assert any("application unreadable" in note for note in section.notes)


def test_a_failed_inventory_degrades_rather_than_raising(state_home: Any) -> None:
    client = _FakeClient(responses={"/gms/overlays/association": {"1": ["1.NE"]}})
    ctx = Ctx(
        client=client,  # type: ignore[arg-type]
        resolver=_FakeResolver(
            [], error="connection refused", overlays=[{"id": 1, "name": "Corp"}]
        ),
    )
    report = fabric.collect(ctx)
    overlays = _section(report, fabric.OVERLAYS)
    # No inventory means no hostnames — the member falls back to its nePk
    # rather than the overlay losing the member.
    assert overlays.overlays[0].members == ("1.NE",)
    assert any("connection refused" in note for note in overlays.notes)
    assert _section(report, fabric.INVENTORY).total == 0
    assert _section(report, fabric.DEPLOYMENT).appliances == ()
    assert {s.name for s in report.degraded} >= {
        fabric.OVERLAYS,
        fabric.TEMPLATES,
        fabric.INVENTORY,
        fabric.DEPLOYMENT,
    }


def test_one_bad_segment_pair_is_one_bad_row(state_home: Any) -> None:
    client = _FakeClient(
        responses={"/zones/vrfZonesMap": {"0": {}}},
        fail={"/vrf/config/securityPolicies": "500 Internal Server Error"},
    )
    section = fabric.collect_security(_fake_ctx(client, []))
    assert len(section.policies) == 1
    assert section.policies[0].unreachable
    assert "500" in section.policies[0].error


def test_an_unreadable_segment_map_falls_back_to_the_default_segment(
    state_home: Any,
) -> None:
    client = _FakeClient(fail={"/zones/vrfZonesMap": "404 Not Found"})
    section = fabric.collect_security(_fake_ctx(client, []))
    assert section.segments == ("0",)
    assert any("segment map unreadable" in note for note in section.notes)
    # Segment 0 exists on every fabric, so the fallback still reads a real pair.
    assert [p.pair for p in section.policies] == ["0_0"]


# -- the segment-pair read is bounded ----------------------------------------


def test_small_fabrics_read_the_full_cross_product() -> None:
    pairs, note = fabric._segment_pairs(["0", "1"])
    assert set(pairs) == {"0_0", "0_1", "1_0", "1_1"}
    assert note == ""


def test_many_segments_drop_to_intra_segment_pairs_and_say_so() -> None:
    segments = [str(i) for i in range(6)]
    pairs, note = fabric._segment_pairs(segments)
    assert pairs == tuple(f"{i}_{i}" for i in range(6))
    assert "inter-segment policy pairs omitted" in note
    assert len(pairs) <= fabric.MAX_POLICY_READS


# -- --section ----------------------------------------------------------------


def test_section_scopes_the_report_and_the_reads(world: dict[str, Any]) -> None:
    report = fabric.collect(world["ctx"], section=fabric.OVERLAYS)
    assert [s.name for s in report.sections] == [fabric.OVERLAYS]
    assert report.requested == (fabric.OVERLAYS,)
    # Scoped out is not the same as empty: the payload omits what was not asked
    # for, so a consumer can tell the two apart.
    payload = fabric.to_payload(report)
    assert set(payload["sections"]) == {fabric.OVERLAYS}


def test_the_security_section_alone_never_touches_the_inventory(state_home: Any) -> None:
    client = _FakeClient(responses={"/zones/vrfZonesMap": {"0": {}}})
    ctx = Ctx(
        client=client,  # type: ignore[arg-type]
        resolver=_FakeResolver([], error="the inventory must not be read"),
    )
    report = fabric.collect(ctx, section=fabric.SECURITY)
    assert [s.name for s in report.sections] == [fabric.SECURITY]
    assert not report.degraded


def test_an_unknown_section_names_the_valid_ones() -> None:
    with pytest.raises(fabric.UnknownSection) as excinfo:
        fabric.resolve_sections("overlay")
    message = str(excinfo.value)
    assert "unknown section 'overlay'" in message
    for name in fabric.SECTIONS:
        assert name in message


@pytest.mark.parametrize("name", fabric.SECTIONS)
def test_every_declared_section_is_collectable(world: dict[str, Any], name: str) -> None:
    report = fabric.collect(world["ctx"], section=name)
    assert [s.name for s in report.sections] == [name]
    assert _rendered(report).strip()


# -- --json -------------------------------------------------------------------


def test_json_emits_the_structured_tree(world: dict[str, Any]) -> None:
    _add_overlay(world, "Guest", ["3.NE"])
    _add_template_group(world, "Branch-Base", ["SNMP"])
    _apply_template_group(world, "3.NE", ["Branch-Base"])
    _add_security_policy(world)

    payload = fabric.to_payload(fabric.collect(world["ctx"]))
    assert payload["requested"] == list(fabric.SECTIONS)
    assert payload["degraded"] == []
    assert set(payload["sections"]) == set(fabric.SECTIONS)

    overlays = payload["sections"]["overlays"]
    assert overlays["count"] == 2
    assert {o["name"] for o in overlays["overlays"]} == {"CorpFabric", "Guest"}
    assert payload["sections"]["templates"]["groups"][0]["applied_to"] == ["BR1-EC"]
    assert payload["sections"]["security"]["total_rules"] == 2
    assert payload["sections"]["appliances"]["by_site"]["HQ"] == 1
    assert payload["sections"]["deployment"]["by_mode"] == {"router": 3}
    # Round-trips: the tree is JSON, not a rendering.
    assert json.loads(json.dumps(payload)) == payload


# -- rendering ----------------------------------------------------------------


def test_the_rendered_report_names_every_section(world: dict[str, Any]) -> None:
    _add_template_group(world, "Branch-Base", ["SNMP"])
    out = _rendered(fabric.collect(world["ctx"]))
    assert "overlays (1)" in out
    assert "CorpFabric" in out
    assert "template groups (1)" in out
    assert "Branch-Base" in out
    assert "security policy" in out
    assert "appliances (3)" in out
    assert "deployment (3)" in out
    assert "router" in out


# -- CLI ----------------------------------------------------------------------


def _cli(world: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", world["port"], "show", "configuration", *args])


def test_cli_show_run_renders_the_fabric_report(world: dict[str, Any]) -> None:
    result = _cli(world, "fabric")
    assert result.exit_code == 0, result.output
    assert "overlays (1)" in result.output
    assert "CorpFabric" in result.output
    assert "deployment (3)" in result.output


def test_cli_show_run_json(world: dict[str, Any]) -> None:
    result = _cli(world, "fabric", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload["sections"]) == set(fabric.SECTIONS)


def test_cli_section_scopes_the_report(world: dict[str, Any]) -> None:
    result = _cli(world, "fabric", "overlays")
    assert result.exit_code == 0, result.output
    assert "overlays (1)" in result.output
    assert "deployment" not in result.output


def test_cli_unknown_section_errors_cleanly_and_lists_the_valid_ones(
    world: dict[str, Any],
) -> None:
    result = _cli(world, "fabric", "overlay")
    assert result.exit_code == 2
    assert "unknown section 'overlay'" in result.output
    for name in fabric.SECTIONS:
        assert name in result.output


def test_cli_show_run_appliance_still_reads_one_appliance(world: dict[str, Any]) -> None:
    """The #55/#56 split: the group callback renders the fabric report only
    when no subcommand was given."""
    result = _cli(world, "appliance", "BR1-EC", "--format", "native")
    assert result.exit_code == 0, result.output
    assert "# BR1-EC running-config" in result.output
    assert "overlays" not in result.output


# -- shell --------------------------------------------------------------------


@pytest.fixture
def shell_state(world: dict[str, Any]) -> ShellState:
    return ShellState(
        ctx=world["ctx"],
        registry=default_registry,
        settings=world["settings"],
        console=Console(record=True, width=200),
        candidate=CandidateStore(world["settings"].host),
    )


def _shell(state: ShellState, line: str) -> str:
    dispatch_operational(line, state)
    return state.console.export_text()


def test_shell_bare_show_run_renders_the_fabric_report(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration fabric")
    assert "overlays (1)" in out
    assert "CorpFabric" in out
    assert "deployment (3)" in out


def test_shell_show_run_scopes_to_one_section(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration fabric overlays")
    assert "overlays (1)" in out
    assert "deployment" not in out


def test_shell_unknown_section_lists_the_valid_ones(shell_state: ShellState) -> None:
    out = _shell(shell_state, "show configuration fabric wibble")
    assert "unknown section 'wibble'" in out
    for name in fabric.SECTIONS:
        assert name in out
    assert "usage: show configuration [running] fabric [<section>]" in out


def test_the_appliance_and_appliances_ambiguity_is_gone_by_construction(
    shell_state: ShellState,
) -> None:
    """`appliance` and the `appliances` *section* differ by one character.

    The old grammar had them in the same position under `show run`, so the
    parser matched `appliance` first and hoped. They are now under different
    scope nouns, so neither can be reached from the other's position and there
    is nothing left to guess at.
    """
    native = _shell(shell_state, "show configuration appliance BR1-EC --format native")
    assert "# BR1-EC (3.NE)" in native

    # `appliance` is not a section...
    section = _shell(shell_state, "show configuration fabric appliance")
    assert "unknown section 'appliance'" in section
    # ...and `appliances` is, still.
    assert "unknown section" not in _shell(shell_state, "show configuration fabric appliances")


def test_shell_completer_offers_both_forms(shell_state: ShellState) -> None:
    """The two halves now sit under different tokens, and completion follows."""
    completer = ShellCompleter(shell_state)
    assert set(fabric.SECTIONS) <= set(completer._options(["show", "configuration", "fabric"]))
    assert "appliance" in completer._options(["show", "configuration"])
    assert "fabric" in completer._options(["show", "configuration"])


# -- read-only ----------------------------------------------------------------


class _WriteRefusingClient:
    """Delegates reads; any write is a test failure.

    The report's read-only claim is a property of the code, not of the
    docstring, so it is asserted by making a write impossible rather than by
    checking after the fact that none happened to land.
    """

    def __init__(self, inner: OrchClient) -> None:
        self._inner = inner
        self.settings = inner.settings
        self.methods: list[str] = []

    def get(self, path: str, **kwargs: Any) -> Any:
        self.methods.append("GET")
        return self._inner.get(path, **kwargs)

    def appliance_request(self, method: str, ne_pk: str, ecos_path: str, **kwargs: Any) -> Any:
        self.methods.append(method)
        if method != "GET":
            raise AssertionError(f"show configuration fabric sent {method} to the appliance proxy")
        return self._inner.appliance_request(method, ne_pk, ecos_path, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("show configuration fabric issued a POST")

    def put(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("show configuration fabric issued a PUT")

    def delete(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("show configuration fabric issued a DELETE")


def test_the_report_only_ever_reads(world: dict[str, Any]) -> None:
    sealed = _WriteRefusingClient(world["client"])
    ctx = Ctx(client=sealed, resolver=world["ctx"].resolver)  # type: ignore[arg-type]
    report = fabric.collect(ctx)
    assert report.sections
    assert set(sealed.methods) == {"GET"}


def test_show_run_leaves_no_candidate_journal_or_transaction(
    world: dict[str, Any], shell_state: ShellState
) -> None:
    _shell(shell_state, "show configuration fabric")
    result = _cli(world, "fabric")
    assert result.exit_code == 0, result.output

    assert journal.list_txns() == []
    assert txn.pending_rollbacks(host=world["settings"].host) == []
    assert len(shell_state.candidate) == 0
    assert len(CandidateStore(world["settings"].host)) == 0
    # The mock flags any appliance a write reached; the report reached none.
    assert not any(a.get("hasUnsavedChanges") for a in world["state"].appliances)



def test_configured_pairs_are_read_not_derived(world: dict[str, Any]) -> None:
    """`GET /vrf/config/securityPolicies` requires map=<src>_<dst> and cannot be
    enumerated, so the pairs were originally guessed at from the segment cross
    product — O(n^2) GETs against a control plane. `securityPoliciesSegments`
    answers the real question in one call; this proves we take it."""
    _add_security_policy(world, "0_0")
    _add_security_policy(world, "1_2")
    pairs, note = fabric._configured_pairs(world["ctx"])
    assert pairs == ("0_0", "1_2")
    assert note == ""
    # And the section reports both without being told the segments exist.
    section = fabric.collect_security(world["ctx"])
    assert {p.pair for p in section.policies} == {"0_0", "1_2"}
    assert not section.degraded


def test_missing_segments_endpoint_falls_back_to_the_cross_product(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older Orchestrators do not carry the endpoint. Losing it must cost the
    optimization, never the section."""
    client = world["client"]
    original = client.get

    def refuse(path: str, **kwargs: Any) -> Any:
        if path == fabric.SECURITY_SEGMENTS_PATH:
            raise OrchApiError("GET", path, 404, "not found")
        return original(path, **kwargs)

    monkeypatch.setattr(client, "get", refuse)
    assert fabric._configured_pairs(world["ctx"]) == ((), "")
    # The section still renders, via the bounded cross-product derivation.
    section = fabric.collect_security(world["ctx"])
    assert isinstance(section, fabric.SecuritySection)


def test_a_non_list_response_falls_back_rather_than_crashing(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live drift from the spec is tolerated everywhere else in this codebase;
    a report is not the place to start trusting a shape."""
    monkeypatch.setattr(world["client"], "get", lambda path, **kw: {"unexpected": "object"})
    assert fabric._configured_pairs(world["ctx"]) == ((), "")
