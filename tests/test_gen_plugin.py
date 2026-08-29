"""Unit tests for tools/gen_plugin.py: Tier-1 plugin stubs from a spec operation.

Three layers, and the third is the epic's definition of done:

* **Rule tests** against tiny hand-built specs pointed at by
  ``$ECSDWAN_SPECS_DIR``, so each decision the emitter makes -- the
  reversibility ladder above all -- fails in isolation with a readable message.
* **Committed-sample tests** drive the two stubs in
  ``pyecsdwan.resources.generated`` for real: the appliance one against the
  bundled mock (proxy write + ``save_changes``), the orchestrator one against
  ``respx``. They also prove the issue's three acceptance criteria: the stubs
  compile and register, their ``normalize()`` raises ``NotCurated``, and the
  engine refuses them a ``commit confirm`` window without
  ``--allow-untransactional``.
* **The end-to-end test** (:func:`test_spec_sync_diff_produces_a_registered_stub`)
  is the epic DoD: a modified spec fixture in, ``spec_sync.py --diff`` detects
  the added endpoint, ``--update`` vendors it, ``gen_plugin.py --from-diff``
  emits a stub, and that stub *imports*, registers, and refuses to normalize.

Plus a corpus pass: every write operation in both vendored baselines is
generated and compiled, which is what catches the emitter's edge cases (a
mandatory DELETE body, a query parameter typed differently on two operations
of one path) that a handful of hand-picked endpoints never would.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - importing registers the built-in plugins
from pyecsdwan import config, specs, txn
from pyecsdwan import registry as registry_mod
from pyecsdwan.cli import main as cli_main
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import (
    Ctx,
    Diff,
    DiffEntry,
    DiffOp,
    NotCurated,
    Ownership,
    Ref,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import Registry, check_untransactional_normalize, default_registry
from pyecsdwan.resolver import Resolver
from pyecsdwan.resources.generated import _stub

REPO_ROOT = Path(__file__).resolve().parent.parent
GEN_PLUGIN = REPO_ROOT / "tools" / "gen_plugin.py"
SPEC_SYNC = REPO_ROOT / "tools" / "spec_sync.py"
STUBS_DIR = REPO_ROOT / "src" / "pyecsdwan" / "resources" / "generated"
BASE = "https://orch.example.com/gms/rest"

#: The two committed samples, one per scope.
APPLIANCE_KIND = "generated/appliance_post_virtualif_vti_by_vti_name"
ORCHESTRATOR_KIND = "generated/orchestrator_post_alarm_correlation_settings"


def _load_module() -> Any:
    """Import tools/gen_plugin.py without making tools/ a package."""
    spec = importlib.util.spec_from_file_location("gen_plugin", GEN_PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_plugin"] = module
    spec.loader.exec_module(module)
    return module


gen_plugin = _load_module()


@pytest.fixture(autouse=True)
def _clear_caches() -> Iterator[None]:
    gen_plugin.gen_models.clear_caches()
    yield
    gen_plugin.gen_models.clear_caches()


# ---------------------------------------------------------------------------
# Hand-built spec fixtures


def write_spec(directory: Path, scope: str, paths: dict[str, Any], version: str = "0.0.1") -> Path:
    document = {
        "openapi": "3.0.0",
        "info": {"title": f"{scope} test", "version": version},
        "paths": paths,
    }
    destination = directory / f"{scope}-openapi-{version}.json"
    destination.write_text(json.dumps(document), encoding="utf-8")
    return destination


def operation(summary: str = "do a thing", *, body: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"summary": summary}
    if body:
        out["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {"type": "object", "properties": {"enable": {"type": "boolean"}}}
                }
            }
        }
    return out


def required_body_operation() -> dict[str, Any]:
    out = operation()
    out["requestBody"]["required"] = True
    return out


@pytest.fixture
def fixture_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path))
    gen_plugin.gen_models.clear_caches()
    return tmp_path


# ---------------------------------------------------------------------------
# The ref-naming convention (pyecsdwan.resources.generated._stub)


ONE = (_stub.StubParam("vtiName", "path", "vti interface name"),)
TWO = (_stub.StubParam("vrfId", "path"), _stub.StubParam("IP", "path"))


def test_a_single_parameter_takes_the_bare_ref_name() -> None:
    ref = Ref(kind="k", name="vti1", appliance="HUB1-EC")
    assert _stub.param_values("k", ref, ONE) == {"vtiName": "vti1"}
    explicit = Ref(kind="k", name="vtiName=vti1", appliance="HUB1-EC")
    assert _stub.param_values("k", explicit, ONE) == {"vtiName": "vti1"}


def test_several_parameters_need_named_pairs_in_any_order() -> None:
    ref = Ref(kind="k", name="IP=10.0.0.1,vrfId=0")
    assert _stub.param_values("k", ref, TWO) == {"vrfId": "0", "IP": "10.0.0.1"}


def test_a_missing_or_unknown_parameter_is_refused_not_guessed() -> None:
    """The whole point of the convention: a stub that quietly defaulted a path
    parameter would address a different instance than the operator named."""
    with pytest.raises(ValueError, match="missing"):
        _stub.param_values("k", Ref(kind="k", name="vrfId=0"), TWO)
    with pytest.raises(ValueError, match="unknown parameter 'vrf'"):
        _stub.param_values("k", Ref(kind="k", name="vrf=0,IP=1.2.3.4"), TWO)
    with pytest.raises(ValueError, match="no '='"):
        _stub.param_values("k", Ref(kind="k", name="0,1.2.3.4"), TWO)
    with pytest.raises(ValueError, match="missing"):
        _stub.param_values("k", Ref(kind="k", name=""), ONE)


def test_no_parameters_means_the_ref_name_is_a_free_label() -> None:
    assert _stub.param_values("k", Ref(kind="k", name="anything"), ()) == {}


def test_as_raw_keeps_objects_and_lists_and_drops_scalars() -> None:
    """A snapshot is 'object or absent'; a bare string is not state."""
    assert _stub.as_raw({"a": 1}) == {"a": 1}
    assert _stub.as_raw([1, 2]) == [1, 2]
    assert _stub.as_raw("Config saved successfully") is None
    assert _stub.as_raw(None) is None


def test_as_raw_round_trips_a_generated_model_including_unknown_fields() -> None:
    from pyecsdwan.generated.models.appliance_get_virtualif_vti_by_vti_name import (
        ApplianceGetVirtualifVtiResponse,
    )

    payload = {"admin": True, "undocumented_field": {"nested": True}}
    model = ApplianceGetVirtualifVtiResponse.model_validate(payload)
    assert _stub.as_raw(model) == payload


def test_as_bool_parses_the_spellings_a_ref_name_can_carry() -> None:
    assert _stub.as_bool("true") is True
    assert _stub.as_bool("OFF") is False
    with pytest.raises(ValueError, match="boolean"):
        _stub.as_bool("maybe")


# ---------------------------------------------------------------------------
# The reversibility ladder


def _fixture_stub(directory: Path, methods: dict[str, Any], primary: str) -> Any:
    write_spec(directory, "appliance", {"/thing": methods})
    return gen_plugin.generate_stub_for("appliance", primary, "/thing")


def test_get_plus_write_plus_delete_is_compensable(fixture_specs: Path) -> None:
    stub = _fixture_stub(
        fixture_specs,
        {"get": operation(body=False), "post": operation(), "delete": operation(body=False)},
        "POST",
    )
    assert stub.reversibility == "COMPENSABLE"
    assert "compensates a creation" in stub.reversibility_reason
    assert "Reversibility.COMPENSABLE" in stub.source
    assert 'self._delete(ctx, ref, "rollback")' in stub.source


def test_get_plus_write_without_a_delete_is_compensable_by_replay_only(
    fixture_specs: Path,
) -> None:
    stub = _fixture_stub(fixture_specs, {"get": operation(body=False), "post": operation()}, "POST")
    assert stub.reversibility == "COMPENSABLE"
    assert "compensates a creation" not in stub.reversibility_reason
    assert "no pre-change state recorded" in stub.source


def test_a_write_with_no_paired_get_is_irreversible(fixture_specs: Path) -> None:
    """Nothing to snapshot means nothing to put back. Declaring COMPENSABLE
    here would promise a rollback the stub cannot perform."""
    stub = _fixture_stub(fixture_specs, {"post": operation()}, "POST")
    assert stub.reversibility == "IRREVERSIBLE"
    assert "no GET" in stub.reversibility_reason
    assert "Reversibility.IRREVERSIBLE" in stub.source
    assert "return None" in stub.source  # fetch() takes no snapshot


def test_a_paired_delete_alone_does_not_buy_compensable(fixture_specs: Path) -> None:
    """The trap this ladder exists for: DELETE compensates a *creation*, and
    without a GET the stub cannot tell a creation from an update. Deleting on
    rollback would then destroy pre-existing configuration."""
    stub = _fixture_stub(
        fixture_specs, {"post": operation(), "delete": operation(body=False)}, "POST"
    )
    assert stub.reversibility == "IRREVERSIBLE"


def test_a_delete_primary_stub_is_irreversible(fixture_specs: Path) -> None:
    stub = _fixture_stub(
        fixture_specs, {"get": operation(body=False), "delete": operation(body=False)}, "DELETE"
    )
    assert stub.reversibility == "IRREVERSIBLE"
    assert "re-creating an object" in stub.reversibility_reason
    assert "was generated from a DELETE operation" in stub.source


def test_reversibility_never_reaches_reversible(fixture_specs: Path) -> None:
    """Codegen may not claim exact snapshot/restore: nothing in a spec says the
    GET's response is accepted verbatim by the write."""
    write_spec(
        fixture_specs, "appliance", {"/thing": {"get": operation(body=False), "put": operation()}}
    )
    for method in ("PUT", "POST"):
        endpoint = specs.find_endpoint("appliance", method, "/thing")
        if endpoint is None:
            continue
        assert gen_plugin.generate_stub(endpoint).reversibility != "REVERSIBLE"


