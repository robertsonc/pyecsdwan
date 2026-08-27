"""Unit tests for tools/gen_models.py: pydantic + binding codegen from a spec op.

Two layers, deliberately:

* **Rule tests** run against tiny hand-built specs pointed at by
  ``$ECSDWAN_SPECS_DIR``, so each rule (map detection, aliasing, path-param
  substitution, ``extra="allow"``) fails in isolation with a readable message.
* **Corpus tests** run against the real vendored baselines and prove the
  issue's acceptance criteria end to end: the generated models validate the
  example payloads the spec itself ships, the bindings route orchestrator
  paths through ``OrchClient.request`` and appliance paths through
  ``appliance_request``, and the committed samples are ruff- and mypy-clean.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pyecsdwan import specs
from pyecsdwan.client import OrchClient

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN_MODELS = REPO_ROOT / "tools" / "gen_models.py"
GENERATED_DIR = REPO_ROOT / "src" / "pyecsdwan" / "generated"


def _load_module() -> Any:
    """Import tools/gen_models.py without making tools/ a package."""
    spec = importlib.util.spec_from_file_location("gen_models", GEN_MODELS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass resolution needs the module registered before execution.
    sys.modules["gen_models"] = module
    spec.loader.exec_module(module)
    return module


gen_models = _load_module()


@pytest.fixture(autouse=True)
def _clear_caches() -> Any:
    gen_models.clear_caches()
    yield
    gen_models.clear_caches()


# ---------------------------------------------------------------------------
# Hand-built spec fixtures


def write_spec(directory: Path, scope: str, paths: dict[str, Any]) -> None:
    """Drop a minimal OpenAPI 3 document where ``specs.baseline_path`` looks."""
    document = {
        "openapi": "3.0.0",
        "info": {"title": f"{scope} test", "version": "0.0.1"},
        "paths": paths,
    }
    (directory / f"{scope}-openapi-0.0.1.json").write_text(json.dumps(document), encoding="utf-8")


def json_body(schema: Any) -> dict[str, Any]:
    return {"requestBody": {"content": {"application/json": {"schema": schema}}}}


def json_response(schema: Any) -> dict[str, Any]:
    return {"responses": {"200": {"content": {"application/json": {"schema": schema}}}}}


@pytest.fixture
def fixture_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path))
    gen_models.clear_caches()
    return tmp_path


def generate(scope: str, method: str, path: str) -> Any:
    return gen_models.generate_for(scope, method, path)


def load_generated(result: Any, tmp_path: Path) -> Any:
    """Write the emitted model module to disk and import it under a throwaway name."""
    module_path = tmp_path / f"{result.slug}_models.py"
    module_path.write_text(result.models_source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_path.stem] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Naming


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ("showAppliances", "show_appliances"),
        ("nePk", "ne_pk"),
        ("rtr_id", "rtr_id"),
        ("1k-blocks", "field_1k_blocks"),
        ("NxImage.buildDate", "nx_image_build_date"),
        ("TCP-Stats", "tcp_stats"),
        ("TACACS+", "tacacs"),
        ("/", "field"),
        # Trap 3: reserved surface. The wire name is never mangled; only the
        # Python spelling moves, and the alias carries the original back.
        ("self", "self_"),
        ("import", "import_"),
        ("from", "from_"),
        ("match", "match_"),
        ("copy", "copy_"),
        ("json", "json_"),
        ("schema", "schema_"),
        ("dict", "dict_"),
        ("validate", "validate_"),
        ("model_dump", "model_dump_"),
        # Not reserved: pydantic 2.13 only protects its own attribute names.
        ("model_no", "model_no"),
        ("fields", "fields"),
    ],
)
def test_python_field_name_sanitizes_without_touching_the_wire_name(
    wire: str, expected: str
) -> None:
    assert gen_models.python_field_name(wire) == expected


def test_python_class_name_never_emits_something_unparseable() -> None:
    """``camel_case`` alone produced ``class 10011`` and ``class 1Ne``."""
    for hint in ("10011", "1.NE", "/", "", "class", "10.0.1.1"):
        name = gen_models.python_class_name(hint)
        assert name.isidentifier(), f"{hint!r} -> {name!r}"
        compile(f"class {name}: pass", "<test>", "exec")


def test_slug_encodes_path_parameters_so_two_paths_cannot_collapse(fixture_specs: Path) -> None:
    write_spec(
        fixture_specs,
        "appliance",
        {"/acls/{id}": {"get": {"responses": {}}}, "/acls/id": {"get": {"responses": {}}}},
    )
    with_param = generate("appliance", "GET", "/acls/{id}").slug
    literal = generate("appliance", "GET", "/acls/id").slug
    assert with_param == "appliance_get_acls_by_id"
    assert literal == "appliance_get_acls_id"
    assert with_param != literal


def test_colliding_slugs_are_disambiguated_deterministically() -> None:
    """``/gms/statsCollection`` and ``/gms/stats/collection`` snake to one name."""
    first = specs.find_endpoint("orchestrator", "GET", "/gms/statsCollection")
    second = specs.find_endpoint("orchestrator", "GET", "/gms/stats/collection")
    assert first is not None and second is not None
    assert gen_models.slug_for(first) == gen_models.slug_for(second)
    assert gen_models.unique_slug_for(first) != gen_models.unique_slug_for(second)
    # Deterministic: the same endpoint always yields the same module name.
    assert gen_models.unique_slug_for(first) == gen_models.unique_slug_for(first)


# ---------------------------------------------------------------------------
# Trap 2: map-shaped `properties` blocks


@pytest.mark.parametrize(
    "name",
    [
        "1",
        "0",
        "65536",
        "<nePk>",
        "<segment_id_1>",
        "<a number between 1-65535>",
        "VrfId1>",
        "<pppoeName>>",
        "1.NE",
        "10.0.1.1",
    ],
)
def test_map_keys_are_recognized(name: str) -> None:
    assert gen_models.is_map_key(name)


@pytest.mark.parametrize(
    "name", ["asn", "enable", "rtr_id", "vrfId1", "self", "model_no", "lan0", "x.x.x.x"]
)
def test_real_field_names_are_not_mistaken_for_map_keys(name: str) -> None:
    assert not gen_models.is_map_key(name)


def test_classify_properties_needs_every_key_to_be_a_map_key() -> None:
    assert gen_models.classify_properties({"1": {}, "2": {}}) == "map"
    assert gen_models.classify_properties({"<nePk>": {}}) == "map"
    # 32 blocks in the baselines mix the two; the one real field must survive.
    assert gen_models.classify_properties({"<nePk>": {}, "header": {}}) == "record"
    assert gen_models.classify_properties({}) == "record"


def test_numeric_key_map_becomes_a_root_model_not_a_field_per_key(
    fixture_specs: Path, tmp_path: Path
) -> None:
    """The overlay-priority map is ``{priority: overlayId}``: the keys are data.

    A generator that reads them as property names emits ``field_1: int``, which
    is wrong for every priority the caller did not happen to look at.
    """
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/gms/overlays/priority": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "1": {"type": "integer"},
                            "2": {"type": "integer"},
                            "3": {"type": "integer"},
                        },
                    }
                )
            }
        },
    )
    result = generate("orchestrator", "POST", "/gms/overlays/priority")
    assert "RootModel[dict[str, int]]" in result.models_source
    assert "field_1" not in result.models_source

    module = load_generated(result, tmp_path)
    model = getattr(module, result.request_model)
    assert model.model_validate({"1": 2, "2": 3, "3": 1}).root == {"1": 2, "2": 3, "3": 1}
    # And a priority the spec never listed still round-trips.
    assert model.model_validate({"9": 4}).root == {"9": 4}


def test_placeholder_key_map_becomes_a_typed_mapping(fixture_specs: Path, tmp_path: Path) -> None:
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/bgp/config/system": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "route_target": {
                                "type": "object",
                                "properties": {
                                    "<session-ID>": {
                                        "type": "object",
                                        "properties": {"import": {"type": "string"}},
                                    }
                                },
                            }
                        },
                    }
                )
            }
        },
    )
    result = generate("appliance", "POST", "/bgp/config/system")
    assert "route_target: dict[str, RouteTargetValue] | None" in result.models_source

    module = load_generated(result, tmp_path)
    model = getattr(module, result.request_model)
    parsed = model.model_validate({"route_target": {"7": {"import": "65000:1"}}})
    assert parsed.route_target["7"].import_ == "65000:1"


def test_mixed_block_keeps_its_real_field_and_absorbs_the_map_keys(
    fixture_specs: Path, tmp_path: Path
) -> None:
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/stats2/aggregate/tunnel": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "<nePk>": {"type": "object"},
                            "header": {"type": "string"},
                        },
                    }
                )
            }
        },
    )
    result = generate("orchestrator", "POST", "/stats2/aggregate/tunnel")
    assert "header: str | None" in result.models_source
    assert "ne_pk" not in result.models_source

    module = load_generated(result, tmp_path)
    parsed = getattr(module, result.request_model).model_validate(
        {"header": "h", "3.NE": {"bytes": 1}}
    )
    assert parsed.header == "h"
    # extra="allow" carried the map-shaped key through untouched.
    assert parsed.model_dump(by_alias=True)["3.NE"] == {"bytes": 1}


def test_map_value_schemas_are_unified_across_the_examples() -> None:
    identical = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert gen_models.unify_map_values([identical, dict(identical)]) == identical
    merged = gen_models.unify_map_values(
        [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ]
    )
    assert set(merged["properties"]) == {"a", "b"}
    # Nothing sensible to merge -> Any, rather than a wrong shape.
    assert gen_models.unify_map_values([{"type": "string"}, {"type": "object"}]) == {}


# ---------------------------------------------------------------------------
# Trap 1: `self`, and trap 3: pydantic's reserved surface


def test_self_is_emitted_aliased_and_round_trips_byte_for_byte(
    fixture_specs: Path, tmp_path: Path
) -> None:
    """``self`` is the server's identity echo; ``security_policy.py`` re-injects
    it on POST, so a model that dropped it would corrupt a read-modify-write."""
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/securityMaps": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "self": {"type": "string"},
                            "gms_marked": {"type": "boolean"},
                        },
                    }
                )
            }
        },
    )
    result = generate("appliance", "POST", "/securityMaps")
    assert 'self_: str | None = Field(default=None, alias="self")' in result.models_source

    module = load_generated(result, tmp_path)
    payload = {"self": "0_0", "gms_marked": False}
    parsed = getattr(module, result.request_model).model_validate(payload)
    assert parsed.self_ == "0_0"
    assert parsed.model_dump(by_alias=True, exclude_unset=True) == payload


def test_reserved_names_are_aliased_never_mangled_on_the_wire(
    fixture_specs: Path, tmp_path: Path
) -> None:
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/probe": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {
                            "copy": {"type": "string"},
                            "json": {"type": "string"},
                            "schema": {"type": "string"},
                            "import": {"type": "string"},
                            "model_dump": {"type": "string"},
                        },
                    }
                )
            }
        },
    )
    result = generate("orchestrator", "POST", "/probe")
    module = load_generated(result, tmp_path)
    payload = {k: "v" for k in ("copy", "json", "schema", "import", "model_dump")}
    parsed = getattr(module, result.request_model).model_validate(payload)
    assert parsed.model_dump(by_alias=True, exclude_unset=True) == payload


def test_unknown_fields_pass_through(fixture_specs: Path, tmp_path: Path) -> None:
    """Live drift from the 7.2.0 baseline must not fail validation."""
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/probe": {
                "post": json_body({"type": "object", "properties": {"a": {"type": "string"}}})
            }
        },
    )
    result = generate("orchestrator", "POST", "/probe")
    assert "GeneratedModel" in result.models_source
    module = load_generated(result, tmp_path)
    parsed = getattr(module, result.request_model).model_validate({"a": "x", "undocumented": [1]})
    assert parsed.model_dump(by_alias=True)["undocumented"] == [1]


def test_enums_stay_open_and_are_documented_rather_than_enforced(
    fixture_specs: Path, tmp_path: Path
) -> None:
    """A closed ``Literal`` would reject values a 9.x Orchestrator really sends."""
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/probe": {
                "post": json_body(
                    {
                        "type": "object",
                        "properties": {"mode": {"type": "string", "enum": ["bridge", "router"]}},
                    }
                )
            }
        },
    )
    result = generate("orchestrator", "POST", "/probe")
    assert "Literal" not in result.models_source
    assert "spec values" in result.models_source
    module = load_generated(result, tmp_path)
    assert getattr(module, result.request_model).model_validate({"mode": "unlisted"}).mode


# ---------------------------------------------------------------------------
# Bindings: routing, path parameters, query parameters


BASE = "https://orch.example.com/gms/rest"


def import_binding(result: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Write both emitted modules into an importable package and load the binding."""
    package = tmp_path / "genpkg"
    (package / "models").mkdir(parents=True, exist_ok=True)
    (package / "bindings").mkdir(parents=True, exist_ok=True)
    for part in ("", "models", "bindings"):
        (package / part / "__init__.py").write_text("", encoding="utf-8")
    models_source = result.models_source.replace(
        f"{gen_models.GENERATED_PACKAGE}._base", "pyecsdwan.generated._base"
    )
    bindings_source = result.bindings_source.replace(
        f"{gen_models.GENERATED_PACKAGE}.models.", "genpkg.models."
    ).replace(f"{gen_models.GENERATED_PACKAGE}._base", "pyecsdwan.generated._base")
    (package / "models" / f"{result.slug}.py").write_text(models_source, encoding="utf-8")
    (package / "bindings" / f"{result.slug}.py").write_text(bindings_source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # Each test builds its own genpkg under its own tmp_path; drop any earlier
    # one so the import machinery does not resolve to a stale directory.
    for name in [n for n in sys.modules if n == "genpkg" or n.startswith("genpkg.")]:
        del sys.modules[name]
    importlib.invalidate_caches()
    module = importlib.import_module(f"genpkg.bindings.{result.slug}")
    return getattr(module, result.function_name), module


@respx.mock
def test_orchestrator_binding_calls_request_on_the_spec_path(
    fixture_specs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/modelSpec": {
                "post": {
                    **json_body(
                        {
                            "type": "object",
                            "properties": {
                                "partNums": {"type": "array", "items": {"type": "string"}}
                            },
                        }
                    ),
                    **json_response({"type": "object", "properties": {"ok": {"type": "boolean"}}}),
                }
            }
        },
    )
    result = generate("orchestrator", "POST", "/modelSpec")
    assert result.endpoint.scope == "orchestrator"
    assert "client.request(" in result.bindings_source
    assert "appliance_request" not in result.bindings_source

    call, module = import_binding(result, tmp_path, monkeypatch)
    route = respx.post(f"{BASE}/modelSpec").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    response = call(OrchClient(settings), {"partNums": ["123456"]})
    assert response.ok is True
    assert json.loads(route.calls.last.request.content) == {"partNums": ["123456"]}
    assert module.PATH == "/modelSpec"


