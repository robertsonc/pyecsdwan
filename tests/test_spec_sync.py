"""Unit tests for tools/spec_sync.py: offline OpenAPI fetch + diff + update.

Everything runs against local fixtures or the vendored _specs/ baseline —
never a live Orchestrator. The committed fixture pair under
tests/fixtures/spec_sync/ carries exactly one added endpoint, proving the
detection required by issue #25 (and feeding epic #6 DoD).
"""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import respx

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_SYNC = REPO_ROOT / "tools" / "spec_sync.py"
REAL_SPECS_DIR = REPO_ROOT / "src" / "pyecsdwan" / "_specs"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "spec_sync"
FIXTURE_SPECS_DIR = FIXTURES / "specs"
FIXTURE_BASELINE = FIXTURE_SPECS_DIR / "orchestrator-openapi-0.9.0.json"
FIXTURE_FETCHED = FIXTURES / "fetched-added-endpoint.json"
ADDED_ENDPOINT = "GET /gms/specSync/probe"


def _load_module():
    """Import tools/spec_sync.py without making tools/ a package."""
    spec = importlib.util.spec_from_file_location("spec_sync", SPEC_SYNC)
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution needs the module registered before execution.
    sys.modules["spec_sync"] = module
    spec.loader.exec_module(module)
    return module


spec_sync = _load_module()