# ---------------------------------------------------------------------------
# What the tool refuses to generate


def test_a_read_only_operation_gets_no_plugin(fixture_specs: Path) -> None:
    write_spec(fixture_specs, "appliance", {"/thing": {"get": operation(body=False)}})
    with pytest.raises(gen_plugin.GenPluginError, match="read-only"):
        gen_plugin.generate_stub_for("appliance", "GET", "/thing")


def test_a_path_with_whitespace_is_refused_with_a_reason(fixture_specs: Path) -> None:
    """``Resource.endpoints`` is a space-separated key; two 7.2.0 paths carry a
    trailing space and simply cannot be declared through it."""
    write_spec(fixture_specs, "appliance", {"/linkIntegrityTest/run ": {"post": operation()}})
    with pytest.raises(gen_plugin.GenPluginError, match="whitespace"):
        gen_plugin.generate_stub_for("appliance", "POST", "/linkIntegrityTest/run ")


def test_a_delete_that_demands_a_body_is_refused(fixture_specs: Path) -> None:
    """A delete's desired state is 'absent'; there is nothing to build a
    mandatory payload from."""
    write_spec(fixture_specs, "appliance", {"/thing": {"delete": required_body_operation()}})
    with pytest.raises(gen_plugin.GenPluginError, match="request body"):
        gen_plugin.generate_stub_for("appliance", "DELETE", "/thing")