@respx.mock
def test_appliance_binding_goes_through_the_proxy_with_a_relative_ecos_path(
    fixture_specs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """The trap: ``appliance_request`` takes the ECOS path *without* a leading
    slash, and a binding that hands it ``/bgp/config/system`` is wrong even
    though the client strips it. Normalization happens on emit."""
    write_spec(
        fixture_specs,
        "appliance",
        {"/bgp/config/system": {"post": json_body({"type": "object", "properties": {}})}},
    )
    result = generate("appliance", "POST", "/bgp/config/system")
    assert "client.appliance_request(" in result.bindings_source
    assert 'ECOS_PATH = "bgp/config/system"' in result.bindings_source

    call, module = import_binding(result, tmp_path, monkeypatch)
    assert module.ECOS_PATH == "bgp/config/system"
    route = respx.post(f"{BASE}/appliance/rest").mock(return_value=httpx.Response(200, json={}))
    call(OrchClient(settings), "3.NE", {"asn": 65000})
    request = route.calls.last.request
    assert dict(request.url.params) == {"nePk": "3.NE", "url": "bgp/config/system"}


@respx.mock
def test_path_parameters_become_required_arguments_and_are_substituted(
    fixture_specs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/nat/maps/{mapName}/prio/{prio}": {
                "delete": {"responses": {"204": {"description": "gone"}}}
            }
        },
    )
    result = generate("appliance", "DELETE", "/nat/maps/{mapName}/prio/{prio}")
    assert result.path_params == (("map_name", "mapName"), ("prio", "prio"))

    call, _module = import_binding(result, tmp_path, monkeypatch)
    route = respx.delete(f"{BASE}/appliance/rest").mock(return_value=httpx.Response(204))
    call(OrchClient(settings), "3.NE", "MAP1", "10")
    assert route.calls.last.request.url.params["url"] == "nat/maps/MAP1/prio/10"

    with pytest.raises(TypeError):
        call(OrchClient(settings), "3.NE")  # both path parameters are required


