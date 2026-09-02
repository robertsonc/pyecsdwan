"""Appliance extra info — location, contact and overlay settings per appliance.

The scenario this kind was built for: a preconfigured lab where every
appliance has a city and every country is the default. The mock is seeded that
way, so the tests are the fix run for real: set the country, commit, verify,
roll back — and nothing else on the object moves.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any

import pytest
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import ResolveError, Resolver
from pyecsdwan.resources.appliance_info import ApplianceInfo, _body, _prune

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

KIND = "appliance-info"
BR1 = Ref(kind=KIND, name="BR1-EC")  # nePk 3.NE, seeded Toronto / ON / US
HUB1 = Ref(kind=KIND, name="HUB1-EC")  # nePk 1.NE
BR2 = Ref(kind=KIND, name="BR2-EC")  # nePk 5.NE

runner = CliRunner()


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    candidate = CandidateStore(settings.origin)
    return {
        "ctx": ctx,
        "settings": settings,
        "candidate": candidate,
        "state": state,
        "port": base_url.rsplit(":", 1)[1],
    }


def _commit(world: dict[str, Any]) -> txn.CommitReport:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])
    if report.ok:
        world["candidate"].clear()
    return report


def _plan_is_empty(world: dict[str, Any]) -> bool:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    return plan.empty


def _rollback(world: dict[str, Any], n: int = 1) -> txn.CommitReport:
    return txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=n)


def _live(world: dict[str, Any], ne_pk: str = "3.NE") -> dict[str, Any]:
    return world["state"].appliance_extra_info[ne_pk]


def _cli(world: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", world["port"], *args])


# -- registration ---------------------------------------------------------------


def test_registered_with_the_expected_contract() -> None:
    resource = default_registry.get(KIND)
    assert isinstance(resource, ApplianceInfo)
    assert resource.scope is Scope.ORCHESTRATOR
    assert resource.tier is Tier.CURATED
    assert resource.reversibility is Reversibility.REVERSIBLE
    # Every managed appliance has this object; there is no absent state.
    assert resource.deletable is False
    # DELETE exists on the API and is deliberately not covered.
    assert not any(e.startswith("orchestrator DELETE") for e in resource.endpoints)


def test_the_noun_resolves_to_it() -> None:
    assert default_registry.resolve_cli("appliance-info", Scope.ORCHESTRATOR) == KIND


def test_normalize_treats_null_and_empty_string_alike() -> None:
    """The live object (2026-09-02) carries ``null`` for fields nobody set and
    ``""`` for fields someone cleared. Both are "unset" to the canonical form,
    so a clear cannot read as drift against a never-set field."""
    live_shape = {
        "location": {"address": "S2 spoke site", "address2": None, "city": "London",
                     "state": None, "zipCode": None, "country": "US"},
        "contact": {"name": "Ops", "email": None, "phoneNumber": None},
        "overlaySettings": {"ipsecUdpPort": "12000", "isUserDefinedIPSecUDPPort": False},
    }
    cleared = copy.deepcopy(live_shape)
    cleared["location"]["state"] = ""
    resource = ApplianceInfo()
    assert resource.normalize(live_shape) == resource.normalize(cleared) == {
        "location": {"address": "S2 spoke site", "city": "London", "country": "US"},
        "contact": {"name": "Ops"},
        "overlaySettings": {"ipsecUdpPort": "12000", "isUserDefinedIPSecUDPPort": False},
    }
    # And the write carries a null it found back as a null, not as "".
    body = _body(live_shape, resource.normalize(live_shape))
    assert body["location"]["state"] is None
    assert body["contact"]["email"] is None


# -- canonical form -----------------------------------------------------------


def test_normalize_drops_empty_strings_and_the_sections_they_empty() -> None:
    raw = {
        "contact": {"email": "", "name": "", "phoneNumber": ""},
        "location": {"address": "", "city": "Toronto", "country": "US", "zipCode": ""},
        "overlaySettings": {"ipsecUdpPort": "", "isUserDefinedIPSecUDPPort": False},
    }
    canonical = ApplianceInfo().normalize(raw)
    assert canonical == {
        "location": {"city": "Toronto", "country": "US"},
        # False is a value, not an absence.
        "overlaySettings": {"isUserDefinedIPSecUDPPort": False},
    }


def test_normalize_is_idempotent_passes_unknown_fields_and_does_not_mutate() -> None:
    raw = {"location": {"city": "Denver", "what3words": "///x.y.z"}, "futureSection": {"k": "v"}}
    before = copy.deepcopy(raw)
    once = ApplianceInfo().normalize(raw)
    assert once == raw
    assert ApplianceInfo().normalize(once) == once
    assert raw == before


def test_normalize_absent_versus_empty() -> None:
    """None is "no such object"; an all-empty object is the object with
    nothing set, which every managed appliance has."""
    assert ApplianceInfo().normalize(None) is None
    assert ApplianceInfo().normalize({}) == {}
    assert ApplianceInfo().normalize({"contact": {"name": ""}}) == {}


def test_prune_keeps_falsy_non_strings() -> None:
    assert _prune({"a": False, "b": 0, "c": "", "d": {"e": ""}}) == {"a": False, "b": 0}


def test_body_is_the_complete_object_with_explicit_clears() -> None:
    """A field the desired state no longer names goes out as "", never as an
    omission the server may keep; a boolean it dropped is carried, since there
    is no clear for one; an unknown field survives in both directions."""
    current = {
        "location": {"city": "Toronto", "country": "US", "zipCode": "M5V"},
        "overlaySettings": {"isUserDefinedIPSecUDPPort": False},
        "futureSection": {"k": "v"},
    }
    desired = {
        "location": {"city": "Toronto", "country": "Canada"},
        "overlaySettings": {"isUserDefinedIPSecUDPPort": False},
        "futureSection": {"k": "v"},
    }
    assert _body(current, desired) == {
        "location": {"city": "Toronto", "country": "Canada", "zipCode": ""},
        "overlaySettings": {"isUserDefinedIPSecUDPPort": False},
        "futureSection": {"k": "v"},
    }
    # Over nothing: the desired state alone.
    assert _body(None, {"location": {"country": "Canada"}}) == {"location": {"country": "Canada"}}
    # A whole section dropped: every string in it cleared, explicitly.
    assert _body({"contact": {"name": "Ops", "email": "ops@example.com"}}, {}) == {
        "contact": {"name": "", "email": ""}
    }


# -- read side against the mock -------------------------------------------------


def test_fetch_reads_the_seeded_lab_state(world: dict[str, Any]) -> None:
    resource = default_registry.get(KIND)
    canonical = resource.normalize(resource.fetch(world["ctx"], BR1))
    assert canonical == {
        "location": {"city": "Toronto", "state": "ON", "country": "US"},
        "overlaySettings": {"isUserDefinedIPSecUDPPort": False},
    }


def test_fetch_of_an_unknown_appliance_fails_at_resolution(world: dict[str, Any]) -> None:
    with pytest.raises(ResolveError):
        default_registry.get(KIND).fetch(world["ctx"], Ref(kind=KIND, name="NO-SUCH-EC"))


def test_list_refs_enumerates_every_appliance(world: dict[str, Any]) -> None:
    names = {r.name for r in default_registry.get(KIND).list_refs(world["ctx"])}
    assert {"HUB1-EC", "BR1-EC", "BR2-EC"} <= names


def test_write_targets_are_per_appliance(world: dict[str, Any]) -> None:
    resource = default_registry.get(KIND)
    targets = {resource.write_target(world["ctx"], r) for r in (BR1, HUB1, BR2)}
    assert len(targets) == 3
    assert all(t and "extra-info" in t for t in targets)


# -- the fix, run for real ------------------------------------------------------


def test_set_country_commit_verify_and_roll_back(world: dict[str, Any]) -> None:
    """The lab scenario: the city is right, the country is the default. One
    `set` changes the country and nothing else on the object; the commit
    verifies; the rollback puts the default back."""
    candidate: CandidateStore = world["candidate"]
    before = copy.deepcopy(_live(world))
    assert before["location"] == {
        "address": "",
        "address2": "",
        "city": "Toronto",
        "country": "US",
        "state": "ON",
        "zipCode": "",
    }

    candidate.set_path(BR1, ["location", "country"], "Canada")
    report = _commit(world)

    assert report.ok, report.messages
    after = _live(world)
    assert after["location"]["country"] == "Canada"
    assert after["location"]["city"] == "Toronto"
    assert after["location"]["state"] == "ON"
    assert after["contact"] == before["contact"]
    assert after["overlaySettings"] == before["overlaySettings"]
    # Only the named appliance moved.
    assert _live(world, "1.NE")["location"]["country"] == "US"
    assert _live(world, "5.NE")["location"]["country"] == "US"

    # Idempotent: the same intent again is an empty plan.
    candidate.set_path(BR1, ["location", "country"], "Canada")
    assert _plan_is_empty(world)

    restore = _rollback(world)
    assert restore.ok, restore.messages
    assert _live(world)["location"]["country"] == "US"
    assert _live(world)["location"]["city"] == "Toronto"


def test_clearing_a_field_reaches_the_server_as_an_empty_string(world: dict[str, Any]) -> None:
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(BR1, ["location", "state"], "")
    report = _commit(world)

    assert report.ok, report.messages
    assert _live(world)["location"]["state"] == ""
    assert _live(world)["location"]["city"] == "Toronto"

    candidate.set_path(BR1, ["location", "state"], "")
    assert _plan_is_empty(world)

    assert _rollback(world).ok
    assert _live(world)["location"]["state"] == "ON"


def test_a_field_added_by_the_change_is_cleared_on_rollback(world: dict[str, Any]) -> None:
    """The snapshot has no zipCode; the rollback must send one anyway, as "",
    or the server is left to decide what an omitted key means."""
    candidate: CandidateStore = world["candidate"]
    candidate.set_path(BR1, ["location", "zipCode"], "M5V 3L9")
    assert _commit(world).ok
    assert _live(world)["location"]["zipCode"] == "M5V 3L9"

    assert _rollback(world).ok
    assert _live(world)["location"]["zipCode"] == ""


def test_every_appliance_in_one_transaction(world: dict[str, Any]) -> None:
    """Eighteen appliances in the lab; three in the mock. One commit."""
    candidate: CandidateStore = world["candidate"]
    for ref in (HUB1, BR1, BR2):
        candidate.set_path(ref, ["location", "country"], "Canada")
    report = _commit(world)

    assert report.ok, report.messages
    assert len(report.applied) == 3
    countries = {_live(world, pk)["location"]["country"] for pk in ("1.NE", "3.NE", "5.NE")}
    assert countries == {"Canada"}
    assert _live(world, "1.NE")["location"]["city"] == "Denver"

    assert _rollback(world).ok
    assert {_live(world, pk)["location"]["country"] for pk in ("1.NE", "3.NE", "5.NE")} == {"US"}


def test_whole_resource_delete_is_refused_at_plan_time(world: dict[str, Any]) -> None:
    world["candidate"].delete(BR1)
    with pytest.raises(txn.CommitError, match="cannot be deleted as a whole"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    assert _live(world)["location"]["city"] == "Toronto"


def test_rollback_without_a_snapshot_refuses(world: dict[str, Any]) -> None:
    result = ApplianceInfo().rollback(world["ctx"], BR1, None)
    assert result.ok is False
    assert "refusing to guess" in result.message
    assert _live(world)["location"]["city"] == "Toronto"


# -- the command line -------------------------------------------------------------


def test_the_scriptable_cli_stages_and_commits_it(world: dict[str, Any]) -> None:
    """What the operator actually types."""
    staged = _cli(world, "set", "appliance-info", "BR1-EC", "location", "country", "Canada")
    assert staged.exit_code == 0, staged.output
    staged = _cli(world, "set", "appliance-info", "HUB1-EC", "location", "country", "Canada")
    assert staged.exit_code == 0, staged.output

    committed = _cli(world, "commit")
    assert committed.exit_code == 0, committed.output
    assert _live(world, "3.NE")["location"]["country"] == "Canada"
    assert _live(world, "1.NE")["location"]["country"] == "Canada"
    assert _live(world, "5.NE")["location"]["country"] == "US"
