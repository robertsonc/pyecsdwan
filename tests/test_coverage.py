"""`ec-cli show coverage`: every known endpoint x tier (#28).

The load-bearing test here is
:func:`test_every_declared_endpoint_exists_in_the_spec_universe`. ``Resource.
endpoints`` is the *only* link between a resource kind and the API surface, so
a typo or a path that drifted out of the vendored baselines would silently
mis-tier the whole report. Asserting the declarations resolve turns them into
a standing drift check — the previous epic shipped several GitHub issues naming
REST paths (``/route_policy``, ``/nat_policy``, ``/services``, ...) that exist
in neither baseline because they were pyedgeconnect *SDK module* names, and
only manual spec-grepping caught it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - importing registers the built-in plugins
from pyecsdwan import specs
from pyecsdwan.cli import main as cli_main
from pyecsdwan.contract import Tier
from pyecsdwan.registry import Registry, default_registry

#: Endpoints a resource legitimately drives that are absent from both vendored
#: baselines. Empty today, and that is the point: every one of the 119 declared
#: endpoints resolves. Add an entry ONLY with a comment naming the endpoint and
#: why the spec does not carry it (the appliance baseline is not exhaustive) —
#: never to silence a typo.
KNOWN_ABSENT_FROM_SPECS: frozenset[str] = frozenset()

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_caches() -> Any:
    specs.clear_caches()
    yield
    specs.clear_caches()
    # The app callback configures structlog process-wide against `sys.stderr`,
    # which under CliRunner is a capture stream that is closed on exit. Leaving
    # that configuration in place makes the *next* test that logs anything die
    # on a closed file, so hand the process back the library defaults.
    structlog.reset_defaults()


@pytest.fixture
def registry() -> Registry:
    return default_registry


# -- the declarations themselves ---------------------------------------------


def test_every_registered_kind_declares_its_endpoints(registry: Registry) -> None:
    missing = [k for k in registry.kinds() if not registry.get(k).endpoints]
    assert missing == [], f"kinds with no endpoints declared: {missing}"


def test_declared_endpoints_are_well_formed(registry: Registry) -> None:
    """``"<scope> <METHOD> <path>"`` — three fields, a known scope, an
    uppercase method, and a path (absolute or ECOS-relative)."""
    for kind in registry.kinds():
        for declared in registry.get(kind).endpoints:
            parts = declared.split(" ")
            assert len(parts) == 3, f"{kind}: malformed endpoint {declared!r}"
            scope, method, path = parts
            assert scope in specs.SCOPES, f"{kind}: bad scope in {declared!r}"
            assert method == method.upper(), f"{kind}: method must be upper in {declared!r}"
            assert method in {m.upper() for m in specs.HTTP_METHODS}, f"{kind}: {declared!r}"
            assert path, f"{kind}: empty path in {declared!r}"


def test_every_declared_endpoint_exists_in_the_spec_universe(registry: Registry) -> None:
    """The typo-and-drift check: a declaration must name a real operation."""
    absent: list[str] = []
    for kind in registry.kinds():
        for declared in registry.get(kind).endpoints:
            scope, method, path = declared.split(" ", 2)
            if specs.find_endpoint(scope, method, path) is None:
                absent.append(f"{kind}: {declared}")
    unexplained = [a for a in absent if a.split(": ", 1)[1] not in KNOWN_ABSENT_FROM_SPECS]
    assert unexplained == [], (
        "declared endpoints missing from the vendored specs — investigate "
        "before deleting the declaration; if genuinely absent, add it to "
        f"KNOWN_ABSENT_FROM_SPECS with a reason: {unexplained}"
    )


def test_declarations_join_through_normalization_not_string_equality() -> None:
    """Appliance modules write ECOS paths relative; the spec writes them
    absolute. Both must resolve to the same operation, or the join silently
    under-counts every appliance-scope resource."""
    resource = default_registry.get("appliance/bgp")
    assert "appliance GET /bgp/config/system" in resource.endpoints
    assert specs.find_endpoint("appliance", "GET", "bgp/config/system") is not None


# -- tier attribution ---------------------------------------------------------


def test_endpoint_coverage_spans_the_whole_universe(registry: Registry) -> None:
    rows = cli_main._endpoint_coverage(registry)
    assert len(rows) == len(specs.endpoint_index())
    assert len(rows) > 1500


def test_tier_attribution_matches_the_declaring_resources(registry: Registry) -> None:
    rows = {(r.scope, r.method, specs.normalize_path(r.path)): r for r in
            cli_main._endpoint_coverage(registry)}
    curated = rows[("appliance", "POST", "/bgp/config/system")]
    assert curated.tier == int(Tier.CURATED)
    assert curated.kinds == ("appliance/bgp",)
    assert curated.reversibility == "reversible"

    # Nothing declares this one; it stays at the passthrough floor.
    raw = rows[("orchestrator", "GET", "/action")]
    assert raw.tier == int(Tier.RAW)
    assert raw.kinds == ()
    assert raw.reversibility == ""


def test_a_shared_endpoint_names_every_covering_kind(registry: Registry) -> None:
    """appliance/deployment and appliance/dhcp both write the deployment
    object; the report names both rather than silently picking one."""
    rows = {(r.scope, r.method, r.path): r for r in cli_main._endpoint_coverage(registry)}
    shared = rows[("appliance", "POST", "/deployment")]
    assert shared.kinds == ("appliance/deployment", "appliance/dhcp")
    assert shared.tier == int(Tier.CURATED)


def test_rollup_partitions_the_universe(registry: Registry) -> None:
    rows = cli_main._endpoint_coverage(registry)
    counts = cli_main._coverage_rollup(rows)
    assert counts["curated"] + counts["generated"] + counts["raw"] == counts["endpoints"]
    # Every unique declared key is curated today (no Tier.GENERATED plugins yet).
    declared = {
        specs.endpoint_key(*e.split(" ", 2))
        for kind in registry.kinds()
        for e in registry.get(kind).endpoints
    }
    assert counts["curated"] == len(declared)


def test_no_declared_endpoint_falls_outside_the_universe(registry: Registry) -> None:
    """The CLI's own runtime version of the drift check must stay quiet."""
    assert cli_main._undeclared_keys(registry) == []


