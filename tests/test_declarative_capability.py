"""Per-resource declarative capability and safe materialization (#126, T8).

The contract exists because of a live event, not a hypothesis: on 2026-08-30
a partial BGP peer declaration applied cleanly and then failed post-apply
verification, because the appliance defaulted every field the client did not
send. The engine auto-reverted a change that had reached the fabric
correctly. That is what "typed partial intent posted as a document" does even
when it works — so capability defaults to unsupported, materialization is a
per-kind proof, and the ledger, not the class attribute, is what a claim is
checked against.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, desired, evidence, txn
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import (
    Ctx,
    DeclarativeCapability,
    MaterializationBlocked,
    Ref,
    Resource,
    Reversibility,
    Tier,
)
from pyecsdwan.registry import Registry, default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    return {"ctx": ctx, "settings": settings, "state": state}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "apiVersion: pyecsdwan/v1\nstate: present\nspec:\n"
        + "".join(f"  {line}\n" for line in text.splitlines()),
        encoding="utf-8",
    )


# -- registry coverage: the capability question is answered, and honestly -----


def test_every_kind_answers_and_no_claim_outruns_its_evidence() -> None:
    """#126's first and fourth criteria in one sweep.

    Coverage is total by construction — the base attribute answers
    "unsupported" for every kind that says nothing — so what is worth
    asserting is the shape of every claim that says *more*: it must be
    curated, it must override the materializer (a capability with the base
    materializer is a claim that blocks itself), and the ledger must record
    live change-and-rollback evidence. Intent never outruns observation.
    """
    claiming = []
    for kind in default_registry.kinds():
        resource = default_registry.get(kind)
        capability = resource.declarative
        assert isinstance(capability, DeclarativeCapability), kind
        if capability is DeclarativeCapability.UNSUPPORTED:
            continue
        claiming.append(kind)
        assert (
            type(resource).materialize_declared is not Resource.materialize_declared
        ), (
            f"{kind} claims {capability.value} with the base materializer, "
            f"which always raises — the claim can never be exercised and "
            f"exists only to mislead a reader"
        )
        level = evidence.ledger().level(kind)
        assert level is not None and level >= evidence.WRITE_SUPPORTED_FLOOR, (
            f"{kind} claims {capability.value} but the ledger records "
            f"{level.label if level else 'nothing'}; capability follows "
            f"evidence (D16), and the claim must be withdrawn or the "
            f"evidence recorded"
        )
        assert capability is not DeclarativeCapability.PRESENT_AND_ABSENT, (
            f"{kind} claims present-and-absent, but T11 has not defined the "
            f"delete-and-restore evidence that claim requires; no kind may "
            f"make it yet"
        )
    # The worked example is really there — a sweep that found zero claimants
    # would pass every assertion above while testing nothing.
    assert "appliance/banners" in claiming


def test_the_gate_caps_a_claim_the_ledger_cannot_back() -> None:
    """Runtime half of "capability matches evidence": a plugin loaded at
    runtime, or a ledger swapped underneath the binary, is caught at plan
    time, not only by the packaged-kind test above."""
    effective, reason = evidence.declarative_gate(
        "no-such-kind", DeclarativeCapability.PRESENT_ONLY
    )
    assert effective is DeclarativeCapability.UNSUPPORTED
    assert reason is not None and "no ledger entry" in reason

    effective, reason = evidence.declarative_gate(
        "interface-labels", DeclarativeCapability.PRESENT_ONLY
    )
    assert effective is DeclarativeCapability.UNSUPPORTED
    assert reason is not None and "live-no-op-write-verified" in reason

    effective, reason = evidence.declarative_gate(
        "appliance/banners", DeclarativeCapability.PRESENT_ONLY
    )
    assert effective is DeclarativeCapability.PRESENT_ONLY
    assert reason is None


def test_the_base_materializer_refuses_with_the_kind_named() -> None:
    resource = default_registry.get("interface-labels")
    with pytest.raises(MaterializationBlocked, match="interface-labels"):
        resource.materialize_declared(
            Ref(kind="interface-labels", name="global"), {"wan": {}}, {}
        )


def test_declarations_without_a_registry_materialize_nothing(tmp_path: Path) -> None:
    """A hand-built `Declared` has no way to reach any kind's proof. The
    fallback used to be replace semantics — the partial write D7 forbids,
    reachable by whoever forgot the argument."""
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "issue: new")
    loaded = desired.load(default_registry, tmp_path)
    bare = desired.Declared(
        items=loaded.items, origins=loaded.origins, declarations=loaded.declarations
    )
    item = next(iter(bare.items.values()))
    with pytest.raises(MaterializationBlocked, match="without a registry"):
        bare.desired_for(item, {"issue": "old", "motd": "keep"})


class _Contradiction(Resource):
    """Says UNSUPPORTED, but carries a materializer that would happily write."""

    kind = "contradiction"
    cli_name = "contradiction"
    scope_name = "orchestrator"
    tier = Tier.CURATED
    reversibility = Reversibility.REVERSIBLE
    declarative = DeclarativeCapability.UNSUPPORTED

    def fetch(self, ctx: Ctx, ref: Ref) -> Any:
        return {"live": "state"}

    def normalize(self, raw: Any) -> Any:
        return dict(raw) if isinstance(raw, dict) else None

    def materialize_declared(self, ref: Ref, spec: Mapping[str, Any], current: Any) -> Any:
        return dict(spec)

    def apply(self, ctx: Ctx, diff: Any) -> Any:  # pragma: no cover - never reached
        raise AssertionError("a blocked reference must never be applied")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: Any) -> Any:  # pragma: no cover
        raise AssertionError("a blocked reference must never be rolled back")


def test_a_kind_declaring_unsupported_cannot_write_through_its_own_override(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The declaration is the contract, the override is not: a kind that says
    UNSUPPORTED is held to the base materializer even if it ships one of its
    own, so the claim cannot be walked around from inside the class."""
    registry = Registry()
    registry.register(_Contradiction())
    _write(tmp_path, "fabric/contradiction/one.yaml", "live: rewritten")

    declared = desired.load(registry, tmp_path)
    plan = txn.build_plan(world["ctx"], registry, declared)

    (blocked,) = plan.blocked_items
    assert "not declaratively writable" in (blocked.blocked or "")
    assert not plan.changed_items