@respx.mock
def test_required_query_parameters_are_named_arguments(
    fixture_specs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/vrf/config/securityPolicies": {
                "get": {
                    "parameters": [
                        {
                            "name": "map",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {"name": "cached", "in": "query", "schema": {"type": "boolean"}},
                    ],
                    **json_response({"type": "object"}),
                }
            }
        },
    )
    result = generate("orchestrator", "GET", "/vrf/config/securityPolicies")
    assert [q.python_name for q in result.query_params] == ["map", "cached"]

    call, _module = import_binding(result, tmp_path, monkeypatch)
    route = respx.get(f"{BASE}/vrf/config/securityPolicies").mock(
        return_value=httpx.Response(200, json={})
    )
    call(OrchClient(settings), map="0_0")
    assert dict(route.calls.last.request.url.params) == {"map": "0_0"}
    call(OrchClient(settings), map="0_0", cached=True)
    assert dict(route.calls.last.request.url.params) == {"map": "0_0", "cached": "true"}


@respx.mock
def test_appliance_query_parameters_ride_inside_the_proxied_url(
    fixture_specs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    """``appliance_request`` has no ``params``; the proxy's ``url`` carries them."""
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/flowDetails": {
                "get": {
                    "parameters": [
                        {
                            "name": "id",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "seq",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    **json_response({"type": "object"}),
                }
            }
        },
    )
    result = generate("appliance", "GET", "/flowDetails")
    call, _module = import_binding(result, tmp_path, monkeypatch)
    route = respx.get(f"{BASE}/appliance/rest").mock(return_value=httpx.Response(200, json={}))
    call(OrchClient(settings), "3.NE", id="7", seq="1")
    assert route.calls.last.request.url.params["url"] == "flowDetails?id=7&seq=1"