# -- the command --------------------------------------------------------------


def _run(*args: str) -> str:
    # Wide console: rich truncates cells to the terminal width, and these
    # assertions are about content, not layout.
    result = runner.invoke(cli_main.app, ["show", "coverage", *args], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    return result.output


def test_default_view_still_lists_resource_kinds() -> None:
    out = _run()
    assert "bio-association" in out
    assert "endpoints curated" in out


def test_cli_and_shell_report_the_same_rollup(registry: Registry) -> None:
    """The shell's summary comes from the same helper the CLI prints, so the
    two views cannot drift apart."""
    assert cli_main.coverage_summary_line(registry) in _run()


def test_endpoints_view_lists_the_whole_universe_as_json() -> None:
    payload = json.loads(_run("--endpoints", "--json"))
    assert len(payload["endpoints"]) == len(specs.endpoint_index())
    assert payload["totals"]["endpoints"] == len(specs.endpoint_index())
    assert payload["spec_versions"]["orchestrator"]
    assert payload["undeclared_in_spec"] == []


def test_kind_filter_restricts_to_that_kinds_endpoints() -> None:
    payload = json.loads(_run("--endpoints", "--json", "--kind", "appliance/vrrp"))
    assert [k["kind"] for k in payload["kinds"]] == ["appliance/vrrp"]
    assert {(e["scope"], e["method"], e["path"]) for e in payload["endpoints"]} == {
        ("appliance", "GET", "/vrrp"),
        ("appliance", "POST", "/vrrp"),
    }


def test_tier_filter_selects_the_passthrough_floor() -> None:
    payload = json.loads(_run("--endpoints", "--json", "--tier", "0"))
    assert payload["endpoints"]
    assert {e["tier"] for e in payload["endpoints"]} == {0}
    assert all(e["kinds"] == [] for e in payload["endpoints"])
    # No registered kind sits at tier 0, so the kind list is empty, not wrong.
    assert payload["kinds"] == []


def test_tier_and_scope_filters_compose() -> None:
    payload = json.loads(_run("--endpoints", "--json", "--tier", "2", "--scope", "appliance"))
    assert payload["endpoints"]
    assert {e["scope"] for e in payload["endpoints"]} == {"appliance"}
    assert {e["tier"] for e in payload["endpoints"]} == {2}


def test_unknown_kind_and_scope_are_rejected() -> None:
    for args in (["--kind", "nope"], ["--scope", "nope"]):
        result = runner.invoke(cli_main.app, ["show", "coverage", *args])
        assert result.exit_code == 2
        assert "nope" in result.output


def test_tier_option_rejects_values_outside_0_2() -> None:
    result = runner.invoke(cli_main.app, ["show", "coverage", "--tier", "3"])
    assert result.exit_code != 0


# -- offline safety -----------------------------------------------------------


def test_coverage_is_offline_safe_with_no_vendored_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed wheel that ships no specs must still print the kind table
    and say why the endpoint numbers are missing — never an empty table."""
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path / "absent"))
    specs.clear_caches()
    out = _run()
    assert "bio-association" in out
    assert "no API specs vendored" in out
    assert "endpoints curated" not in out

    out = _run("--endpoints")
    assert "no API specs vendored" in out

    payload = json.loads(_run("--endpoints", "--json"))
    assert payload["totals"]["endpoints"] == 0
    assert payload["endpoints"] == []


def test_summary_line_says_unknown_rather_than_zero_without_specs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: Registry
) -> None:
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path / "absent"))
    specs.clear_caches()
    line = cli_main.coverage_summary_line(registry)
    assert "unavailable" in line
    assert "0 of 0" not in line