def run_cli(*argv, env_extra=None):
    """Run the tool as a real subprocess, stripped of ambient ECSDWAN_* config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ECSDWAN_")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SPEC_SYNC), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# CLI --diff against the committed fixture pair (the issue-#25 detection proof)


def test_cli_diff_detects_the_one_added_endpoint():
    result = run_cli(
        "--diff",
        "--spec", "orchestrator",
        "--source", str(FIXTURE_FETCHED),
        "--specs-dir", str(FIXTURE_SPECS_DIR),
        "--json",
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["drift"] is True
    target = report["targets"]["orchestrator"]
    assert target["endpoints"]["added"] == [ADDED_ENDPOINT]
    assert target["endpoints"]["removed"] == []
    assert target["endpoints"]["changed"] == []
    assert target["schemas"] == {"added": [], "removed": [], "changed": []}
    # The fetched fixture carries a lab hostname in servers/basePath; both
    # sides are sanitized before comparing, so no metadata noise appears.
    assert target["metadata"] == []


def test_cli_diff_human_output_and_exit_codes():
    drift = run_cli(
        "--diff", "--spec", "orchestrator",
        "--source", str(FIXTURE_FETCHED), "--specs-dir", str(FIXTURE_SPECS_DIR),
    )
    assert drift.returncode == 1
    assert f"+ {ADDED_ENDPOINT}" in drift.stdout
    assert "drift detected" in drift.stdout

    clean = run_cli(
        "--diff", "--spec", "orchestrator",
        "--source", str(FIXTURE_BASELINE), "--specs-dir", str(FIXTURE_SPECS_DIR),
    )
    assert clean.returncode == 0
    assert "baselines in sync" in clean.stdout


def test_cli_source_from_environment_variable():
    result = run_cli(
        "--diff", "--spec", "orchestrator", "--specs-dir", str(FIXTURE_SPECS_DIR), "--json",
        env_extra={"ECSDWAN_SPEC_SOURCE_ORCHESTRATOR": str(FIXTURE_FETCHED)},
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["targets"]["orchestrator"]["endpoints"]["added"] == [ADDED_ENDPOINT]
    assert report["skipped"] == ["appliance"] or report["skipped"] == []


def test_cli_without_any_source_exits_2_with_guidance():
    result = run_cli("--diff")
    assert result.returncode == 2
    assert "ECSDWAN_SPEC_SOURCE_ORCHESTRATOR" in result.stderr
    assert "nothing to do" in result.stderr


def test_cli_source_with_spec_both_is_rejected():
    result = run_cli("--diff", "--source", str(FIXTURE_FETCHED))
    assert result.returncode == 2
    assert "single target" in result.stderr


# ---------------------------------------------------------------------------
# Detection against the real vendored baseline (no fixture duplication)


def test_injected_endpoint_detected_against_real_orchestrator_baseline():
    baseline = json.loads((REAL_SPECS_DIR / "orchestrator-openapi-7.2.0.json").read_text())
    fetched = copy.deepcopy(baseline)
    fetched["paths"]["/gms/specSync/probe"] = {
        "get": {"operationId": "probe", "responses": {"200": {"description": "OK"}}}
    }
    diff = spec_sync.diff_specs(
        spec_sync.sanitize_spec(baseline, "orchestrator")[0],
        spec_sync.sanitize_spec(fetched, "orchestrator")[0],
    )
    assert diff.added == [ADDED_ENDPOINT]
    assert diff.removed == []
    assert diff.changed == []
    assert diff.drift is True


@pytest.mark.parametrize("target", ["orchestrator", "appliance"])
def test_real_baselines_self_diff_clean(target):
    path = next(REAL_SPECS_DIR.glob(f"{target}-openapi-*.json"))
    one = json.loads(path.read_text())
    two = json.loads(path.read_text())
    diff = spec_sync.diff_specs(
        spec_sync.sanitize_spec(one, target)[0], spec_sync.sanitize_spec(two, target)[0]
    )
    assert diff.drift is False


# ---------------------------------------------------------------------------
# Diff semantics on small in-memory specs


def _mini_spec(**overrides):
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "7.2.0"},
        "servers": [{"url": "https://orchestrator.example.com/gms/rest"}],
        "paths": {
            "/thing": {
                "parameters": [{"name": "cached", "in": "query"}],
                "get": {"operationId": "getThing"},
                "post": {"operationId": "postThing"},
            }
        },
        "components": {"schemas": {"Thing": {"type": "object"}}},
    }
    spec.update(overrides)
    return spec


def _diff(baseline, fetched):
    return spec_sync.diff_specs(
        spec_sync.sanitize_spec(baseline, "orchestrator")[0],
        spec_sync.sanitize_spec(fetched, "orchestrator")[0],
    )


def test_removed_and_changed_endpoints_detected():
    fetched = copy.deepcopy(_mini_spec())
    del fetched["paths"]["/thing"]["post"]
    fetched["paths"]["/thing"]["get"]["deprecated"] = True
    diff = _diff(_mini_spec(), fetched)
    assert diff.removed == ["POST /thing"]
    assert diff.changed == ["GET /thing"]
    assert diff.added == []


def test_shared_path_parameter_change_marks_all_methods_changed():
    fetched = copy.deepcopy(_mini_spec())
    fetched["paths"]["/thing"]["parameters"].append({"name": "extra", "in": "query"})
    diff = _diff(_mini_spec(), fetched)
    assert diff.changed == ["GET /thing", "POST /thing"]
    assert diff.drift is True


def test_schema_and_version_drift_detected():
    fetched = copy.deepcopy(_mini_spec())
    fetched["info"]["version"] = "7.3.0"
    fetched["components"]["schemas"]["Thing"]["type"] = "array"
    fetched["components"]["schemas"]["NewThing"] = {"type": "object"}
    diff = _diff(_mini_spec(), fetched)
    assert diff.schemas_changed == ["Thing"]
    assert diff.schemas_added == ["NewThing"]
    assert ("info.version", "7.2.0", "7.3.0") in diff.metadata
    assert diff.added == diff.removed == diff.changed == []
    assert diff.drift is True


# ---------------------------------------------------------------------------
# Sanitization


def test_sanitize_replaces_hosts_and_strips_server_extras():
    spec = {
        "servers": [
            {"url": "http://orch-lab.internal.invalid:9443/gms/rest", "description": "lab box"},
            {"url": "https://other.internal.invalid/gms/rest", "variables": {"x": {}}},
        ],
        "basePath": "https://orch-lab.internal.invalid:/gms/rest",
        "host": "orch-lab.internal.invalid",
        "paths": {},
    }
    sanitized, touched = spec_sync.sanitize_spec(spec, "orchestrator")
    # Both entries collapse to the same placeholder and are deduplicated.
    assert sanitized["servers"] == [{"url": "https://orchestrator.example.com/gms/rest"}]
    assert sanitized["basePath"] == "https://orchestrator.example.com/gms/rest"
    assert sanitized["host"] == "orchestrator.example.com"
    assert "basePath" in touched and "host" in touched
    assert not json.dumps(sanitized).count("invalid")
    # The input spec is not mutated.
    assert spec["host"] == "orch-lab.internal.invalid"


def test_sanitize_leaves_relative_base_path_and_clean_specs_alone():
    spec = {
        "servers": [{"url": "https://appliance.example.com/rest/json"}],
        "basePath": "/rest/json",
        "paths": {},
    }
    sanitized, touched = spec_sync.sanitize_spec(spec, "appliance")
    assert touched == []
    assert sanitized["servers"] == spec["servers"]
    assert sanitized["basePath"] == "/rest/json"


# ---------------------------------------------------------------------------
# CLI --update


def test_cli_update_writes_sanitized_compact_baseline(tmp_path):
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    decoy = specs_dir / "orchestrator-openapi-0.0.1.json"
    decoy.write_text('{"paths":{}}')

    result = run_cli(
        "--update", "--spec", "orchestrator",
        "--source", str(FIXTURE_FETCHED), "--specs-dir", str(specs_dir), "--json",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)

    written = specs_dir / "orchestrator-openapi-0.9.0.json"
    assert report["targets"]["orchestrator"]["written"] == str(written)
    # The superseded differently-versioned baseline is gone: one per target.
    assert not decoy.exists()
    assert sorted(p.name for p in specs_dir.iterdir()) == [written.name]

    raw = written.read_bytes()
    spec = json.loads(raw)
    # Byte-identical to the existing baseline convention: compact single-line
    # ASCII JSON, no trailing newline (specs/*.json round-trip the same way).
    assert raw == json.dumps(spec, separators=(",", ":")).encode()
    assert b"\n" not in raw
    # The lab hostname is fully sanitized away; paths survive untouched.
    assert b"invalid" not in raw
    assert spec["servers"] == [{"url": "https://orchestrator.example.com/gms/rest"}]
    assert spec["basePath"] == "https://orchestrator.example.com/gms/rest"
    assert "/gms/specSync/probe" in spec["paths"]


def test_cli_update_then_diff_is_in_sync(tmp_path):
    specs_dir = tmp_path / "specs"
    update = run_cli(
        "--update", "--spec", "orchestrator",
        "--source", str(FIXTURE_FETCHED), "--specs-dir", str(specs_dir),
    )
    assert update.returncode == 0, update.stderr
    diff = run_cli(
        "--diff", "--spec", "orchestrator",
        "--source", str(FIXTURE_FETCHED), "--specs-dir", str(specs_dir), "--json",
    )
    assert diff.returncode == 0, diff.stdout
    assert json.loads(diff.stdout)["drift"] is False


# ---------------------------------------------------------------------------
# URL fetch (respx-mocked; still no live Orchestrator)


@respx.mock
def test_load_spec_from_url_sends_api_key_header(monkeypatch):
    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    route = respx.get("https://orch.example.com/gms/rest/apiDocs").mock(
        return_value=httpx.Response(200, json={"openapi": "3.0.0", "paths": {}})
    )
    spec = spec_sync.load_spec(
        "https://orch.example.com/gms/rest/apiDocs", timeout=5.0, insecure=False
    )
    assert spec["openapi"] == "3.0.0"
    assert route.calls.last.request.headers["X-Auth-Token"] == "test-key"


@respx.mock
def test_load_spec_http_error_is_wrapped():
    respx.get("https://orch.example.com/apiDocs").mock(return_value=httpx.Response(401))
    with pytest.raises(spec_sync.SpecSyncError, match="fetch failed"):
        spec_sync.load_spec("https://orch.example.com/apiDocs", timeout=5.0, insecure=False)


def test_load_spec_rejects_non_openapi_documents(tmp_path):
    bogus = tmp_path / "bogus.json"
    bogus.write_text('{"hello": "world"}')
    with pytest.raises(spec_sync.SpecSyncError, match="no 'paths'"):
        spec_sync.load_spec(str(bogus), timeout=5.0, insecure=False)
    with pytest.raises(spec_sync.SpecSyncError, match="not found"):
        spec_sync.load_spec(str(tmp_path / "missing.json"), timeout=5.0, insecure=False)
