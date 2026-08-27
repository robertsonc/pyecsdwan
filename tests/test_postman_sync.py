"""Unit tests for tools/postman_sync.py: offline distillation of the vendor's
Postman collections into specs/payload-examples-*.json (issue #51).

Nothing here touches the network. The collection fixtures are hand-built in
tmp_path -- the real documents are ~5 MB each and are deliberately not
vendored -- and are exercised alongside cheap assertions against the committed
artifact.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pyecsdwan import specs

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTMAN_SYNC = REPO_ROOT / "tools" / "postman_sync.py"
REAL_SPECS_DIR = REPO_ROOT / "specs"


def _load_module():
    """Import tools/postman_sync.py without making tools/ a package."""
    spec = importlib.util.spec_from_file_location("postman_sync", POSTMAN_SYNC)
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution needs the module registered before execution.
    sys.modules["postman_sync"] = module
    spec.loader.exec_module(module)
    return module


postman_sync = _load_module()


# ---------------------------------------------------------------------------
# Fixture builders — the shape of Postman's internal v1 model, minimally


def make_request(method, url, *, name="op", folder=None, raw=None, responses=None):
    return {
        "id": f"{method}-{url}",
        "name": name,
        "method": method,
        "url": url,
        "folder": folder,
        "rawModeData": raw,
        "responses": responses or [],
    }


def make_response(text, *, code=200, name="No response was specified"):
    return {"name": name, "text": text, "responseCode": {"code": code, "name": "OK"}}


def make_collection(requests, folders=(), *, name="EdgeConnect SD-WAN test"):
    """Wrap in the ``{"data": ...}`` envelope the web API actually returns."""
    return {"model_id": 1, "meta": {"populate": True},
            "data": {"name": name, "folders": list(folders), "requests": list(requests)}}


def write_collection(tmp_path, filename, requests, folders=()):
    path = tmp_path / filename
    path.write_text(json.dumps(make_collection(requests, folders)), encoding="utf-8")
    return path


def run_cli(*argv, env_extra=None):
    """Run the tool as a real subprocess, stripped of ambient ECSDWAN_* config."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ECSDWAN_")}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(POSTMAN_SYNC), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


# ---------------------------------------------------------------------------
# Path-parameter dialects and scope


def test_both_path_parameter_dialects_reach_the_spec_key():
    """Orchestrator writes ``{{nePk}}``, appliance writes ``:nePk``, spec ``{nePk}``."""
    data = make_collection([
        make_request("GET", "{{orchestratorBaseUrl}}/appliance/{{nePk}}"),
        make_request("GET", "{{applianceBaseUrl}}/appliance/:nePk"),
    ])["data"]
    entries = postman_sync.distil_release(data)
    assert set(entries) == {
        specs.endpoint_key("orchestrator", "GET", "/appliance/{nePk}"),
        specs.endpoint_key("appliance", "GET", "/appliance/{nePk}"),
    }
    # The raw path is kept verbatim, because the key normalizes the name away.
    orch = entries[specs.endpoint_key("orchestrator", "GET", "/appliance/{nePk}")]
    appl = entries[specs.endpoint_key("appliance", "GET", "/appliance/{nePk}")]
    assert orch["path"] == "/appliance/{{nePk}}"
    assert appl["path"] == "/appliance/:nePk"


def test_normalization_is_borrowed_from_the_package_not_reimplemented():
    assert postman_sync._endpoint_key_fn() is specs.endpoint_key


def test_scope_comes_from_the_url_variable():
    data = make_collection([
        make_request("GET", "{{orchestratorBaseUrl}}/gms/grNodes"),
        make_request("GET", "{{applianceBaseUrl}}/gms/grNodes"),
    ])["data"]
    entries = postman_sync.distil_release(data)
    assert "orchestrator GET /gms/grNodes" in entries
    assert "appliance GET /gms/grNodes" in entries


def test_a_request_with_no_base_url_variable_is_dropped_not_guessed():
    data = make_collection([
        make_request("GET", "{{somethingElse}}/gms/grNodes"),
        make_request("GET", "https://orch.example.com/gms/grNodes"),
    ])["data"]
    assert postman_sync.distil_release(data) == {}
    assert postman_sync.split_url("{{nope}}/x") is None


# ---------------------------------------------------------------------------
# Body handling