def test_expected_status_comes_from_the_declared_2xx_responses(fixture_specs: Path) -> None:
    write_spec(
        fixture_specs,
        "orchestrator",
        {
            "/a": {"post": {"responses": {"201": {"description": "made"}, "400": {}}}},
            "/b": {"get": {"responses": {}}},
        },
    )
    a = specs.find_endpoint("orchestrator", "POST", "/a")
    b = specs.find_endpoint("orchestrator", "GET", "/b")
    assert a is not None and b is not None
    assert gen_models.expected_status(a) == (201,)
    assert gen_models.expected_status(b) == (200, 204)


# ---------------------------------------------------------------------------
# The request-shape fallback ladder


def test_request_shape_falls_back_to_a_spec_example_when_no_schema_is_declared(
    fixture_specs: Path, tmp_path: Path
) -> None:
    """204 of 710 writes declare no request schema; ``POST /poe/config`` only
    documents its body by example, and that beats an untyped passthrough."""
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/poe/config": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"example": {"lan0": {"poe": {"enable": True}}}}
                        }
                    },
                    "responses": {},
                }
            }
        },
    )
    result = generate("appliance", "POST", "/poe/config")
    assert result.body_source == "spec-example"
    module = load_generated(result, tmp_path)
    parsed = getattr(module, result.request_model).model_validate(
        {"lan0": {"poe": {"enable": True}}}
    )
    assert parsed.lan0.poe.enable is True