def test_a_delete_that_demands_a_body_is_not_used_as_a_compensator(
    fixture_specs: Path,
) -> None:
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/thing": {
                "get": operation(body=False),
                "post": operation(),
                "delete": required_body_operation(),
            }
        },
    )
    stub = gen_plugin.generate_stub_for("appliance", "POST", "/thing")
    assert stub.reversibility == "COMPENSABLE"
    assert "compensates a creation" not in stub.reversibility_reason
    assert len(stub.operations) == 2  # the delete binding is not even generated


# ---------------------------------------------------------------------------
# The hand-off from gen_models: the emitted call must match the emitted binding


@pytest.mark.parametrize("kind", [APPLIANCE_KIND, ORCHESTRATOR_KIND])
def test_the_emitted_call_matches_the_bindings_real_signature(kind: str) -> None:
    """``binding_parameters`` reads gen_models' rendered signature back rather
    than re-deriving its rules. If that parse ever drifts, the emitted call
    would be wrong in a way mypy catches only for the committed samples -- so
    assert the parse against the imported function itself."""
    scope, method, path = _endpoints_of(kind)[0].split(" ", 2)
    stub = gen_plugin.generate_stub_for(scope, method, path)
    for gen_op in stub.operations:
        module = importlib.import_module(gen_op.bindings_module)
        real = tuple(inspect.signature(getattr(module, gen_op.function_name)).parameters)
        assert gen_plugin.binding_parameters(gen_op) == real, gen_op.function_name


