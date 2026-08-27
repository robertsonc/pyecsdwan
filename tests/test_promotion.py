"""Promotion-checklist gating, Tier-1 -> Tier-2 (issue #29).

``docs/plugin-promotion.md`` was advisory prose. The transaction engine already
refuses a Tier-0/1 resource a confirm window (``txn.py``: ``low_tier``), but
that is a *runtime* guard — it fires when an operator is already pointed at a
fabric. This module is the static gate that fires first, in ``make check``.

Two obligations, taken straight from the checklist:

1. Every ``Tier.GENERATED`` (or ``RAW``) resource's ``normalize()`` raises
   ``NotCurated``. A stub that quietly returns something looks curated to every
   caller, and the engine's tier guard becomes the only thing left between it
   and a fabric.
2. Every ``Tier.CURATED`` resource is *proven* idempotent — one test each,
   parametrized off the registry, so a newly registered kind is covered with no
   bookkeeping.

Design, and why it is not the obvious one
-----------------------------------------
The tempting implementation of obligation 2 is a static scan of ``tests/`` for
a test named after each kind. That asserts a *filename*, not a property: rename
the test and the gate silently stops covering that kind; write a test that
asserts nothing and it still counts. So nothing here looks at test names. Each
check evaluates the behavior the checklist demands
(``pyecsdwan.registry.check_idempotent``) against a sample raw state.

Sample states come from a two-rung ladder, per kind:

``enumerated`` (39/41 kinds)
    ``list_refs()`` against the bundled mock Orchestrator, taking the first ref
    whose ``fetch()`` canonicalizes to something with actual content. The
    resource's own read path is exercised, on the mock's realistic fixtures.
``probe-ref`` (2/41 kinds)
    Kinds that do not implement ``list_refs()`` at all, named in
    :data:`PROBE_REFS` with a reason. Same fetch-from-the-mock check, just with
    the ref supplied instead of discovered.

There is no third rung and no opt-out list: :data:`OPT_OUTS` is empty and
asserted empty, so a kind cannot be silently absent from coverage.

Vacuity is the failure mode a gate like this dies of, so it is closed three
ways: the ladder rejects a sample whose canonical form carries no leaf values
(``{"templates": {}}`` is *not* a sample); :data:`OPT_OUTS` is asserted; and
:func:`test_the_gate_bites_a_generated_stub` and friends run the same gate
functions over deliberately broken resources and require them to FAIL.

Caveat on the seeded fixtures: two of the mock's tables start empty, so
:func:`_seed_untouched_tables` fills them. Where the fill comes from the
vendor's published Postman payload examples (``specs.payload_example``, #51),
the values are *shape only* — the vendor writes ``"string"`` / ``0`` / ``true``
into every scalar. Those samples prove field names and nesting round-trip; they
prove nothing about real values.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - importing registers the built-in plugins
from pyecsdwan import config, specs
from pyecsdwan import registry as registry_mod
from pyecsdwan.cli import main as cli_main
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import (
    CanonicalState,
    Ctx,
    NotCurated,
    RawState,
    Ref,
    Resource,
    Tier,
)
from pyecsdwan.registry import (
    Check,
    CheckStatus,
    Registry,
    check_idempotent,
    check_untransactional_normalize,
    default_registry,
    has_content,
)
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

#: Kinds whose ``list_refs()`` does not enumerate (the default returns ``[]``),
#: with the ref to fetch instead and why the resource has no enumeration. Both
#: still go through the resource's real ``fetch()`` against the mock — this is
#: not a weaker check, only a supplied starting point.
#:
#: Asserted non-redundant by :func:`test_probe_refs_are_all_still_needed`: an
#: entry whose kind *does* enumerate is stale and fails the suite.
PROBE_REFS: dict[str, tuple[Ref, str]] = {
    "appliance/vrrp": (
        Ref(kind="appliance/vrrp", name="global", appliance="HUB1-EC"),
        "singleton per appliance; enumerating it would mean a proxy GET per "
        "appliance in the fabric, so the resource does not implement list_refs()",
    ),
    "security-policy": (
        Ref(kind="security-policy", name="0_0"),
        "instances are addressed by segment pair (srcSeg_dstSeg) and the "
        "Orchestrator exposes no endpoint that lists the configured pairs",
    ),
}

#: Curated kinds excused from the property check, kind -> reason. **Empty, and
#: asserted empty.** An entry here is a promise that no sample can be derived;
#: it is not a place to park a kind whose normalize() fails.
OPT_OUTS: dict[str, str] = {}


def _seed_untouched_tables(state: MockState) -> None:
    """Fill the two mock tables the bundled fixtures leave empty.

    Everything else in the mock ships realistic seed data (captured against a
    lab Orchestrator); template groups and orchestrator-scope security policies
    start empty because most tests create them as part of what they exercise.
    Three curated kinds read them, so the gate seeds them once here.
    """
    # Security policy: the vendor's own published example for
    # GET /vrf/config/securityPolicies. SHAPE ONLY — every scalar is a
    # placeholder — but it carries the real nesting (map -> zone pair -> prio
    # -> rule) plus the `self` echoes and `gms_marked` flags normalize() must
    # strip, which is exactly what the round-trip needs to exercise.
    example = specs.payload_example("orchestrator", "GET", "/vrf/config/securityPolicies")
    if example is not None and isinstance(example.get("response"), dict):
        data = example["response"].get("data")
        if isinstance(data, dict):
            state.security_policies["0_0"] = data

    # Template group: the vendor example for this one has an empty valObject,
    # which would canonicalize to {"templates": {"string": {}}} — no leaf
    # values, so the ladder would (correctly) reject it as vacuous. Use the
    # vendor's *envelope* with the mock's own seeded SNMP section as the
    # valObject, which is what a real template section payload looks like.
    val_object = state.appliance_ecos.get("1.NE", {}).get("snmp", {"enable": True})
    state.template_groups["Branch-Std"] = {
        "name": "Branch-Std",
        "comment": "promotion-gate sample",
        "templates": [{"name": "snmp", "valObject": val_object}],
    }
    # ... and associate it, so template-association has a non-empty membership
    # list to round-trip rather than the empty one every appliance starts with.
    state.template_association["1.NE"] = ["Branch-Std"]


# -- sample acquisition -------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Sample:
    """One kind's sample raw state and where it came from."""

    kind: str
    ref: Ref
    raw: RawState
    source: str  # "enumerated" | "probe-ref"


