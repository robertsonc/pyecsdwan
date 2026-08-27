"""Shared spec view (epic #6 foundation): normalization, $ref resolution, lookup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import specs


@pytest.fixture(autouse=True)
def _clear_caches() -> Any:
    specs.clear_caches()
    yield
    specs.clear_caches()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/appliance/{nePk}", "/appliance/{}"),
        ("/appliance/:nePk", "/appliance/{}"),
        ("/appliance/{{nePk}}", "/appliance/{}"),
        ("/nat/maps/{mapName}/prio/{prio}", "/nat/maps/{}/prio/{}"),
        ("/gms/grNodes?limit=5", "/gms/grNodes"),
        ("/gms/grNodes/", "/gms/grNodes"),
        ("/", "/"),
        ("bgp/config/system", "/bgp/config/system"),
    ],
)
def test_normalize_path_collapses_every_parameter_dialect(raw: str, expected: str) -> None:
    """The specs, the Postman collections and live URLs spell params three
    different ways; they must all join on the same key."""
    assert specs.normalize_path(raw) == expected


def test_endpoint_key_is_scope_method_and_normalized_path() -> None:
    assert specs.endpoint_key("appliance", "get", "/acls/:name") == "appliance GET /acls/{}"


def test_baselines_load_and_declare_a_version() -> None:
    for scope in specs.SCOPES:
        assert specs.load_spec(scope)["paths"]
        assert specs.spec_version(scope)


def test_endpoint_universe_is_indexed_without_collisions() -> None:
    endpoints = list(specs.iter_endpoints())
    assert len(endpoints) > 1500
    # setdefault() would silently drop a duplicate; assert the index is total.
    assert len(specs.endpoint_index()) == len({e.key for e in endpoints})


def test_iter_endpoints_can_be_scoped() -> None:
    appliance = list(specs.iter_endpoints("appliance"))
    assert appliance
    assert {e.scope for e in appliance} == {"appliance"}


def test_find_endpoint_resolves_refs_into_real_property_names() -> None:
    """The load-bearing case: BGP system config is a $ref in the appliance
    spec, and codegen needs the inlined properties, not the pointer."""
    endpoint = specs.find_endpoint("appliance", "POST", "bgp/config/system")
    assert endpoint is not None
    schema = endpoint.request_schema()
    assert "asn" in schema["properties"]


def test_find_endpoint_tolerates_a_leading_slash_either_way() -> None:
    """ECOS paths are written relative in resource modules and absolute in the
    spec; both must find the same operation."""
    assert specs.find_endpoint("appliance", "GET", "bgp/config/system") == specs.find_endpoint(
        "appliance", "GET", "/bgp/config/system"
    )


def test_endpoint_flags_and_path_params() -> None:
    endpoint = specs.find_endpoint("appliance", "DELETE", "/nat/maps/{mapName}/prio/{prio}")
    assert endpoint is not None
    assert endpoint.is_write
    assert endpoint.path_param_names == ("mapName", "prio")
    assert endpoint.normalized_path == "/nat/maps/{}/prio/{}"


def test_resolve_schema_stops_at_max_depth_instead_of_recursing_forever() -> None:
    """EdgeConnect component schemas are mutually recursive; a naive resolver
    never returns."""
    spec: specs.JsonDict = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                }
            }
        }
    }
    resolved = specs.resolve_schema(spec, {"$ref": "#/components/schemas/Node"})
    depth = 0
    node: Any = resolved
    while isinstance(node, dict) and "properties" in node:
        node = node["properties"]["child"]
        depth += 1
    assert depth <= specs.MAX_REF_DEPTH
    assert node == {"$ref": "#/components/schemas/Node"}


def test_resolve_ref_returns_none_for_external_or_missing_targets() -> None:
    spec: specs.JsonDict = {"components": {"schemas": {}}}
    assert specs.resolve_ref(spec, "https://example.com/x.json#/Foo") is None
    assert specs.resolve_ref(spec, "#/components/schemas/Absent") is None


def test_sibling_keys_override_a_resolved_ref() -> None:
    """``{"$ref": X, "description": "..."}`` keeps the local description."""
    spec: specs.JsonDict = {
        "components": {"schemas": {"A": {"type": "object", "description": "from ref"}}}
    }
    out = specs.resolve_schema(spec, {"$ref": "#/components/schemas/A", "description": "local"})
    assert out == {"type": "object", "description": "local"}


def test_missing_specs_directory_degrades_to_an_empty_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An installed wheel that ships no specs must still run `show coverage`."""
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path / "nope"))
    specs.clear_caches()
    assert specs.specs_dir() is None
    assert specs.load_spec("orchestrator") == {}
    assert list(specs.iter_endpoints()) == []
    assert specs.payload_examples() == {}


def test_payload_examples_are_read_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "orchestrator POST /apiKey"
    (tmp_path / "payload-examples-9.6.json").write_text(
        json.dumps({"endpoints": {key: {"request": {"name": "string"}}}}), encoding="utf-8"
    )
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path))
    specs.clear_caches()
    example = specs.payload_example("orchestrator", "POST", "/apiKey")
    assert example == {"request": {"name": "string"}}
    assert specs.payload_example("orchestrator", "POST", "/absent") is None


def test_payload_examples_ignore_a_malformed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "payload-examples-9.6.json").write_text('{"endpoints": []}', encoding="utf-8")
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path))
    specs.clear_caches()
    assert specs.payload_examples() == {}