def _endpoints_of(kind: str) -> tuple[str, ...]:
    return default_registry.get(kind).endpoints


def test_scope_decides_the_transport_and_the_save_changes_tail() -> None:
    """Appliance stubs go through the proxy and must persist; orchestrator
    stubs must not call save_changes at all."""
    appliance = (STUBS_DIR / f"{APPLIANCE_KIND.split('/')[1]}.py").read_text(encoding="utf-8")
    orchestrator = (STUBS_DIR / f"{ORCHESTRATOR_KIND.split('/')[1]}.py").read_text(encoding="utf-8")
    assert "ne_pk_for(ctx, KIND, ref)" in appliance
    assert "ctx.save_changes([ne_pk]" in appliance
    assert "Scope.APPLIANCE" in appliance
    assert "save_changes" not in orchestrator
    assert "ne_pk" not in orchestrator
    assert "Scope.ORCHESTRATOR" in orchestrator


# ---------------------------------------------------------------------------
# Acceptance criterion 1: the emitted stub compiles and registers


def _generated_kinds() -> list[str]:
    return [k for k in default_registry.kinds() if k.startswith(gen_plugin.KIND_PREFIX)]


def test_the_committed_stubs_registered_at_tier_one() -> None:
    kinds = _generated_kinds()
    assert set(kinds) == {APPLIANCE_KIND, ORCHESTRATOR_KIND}
    scopes = set()
    for kind in kinds:
        resource = default_registry.get(kind)
        assert resource.tier is Tier.GENERATED
        assert resource.reversibility is not Reversibility.REVERSIBLE
        assert resource.endpoints, f"{kind} declares no endpoints"
        scopes.add(resource.scope)
    assert scopes == {Scope.APPLIANCE, Scope.ORCHESTRATOR}


def test_the_stub_package_init_imports_exactly_what_is_on_disk() -> None:
    """A stub dropped in without regenerating ``__init__.py`` would never
    register; the acceptance criterion is 'registers', not 'could register'."""
    from pyecsdwan.resources import generated

    assert sorted(generated.__all__) == gen_plugin.stub_slugs(STUBS_DIR)


def test_show_coverage_reports_the_stubs_at_tier_one() -> None:
    rows = {
        (r.scope, r.method, specs.normalize_path(r.path)): r
        for r in cli_main._endpoint_coverage(default_registry)
    }
    row = rows[("appliance", "POST", "/virtualif/vti/{}")]
    assert row.tier == int(Tier.GENERATED)
    assert row.kinds == (APPLIANCE_KIND,)
    assert row.reversibility == "compensable"
    counts = cli_main._coverage_rollup(list(rows.values()))
    assert counts["generated"] == 5
    assert "5 generated" in cli_main.coverage_summary_line(default_registry)


# ---------------------------------------------------------------------------
# Acceptance criterion 2: normalize() raises NotCurated


@pytest.mark.parametrize("kind", [APPLIANCE_KIND, ORCHESTRATOR_KIND])
def test_normalize_refuses_until_curated(kind: str) -> None:
    """DoD #8. ``tests/test_promotion.py`` runs this over the whole registry;
    asserted here too, with the message a curator actually reads."""
    resource = default_registry.get(kind)
    for raw in ({}, None, {"enable": True}, [1, 2]):
        with pytest.raises(NotCurated, match="Tier-1 generated stub"):
            resource.normalize(raw)
    assert not check_untransactional_normalize(resource).failed


def test_a_stub_generated_right_now_also_refuses(fixture_specs: Path) -> None:
    """Not just the committed pair: the property holds for freshly emitted
    source, which is what makes the promotion gate meaningful for stubs nobody
    has written yet."""
    stub = _fixture_stub(fixture_specs, {"get": operation(body=False), "post": operation()}, "POST")
    assert "raise NotCurated(" in stub.source
    assert "NotImplementedError" not in stub.source


