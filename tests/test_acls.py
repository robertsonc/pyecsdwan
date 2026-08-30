"""Tests for the ACL / IP-object / AppExpress resources (#31).

Covers the acceptance criteria: idempotent normalize() on all five kinds, the
``GET dependency/acl/{name}`` removal pre-flight (a named error instead of a
raw API rejection), managed_by() preferring the per-rule ``gms_marked`` flag
over the template-section join, and e2e round-trips through ``txn`` against
the bundled mock (idempotent replan, apply, rollback) — mirroring
``tests/test_routes.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from pyecsdwan import config, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ref
from pyecsdwan.journal import TxnState
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.acls import (
    AclInUseError,
    Acls,
    AppExpressAssociation,
    AppExpressGroup,
    IpAddressGroup,
    IpServiceGroup,
    _dependency_users,
    _inject_acl_self,
)
from pyecsdwan.txn import CommitError

ACL_REF = Ref(kind="appliance/acl", name="global", appliance="BR1-EC")
AG_REF = Ref(kind="ip-address-group", name="Branch-Nets")
SG_REF = Ref(kind="ip-service-group", name="Web")
AX_REF = Ref(kind="app-express-group", name="SaaS")
AXA_REF = Ref(kind="app-express-association", name="global")
NE_PK = "3.NE"


# -- ACL normalize: pure function, no ctx/network involved ---------------------


def test_acl_normalize_none_yields_empty_table() -> None:
    assert Acls().normalize(None) == {"acl": {}}


def test_acl_normalize_strips_self_gms_marked_and_derived_maps() -> None:
    raw = {
        "MyACL": {
            "entry": {
                "1000": {
                    "self": 1000,
                    "gms_marked": True,
                    "comment": "",
                    "permit": True,
                    "application": "ftp",
                }
            },
            "qmap": {"someQosMap": 1},
            "rmap": {"someRouteMap": 1},
        }
    }
    once = Acls().normalize(raw)
    assert isinstance(once, dict)
    acl = once["acl"]["MyACL"]
    rule = acl["entry"]["1000"]
    assert "self" not in rule
    assert "gms_marked" not in rule
    # qmap/rmap are server-derived reference info, never user intent.
    assert "qmap" not in acl
    assert "rmap" not in acl
    assert rule["application"] == "ftp"
    # Idempotent round-trip (the Tier-2 promotion checklist's litmus test).
    assert Acls().normalize(once) == once


def test_acl_normalize_sorts_names_and_priorities_numerically() -> None:
    raw = {
        "Zeta": {"entry": {}},
        "Alpha": {"entry": {"1010": {}, "200": {}, "1000": {}}},
    }
    once = Acls().normalize(raw)
    assert isinstance(once, dict)
    assert list(once["acl"]) == ["Alpha", "Zeta"]
    assert list(once["acl"]["Alpha"]["entry"]) == ["200", "1000", "1010"]


def test_acl_normalize_passes_unknown_fields_through() -> None:
    once = Acls().normalize({"A": {"entry": {"10": {"tbehavior": "Voice"}}, "future": 7}})
    assert isinstance(once, dict)
    assert once["acl"]["A"]["future"] == 7
    assert once["acl"]["A"]["entry"]["10"]["tbehavior"] == "Voice"


def test_acl_normalize_canonicalizes_leading_zero_priorities() -> None:
    once = Acls().normalize({"A": {"entry": {"0100": {"permit": True}}}})
    assert isinstance(once, dict)
    assert list(once["acl"]["A"]["entry"]) == ["100"]


def test_acl_normalize_rejects_duplicate_priority_after_canonicalization() -> None:
    with pytest.raises(ValueError, match="duplicate priority"):
        Acls().normalize({"A": {"entry": {"0100": {}, "100": {}}}})


def test_acl_normalize_rejects_non_mapping_entry() -> None:
    with pytest.raises(ValueError, match="entry must be a mapping"):
        Acls().normalize({"A": {"entry": "nope"}})


def test_acl_normalize_rejects_non_mapping_rule() -> None:
    with pytest.raises(ValueError, match="must be a mapping of rule fields"):
        Acls().normalize({"A": {"entry": {"10": "nope"}}})


def test_acl_normalize_accepts_wrapped_canonical_state() -> None:
    once = Acls().normalize({"A": {"entry": {"10": {"permit": True}}}})
    assert Acls().normalize(once) == once


def test_inject_acl_self_echoes_priority_on_rules_only() -> None:
    payload = _inject_acl_self({"A": {"entry": {"1000": {"permit": True}}}})
    assert payload["A"]["entry"]["1000"]["self"] == 1000
    # The captured live shape carries no ACL-level `self`; none is invented.
    assert "self" not in payload["A"]


def test_acl_ne_pk_requires_appliance_on_ref() -> None:
    with pytest.raises(ValueError, match="appliance-scoped"):
        Acls._ne_pk(None, Ref(kind="appliance/acl", name="global"))  # type: ignore[arg-type]


# -- dependency-response parsing (shape-tolerant, fails closed) ----------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ({}, []),
        ([], []),
        ({"rmap": {}, "qmap": {}}, []),
        ({"rmap": ["RM1", "RM0"]}, ["rmap RM0", "rmap RM1"]),
        ({"qmap": {"QM1": {}}}, ["qmap QM1"]),
        (["RouteMapA"], ["RouteMapA"]),
        ({"inUse": True}, ["inUse"]),
        ({"inUse": False}, []),
    ],
)
def test_dependency_users_parsing(raw: Any, expected: list[str]) -> None:
    assert _dependency_users(raw) == expected


def test_dependency_users_fails_closed_on_unparseable_payload() -> None:
    # Anything non-empty it cannot flatten still counts as a reference, so the
    # pre-flight never green-lights a removal it did not understand.
    assert _dependency_users("something-unexpected") == ["something-unexpected"]
    assert _dependency_users({"weird": 5}) == ["weird 5"]


# -- ip-object group normalize -------------------------------------------------


def test_address_group_normalize_sorts_and_fills_defaults() -> None:
    once = IpAddressGroup().normalize(
        {"name": "AG1", "type": "AG", "rules": [{"includedIPs": ["10.2.0.0/16", "10.1.0.0/16"]}]}
    )
    assert isinstance(once, dict)
    rule = once["rules"][0]
    assert rule["includedIPs"] == ["10.1.0.0/16", "10.2.0.0/16"]
    assert rule["excludedIPs"] == []
    assert rule["includedGroups"] == []
    assert rule["comment"] == ""
    assert once["type"] == "AG"
    assert IpAddressGroup().normalize(once) == once


def test_address_group_normalize_sorts_rules_stably() -> None:
    unsorted_rules = [
        {"includedIPs": ["10.9.0.0/16"], "comment": "z"},
        {"includedIPs": ["10.1.0.0/16"], "comment": "a"},
    ]
    once = IpAddressGroup().normalize({"name": "AG1", "rules": unsorted_rules})
    twice = IpAddressGroup().normalize({"name": "AG1", "rules": list(reversed(unsorted_rules))})
    # Canonical lists must be stably sorted: structural_diff compares them
    # positionally, so two orderings of the same rules must converge.
    assert once == twice


def test_address_group_normalize_requires_name() -> None:
    with pytest.raises(ValueError, match="'name'"):
        IpAddressGroup().normalize({"rules": []})


def test_address_group_normalize_absent_is_none() -> None:
    assert IpAddressGroup().normalize(None) is None
    assert IpAddressGroup().normalize({}) is None


def test_service_group_normalize_requires_protocol_per_rule() -> None:
    with pytest.raises(ValueError, match="required field 'protocol'"):
        IpServiceGroup().normalize({"name": "SG1", "rules": [{"includedPorts": ["443"]}]})


def test_service_group_normalize_fills_icmp_and_port_defaults() -> None:
    once = IpServiceGroup().normalize(
        {"name": "SG1", "rules": [{"protocol": "TCP", "includedPorts": ["8002", "443"]}]}
    )
    assert isinstance(once, dict)
    rule = once["rules"][0]
    assert rule["includedPorts"] == ["443", "8002"]
    assert rule["icmpTypes"] == []
    assert rule["icmpCodes"] == []
    assert rule["excludedGroups"] == []
    assert once["type"] == "SG"
    assert IpServiceGroup().normalize(once) == once


def test_group_rules_must_be_a_list() -> None:
    with pytest.raises(ValueError, match="rules must be a list"):
        IpAddressGroup().normalize({"name": "AG1", "rules": {"nope": 1}})


# -- app-express normalize -----------------------------------------------------


def test_app_express_group_normalize_fills_spec_defaults_and_sorts() -> None:
    once = AppExpressGroup().normalize(
        {
            "name": "SaaS",
            "appExpressApps": ["zoom", "office365"],
            "sourceLoopbacks": [
                {"loopbackName": "lo2", "segmentName": "default"},
                {"loopbackName": "lo1", "segmentName": "default"},
            ],
        }
    )
    assert isinstance(once, dict)
    assert once["targetQoE"] == "EXCELLENT"  # spec-declared default
    assert once["useSystemDnsServer"] is False
    assert once["appExpressApps"] == ["office365", "zoom"]
    assert once["dnsServers"] == []
    assert [lb["loopbackName"] for lb in once["sourceLoopbacks"]] == ["lo1", "lo2"]
    assert AppExpressGroup().normalize(once) == once


def test_app_express_group_normalize_rejects_unknown_target_qoe() -> None:
    with pytest.raises(ValueError, match="targetQoE must be one of"):
        AppExpressGroup().normalize({"name": "SaaS", "targetQoE": "SUPERB"})


def test_app_express_group_normalize_upcases_target_qoe() -> None:
    once = AppExpressGroup().normalize({"name": "SaaS", "targetQoE": "fair"})
    assert isinstance(once, dict)
    assert once["targetQoE"] == "FAIR"


def test_app_express_association_normalize_sorts_and_wraps() -> None:
    once = AppExpressAssociation().normalize(
        [
            {"nePk": "3.NE", "appExpressGroupName": "SaaS"},
            {"nePk": "1.NE", "appExpressGroupName": "SaaS"},
        ]
    )
    assert once == {
        "associations": [
            {"nePk": "1.NE", "appExpressGroupName": "SaaS"},
            {"nePk": "3.NE", "appExpressGroupName": "SaaS"},
        ]
    }
    assert AppExpressAssociation().normalize(once) == once


def test_app_express_association_normalize_requires_both_fields() -> None:
    with pytest.raises(ValueError, match="'appExpressGroupName'"):
        AppExpressAssociation().normalize([{"nePk": "1.NE"}])
    with pytest.raises(ValueError, match="'nePk'"):
        AppExpressAssociation().normalize([{"appExpressGroupName": "SaaS"}])


# -- registration / declared properties ---------------------------------------


def test_registered_kinds_and_flags() -> None:
    acl = default_registry.get("appliance/acl")
    assert acl.deletable is False
    assert acl.dependencies == ("ip-address-group", "ip-service-group")
    assert default_registry.get("ip-address-group").deletable is True
    assert default_registry.get("ip-service-group").deletable is True
    assert default_registry.get("app-express-group").deletable is True
    assoc = default_registry.get("app-express-association")
    assert assoc.deletable is False
    assert assoc.dependencies == ("app-express-group",)


def test_association_applies_after_its_group() -> None:
    ordered = default_registry.order_refs([AXA_REF, AX_REF])
    assert [r.kind for r in ordered] == ["app-express-group", "app-express-association"]


def test_group_removal_applies_after_the_acl_that_points_at_it() -> None:
    ordered = default_registry.order_refs(
        [AG_REF, ACL_REF], deletes={AG_REF.key(), ACL_REF.key()}
    )
    assert [r.kind for r in ordered] == ["appliance/acl", "ip-address-group"]


# -- e2e against the bundled mock ---------------------------------------------


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
    return {"ctx": ctx, "settings": settings, "state": state, "candidate": candidate}


def _appliance(state: MockState, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


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


def test_acl_fetch_reads_seeded_table(world: dict[str, Any]) -> None:
    raw = Acls().fetch(world["ctx"], ACL_REF)
    assert isinstance(raw, dict)
    assert set(raw) == {"Overlay_BulkApps", "Overlay_CriticalApps"}


@pytest.mark.parametrize(
    ("ref", "resource_kind"),
    [
        (ACL_REF, "appliance/acl"),
        (AG_REF, "ip-address-group"),
        (SG_REF, "ip-service-group"),
        (AX_REF, "app-express-group"),
        (AXA_REF, "app-express-association"),
    ],
)
def test_idempotent_replan_is_empty(
    world: dict[str, Any], ref: Ref, resource_kind: str
) -> None:
    ctx, candidate = world["ctx"], world["candidate"]
    resource = default_registry.get(resource_kind)
    assert _plan_is_empty(world)  # nothing staged
    current = resource.normalize(resource.fetch(ctx, ref))
    assert isinstance(current, dict)
    candidate.set_desired(ref, current)
    assert _plan_is_empty(world)  # staging exactly the server's own state


def test_add_acl_then_rollback_restores_the_table(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(ACL_REF, ["acl", "NewACL", "entry", "1000", "permit"], True)

    report = _commit(world)
    assert report.ok, report.messages
    table = state.acls[NE_PK]
    assert "NewACL" in table
    assert table["NewACL"]["entry"]["1000"]["self"] == 1000  # self echo re-injected
    assert "Overlay_BulkApps" in table  # untouched by the add
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert "NewACL" not in state.acls[NE_PK]
    assert "Overlay_BulkApps" in state.acls[NE_PK]
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False


def test_modify_acl_rule_then_rollback(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(
        ACL_REF, ["acl", "Overlay_BulkApps", "entry", "1000", "application"], "sftp"
    )
    report = _commit(world)
    assert report.ok, report.messages
    assert state.acls[NE_PK]["Overlay_BulkApps"]["entry"]["1000"]["application"] == "sftp"

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.acls[NE_PK]["Overlay_BulkApps"]["entry"]["1000"]["application"] == "ftp"


def test_delete_acl_entry_then_rollback(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.delete(ACL_REF, ["acl", "Overlay_CriticalApps"])
    report = _commit(world)
    assert report.ok, report.messages
    assert "Overlay_CriticalApps" not in state.acls[NE_PK]
    assert "Overlay_BulkApps" in state.acls[NE_PK]

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert "Overlay_CriticalApps" in state.acls[NE_PK]


def test_whole_acl_table_delete_is_refused(world: dict[str, Any]) -> None:
    world["candidate"].delete(ACL_REF)  # no path -> whole-resource delete
    with pytest.raises(CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


def test_acl_failed_save_fails_apply_and_reverts(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(ACL_REF, ["acl", "NewACL", "entry", "1000", "permit"], True)
    state.fail_next_action = True  # consumed by the apply's own save-changes

    report = _commit(world)
    assert not report.ok
    assert report.state == TxnState.REVERTED
    assert "NewACL" not in state.acls[NE_PK]  # revert's compensating write landed
    assert "Overlay_BulkApps" in state.acls[NE_PK]
    assert _appliance(state, NE_PK)["hasUnsavedChanges"] is False


# -- the dependency pre-flight -------------------------------------------------


def test_dependency_lookup_reads_the_appliance_endpoint(world: dict[str, Any]) -> None:
    state = world["state"]
    state.acl_dependencies[NE_PK]["Overlay_BulkApps"] = {"rmap": ["RM-Bulk"]}
    assert Acls().acl_dependencies(world["ctx"], ACL_REF, "Overlay_BulkApps") == [
        "rmap RM-Bulk"
    ]
    assert Acls().acl_dependencies(world["ctx"], ACL_REF, "Overlay_CriticalApps") == []


def test_removing_an_in_use_acl_is_refused_at_plan_time(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    state.acl_dependencies[NE_PK]["Overlay_BulkApps"] = {"rmap": ["RM-Bulk"]}
    candidate.delete(ACL_REF, ["acl", "Overlay_BulkApps"])

    with pytest.raises(AclInUseError) as excinfo:
        txn.build_plan(world["ctx"], default_registry, candidate)
    message = str(excinfo.value)
    assert "Overlay_BulkApps" in message
    assert "RM-Bulk" in message
    assert "delDependent" in message
    # Nothing was written: the refusal happens before any transaction opens.
    assert "Overlay_BulkApps" in state.acls[NE_PK]


def test_removing_an_unreferenced_acl_is_allowed(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    state.acl_dependencies[NE_PK]["Overlay_CriticalApps"] = {"rmap": [], "qmap": {}}
    candidate.delete(ACL_REF, ["acl", "Overlay_CriticalApps"])
    report = _commit(world)
    assert report.ok, report.messages
    assert "Overlay_CriticalApps" not in state.acls[NE_PK]


def test_staged_del_dependent_permits_the_removal(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    state.acl_dependencies[NE_PK]["Overlay_BulkApps"] = {"rmap": ["RM-Bulk"]}
    candidate.delete(ACL_REF, ["acl", "Overlay_BulkApps"])
    candidate.set_path(ACL_REF, ["delDependent"], True)

    report = _commit(world)
    assert report.ok, report.messages
    assert "Overlay_BulkApps" not in state.acls[NE_PK]


def test_del_dependent_directive_never_shows_as_drift(world: dict[str, Any]) -> None:
    # The directive is operator intent, not server state: staging it alone
    # must not diff, or post-apply verify() would report permanent drift.
    world["candidate"].set_path(ACL_REF, ["delDependent"], True)
    assert _plan_is_empty(world)


# -- managed_by: gms_marked preferred over the template-section join -----------


def test_acl_managed_by_unowned_when_no_group_is_associated(world: dict[str, Any]) -> None:
    """No associated template group is the one negative that holds whatever the
    section names are: ownership needs a group, and there is none (#20)."""
    owns = Acls().managed_by(world["ctx"], ACL_REF)
    assert owns.state is Owned.UNOWNED
    assert not owns.blocks_write


def test_acl_managed_by_prefers_gms_marked(world: dict[str, Any]) -> None:
    state = world["state"]
    state.acls[NE_PK]["Overlay_BulkApps"]["entry"]["1000"]["gms_marked"] = True
    owns = Acls().managed_by(world["ctx"], ACL_REF)
    assert owns.state is Owned.OWNED
    assert "gms_marked" in owns.owner


def test_acl_managed_by_falls_back_to_template_section(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["Branch-Std"] = {"name": "Branch-Std", "templates": []}
    state.template_selection["Branch-Std"] = ["acls"]
    state.template_association[NE_PK] = ["Branch-Std"]
    owns = Acls().managed_by(world["ctx"], ACL_REF)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Branch-Std"


# -- orchestrator-scope e2e ----------------------------------------------------


def test_create_address_group_then_rollback_removes_it(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    ref = Ref(kind="ip-address-group", name="DC-Nets")
    candidate.set_desired(ref, {"rules": [{"includedIPs": ["172.16.0.0/12"]}]})

    report = _commit(world)
    assert report.ok, report.messages
    created = state.address_groups["DC-Nets"]
    assert created["type"] == "AG"  # discriminator supplied by the resource
    assert created["rules"][0]["includedIPs"] == ["172.16.0.0/12"]
    assert "Branch-Nets" in state.address_groups  # untouched

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert "DC-Nets" not in state.address_groups
    assert "Branch-Nets" in state.address_groups


def test_modify_address_group_then_rollback_restores_it(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_desired(
        AG_REF, {"rules": [{"includedIPs": ["10.1.0.0/16"], "comment": "trimmed"}]}
    )
    report = _commit(world)
    assert report.ok, report.messages
    assert state.address_groups["Branch-Nets"]["rules"][0]["comment"] == "trimmed"

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.address_groups["Branch-Nets"]["rules"][0]["comment"] == "branch subnets"


def test_delete_service_group_then_rollback_recreates_it(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.delete(SG_REF)
    report = _commit(world)
    assert report.ok, report.messages
    assert "Web" not in state.service_groups

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.service_groups["Web"]["rules"][0]["protocol"] == "TCP"


def test_service_group_missing_protocol_fails_before_any_write(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_desired(
        Ref(kind="ip-service-group", name="Bad"), {"rules": [{"includedPorts": ["1"]}]}
    )
    with pytest.raises(ValueError, match="required field 'protocol'"):
        txn.build_plan(world["ctx"], default_registry, candidate)
    assert "Bad" not in state.service_groups


def test_ip_object_list_refs(world: dict[str, Any]) -> None:
    assert [r.name for r in IpAddressGroup().list_refs(world["ctx"])] == ["Branch-Nets"]
    assert [r.name for r in IpServiceGroup().list_refs(world["ctx"])] == ["Web"]


def test_app_express_group_update_then_rollback(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    candidate.set_path(AX_REF, ["targetQoE"], "FAIR")
    report = _commit(world)
    assert report.ok, report.messages
    assert state.app_express_groups["saas"]["targetQoE"] == "FAIR"

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.app_express_groups["saas"]["targetQoE"] == "EXCELLENT"


def test_app_express_group_list_refs_uses_real_casing(world: dict[str, Any]) -> None:
    # The table is keyed by the lower-cased name; the config body carries the
    # real casing, which is what a ref must use.
    assert [r.name for r in AppExpressGroup().list_refs(world["ctx"])] == ["SaaS"]


def test_create_group_and_association_in_one_changeset(world: dict[str, Any]) -> None:
    candidate, state = world["candidate"], world["state"]
    new_group = Ref(kind="app-express-group", name="Voice")
    candidate.set_desired(new_group, {"targetQoE": "FAIR", "appExpressApps": ["teams"]})
    candidate.set_desired(
        AXA_REF,
        {
            "associations": [
                {"nePk": "1.NE", "appExpressGroupName": "SaaS"},
                {"appliance": "BR1-EC", "appExpressGroupName": "Voice"},
            ]
        },
    )

    report = _commit(world)
    assert report.ok, report.messages
    # Ordering: the group exists before the association that names it.
    assert report.applied.index(new_group.key()) < report.applied.index(AXA_REF.key())
    assert state.app_express_groups["voice"]["targetQoE"] == "FAIR"
    # The hostname was resolved to a nePk by canonicalize_desired.
    assert {"nePk": "3.NE", "appExpressGroupName": "Voice"} in state.app_express_associations

    restore = txn.rollback_history_txn(world["ctx"], default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert "voice" not in state.app_express_groups
    assert state.app_express_associations == [{"nePk": "1.NE", "appExpressGroupName": "SaaS"}]


def test_whole_association_table_delete_is_refused(world: dict[str, Any]) -> None:
    world["candidate"].delete(AXA_REF)
    with pytest.raises(CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])


def test_association_rollback_refuses_an_absent_snapshot(world: dict[str, Any]) -> None:
    result = AppExpressAssociation().rollback(world["ctx"], AXA_REF, None)
    assert result.ok is False
    assert "refusing" in result.message


def test_acl_rollback_refuses_an_absent_snapshot(world: dict[str, Any]) -> None:
    result = Acls().rollback(world["ctx"], ACL_REF, None)
    assert result.ok is False
    assert "refusing" in result.message


# -- optional live smoke test (read-only) -------------------------------------


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("ECSDWAN_ORCH_URL") or not os.environ.get("ECSDWAN_API_KEY"),
    reason="needs ECSDWAN_ORCH_URL + ECSDWAN_API_KEY against a real Orchestrator",
)
def test_live_ip_objects_read_only_normalize_is_a_fixed_point() -> None:
    """Read-only: GET the real orchestrator-scope collections and prove
    normalize() is a fixed point on whatever shape comes back.

    These are the spec-derived kinds (empty on the lab this session), so this
    is the test that would catch a shape mismatch first. Never writes. Run
    with ``pytest -m live``.
    """
    ctx = Ctx(client=OrchClient(config.settings_from_env()), resolver=None)  # type: ignore[arg-type]
    for resource in (IpAddressGroup(), IpServiceGroup(), AppExpressGroup()):
        for ref in resource.list_refs(ctx):
            once = resource.normalize(resource.fetch(ctx, ref))
            assert resource.normalize(once) == once
    assoc = AppExpressAssociation()
    once_assoc = assoc.normalize(assoc.fetch(ctx, AXA_REF))
    assert assoc.normalize(once_assoc) == once_assoc