# -- materialization: complete target from current + declaration --------------


def test_banners_materialization_completes_the_target() -> None:
    """D7, on the worked example: the declared field wins, the undeclared
    field and the field the model does not even know about both survive into
    the complete target. Replace semantics — what a declaration got before
    T8 — would have erased both."""
    resource = default_registry.get("appliance/banners")
    ref = Ref(kind="appliance/banners", name="global", appliance="BR1-EC")
    current = {
        "motd": "undeclared live value",
        "issue": "old",
        "futureServerField": "unknown to the model",
    }
    target = resource.materialize_declared(ref, {"issue": "new"}, current)
    assert target == {
        "motd": "undeclared live value",
        "issue": "new",
        "futureServerField": "unknown to the model",
    }
    assert current["issue"] == "old", "materialization mutated its input"


def test_banners_refuses_to_materialize_over_nothing() -> None:
    """A merge over nothing is exactly the partial write D8 forbids."""
    resource = default_registry.get("appliance/banners")
    ref = Ref(kind="appliance/banners", name="global", appliance="BR1-EC")
    with pytest.raises(MaterializationBlocked, match="no current state"):
        resource.materialize_declared(ref, {"issue": "new"}, None)


# -- the plan: blocked is visible, refused, and never "no changes" ------------


def test_an_unsupported_kind_blocks_visibly_and_a_capable_one_plans(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """One directory, two kinds, three properties: the capable kind produces
    an applicable diff; the unsupported kind appears as a *blocked item* in
    the same plan rather than vanishing; and commit refuses the changeset
    while the blocker stands — a blocked reference must never be silently
    skipped so the rest can land (Principle II)."""
    world["state"].appliance_ecos["3.NE"]["banners"] = {
        "motd": "keep me",
        "issue": "old",
    }
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "issue: new")
    _write(tmp_path, "fabric/interface-labels/global.yaml", "wan: {}")

    declared = desired.load(default_registry, tmp_path)
    plan = txn.build_plan(world["ctx"], default_registry, declared)

    (blocked,) = plan.blocked_items
    assert blocked.ref.kind == "interface-labels"
    assert blocked.blocked is not None and "not declaratively writable" in blocked.blocked
    assert any("blocked" in w for w in plan.warnings)

    (changed,) = plan.changed_items
    assert changed.ref.kind == "appliance/banners"

    with pytest.raises(txn.CommitError, match="blocked at preflight"):
        txn.commit(world["ctx"], default_registry, plan, world["settings"])


def test_a_wholly_blocked_plan_is_not_no_changes(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """`plan.empty` is true — nothing plannable changed — and that is exactly
    why the exit-code seam looks at blocked items too: "could not do what the
    directory asks" and "nothing to do" must never share an answer."""
    _write(tmp_path, "fabric/interface-labels/global.yaml", "wan: {}")
    declared = desired.load(default_registry, tmp_path)
    plan = txn.build_plan(world["ctx"], default_registry, declared)
    assert plan.empty
    assert plan.blocked_items
    with pytest.raises(txn.CommitError):
        txn.commit(world["ctx"], default_registry, plan, world["settings"])


def test_a_declaration_built_from_a_redacted_export_blocks(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """R12's redacted-secrets case: a spec carrying a redaction marker was
    copied from a masked export, and writing it would replace the real value
    with the mask — on any kind, however capable."""
    _write(
        tmp_path,
        "appliances/BR1-EC/banners/global.yaml",
        "issue: '<redacted:deadbeef>'",
    )
    declared = desired.load(default_registry, tmp_path)
    plan = txn.build_plan(world["ctx"], default_registry, declared)
    (blocked,) = plan.blocked_items
    assert blocked.blocked is not None
    assert "redacted" in blocked.blocked


# -- the acceptance criterion: a declared change verifies ---------------------


def test_a_partial_declaration_applies_completely_and_verifies(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """#126's second criterion, end to end through the real transaction
    engine: a declaration naming one field commits, verifies post-apply, and
    — the half replace semantics failed — the field nobody declared is still
    on the appliance afterwards. This is the mock twin of the live BGP
    lesson, run on the kind whose ledger evidence actually licenses it."""
    state = world["state"]
    state.appliance_ecos["3.NE"]["banners"] = {"motd": "keep me", "issue": "old"}
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "issue: PROPERTY OF ACME")

    declared = desired.load(default_registry, tmp_path)
    plan = txn.build_plan(world["ctx"], default_registry, declared)
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert report.ok, report.messages
    live = state.appliance_ecos["3.NE"]["banners"]
    assert live["issue"] == "PROPERTY OF ACME"
    assert live["motd"] == "keep me", (
        "the undeclared field was erased — the exact failure D7/D8 exist to stop"
    )