def test_a_write_with_no_shape_at_all_gets_an_untyped_passthrough_body(
    fixture_specs: Path,
) -> None:
    write_spec(fixture_specs, "orchestrator", {"/probe": {"post": {"responses": {}}}})
    result = generate("orchestrator", "POST", "/probe")
    assert result.body_source == "none"
    assert result.request_model is None
    assert "RequestBody = Mapping[str, Any] | list[Any]" in result.bindings_source


def test_postman_payload_examples_light_up_the_third_rung(
    fixture_specs: Path, tmp_path: Path
) -> None:
    """Issue #51 has not landed; wire it now so the merge needs no change here."""
    write_spec(fixture_specs, "orchestrator", {"/probe": {"post": {"responses": {}}}})
    (fixture_specs / "payload-examples-9.6.json").write_text(
        json.dumps({"endpoints": {"orchestrator POST /probe": {"request": {"hostName": "edge1"}}}}),
        encoding="utf-8",
    )
    gen_models.clear_caches()
    result = generate("orchestrator", "POST", "/probe")
    assert result.body_source == "postman-example"
    module = load_generated(result, tmp_path)
    assert getattr(module, result.request_model).model_validate({"hostName": "edge1"}).host_name


def test_infer_schema_reads_shape_not_obligation() -> None:
    inferred = gen_models.infer_schema({"a": 1, "b": [{"c": "x"}], "d": True, "e": 1.5})
    assert inferred["properties"]["a"] == {"type": "integer"}
    assert inferred["properties"]["d"] == {"type": "boolean"}
    assert inferred["properties"]["e"] == {"type": "number"}
    assert inferred["properties"]["b"]["items"]["properties"]["c"] == {"type": "string"}
    assert "required" not in inferred