@pytest.fixture(scope="module")
def mock_fabric() -> Iterator[str]:
    """Base URL of a bundled mock with the empty tables seeded."""
    base_url, state, shutdown = run_in_thread()
    try:
        state.reset()
        _seed_untouched_tables(state)
        yield base_url
    finally:
        shutdown()


@pytest.fixture(scope="module")
def mock_ctx(mock_fabric: str) -> Ctx:
    """A Ctx pointed at that mock."""
    settings = config.Settings(orch_url=mock_fabric, api_key="test-key", job_timeout=5.0)
    return Ctx(client=OrchClient(settings), resolver=Resolver(OrchClient(settings)))


def _sample_for(ctx: Ctx, resource: Resource) -> Sample | None:
    """Walk the ladder for one kind; ``None`` when no rung yields content."""
    for ref in resource.list_refs(ctx):
        raw = resource.fetch(ctx, ref)
        if has_content(resource.normalize(raw)):
            return Sample(resource.kind, ref, raw, "enumerated")
    probe = PROBE_REFS.get(resource.kind)
    if probe is not None:
        ref = probe[0]
        raw = resource.fetch(ctx, ref)
        if has_content(resource.normalize(raw)):
            return Sample(resource.kind, ref, raw, "probe-ref")
    return None


@pytest.fixture(scope="module")
def samples(mock_ctx: Ctx) -> dict[str, Sample]:
    """Every curated kind's sample, gathered in one pass over one mock boot."""
    found: dict[str, Sample] = {}
    for kind in default_registry.kinds():
        resource = default_registry.get(kind)
        if resource.tier < Tier.CURATED:
            continue
        sample = _sample_for(mock_ctx, resource)
        if sample is not None:
            found[kind] = sample
    return found


CURATED_KINDS = [
    k for k in default_registry.kinds() if default_registry.get(k).tier >= Tier.CURATED
]
ALL_KINDS = default_registry.kinds()