def test_non_json_request_body_is_preserved_not_dropped():
    """A handful of vendor bodies are the bare word ``string``; keep them."""
    data = make_collection([
        make_request("POST", "{{orchestratorBaseUrl}}/zscaler/country", raw="string"),
        make_request("POST", "{{orchestratorBaseUrl}}/apiKey", raw='{"name": "string"}'),
    ])["data"]
    entries = postman_sync.distil_release(data)
    scalar = entries["orchestrator POST /zscaler/country"]
    assert scalar["request_raw"] == "string"
    assert "request" not in scalar
    typed = entries["orchestrator POST /apiKey"]
    assert typed["request"] == {"name": "string"}
    assert "request_raw" not in typed


def test_non_json_response_body_is_preserved_not_dropped():
    data = make_collection([
        make_request(
            "POST", "{{applianceBaseUrl}}/profiles/deleteMultiple",
            responses=[make_response("Successfully deleted global end entity profiles")],
        )
    ])["data"]
    entry = postman_sync.distil_release(data)["appliance POST /profiles/deleteMultiple"]
    assert entry["response_raw"].startswith("Successfully deleted")
    assert "response" not in entry


def test_empty_bodies_produce_no_keys_at_all():
    data = make_collection([
        make_request("GET", "{{orchestratorBaseUrl}}/gms/grNodes", raw="",
                     responses=[make_response(""), make_response(None)])
    ])["data"]
    entry = postman_sync.distil_release(data)["orchestrator GET /gms/grNodes"]
    assert not {"request", "request_raw", "response", "response_raw"} & set(entry)


def test_response_choice_prefers_a_2xx_body_and_records_a_non_2xx_status():
    """A 400 example must never be presented as the success shape unlabelled."""
    good = postman_sync.pick_response([
        make_response('{"error": "bad"}', code=400),
        make_response('{"ok": true}', code=200),
    ])
    assert good == ({"ok": True}, None, 200)

    data = make_collection([
        make_request("POST", "{{orchestratorBaseUrl}}/only/bad",
                     responses=[make_response('{"error": "bad"}', code=400)])
    ])["data"]
    entry = postman_sync.distil_release(data)["orchestrator POST /only/bad"]
    assert entry["response"] == {"error": "bad"}
    assert entry["response_status"] == 400


# ---------------------------------------------------------------------------
# Folders


def test_folder_path_is_root_first_and_cycle_safe():
    folders = [
        {"id": "root", "name": "Orchestrator Level"},
        {"id": "leaf", "name": "alarm", "folder": "root"},
        {"id": "loop", "name": "bad", "folder": "loop"},
    ]
    paths = postman_sync.folder_paths(folders)
    assert paths["leaf"] == ["Orchestrator Level", "alarm"]
    assert paths["loop"] == ["bad"]

    data = make_collection(
        [make_request("POST", "{{orchestratorBaseUrl}}/alarm/alarmConfig", folder="leaf")],
        folders,
    )["data"]
    entry = postman_sync.distil_release(data)["orchestrator POST /alarm/alarmConfig"]
    assert entry["folder"] == ["Orchestrator Level", "alarm"]


# ---------------------------------------------------------------------------
# Cross-release folding


def _two_releases():
    old = make_collection([
        make_request("GET", "{{orchestratorBaseUrl}}/legacy", name="old name",
                     responses=[make_response('{"v": "9.3"}')]),
        make_request("GET", "{{orchestratorBaseUrl}}/kept", name="9.3 name"),
    ])["data"]
    new = make_collection([
        make_request("GET", "{{orchestratorBaseUrl}}/kept", name="9.6 name"),
        make_request("GET", "{{orchestratorBaseUrl}}/fresh"),
    ])["data"]
    return [("9.3", old), ("9.6", new)]


def test_since_is_the_earliest_release_containing_the_endpoint():
    artifact = postman_sync.distil(_two_releases(), retrieved="2026-01-01")
    endpoints = artifact["endpoints"]
    assert endpoints["orchestrator GET /kept"]["since"] == "9.3"
    assert endpoints["orchestrator GET /legacy"]["since"] == "9.3"
    assert endpoints["orchestrator GET /fresh"]["since"] == "9.6"
    assert artifact["meta"]["releases"] == ["9.3", "9.6"]
    assert artifact["meta"]["retrieved"] == "2026-01-01"
    assert "placeholder" in artifact["meta"]["caveat"]