# ---------------------------------------------------------------------------
# Determinism and the CLI


def test_generation_is_byte_identical_across_runs() -> None:
    """A regeneration diff has to mean a spec change, never emitter noise."""
    for key in ("appliance POST /bgp/config/system", "orchestrator POST /gms/treeConfig"):
        scope, method, path = key.split(" ")
        first = generate(scope, method, path)
        gen_models.clear_caches()
        second = generate(scope, method, path)
        assert first.models_source == second.models_source
        assert first.bindings_source == second.bindings_source


def test_cli_dry_run_prints_both_modules_and_exits_zero() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(GEN_MODELS),
            "--scope",
            "appliance",
            "--method",
            "POST",
            "--path",
            "bgp/config/system",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "# ==> models/appliance_post_bgp_config_system.py" in completed.stdout
    assert "# ==> bindings/appliance_post_bgp_config_system.py" in completed.stdout


def test_cli_reports_an_unknown_operation_with_suggestions() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(GEN_MODELS),
            "--scope",
            "appliance",
            "--method",
            "POST",
            "--path",
            "bgp/config/systemm",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 1
    assert "no such operation" in completed.stderr


# ---------------------------------------------------------------------------
# Acceptance criteria, against the real vendored baselines


def _example_bearing_operations() -> list[specs.Endpoint]:
    return [e for e in specs.iter_endpoints() if any(gen_models.spec_examples(e).values())]


def test_generated_models_validate_the_example_payloads_in_the_spec(tmp_path: Path) -> None:
    """Acceptance criterion 1, proved literally over every example in both baselines."""
    validated = 0
    for endpoint in _example_bearing_operations():
        result = gen_models.generate(endpoint)
        module = load_generated(result, tmp_path)
        examples = gen_models.spec_examples(endpoint)
        for role, model_name in (
            ("request", result.request_model),
            ("response", result.response_model),
        ):
            if model_name is None:
                # Scalar bodies ("EST Config saved successfully") get no model
                # by design; the binding returns the parsed JSON untouched.
                assert all(not isinstance(x, (dict, list)) for x in examples[role]), (
                    f"{endpoint.key} {role}: structured example but no model"
                )
                continue
            for payload in examples[role]:
                getattr(module, model_name).model_validate(payload)
                validated += 1
    assert validated >= 20, f"only {validated} example payloads exercised"


def test_every_committed_binding_routes_by_scope() -> None:
    """Acceptance criterion 2, over the committed samples."""
    seen = {"orchestrator": 0, "appliance": 0}
    for path in sorted((GENERATED_DIR / "bindings").glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        if 'SCOPE = "appliance"' in source:
            seen["appliance"] += 1
            assert "client.appliance_request(" in source, path.name
            assert "client.request(" not in source, path.name
            ecos = source.split('ECOS_PATH = "', 1)[1].split('"', 1)[0]
            assert not ecos.startswith("/"), f"{path.name}: ECOS path must be relative"
        else:
            seen["orchestrator"] += 1
            assert "client.request(" in source, path.name
            assert "appliance_request" not in source, path.name
    assert seen["orchestrator"] and seen["appliance"]


def test_committed_samples_cover_the_shapes_the_issue_calls_out() -> None:
    names = {p.stem for p in (GENERATED_DIR / "bindings").glob("*.py")} - {"__init__"}
    assert any(n.startswith("orchestrator_") for n in names)
    assert any(n.startswith("appliance_") for n in names)
    assert any("_by_" in n for n in names), "no committed sample carries a path parameter"
    priority = (GENERATED_DIR / "models" / "orchestrator_post_gms_overlays_priority.py").read_text()
    assert "RootModel[dict[str," in priority, "the map-shaped case is not represented"


@pytest.mark.parametrize("tool", ["check", "format"])
def test_committed_generated_code_is_ruff_clean(tool: str) -> None:
    """Acceptance criterion 3, and the determinism guarantee: the emitter's own
    output is already a fixed point of ``ruff format``."""
    ruff = gen_models.find_ruff()
    if ruff is None:
        pytest.skip("ruff is not installed in this environment")
    argv = [ruff, tool, str(GENERATED_DIR)]
    if tool == "format":
        argv.insert(2, "--diff")
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout + completed.stderr