# -- obligation 1: un-curated tiers must refuse -------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_untransactional_kinds_refuse_to_normalize(kind: str) -> None:
    """Tier 0/1: normalize() raises NotCurated. Tier 2: nothing to prove here.

    No registered kind is below Tier 2 today, so this passes trivially over the
    live registry — :func:`test_the_gate_bites_a_generated_stub` is what proves
    the check itself works.
    """
    check = check_untransactional_normalize(default_registry.get(kind))
    assert not check.failed, f"{kind}: {check.detail}"


# -- obligation 2: curated kinds are idempotent -------------------------------


@pytest.mark.parametrize("kind", CURATED_KINDS)
def test_curated_kind_is_idempotent(kind: str, samples: dict[str, Sample], mock_ctx: Ctx) -> None:
    """One test per curated kind: normalize is idempotent and re-planning the
    server's own state produces no diff.

    This is the acceptance criterion "curated resources are proven idempotent
    by a test each" — parametrized off the registry, so registering a kind
    creates its test, and failing to make it idempotent fails ``make check``.
    """
    if kind in OPT_OUTS:
        pytest.skip(f"declared opt-out: {OPT_OUTS[kind]}")
    sample = samples.get(kind)
    assert sample is not None, (
        f"{kind}: no sample raw state could be derived from the bundled mock. "
        f"Add a PROBE_REFS entry (with the reason list_refs() does not "
        f"enumerate), seed the mock table it reads in _seed_untouched_tables(), "
        f"or - last resort, with a written justification - an OPT_OUTS entry."
    )
    resource = default_registry.get(kind)
    failures = [c for c in check_idempotent(resource, sample.ref, sample.raw, mock_ctx) if c.failed]
    assert not failures, "\n".join(f"{c.name}: {c.detail}" for c in failures)


def test_every_curated_kind_has_a_sample(samples: dict[str, Sample]) -> None:
    """Coverage bookkeeping: no kind silently absent from the property check."""
    uncovered = sorted(set(CURATED_KINDS) - set(samples) - set(OPT_OUTS))
    assert uncovered == [], f"curated kinds with no sample and no opt-out: {uncovered}"


def test_opt_out_list_is_empty() -> None:
    """Every curated kind is checked directly today. Adding an entry here is a
    deliberate, reviewable weakening of the gate - not a quiet one."""
    assert OPT_OUTS == {}, f"undocumented weakening of the promotion gate: {OPT_OUTS}"


def test_probe_refs_are_all_still_needed(mock_ctx: Ctx) -> None:
    """A PROBE_REFS entry for a kind that now enumerates is dead weight; drop
    it so the table stays short enough to read."""
    for kind in PROBE_REFS:
        assert kind in default_registry, f"PROBE_REFS names an unregistered kind: {kind}"
        resource = default_registry.get(kind)
        assert resource.list_refs(mock_ctx) == [], (
            f"{kind} enumerates through list_refs() now; remove its PROBE_REFS entry"
        )


def test_most_kinds_need_no_hand_holding(samples: dict[str, Sample]) -> None:
    """The ladder must stay overwhelmingly automatic. If declared probe refs
    ever outgrow enumeration, the gate has turned into a fixture table."""
    enumerated = [s.kind for s in samples.values() if s.source == "enumerated"]
    probed = [s.kind for s in samples.values() if s.source == "probe-ref"]
    assert len(enumerated) > 4 * len(probed), (
        f"{len(probed)} probe-ref kinds vs {len(enumerated)} enumerated: {sorted(probed)}"
    )


# -- proof the gate bites -----------------------------------------------------
#
# The registry holds no Tier-0/1 kind, and every curated kind passes. A gate
# that is only ever run against passing input is not known to work, so the rest
# of this module runs the same functions over deliberately broken resources.


class _GoodStub(Resource):
    """A correctly written Tier-1 stub, as tools/gen_plugin.py emits."""

    kind = "test/generated-good"
    tier = Tier.GENERATED

    def normalize(self, raw: RawState) -> CanonicalState:
        raise NotCurated(f"{self.kind} is Tier-1; finish normalize() before curating")