# ---------------------------------------------------------------------------
# Acceptance criterion 3: no commit confirm without --allow-untransactional


def _plan_item(kind: str) -> txn.PlanItem:
    resource = default_registry.get(kind)
    ref = Ref(kind=kind, name="vti1", appliance="HUB1-EC")
    desired: dict[str, Any] = {"admin": "up"}
    diff = Diff(
        ref=ref,
        entries=[DiffEntry(DiffOp.ADD, ("admin",), None, "up")],
        desired=desired,
        current=None,
    )
    return txn.PlanItem(
        ref=ref,
        resource=resource,
        delete=False,
        current_raw=None,
        current=None,
        desired=desired,
        diff=diff,
        # Explicit, because PlanItem now defaults to UNKNOWN and UNKNOWN is
        # refused (#20). These tests are about the *tier* guard; leaving the
        # default would make them pass on the wrong refusal.
        ownership=Ownership.unowned("not what this test is about"),
    )


def test_commit_confirm_is_refused_without_allow_untransactional(settings: Any) -> None:
    item = _plan_item(APPLIANCE_KIND)
    with pytest.raises(txn.CommitError, match="allow-untransactional"):
        txn._guard([item], settings, 5.0, False, False, False)
    # ... and accepted once the operator says so explicitly.
    txn._guard([item], settings, 5.0, False, False, True)
    # A plain commit (no confirm window) was never gated on the tier.
    txn._guard([item], settings, None, False, False, False)


def test_the_refusal_reaches_the_operator_through_commit(state_home: Any, settings: Any) -> None:
    ctx = Ctx(client=OrchClient(settings), resolver=Resolver(OrchClient(settings)))
    plan = txn.Plan(items=[_plan_item(ORCHESTRATOR_KIND)])
    with pytest.raises(txn.CommitError, match="tier-1"):
        txn.commit(ctx, default_registry, plan, settings, confirm_minutes=5)


# ---------------------------------------------------------------------------
# The committed stubs, driven for real


pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import run_in_thread  # noqa: E402 - after importorskip


@pytest.fixture(scope="module")
def mock_fabric() -> Iterator[str]:
    base_url, state, shutdown = run_in_thread()
    try:
        state.reset()
        yield base_url
    finally:
        shutdown()