def test_the_newest_release_supplies_the_example():
    endpoints = postman_sync.distil(_two_releases())["endpoints"]
    assert endpoints["orchestrator GET /kept"]["name"] == "9.6 name"


def test_an_endpoint_dropped_before_the_newest_release_is_kept_and_marked():
    endpoints = postman_sync.distil(_two_releases())["endpoints"]
    legacy = endpoints["orchestrator GET /legacy"]
    assert legacy["removed_after"] == "9.3"
    assert legacy["response"] == {"v": "9.3"}
    assert "removed_after" not in endpoints["orchestrator GET /kept"]
    assert "removed_after" not in endpoints["orchestrator GET /fresh"]


# ---------------------------------------------------------------------------
# Loading


def test_a_bare_data_object_loads_as_well_as_the_web_api_envelope(tmp_path):
    enveloped = write_collection(tmp_path, "env.json", [])
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"name": "x", "folders": [], "requests": []}), encoding="utf-8")
    for source in (enveloped, bare):
        data = postman_sync.load_collection(str(source), timeout=1, insecure=False)
        assert data["requests"] == []


def test_an_unpopulated_stub_is_rejected_with_the_populate_hint(tmp_path):
    stub = tmp_path / "stub.json"
    stub.write_text(json.dumps({"data": {"isLargeCollection": True}}), encoding="utf-8")
    with pytest.raises(postman_sync.PostmanSyncError, match="populate=true"):
        postman_sync.load_collection(str(stub), timeout=1, insecure=False)


# ---------------------------------------------------------------------------
# The committed artifact


def test_committed_artifact_is_read_by_the_package_api():
    specs.clear_caches()
    examples = specs.payload_examples()
    assert len(examples) > 1500
    example = specs.payload_example("orchestrator", "POST", "/alarm/alarmConfig")
    assert example is not None
    assert example["folder"] == ["Orchestrator Level", "alarm"]
    assert example["since"] in ("9.3", "9.4", "9.5", "9.6")
    # The appliance ``:nePk`` dialect joins onto the spec's ``{nePk}``.
    assert specs.payload_example("appliance", "GET", "/bgp/config/system") is not None


def test_committed_artifact_keys_all_round_trip_through_endpoint_key():
    artifact = json.loads((REAL_SPECS_DIR / "payload-examples-9.6.json").read_text())
    assert artifact["meta"]["releases"] == ["9.3", "9.4", "9.5", "9.6"]
    for key in artifact["endpoints"]:
        scope, method, path = key.split(" ", 2)
        assert specs.endpoint_key(scope, method, path) == key


def test_committed_artifact_joins_onto_the_spec_baselines():
    """The point of the artifact: entries land on real spec operations."""
    specs.clear_caches()
    universe = set(specs.endpoint_index())
    examples = set(specs.payload_examples())
    assert len(examples & universe) > 1500
    # Payload shape, not endpoint breadth: the collections add ~nothing new.
    assert len(examples - universe) < 25


def test_committed_artifact_is_stored_compact_and_sorted():
    raw = (REAL_SPECS_DIR / "payload-examples-9.6.json").read_text()
    assert "\n" not in raw
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))


def test_raw_collections_are_not_vendored():
    """5 MB per release; only the distilled artifact belongs in the tree."""
    assert sorted(p.name for p in REAL_SPECS_DIR.glob("*.json")) == [
        "appliance-openapi-7.2.0.json",
        "orchestrator-openapi-7.2.0.json",
        "payload-examples-9.6.json",
    ]


# ---------------------------------------------------------------------------
# CLI


def _cli_specs_dir(tmp_path, requests):
    """A tmp specs/ holding an artifact distilled from *requests* as 9.6."""
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    artifact = postman_sync.distil(
        [("9.6", make_collection(requests)["data"])], retrieved="2026-01-01"
    )
    postman_sync.write_artifact(artifact, specs_dir)
    return specs_dir


def test_cli_diff_exits_0_when_the_distillation_matches(tmp_path):
    requests = [make_request("GET", "{{orchestratorBaseUrl}}/gms/grNodes")]
    specs_dir = _cli_specs_dir(tmp_path, requests)
    source = write_collection(tmp_path, "ec-9.6.json", requests)
    result = run_cli(
        "--diff", "--release", "9.6", "--source", str(source), "--specs-dir", str(specs_dir),
    )
    assert result.returncode == 0, result.stderr
    assert "payload examples in sync" in result.stdout