class _SilentStub(Resource):
    """The failure this issue exists for: a stub someone wired up far enough to
    return, but never curated. Looks curated to every caller."""

    kind = "test/generated-silent"
    tier = Tier.GENERATED

    def normalize(self, raw: RawState) -> CanonicalState:
        return raw if isinstance(raw, dict) else None


class _WrongErrorStub(Resource):
    """Raises, but not the error the tier guard and the operator agree on."""

    kind = "test/generated-wrong-error"
    tier = Tier.GENERATED

    def normalize(self, raw: RawState) -> CanonicalState:
        raise NotImplementedError("todo")


class _DriftingResource(Resource):
    """Curated, but normalize() is not a fixed point: each pass appends a
    revision marker, so every re-plan shows phantom drift."""

    kind = "test/drifting"

    def normalize(self, raw: RawState) -> CanonicalState:
        assert isinstance(raw, dict)
        out = dict(raw)
        out["revision"] = int(out.get("revision", 0)) + 1
        return out


class _VacuousResource(Resource):
    """Curated, and idempotent - because it canonicalizes everything to an
    empty shell. Idempotency holds and means nothing."""

    kind = "test/vacuous"

    def normalize(self, raw: RawState) -> CanonicalState:
        return {"entries": {}}


class _NonReplanningResource(Resource):
    """Curated and idempotent, but user intent is shaped differently from
    server state, so re-planning what the server already reports writes again."""

    kind = "test/non-replanning"

    def normalize(self, raw: RawState) -> CanonicalState:
        assert isinstance(raw, dict)
        return dict(raw)

    def canonicalize_desired(self, ctx: Ctx, ref: Ref, desired: Any) -> CanonicalState:
        return {**dict(desired), "injected-default": "enabled"}


_SAMPLE_RAW: dict[str, Any] = {"name": "sample", "enabled": True}
_STUB_REF = Ref(kind="test/stub", name="sample")


def _statuses(checks: list[Check]) -> dict[str, CheckStatus]:
    return {c.name: c.status for c in checks}


def test_the_gate_bites_a_generated_stub() -> None:
    """A Tier-1 stub whose normalize() returns instead of raising must FAIL."""
    check = check_untransactional_normalize(_SilentStub())
    assert check.failed
    assert "instead of raising NotCurated" in check.detail

    wrong = check_untransactional_normalize(_WrongErrorStub())
    assert wrong.failed
    assert "NotImplementedError" in wrong.detail

    assert not check_untransactional_normalize(_GoodStub()).failed


def test_the_gate_bites_a_non_idempotent_curated_resource() -> None:
    checks = check_idempotent(_DriftingResource(), _STUB_REF, dict(_SAMPLE_RAW))
    assert _statuses(checks)["normalize-idempotent"] is CheckStatus.FAIL
    detail = next(c.detail for c in checks if c.name == "normalize-idempotent")
    assert "/revision" in detail, detail


def test_the_gate_bites_a_vacuous_sample() -> None:
    """The check that stops this whole module from passing for free."""
    checks = check_idempotent(_VacuousResource(), _STUB_REF, dict(_SAMPLE_RAW))
    assert _statuses(checks)["sample-non-trivial"] is CheckStatus.FAIL
    # ... and the ladder refuses such a state as a sample in the first place.
    assert not has_content(_VacuousResource().normalize(dict(_SAMPLE_RAW)))
    assert not has_content(None)
    assert not has_content({"a": {}, "b": []})
    assert has_content({"a": {"b": False}})


def test_the_gate_bites_a_resource_that_replans_dirty(mock_ctx: Ctx) -> None:
    checks = check_idempotent(_NonReplanningResource(), _STUB_REF, dict(_SAMPLE_RAW), mock_ctx)
    assert _statuses(checks)["replan-empty"] is CheckStatus.FAIL
    assert not check_idempotent(_NonReplanningResource(), _STUB_REF, dict(_SAMPLE_RAW))[
        -1
    ].failed  # without a ctx the replan box is not evaluated at all


def test_the_gate_bites_over_a_whole_registry() -> None:
    """End to end: the same sweep the suite runs over ``default_registry``,
    run over a registry holding one un-curated stub, fails."""
    broken = Registry()
    broken.register(_SilentStub())
    broken.register(_GoodStub())
    failures = {
        kind: check_untransactional_normalize(broken.get(kind))
        for kind in broken.kinds()
        if check_untransactional_normalize(broken.get(kind)).failed
    }
    assert sorted(failures) == ["test/generated-silent"]