@pytest.fixture
def mock_ctx(mock_fabric: str) -> Ctx:
    settings = config.Settings(orch_url=mock_fabric, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    return Ctx(client=client, resolver=Resolver(client))


def _diff(ref: Ref, desired: Any, current: Any = None) -> Diff:
    return Diff(
        ref=ref,
        entries=[DiffEntry(DiffOp.ADD, ("admin",), None, True)],
        desired=desired,
        current=current,
    )


def test_appliance_stub_writes_through_the_proxy_and_persists(mock_ctx: Ctx) -> None:
    """End to end against the bundled mock: the ref name carries the path
    parameter, the write goes through the appliance proxy, and the running
    config is persisted with one batched save-changes (#11)."""
    resource = default_registry.get(APPLIANCE_KIND)
    ref = Ref(kind=APPLIANCE_KIND, name="vti1", appliance="HUB1-EC")
    assert resource.fetch(mock_ctx, ref) == {}

    result = resource.apply(mock_ctx, _diff(ref, {"admin": True, "label": "wan-vti"}))
    assert result.ok, result.message
    assert [job.state for job in result.jobs] == ["SUCCESS"]
    assert resource.fetch(mock_ctx, ref) == {"admin": True, "label": "wan-vti"}


def test_appliance_stub_compensates_a_creation_by_deleting(mock_ctx: Ctx) -> None:
    """The COMPENSABLE half the ladder actually promises: an absent snapshot
    means the object did not exist, so rollback removes it."""
    resource = default_registry.get(APPLIANCE_KIND)
    ref = Ref(kind=APPLIANCE_KIND, name="vti-rollback", appliance="HUB1-EC")
    resource.apply(mock_ctx, _diff(ref, {"admin": True}))
    assert resource.fetch(mock_ctx, ref) == {"admin": True}

    back = resource.rollback(mock_ctx, ref, None)
    assert back.ok, back.message
    assert resource.fetch(mock_ctx, ref) == {}


def test_appliance_stub_replays_a_snapshot_on_rollback(mock_ctx: Ctx) -> None:
    resource = default_registry.get(APPLIANCE_KIND)
    ref = Ref(kind=APPLIANCE_KIND, name="vti-replay", appliance="HUB1-EC")
    resource.apply(mock_ctx, _diff(ref, {"admin": True, "label": "before"}))
    snapshot = resource.fetch(mock_ctx, ref)
    resource.apply(mock_ctx, _diff(ref, {"admin": False, "label": "after"}))

    back = resource.rollback(mock_ctx, ref, snapshot)
    assert back.ok, back.message
    assert resource.fetch(mock_ctx, ref) == snapshot


def test_appliance_stub_refuses_a_ref_with_no_appliance(mock_ctx: Ctx) -> None:
    resource = default_registry.get(APPLIANCE_KIND)
    with pytest.raises(ValueError, match="appliance-scoped"):
        resource.fetch(mock_ctx, Ref(kind=APPLIANCE_KIND, name="vti1"))


@respx.mock
def test_orchestrator_stub_reads_and_writes_the_spec_path(settings: Any) -> None:
    resource = default_registry.get(ORCHESTRATOR_KIND)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    ref = Ref(kind=ORCHESTRATOR_KIND, name="global")

    respx.get(f"{BASE}/alarm/correlationSettings").mock(
        return_value=httpx.Response(200, json={"correlationEnabled": True})
    )
    assert resource.fetch(ctx, ref) == {"correlationEnabled": True}

    route = respx.post(f"{BASE}/alarm/correlationSettings").mock(
        return_value=httpx.Response(200, json={})
    )
    result = resource.apply(ctx, _diff(ref, {"correlationEnabled": False}))
    assert result.ok, result.message
    assert result.jobs == []  # orchestrator scope: nothing to persist
    assert json.loads(route.calls.last.request.content) == {"correlationEnabled": False}


@respx.mock
def test_a_404_snapshot_is_absent_and_anything_else_propagates(settings: Any) -> None:
    """Best-effort means 'absent is not an error'. It does not mean swallowing
    a 500: a snapshot that silently came back empty would make rollback()
    write an empty object over live configuration."""
    from pyecsdwan.client import OrchApiError

    resource = default_registry.get(ORCHESTRATOR_KIND)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    ref = Ref(kind=ORCHESTRATOR_KIND, name="global")

    respx.get(f"{BASE}/alarm/correlationSettings").mock(return_value=httpx.Response(404))
    assert resource.fetch(ctx, ref) is None

    respx.get(f"{BASE}/alarm/correlationSettings").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(OrchApiError):
        resource.fetch(ctx, ref)


@respx.mock
def test_orchestrator_stub_refuses_a_delete_it_has_no_endpoint_for(settings: Any) -> None:
    resource = default_registry.get(ORCHESTRATOR_KIND)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    ref = Ref(kind=ORCHESTRATOR_KIND, name="global")
    result = resource.apply(ctx, _diff(ref, None))
    assert not result.ok
    assert "no DELETE on this path" in result.message
    assert resource.deletable is False


# ---------------------------------------------------------------------------
# The epic DoD: spec_sync detects an added endpoint -> a compiling, registered
# stub whose normalize() raises.


@pytest.fixture
def scratch_registry(monkeypatch: pytest.MonkeyPatch) -> Registry:
    """A throwaway ``default_registry`` so importing a stub does not pollute
    the process-wide one (and cannot collide with the committed samples)."""
    fresh = Registry()
    monkeypatch.setattr(registry_mod, "default_registry", fresh)
    return fresh


@pytest.fixture
def importable(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make modules written under a temp tree importable under their real
    dotted names, by extending the packages' ``__path__``."""
    import pyecsdwan.generated.bindings as bindings_pkg
    import pyecsdwan.generated.models as models_pkg
    import pyecsdwan.resources.generated as stubs_pkg

    def extend(root: Path) -> None:
        for package, relative in (
            (models_pkg, Path("generated") / "models"),
            (bindings_pkg, Path("generated") / "bindings"),
            (stubs_pkg, Path("stubs")),
        ):
            monkeypatch.setattr(
                package, "__path__", [*package.__path__, str(root / relative)], raising=True
            )
        importlib.invalidate_caches()

    return extend


def _run_spec_sync(*args: str) -> subprocess.CompletedProcess[str]:
    # Argument list, never shell=True.
    return subprocess.run(
        [sys.executable, str(SPEC_SYNC), *args], capture_output=True, text=True, timeout=120
    )


def test_spec_sync_diff_produces_a_registered_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scratch_registry: Registry,
    importable: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The epic's definition of done, driven as three real commands.

    A modified spec fixture adds ``POST``/``DELETE`` to a path the baseline
    only exposed ``GET`` on. ``spec_sync.py --diff`` reports the addition,
    ``--update`` vendors it (nothing can be generated from an operation
    ``specs/`` does not carry), and ``gen_plugin.py --from-diff`` reads that
    same report back and emits one stub for the pair. The stub is then
    *imported* -- which is what "compiles and registers" means -- and its
    ``normalize()`` must raise ``NotCurated``.
    """
    baseline_dir = tmp_path / "specs"
    baseline_dir.mkdir()
    write_spec(baseline_dir, "appliance", {"/widget/config": {"get": operation(body=False)}})
    published = tmp_path / "published.json"
    published.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "appliance test", "version": "0.0.2"},
                "paths": {
                    "/widget/config": {
                        "get": operation(body=False),
                        "post": operation("Set the widget configuration"),
                        "delete": operation("Remove the widget configuration", body=False),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    # 1. detect
    diff = _run_spec_sync(
        "--diff",
        "--spec",
        "appliance",
        "--source",
        str(published),
        "--specs-dir",
        str(baseline_dir),
        "--json",
    )
    assert diff.returncode == 1, diff.stderr
    report = json.loads(diff.stdout)
    added = report["targets"]["appliance"]["endpoints"]["added"]
    assert sorted(added) == ["DELETE /widget/config", "POST /widget/config"]
    report_path = tmp_path / "drift.json"
    report_path.write_text(diff.stdout, encoding="utf-8")

    # 2. vendor it, so there is something to generate from
    update = _run_spec_sync(
        "--update",
        "--spec",
        "appliance",
        "--source",
        str(published),
        "--specs-dir",
        str(baseline_dir),
    )
    assert update.returncode == 0, update.stderr
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(baseline_dir))
    gen_plugin.gen_models.clear_caches()

    # 3. generate — one stub for the whole path, from the strongest write
    stubs = tmp_path / "stubs"
    exit_code = gen_plugin.main(
        [
            "--from-diff",
            str(report_path),
            "--out",
            str(stubs),
            "--out-bindings",
            str(tmp_path / "generated"),
            "--no-format",
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "tier 1" in captured.out
    emitted = sorted(p.name for p in stubs.glob("*.py"))
    assert emitted == ["__init__.py", "appliance_post_widget_config.py"]

    # 4. it compiles and registers ...
    importable(tmp_path)
    module_name = "pyecsdwan.resources.generated.appliance_post_widget_config"
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    importlib.import_module(module_name)
    try:
        assert scratch_registry.kinds() == ["generated/appliance_post_widget_config"]
        resource = scratch_registry.get("generated/appliance_post_widget_config")
        assert resource.tier is Tier.GENERATED
        assert resource.scope is Scope.APPLIANCE
        assert resource.reversibility is Reversibility.COMPENSABLE
        assert resource.endpoints == (
            "appliance POST /widget/config",
            "appliance GET /widget/config",
            "appliance DELETE /widget/config",
        )

        # 5. ... and refuses to normalize until a human curates it. (DoD #8.)
        with pytest.raises(NotCurated):
            resource.normalize({})
        assert not check_untransactional_normalize(resource).failed
    finally:
        sys.modules.pop(module_name, None)


def test_from_diff_says_so_when_the_baseline_was_not_updated_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one ordering mistake the flow invites: generating straight off a
    diff, before ``--update`` has vendored the operation."""
    baseline_dir = tmp_path / "specs"
    baseline_dir.mkdir()
    write_spec(baseline_dir, "appliance", {"/widget/config": {"get": operation(body=False)}})
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(baseline_dir))
    gen_plugin.gen_models.clear_caches()
    report = tmp_path / "drift.json"
    report.write_text(
        json.dumps({"targets": {"appliance": {"endpoints": {"added": ["POST /widget/config"]}}}}),
        encoding="utf-8",
    )
    assert gen_plugin.main(["--from-diff", str(report), "--out", str(tmp_path / "s")]) == 1
    captured = capsys.readouterr()
    assert "not in the vendored baseline" in captured.err
    assert "spec_sync.py --update" in captured.err


def test_one_path_collapses_to_one_stub(fixture_specs: Path) -> None:
    """A new resource arrives in a diff as GET + POST + DELETE. That is one
    resource, not three."""
    write_spec(
        fixture_specs,
        "appliance",
        {
            "/thing": {
                "get": operation(body=False),
                "post": operation(),
                "delete": operation(body=False),
            }
        },
    )
    added = [
        specs.find_endpoint("appliance", method, "/thing") for method in ("GET", "POST", "DELETE")
    ]
    chosen = gen_plugin.select_primary([e for e in added if e is not None])
    assert [e.method for e in chosen] == ["POST"]


# ---------------------------------------------------------------------------
# Corpus: the emitter against every write operation in both baselines


#: Reasons :func:`generate_stub` may refuse an operation. Both are documented
#: in ``tools/README.md``; anything else is an emitter bug, not a spec quirk.
REFUSAL_REASONS = ("whitespace", "request body")


def test_every_write_operation_generates_a_stub_that_compiles() -> None:
    """Acceptance criterion 1, over the whole corpus rather than two samples.

    ``compile()`` is the literal reading of "the emitted stub compiles"; the
    committed samples carry the rest (imports resolve, mypy --strict passes,
    registration happens). The line-length assertion is here because ``ruff``
    is not guaranteed installed and an over-long emitted line is the single
    most common way generated code stops being lint-clean.
    """
    generated = 0
    refused: list[str] = []
    for endpoint in specs.iter_endpoints():
        if endpoint.method not in gen_plugin.WRITE_METHODS:
            continue
        try:
            stub = gen_plugin.generate_stub(endpoint)
        except gen_plugin.GenPluginError as exc:
            assert any(reason in str(exc) for reason in REFUSAL_REASONS), f"{endpoint.key}: {exc}"
            refused.append(endpoint.key)
            continue
        compile(stub.source, stub.slug, "exec")
        long = [line for line in stub.source.splitlines() if len(line) > 100]
        assert long == [], f"{stub.slug}: {long[:1]}"
        assert stub.reversibility in ("COMPENSABLE", "IRREVERSIBLE")
        generated += 1
    assert generated > 800, generated
    assert len(refused) < 10, refused


def test_generation_is_byte_identical_across_runs() -> None:
    first = gen_plugin.generate_stub_for("appliance", "POST", "/virtualif/vti/{vtiName}")
    gen_plugin.gen_models.clear_caches()
    second = gen_plugin.generate_stub_for("appliance", "POST", "/virtualif/vti/{vtiName}")
    assert first.source == second.source


def test_the_committed_stubs_are_what_the_tool_emits_today() -> None:
    """Regenerating a committed sample is a no-op. If this fails, either the
    emitter changed (regenerate) or the stub was hand-curated (which is
    allowed -- but then it should not still be at tier 1)."""
    for kind in (APPLIANCE_KIND, ORCHESTRATOR_KIND):
        scope, method, path = _endpoints_of(kind)[0].split(" ", 2)
        stub = gen_plugin.generate_stub_for(scope, method, path)
        on_disk = (STUBS_DIR / stub.path).read_text(encoding="utf-8")
        assert stub.source == on_disk, f"{kind}: rerun tools/gen_plugin.py --force"


@pytest.mark.parametrize("tool", ["check", "format"])
def test_committed_stubs_are_ruff_clean(tool: str) -> None:
    ruff = gen_plugin.gen_models.find_ruff()
    if ruff is None:
        pytest.skip("ruff is not installed in this environment")
    argv = [ruff, tool, str(STUBS_DIR)]
    if tool == "format":
        argv.insert(2, "--diff")
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert completed.returncode == 0, completed.stdout + completed.stderr