def test_cli_diff_exits_1_and_names_the_drift(tmp_path):
    specs_dir = _cli_specs_dir(tmp_path, [make_request("GET", "{{orchestratorBaseUrl}}/kept")])
    source = write_collection(tmp_path, "ec-9.6.json", [
        make_request("GET", "{{orchestratorBaseUrl}}/kept", raw='{"changed": true}'),
        make_request("GET", "{{orchestratorBaseUrl}}/added"),
    ])
    result = run_cli(
        "--diff", "--release", "9.6", "--source", str(source),
        "--specs-dir", str(specs_dir), "--json",
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["drift"] is True
    assert report["endpoints"]["added"] == ["orchestrator GET /added"]
    assert report["endpoints"]["changed"] == ["orchestrator GET /kept"]
    assert report["endpoints"]["removed"] == []
    # Both sides cover release 9.6 only, so no content metadata moved.
    assert report["metadata"] == []


def test_cli_diff_ignores_the_retrieved_stamp(tmp_path):
    """Provenance is not content: an unchanged collection must stay in sync."""
    requests = [make_request("GET", "{{orchestratorBaseUrl}}/gms/grNodes")]
    specs_dir = _cli_specs_dir(tmp_path, requests)
    source = write_collection(tmp_path, "ec-9.6.json", requests)
    result = run_cli(
        "--diff", "--release", "9.6", "--source", str(source),
        "--specs-dir", str(specs_dir), "--retrieved", "2099-12-31",
    )
    assert result.returncode == 0, result.stderr


def test_cli_update_writes_a_compact_sorted_artifact(tmp_path):
    specs_dir = tmp_path / "specs"
    source = write_collection(tmp_path, "ec-9.6.json", [
        make_request("POST", "{{applianceBaseUrl}}/acls/:aclName", raw='{"a": 1}')
    ])
    result = run_cli(
        "--update", "--release", "9.6", "--source", str(source), "--specs-dir", str(specs_dir),
    )
    assert result.returncode == 0, result.stderr
    written = specs_dir / "payload-examples-9.6.json"
    assert written.is_file()
    raw = written.read_text()
    assert "\n" not in raw
    artifact = json.loads(raw)
    assert artifact["endpoints"]["appliance POST /acls/{}"]["request"] == {"a": 1}


def test_cli_source_from_environment_variable(tmp_path):
    requests = [make_request("GET", "{{orchestratorBaseUrl}}/gms/grNodes")]
    specs_dir = _cli_specs_dir(tmp_path, requests)
    source = write_collection(tmp_path, "ec-9.6.json", requests)
    result = run_cli(
        "--diff", "--release", "9.6", "--specs-dir", str(specs_dir),
        env_extra={"ECSDWAN_POSTMAN_SOURCE_9_6": str(source)},
    )
    assert result.returncode == 0, result.stderr


def test_cli_no_fetch_without_any_source_exits_2_with_guidance():
    result = run_cli("--diff", "--no-fetch")
    assert result.returncode == 2
    assert "ECSDWAN_POSTMAN_SOURCE_9_3" in result.stderr
    assert "nothing to do" in result.stderr


def test_cli_source_with_all_releases_is_rejected(tmp_path):
    source = write_collection(tmp_path, "ec.json", [])
    result = run_cli("--diff", "--source", str(source))
    assert result.returncode == 2
    assert "single release" in result.stderr


def test_cli_diff_without_a_committed_artifact_exits_2(tmp_path):
    empty = tmp_path / "specs"
    empty.mkdir()
    source = write_collection(tmp_path, "ec-9.6.json", [])
    result = run_cli(
        "--diff", "--release", "9.6", "--source", str(source), "--specs-dir", str(empty),
    )
    assert result.returncode == 2
    assert "--update first" in result.stderr


def test_default_sources_are_the_public_populate_urls():
    for release in postman_sync.RELEASES:
        url = postman_sync.PUBLIC_SOURCE[release]
        assert url.startswith("https://www.postman.com/_api/collection/")
        assert url.endswith("?populate=true")
    assert postman_sync.resolve_source("9.6", None, no_fetch=True) is None
    assert postman_sync.resolve_source("9.6", "x", no_fetch=True) == "x"