def test_manual_checks_are_reported_not_dropped() -> None:
    """The boxes no machine can decide stay on the checklist, visibly."""
    manual = registry_mod.manual_checks()
    assert {c.status for c in manual} == {CheckStatus.MANUAL}
    assert not any(c.failed for c in manual)
    names = {c.name for c in manual}
    assert {"reversibility-class", "async-jobs", "dependencies"} <= names


# -- `ec-cli plugin promote` --------------------------------------------------
#
# The same checks, pointed at a real Orchestrator instead of the mock — the one
# thing `make check` cannot do, because a plugin author's own fabric carries
# shapes the fixtures do not. Built because the checklist logic was reusable as
# is; it deliberately does NOT edit the plugin to flip the tier, since the tier
# is a reviewed source declaration, not a runtime toggle.

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    # The app callback binds structlog process-wide to CliRunner's capture
    # stream, which is closed on exit; leaving it bound kills the next test
    # that logs. (Same teardown as tests/test_coverage.py.)
    structlog.reset_defaults()


def _promote(mock_fabric: str, *args: str) -> Any:
    port = mock_fabric.rsplit(":", 1)[1]
    return runner.invoke(cli_main.app, ["--mock", port, "plugin", "promote", *args])


def test_promote_reports_a_curated_kind_as_green(state_home: Any, mock_fabric: str) -> None:
    result = _promote(mock_fabric, "zones", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["kind"] == "zones"
    assert payload["green"] is True
    names = {c["name"]: c["status"] for c in payload["checks"]}
    assert names["normalize-idempotent"] == "pass"
    assert names["replan-empty"] == "pass"
    # The boxes a machine cannot decide are reported, never silently dropped.
    assert names["reversibility-class"] == "manual"


def test_promote_needs_a_ref_it_cannot_enumerate(state_home: Any, mock_fabric: str) -> None:
    """security-policy has no listing endpoint; the command says so instead of
    quietly reporting green on nothing."""
    result = _promote(mock_fabric, "security-policy")
    assert result.exit_code == 2
    assert "enumerates no instances" in result.output
    named = _promote(mock_fabric, "security-policy", "--name", "0_0", "--json")
    assert named.exit_code == 0, named.output
    assert json.loads(named.stdout)["green"] is True


def test_promote_rejects_an_unknown_kind(state_home: Any, mock_fabric: str) -> None:
    result = _promote(mock_fabric, "not-a-kind")
    assert result.exit_code == 2
    assert "unknown resource kind" in result.output


def test_promote_validates_scope_on_a_named_ref(state_home: Any, mock_fabric: str) -> None:
    """--name on an appliance-scope kind still needs --appliance."""
    result = _promote(mock_fabric, "appliance/bgp", "--name", "config")
    assert result.exit_code == 2
    assert "appliance-scoped" in result.output


def test_promote_refuses_to_green_light_an_un_curated_stub(
    state_home: Any, mock_fabric: str
) -> None:
    """A Tier-1 stub passes exactly one box — "normalize() refuses" — and the
    Tier-2 boxes cannot run while normalize() raises. Reporting that as green
    would tell a curator to set tier = Tier.CURATED on a resource whose
    normalize() still raises, which `make check` then rejects. The advice has
    to match what was actually verified."""
    stub_kinds = [
        k for k in default_registry.kinds() if default_registry.get(k).tier < Tier.CURATED
    ]
    assert stub_kinds, "expected at least one generated stub to be registered"
    kind = stub_kinds[0]

    result = _promote(mock_fabric, kind)
    assert result.exit_code == 1, result.output
    assert "were NOT checked" in result.output

    as_json = _promote(mock_fabric, kind, "--json")
    assert as_json.exit_code == 1
    payload = json.loads(as_json.stdout)
    assert payload["green"] is False
    assert payload["tier2_evaluated"] is False
    # The one box it *can* evaluate still passes — the stub is correct.
    names = {c["name"]: c["status"] for c in payload["checks"]}
    assert names["generated-normalize-refuses"] == "pass"
    assert "normalize-idempotent" not in names
