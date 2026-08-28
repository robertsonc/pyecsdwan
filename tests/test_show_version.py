"""``show fabric version``: fabric version report, active + backup partitions (#57).

The two conditions this command exists to surface are what most of these tests
pin: **fleet version skew** (BR2-EC is a minor release behind) and a
**next boot that differs from the active partition** (BR2-EC would come back on
its fallback partition after a reload). Both are seeded in the mock fixture, so
a report that renders them correctly there renders the interesting cases.

Two API traps get their own tests because getting either wrong is invisible
until it reaches real gear:

* ``/gms/versions`` returns ``current`` (running) *and* ``installed`` (three
  versions available to upgrade to). The spec's summary line makes the whole
  response read like an answer to "what version is this?"; it is not.
* ``/appliancesSoftwareVersions`` requires ``cached`` as well as ``nePk``, so
  ``--no-cache`` has to send ``cached=false`` rather than omit the parameter.
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
from pyecsdwan import config, journal
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.cli.main import render_version_report
from pyecsdwan.cli.shell import ShellState, dispatch_operational
from pyecsdwan.client import OrchApiError, OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import versions
from pyecsdwan.resolver import Resolver

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    # The app callback binds structlog process-wide to click's capture stream,
    # which CliRunner closes on exit; leaving it bound kills the next test that
    # logs anything on a closed file.
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def settings_for_mock(state_home: Any, mock_server: tuple[str, MockState]) -> config.Settings:
    base_url, mstate = mock_server
    mstate.reset()
    return config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)


@pytest.fixture
def ctx(settings_for_mock: config.Settings) -> Ctx:
    client = OrchClient(settings_for_mock)
    return Ctx(client=client, resolver=Resolver(client))


@pytest.fixture
def report(ctx: Ctx) -> versions.FabricVersions:
    return versions.collect(ctx)


def _rendered(report: versions.FabricVersions) -> str:
    console = Console(record=True, width=200)
    render_version_report(console, report)
    return console.export_text()


def _appliance(report: versions.FabricVersions, hostname: str) -> versions.ApplianceVersions:
    match = [a for a in report.appliances if a.hostname == hostname]
    assert match, f"{hostname} missing from report"
    return match[0]


# -- fake client: the query-parameter and failure cases the mock cannot pin ---


class _RecordingClient:
    """Minimal OrchClient stand-in that records every GET.

    Used where the assertion is about *what was sent* (``cached=false``) or
    about a failure the mock cannot produce on demand (one dead appliance).
    """

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.fail_for = fail_for or set()

    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((path, params))
        if path == versions.ORCHESTRATOR_VERSIONS_PATH:
            return {"current": "9.4.2.40100", "installed": ["9.4.2.40100", "9.4.1.40077"]}
        ne_pk = str((params or {}).get("nePk"))
        if ne_pk in self.fail_for:
            raise OrchApiError("GET", path, None, "connection refused")
        return [
            {
                "partition": 0,
                "build_version": "9.4.2.40100",
                "build_time": "2026-06-11T02:14:00Z",
                "active": True,
                "next_boot": True,
                "fallback_boot": False,
            },
            {
                "partition": 1,
                "build_version": "9.4.1.40077",
                "build_time": "2026-04-02T02:11:00Z",
                "active": False,
                "next_boot": False,
                "fallback_boot": True,
            },
        ]

    def version_params(self) -> list[dict[str, Any]]:
        return [
            params or {}
            for path, params in self.calls
            if path == versions.APPLIANCE_VERSIONS_PATH
        ]


class _FakeResolver:
    def __init__(self, inventory: list[dict[str, Any]]) -> None:
        self._inventory = inventory

    def appliances(self) -> list[dict[str, Any]]:
        return self._inventory


def _fake_ctx(client: _RecordingClient, inventory: list[dict[str, Any]] | None = None) -> Ctx:
    if inventory is None:
        inventory = [
            {"nePk": "1.NE", "hostName": "HUB1-EC"},
            {"nePk": "5.NE", "hostName": "BR2-EC"},
        ]
    return Ctx(client=client, resolver=_FakeResolver(inventory))  # type: ignore[arg-type]


# -- the Orchestrator version -------------------------------------------------


def test_orchestrator_version_is_current_not_installed(ctx: Ctx) -> None:
    """`installed` is the list of versions available to upgrade *to*. Rendering
    it as "the Orchestrator version" would be wrong, and the mock now seeds a
    newer release at installed[0] so that specific mistake fails here."""
    running, available = versions.orchestrator_version(ctx.client)
    assert running == "9.4.2.40100"
    assert list(available) == ["9.4.3.40210", "9.4.2.40100", "9.4.1.40077"]
    # The mistake this test exists for: installed[0] is a staged newer build.
    assert running != available[0]


def test_orchestrator_version_ignores_installed_even_at_index_zero() -> None:
    """Belt and braces alongside the fixture fix: even on a payload where
    `current` DOES appear in `installed`, a report rendering ``installed[0]``
    still being wrong. This pins the semantic on a payload where the two
    disagree: `current` is the running Orchestrator, full stop."""

    class _Divergent(_RecordingClient):
        def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
            if path == versions.ORCHESTRATOR_VERSIONS_PATH:
                self.calls.append((path, params))
                # A freshly upgraded Orchestrator whose available-version list
                # has not been refreshed: nothing in `installed` is running.
                return {
                    "current": "9.5.0.40500",
                    "installed": ["9.4.2.40100", "9.4.1.40077", "9.3.4.39802"],
                }
            return super().get(path, params=params)

    result = versions.collect(_fake_ctx(_Divergent()))
    assert result.orchestrator == "9.5.0.40500"
    header = _rendered(result).splitlines()[0]
    assert header.strip() == "Orchestrator 9.5.0.40500"
    assert "9.4.2.40100" not in header


def test_orchestrator_version_is_the_header(report: versions.FabricVersions) -> None:
    out = _rendered(report)
    header = out.splitlines()[0]
    assert "Orchestrator 9.4.2.40100" in header
    # The available-versions line exists, but below the header and labelled as
    # available — never presented as the running version.
    assert "orchestrator versions available" in out


def test_orchestrator_failure_still_renders_the_appliance_table() -> None:
    """A version report is most needed when something is down."""

    class _NoOrchestrator(_RecordingClient):
        def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
            if path == versions.ORCHESTRATOR_VERSIONS_PATH:
                raise OrchApiError("GET", path, 503, "service unavailable")
            return super().get(path, params=params)

    result = versions.collect(_fake_ctx(_NoOrchestrator()))
    assert result.orchestrator == versions.UNKNOWN
    assert "503" in result.orchestrator_error
    assert len(result.appliances) == 2
    out = _rendered(result)
    assert "Orchestrator version unknown" in out
    assert "HUB1-EC" in out


# -- partitions: active vs fallback vs next boot ------------------------------


def test_each_row_distinguishes_active_from_fallback(report: versions.FabricVersions) -> None:
    hub = _appliance(report, "HUB1-EC")
    active, backup = hub.active, hub.backup
    assert active is not None and backup is not None
    assert (active.index, active.version) == (0, "9.4.2.40100")
    assert (backup.index, backup.version) == (1, "9.4.1.40077")
    assert active.active and not backup.active
    assert backup.fallback_boot


def test_backup_falls_back_to_the_other_partition_when_unflagged() -> None:
    """Some appliances report no `fallback_boot` at all; "the other partition"
    is still what an operator means by the backup."""
    appliance = versions.ApplianceVersions(
        ne_pk="1.NE",
        hostname="HUB1-EC",
        partitions=(
            versions.Partition(0, "9.4.2.40100", "", active=True, next_boot=True,
                               fallback_boot=False),
            versions.Partition(1, "9.4.1.40077", "", active=False, next_boot=False,
                               fallback_boot=False),
        ),
    )
    backup = appliance.backup
    assert backup is not None and backup.index == 1


def test_divergent_next_boot_is_flagged(report: versions.FabricVersions) -> None:
    """BR2-EC's next boot is its fallback partition: a reload changes the
    running version, which is exactly what an operator needs before a
    maintenance window."""
    br2 = _appliance(report, "BR2-EC")
    assert br2.next_boot_diverges
    upcoming, active = br2.next_boot, br2.active
    assert upcoming is not None and active is not None
    assert (upcoming.index, upcoming.version) == (1, "9.4.1.40077")
    assert (active.index, active.version) == (0, "9.3.4.39802")

    out = _rendered(report)
    assert "! 9.4.1.40077 (p1)" in out
    assert "BR2-EC: next reload boots 9.4.1.40077 (p1), not the running 9.3.4.39802 (p0)" in out


def test_aligned_next_boot_is_not_flagged(report: versions.FabricVersions) -> None:
    hub = _appliance(report, "HUB1-EC")
    assert not hub.next_boot_diverges
    assert [a.hostname for a in report.divergent_next_boot] == ["BR2-EC"]


def test_unknown_next_boot_is_not_divergence() -> None:
    """An appliance flagging no next boot is unknown, not "consistent"."""
    appliance = versions.ApplianceVersions(
        ne_pk="1.NE",
        hostname="HUB1-EC",
        partitions=(
            versions.Partition(0, "9.4.2.40100", "", active=True, next_boot=False,
                               fallback_boot=False),
        ),
    )
    assert appliance.next_boot is None
    assert not appliance.next_boot_diverges


# -- fleet skew ---------------------------------------------------------------


def test_version_skew_is_detected_and_highlighted(report: versions.FabricVersions) -> None:
    assert report.skewed
    assert report.baseline_version == "9.4.2.40100"
    assert list(report.active_versions) == ["9.4.2.40100", "9.3.4.39802"]
    assert report.is_outlier(_appliance(report, "BR2-EC"))
    assert not report.is_outlier(_appliance(report, "HUB1-EC"))

    out = _rendered(report)
    assert "version skew" in out
    assert "baseline 9.4.2.40100" in out
    assert "off-baseline: BR2-EC" in out


def test_a_uniform_fleet_reports_no_skew() -> None:
    result = versions.collect(_fake_ctx(_RecordingClient()))
    assert not result.skewed
    assert list(result.active_versions) == ["9.4.2.40100"]
    assert not any(result.is_outlier(a) for a in result.appliances)
    assert "fleet is uniform on 9.4.2.40100" in _rendered(result)


def test_version_ordering_is_numeric_not_lexicographic() -> None:
    """`9.10` is newer than `9.4`; a string compare says the opposite and would
    name the wrong baseline."""
    ordered = sorted(["9.4.2.40100", "9.10.0.1", "9.3.4.39802"], key=versions.version_key)
    assert ordered == ["9.3.4.39802", "9.4.2.40100", "9.10.0.1"]
    # Unparseable versions sort rather than raise.
    assert versions.version_key("not-a-version")


def test_baseline_breaks_a_tie_toward_the_newest() -> None:
    """A fabric split evenly across two releases names the newer as baseline
    and flags the laggard, rather than picking arbitrarily."""
    old = versions.Partition(0, "9.3.4.39802", "", active=True, next_boot=True,
                             fallback_boot=False)
    new = versions.Partition(0, "9.4.2.40100", "", active=True, next_boot=True,
                             fallback_boot=False)
    result = versions.FabricVersions(
        orchestrator="9.4.2.40100",
        appliances=(
            versions.ApplianceVersions("1.NE", "A", (new,)),
            versions.ApplianceVersions("3.NE", "B", (old,)),
        ),
    )
    assert result.baseline_version == "9.4.2.40100"
    assert [a.hostname for a in result.appliances if result.is_outlier(a)] == ["B"]


# -- the `cached` query parameter --------------------------------------------


def test_default_sends_cached_true() -> None:
    client = _RecordingClient()
    versions.collect(_fake_ctx(client))
    params = client.version_params()
    assert len(params) == 2
    assert all(p["cached"] == "true" for p in params)
    assert [p["nePk"] for p in params] == ["1.NE", "5.NE"]


def test_no_cache_sends_cached_false_never_omits_it() -> None:
    """Both parameters are required by the spec: an omitted `cached` is a 422,
    not a default."""
    client = _RecordingClient()
    versions.collect(_fake_ctx(client), cached=False)
    params = client.version_params()
    assert params, "no appliance version calls were made"
    for sent in params:
        assert "cached" in sent, "cached must be sent, not omitted"
        assert sent["cached"] == "false"


def test_cached_parameter_is_accepted_by_the_api(ctx: Ctx) -> None:
    """End-to-end against the mock, which declares both parameters required —
    a dropped or misspelled `cached` 422s here as it would live."""
    for cached in (True, False):
        partitions = versions.appliance_partitions(ctx.client, "1.NE", cached=cached)
        assert len(partitions) == 2


# -- unreachable appliances ---------------------------------------------------


def test_unreachable_appliance_becomes_a_row_and_the_rest_still_renders() -> None:
    client = _RecordingClient(fail_for={"5.NE"})
    result = versions.collect(_fake_ctx(client))
    assert [a.hostname for a in result.unreachable] == ["BR2-EC"]
    assert [a.hostname for a in result.reachable] == ["HUB1-EC"]

    out = _rendered(result)
    assert "unreachable" in out
    assert "BR2-EC" in out
    # The rest of the report is intact, not lost with the dead appliance.
    assert "HUB1-EC" in out
    assert "9.4.2.40100 (p0)" in out


def test_unreachable_appliance_is_not_counted_as_skew() -> None:
    client = _RecordingClient(fail_for={"5.NE"})
    result = versions.collect(_fake_ctx(client))
    assert not result.skewed
    assert not result.is_outlier(_appliance(result, "BR2-EC"))


def test_an_appliance_with_no_partitions_renders_a_row() -> None:
    class _Empty(_RecordingClient):
        def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
            if path == versions.APPLIANCE_VERSIONS_PATH:
                self.calls.append((path, params))
                return []
            return super().get(path, params=params)

    result = versions.collect(_fake_ctx(_Empty()))
    assert result.reachable == ()
    out = _rendered(result)
    assert "reported no partitions" in out


# -- CLI ----------------------------------------------------------------------


def _invoke(settings: config.Settings, args: list[str], monkeypatch: Any) -> Any:
    monkeypatch.setenv(config.ENV_ORCH_URL, settings.orch_url)
    monkeypatch.setenv(config.ENV_API_KEY, settings.api_key or "")
    return runner.invoke(cli_main.app, args)


def test_cli_show_version_renders_the_report(
    settings_for_mock: config.Settings, monkeypatch: Any
) -> None:
    result = _invoke(settings_for_mock, ["show", "version"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Orchestrator 9.4.2.40100" in result.output
    assert "BR2-EC" in result.output
    assert "version skew" in result.output


def test_cli_json_emits_full_per_partition_data(
    settings_for_mock: config.Settings, monkeypatch: Any
) -> None:
    """`--json` is not the rendered summary: every partition, with every field
    the appliance reported, has to be in there."""
    result = _invoke(settings_for_mock, ["show", "version", "--json"], monkeypatch)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["orchestrator"]["current"] == "9.4.2.40100"
    assert payload["orchestrator"]["available"] == [
        "9.4.3.40210",
        "9.4.2.40100",
        "9.4.1.40077",
    ]
    assert payload["cached"] is True
    assert payload["fleet"]["skewed"] is True
    assert payload["fleet"]["baseline_version"] == "9.4.2.40100"

    by_host = {a["hostname"]: a for a in payload["appliances"]}
    assert set(by_host) == {"HUB1-EC", "BR1-EC", "BR2-EC"}
    br2 = by_host["BR2-EC"]
    assert br2["next_boot_diverges"] is True
    assert br2["version_skew"] is True
    assert len(br2["partitions"]) == 2
    for partition in br2["partitions"]:
        # Every field of the API entry survives, including build_time, which
        # the table never renders.
        assert set(partition) >= {
            "partition",
            "build_version",
            "build_time",
            "active",
            "next_boot",
            "fallback_boot",
        }
        assert partition["build_time"]
    fallback = [p for p in br2["partitions"] if p["fallback_boot"]]
    assert len(fallback) == 1
    assert fallback[0]["next_boot"] is True
    assert fallback[0]["active"] is False


def test_cli_no_cache_is_reported_and_succeeds(
    settings_for_mock: config.Settings, monkeypatch: Any
) -> None:
    result = _invoke(settings_for_mock, ["show", "version", "--no-cache"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "--no-cache" in result.output

    as_json = _invoke(settings_for_mock, ["show", "version", "--no-cache", "--json"], monkeypatch)
    assert as_json.exit_code == 0, as_json.output
    assert json.loads(as_json.output)["cached"] is False


# -- read-only ----------------------------------------------------------------


def test_show_version_has_no_transactional_side_effects(
    settings_for_mock: config.Settings, monkeypatch: Any
) -> None:
    """A report is not a transaction: no candidate item, no journal entry, no
    pending transaction, and nothing but GETs on the wire."""
    candidate = CandidateStore(settings_for_mock.host)
    assert candidate.ordered_items() == []
    assert journal.list_txns() == []

    result = _invoke(settings_for_mock, ["show", "version"], monkeypatch)
    assert result.exit_code == 0, result.output

    assert CandidateStore(settings_for_mock.host).ordered_items() == []
    assert journal.list_txns() == []


def test_collect_issues_only_gets() -> None:
    """The recording client has no post/put/delete at all, so a write would be
    an AttributeError; assert the paths too, so a future edit cannot quietly
    add a non-GET call through some other door."""
    client = _RecordingClient()
    versions.collect(_fake_ctx(client))
    assert {path for path, _params in client.calls} == {
        versions.ORCHESTRATOR_VERSIONS_PATH,
        versions.APPLIANCE_VERSIONS_PATH,
    }


# -- the interactive shell ----------------------------------------------------


@pytest.fixture
def shell_state(settings_for_mock: config.Settings) -> ShellState:
    client = OrchClient(settings_for_mock)
    return ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=default_registry,
        settings=settings_for_mock,
        console=Console(record=True, width=200),
        candidate=CandidateStore(settings_for_mock.host),
    )


def test_shell_show_version(shell_state: ShellState) -> None:
    dispatch_operational("show fabric version", shell_state)
    out = shell_state.console.export_text()
    assert "Orchestrator 9.4.2.40100" in out
    assert "version skew" in out
    assert "BR2-EC: next reload boots" in out


def test_shell_show_version_is_read_only(shell_state: ShellState) -> None:
    dispatch_operational("show fabric version", shell_state)
    assert len(shell_state.candidate) == 0
    assert journal.list_txns() == []


def test_shell_completes_version_under_the_fabric_scope(shell_state: ShellState) -> None:
    from pyecsdwan.cli.shell import ShellCompleter

    completer = ShellCompleter(shell_state)
    from prompt_toolkit.document import Document

    completions = [
        c.text
        for c in completer.get_completions(Document("show fabric ver"), None)  # type: ignore[arg-type]
    ]
    assert "version" in completions
    # It is a fabric-wide read — one call per appliance — so it lives under the
    # scope noun that says so, and no longer at the top level (#74).
    top = [
        c.text for c in completer.get_completions(Document("show ver"), None)  # type: ignore[arg-type]
    ]
    assert "version" not in top
