"""Unit + e2e tests for the appliance-common settings resources (Phase 3, #38).

Covers ``appliance/snmp``, ``appliance/logging``, ``appliance/mgmt-services``,
``appliance/banners`` (all ``Scope.APPLIANCE``, written through the appliance
proxy) and ``schedule-timezone`` (``Scope.ORCHESTRATOR``).

Unit half: normalize() defaults / validation / idempotency
(``normalize(normalize(x)) == normalize(x)``), unknown-key passthrough, the
``self``-echo strip on mgmtServices, and no phantom drift between the raw
server shape and hand-authored user intent. Write half (respx, mirroring
tests/test_vrrp.py): the proxy POST plus the mandatory batched save-changes,
and the non-SUCCESS save path failing the operation. E2e half: idempotent
round-trip, apply-persists, rollback and managed_by() through ``txn`` against
the bundled mock.
"""

from __future__ import annotations

import json as _json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, ownership, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref, Reversibility, Scope, Tier
from pyecsdwan.registry import default_registry
from pyecsdwan.resources.common_settings import (
    Banners,
    Logging,
    MgmtServices,
    ScheduleTimezone,
    Snmp,
)

BASE = "https://orch.example.com/gms/rest"
APPLIANCE_URL = f"{BASE}/appliance/rest"
SAVE_URL = f"{BASE}/appliance/saveChanges"
STATUS_URL = f"{BASE}/action/status"
TIMEZONE_URL = f"{BASE}/gms/scheduleTimezone"

#: (resource, ECOS path) for every appliance-scope resource in this module.
APPLIANCE_RESOURCES = [
    (Snmp, "snmp"),
    (Logging, "logging/config"),
    (MgmtServices, "mgmtServices"),
    (Banners, "banners"),
]


class _StubResolver:
    """Resolves any appliance name straight through to a canned nePk."""

    def ne_pk_for(self, name: str) -> str:
        return {"BR1-EC": "3.NE", "HUB1-EC": "1.NE"}.get(name, name)

    def appliance_names(self) -> list[str]:
        return ["HUB1-EC", "BR1-EC"]


def _ctx(settings: Any) -> Ctx:
    return Ctx(client=OrchClient(settings), resolver=_StubResolver())


def _ref(kind: str, appliance: str = "BR1-EC") -> Ref:
    return Ref(kind=kind, name="global", appliance=appliance)


def _mock_save(ok: bool = True, key: str = "k1") -> Any:
    """Wire the appliance-proxy POST + save-changes + action-status trio.

    Returns the proxy route so a caller can inspect the request it captured.
    """
    proxy = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    respx.post(SAVE_URL).mock(return_value=httpx.Response(200, json={"clientKey": key}))
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "guid": key,
                    "nepk": "3.NE",
                    "taskStatus": "Completed" if ok else "Failed",
                    "percentComplete": 100,
                    "completionStatus": ok,
                    "endTime": 1,
                    "result": "ok" if ok else "mock failure",
                }
            ],
        )
    )
    return proxy


# -- sample payloads (shaped from the live captures in the module docstring) --

SNMP_RAW: dict[str, Any] = {
    "access": {"rocommunity": "public"},
    "auto_launch": True,
    "listen": {"enable": True},
    "syscontact": "netops@example.com",
    "sysdescr": "HUB1 EdgeConnect",
    "syslocation": "HQ",
    "traps": {"enable": True, "trap_community": "public"},
    "trapsink": {"sink": {"10.0.0.9": {"self": "10.0.0.9", "version": "v2c"}}},
    "v3": {"users": {}},
    "hash_algs": ["MD5", "SHA"],
    "priv_algs": ["DES", "AES-128"],
}

LOGGING_RAW: dict[str, Any] = {
    "min_priority": "Notice",
    "threshold_size": 50,
    "keep_number": 30,
    "auditlog": "local0",
    "flow": "local1",
    "system": "local2",
    "ids": "local3",
    "logStatefulWanDrops": False,
    "mask_enable": False,
    "mask_ipv4": 24,
    "format_log_enable": False,
}

MGMT_RAW: dict[str, Any] = {
    "aaa": {"self": "aaa", "displayname": "AAA", "srcinf": "mgmt0"},
    "sshd": {"self": "sshd", "displayname": "SSH", "srcinf": "mgmt0"},
    "ntpd": {"self": "ntpd", "displayname": "NTP", "srcinf": ""},
}

BANNERS_RAW: dict[str, Any] = {"motd": "Welcome to HUB1", "issue": "Authorized access only."}

#: One live-shaped raw document per appliance-scope ECOS path.
RAW_BY_PATH: dict[str, dict[str, Any]] = {
    "snmp": SNMP_RAW,
    "logging/config": LOGGING_RAW,
    "mgmtServices": MGMT_RAW,
    "banners": BANNERS_RAW,
}


# == class-level contract =====================================================


def test_kinds_scopes_and_reversibility() -> None:
    for cls, ecos_path in APPLIANCE_RESOURCES:
        res = cls()
        assert res.scope is Scope.APPLIANCE
        assert res.reversibility is Reversibility.REVERSIBLE
        assert res.tier is Tier.CURATED
        # Singleton settings documents: turning a feature off is a field
        # value, never a whole-resource delete.
        assert res.deletable is False
        assert res.ecos_path == ecos_path
        assert res.kind.startswith("appliance/")

    tz = ScheduleTimezone()
    assert tz.scope is Scope.ORCHESTRATOR
    assert tz.kind == "schedule-timezone"
    assert tz.deletable is False


def test_every_kind_is_registered() -> None:
    for cls, _ in APPLIANCE_RESOURCES:
        assert cls().kind in default_registry
    assert ScheduleTimezone().kind in default_registry


# == ownership catalog (#20) ==================================================


def test_ownership_sections_are_wired_for_every_appliance_resource() -> None:
    sections = ownership.KIND_TO_TEMPLATE_SECTIONS
    # Live-confirmed section names (a real Default Template Group's selected
    # section list was probed this session).
    assert sections["appliance/snmp"] == ("snmp",)
    assert sections["appliance/logging"] == ("logging",)
    assert sections["appliance/mgmt-services"] == ("mgmtServices",)
    # UNVERIFIED candidate — "banners" was not in that confirmed list.
    assert sections["appliance/banners"] == ("banners",)
    # Orchestrator-scope config has no template owner.
    assert "schedule-timezone" not in sections


# == normalize: defaults, validation, idempotency =============================


@pytest.mark.parametrize(
    ("cls", "raw"),
    [
        (Snmp, SNMP_RAW),
        (Logging, LOGGING_RAW),
        (MgmtServices, MGMT_RAW),
        (Banners, BANNERS_RAW),
        (ScheduleTimezone, {"defaultTimezone": "US/East-Indiana"}),
    ],
)
def test_normalize_is_idempotent(cls: Any, raw: dict[str, Any]) -> None:
    res = cls()
    once = res.normalize(raw)
    assert res.normalize(once) == once


@pytest.mark.parametrize(
    ("cls", "absent"),
    [(cls, absent) for cls, _ in APPLIANCE_RESOURCES for absent in (None, {})]
    + [(ScheduleTimezone, None), (ScheduleTimezone, {})],
)
def test_normalize_absent_and_empty_agree(cls: Any, absent: Any) -> None:
    """``None`` (absent) and ``{}`` (the proxy's answer for an unseeded path)
    both mean "nothing configured yet" and must normalize identically."""
    res = cls()
    assert res.normalize(absent) == res.normalize(None)
    # A singleton settings document is never "absent" after normalization —
    # it collapses to its documented defaults, not to None.
    assert isinstance(res.normalize(absent), dict)


@pytest.mark.parametrize("cls", [Snmp, Logging, MgmtServices, Banners, ScheduleTimezone])
def test_normalize_rejects_non_mapping(cls: Any) -> None:
    with pytest.raises(ValueError, match="mapping"):
        cls().normalize(["not", "a", "mapping"])


# -- snmp ---------------------------------------------------------------------


def test_snmp_normalize_fills_documented_defaults() -> None:
    assert Snmp().normalize({}) == {
        "access": {"rocommunity": ""},
        "auto_launch": False,
        "listen": {"enable": False},
        "syscontact": "",
        "sysdescr": "",
        "syslocation": "",
        "traps": {"enable": False, "trap_community": ""},
        "trapsink": {"sink": {}},
        "v3": {"users": {}},
    }


def test_snmp_normalize_preserves_live_only_keys() -> None:
    # hash_algs/priv_algs are live-present but absent from the vendored SNMP
    # schema; a full-object replace that dropped them would reset them server
    # side, so they must ride the unknown-key passthrough.
    once = Snmp().normalize(SNMP_RAW)
    assert once["hash_algs"] == ["MD5", "SHA"]
    assert once["priv_algs"] == ["DES", "AES-128"]


def test_snmp_normalize_keeps_trapsink_and_v3_verbatim() -> None:
    # These keyed sub-maps are opaque passthrough on purpose: their self-echo
    # convention was not captured live, so nothing is stripped or injected.
    once = Snmp().normalize(SNMP_RAW)
    assert once["trapsink"]["sink"]["10.0.0.9"] == {"self": "10.0.0.9", "version": "v2c"}


def test_snmp_normalize_does_not_alias_the_input() -> None:
    raw = {"trapsink": {"sink": {"10.0.0.9": {"version": "v2c"}}}}
    once = Snmp().normalize(raw)
    assert isinstance(once, dict)
    once["trapsink"]["sink"]["10.0.0.9"]["version"] = "v3"
    assert raw["trapsink"]["sink"]["10.0.0.9"]["version"] == "v2c"


def test_snmp_normalize_accepts_string_and_int_boolean_spellings() -> None:
    once = Snmp().normalize({"auto_launch": "true", "listen": {"enable": 1}})
    assert once["auto_launch"] is True
    assert once["listen"]["enable"] is True


def test_snmp_normalize_rejects_non_boolean_enable() -> None:
    with pytest.raises(ValueError, match=re.escape("snmp.listen.enable")):
        Snmp().normalize({"listen": {"enable": "yes"}})


def test_snmp_normalize_rejects_non_string_community() -> None:
    with pytest.raises(ValueError, match="rocommunity"):
        Snmp().normalize({"access": {"rocommunity": 42}})


def test_snmp_normalize_rejects_non_mapping_submap() -> None:
    with pytest.raises(ValueError, match=re.escape("snmp.access")):
        Snmp().normalize({"access": "public"})


# -- logging ------------------------------------------------------------------


def test_logging_normalize_fills_spec_defaults() -> None:
    assert Logging().normalize({}) == {
        "min_priority": "Error",
        "auditlog": "local0",
        "flow": "local1",
        "system": "local2",
        "threshold_size": 50,
        "keep_number": 30,
        "logStatefulWanDrops": False,
        "mask_enable": False,
        "mask_ipv4": 24,
        "format_log_enable": False,
    }


def test_logging_normalize_preserves_live_only_ids_key_unvalidated() -> None:
    # 'ids' is live-present but spec-absent: preserved verbatim and NOT
    # validated against the facility enum (nothing confirms it is one).
    assert Logging().normalize(LOGGING_RAW)["ids"] == "local3"
    assert Logging().normalize({"ids": "something-else"})["ids"] == "something-else"


def test_logging_normalize_coerces_numeric_strings() -> None:
    once = Logging().normalize({"threshold_size": "100", "keep_number": "7"})
    assert once["threshold_size"] == 100
    assert once["keep_number"] == 7


@pytest.mark.parametrize(
    ("field_name", "bad"),
    [
        ("min_priority", "Chatty"),
        ("auditlog", "local9"),
        ("flow", "syslog"),
        ("system", ""),
    ],
)
def test_logging_normalize_rejects_bad_enum_values(field_name: str, bad: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        Logging().normalize({field_name: bad})


def test_logging_normalize_rejects_bad_mask_width() -> None:
    with pytest.raises(ValueError, match="mask_ipv4"):
        Logging().normalize({"mask_ipv4": 12})


def test_logging_normalize_rejects_negative_keep_number() -> None:
    with pytest.raises(ValueError, match="keep_number"):
        Logging().normalize({"keep_number": 0})


# -- mgmt services -------------------------------------------------------------


def test_mgmt_services_normalize_strips_self_echo() -> None:
    once = MgmtServices().normalize(MGMT_RAW)
    assert once == {
        "aaa": {"displayname": "AAA", "srcinf": "mgmt0"},
        "ntpd": {"displayname": "NTP", "srcinf": ""},
        "sshd": {"displayname": "SSH", "srcinf": "mgmt0"},
    }
    for service in once.values():
        assert "self" not in service


def test_mgmt_services_normalize_sorts_service_ids() -> None:
    once = MgmtServices().normalize(MGMT_RAW)
    assert list(once) == ["aaa", "ntpd", "sshd"]


def test_mgmt_services_normalize_fills_missing_fields_and_keeps_unknown() -> None:
    once = MgmtServices().normalize({"other": {"self": "other", "someNewKey": 7}})
    assert once == {"other": {"displayname": "", "srcinf": "", "someNewKey": 7}}


def test_mgmt_services_normalize_rejects_non_mapping_service() -> None:
    with pytest.raises(ValueError, match=re.escape("mgmtServices.sshd")):
        MgmtServices().normalize({"sshd": "mgmt0"})


def test_mgmt_services_normalize_rejects_non_string_srcinf() -> None:
    with pytest.raises(ValueError, match="srcinf"):
        MgmtServices().normalize({"sshd": {"srcinf": 0}})


# -- banners -------------------------------------------------------------------


def test_banners_normalize_fills_defaults() -> None:
    assert Banners().normalize({}) == {"issue": "", "motd": ""}


def test_banners_normalize_keeps_whitespace_verbatim() -> None:
    # A banner's exact whitespace is what the operator sees; normalize must
    # not trim or rewrite it (that would be an un-round-trippable edit).
    text = "  line one\n\n  line two  \n"
    assert Banners().normalize({"motd": text})["motd"] == text


def test_banners_normalize_rejects_non_string() -> None:
    with pytest.raises(ValueError, match=re.escape("banners.motd")):
        Banners().normalize({"motd": ["a", "b"]})


# -- schedule timezone ---------------------------------------------------------


def test_schedule_timezone_normalize_strips_surrounding_whitespace() -> None:
    assert ScheduleTimezone().normalize({"defaultTimezone": " UTC "}) == {"defaultTimezone": "UTC"}


def test_schedule_timezone_normalize_tolerates_empty_value() -> None:
    # A value the server itself reported must never make fetch() explode; the
    # refusal lives on the write side instead.
    assert ScheduleTimezone().normalize({}) == {"defaultTimezone": ""}


# == no phantom drift between raw server shape and hand-authored intent =======


@pytest.mark.parametrize(
    ("cls", "server_raw", "user_intent"),
    [
        (
            Snmp,
            SNMP_RAW,
            {**SNMP_RAW, "auto_launch": "true", "listen": {"enable": "true"}},
        ),
        (
            Logging,
            LOGGING_RAW,
            {**LOGGING_RAW, "threshold_size": "50", "keep_number": "30"},
        ),
        (
            MgmtServices,
            MGMT_RAW,
            # A human authoring YAML would not repeat the server's self echoes.
            {sid: {k: v for k, v in cfg.items() if k != "self"} for sid, cfg in MGMT_RAW.items()},
        ),
        (Banners, BANNERS_RAW, dict(BANNERS_RAW)),
        (
            ScheduleTimezone,
            {"defaultTimezone": "UTC"},
            {"defaultTimezone": " UTC "},
        ),
    ],
)
def test_no_phantom_drift(
    cls: Any, server_raw: dict[str, Any], user_intent: dict[str, Any]
) -> None:
    res = cls()
    ref = _ref(res.kind) if res.scope is Scope.APPLIANCE else Ref(kind=res.kind, name="global")
    current = res.normalize(server_raw)
    desired = res.normalize(user_intent)
    assert res.diff(ref, current, desired).empty


# == write side: proxy POST + mandatory batched save-changes ==================


@respx.mock
@pytest.mark.parametrize(("cls", "ecos_path"), APPLIANCE_RESOURCES)
def test_apply_posts_document_then_saves(cls: Any, ecos_path: str, settings: Any) -> None:
    proxy = _mock_save()
    res = cls()
    ref = _ref(res.kind)
    # Apply the live-shaped document onto an appliance sitting at defaults.
    current = res.normalize({})
    desired = res.normalize(RAW_BY_PATH[ecos_path])

    result = res.apply(_ctx(settings), res.diff(ref, current, desired))
    assert result.ok, result.message
    assert "persisted" in result.message

    request = proxy.calls.last.request
    assert request.url.params["nePk"] == "3.NE"
    assert request.url.params["url"] == ecos_path
    assert _json.loads(request.content) == desired
    # Exactly one batched save-changes for the operation.
    assert len(result.jobs) == 1


@respx.mock
@pytest.mark.parametrize(("cls", "_ecos"), APPLIANCE_RESOURCES)
def test_apply_is_noop_on_empty_diff(cls: Any, _ecos: str, settings: Any) -> None:
    proxy = respx.post(APPLIANCE_URL).mock(return_value=httpx.Response(204))
    res = cls()
    state = res.normalize({})
    result = res.apply(_ctx(settings), res.diff(_ref(res.kind), state, state))
    assert result.ok
    assert result.changed is False
    assert proxy.call_count == 0


@respx.mock
@pytest.mark.parametrize(("cls", "ecos_path"), APPLIANCE_RESOURCES)
def test_apply_fails_when_save_changes_fails(cls: Any, ecos_path: str, settings: Any) -> None:
    # The proxy write lands but the running config is never persisted — the
    # operation must report failure, not success (docs/plugin-promotion.md).
    _mock_save(ok=False)
    res = cls()
    ref = _ref(res.kind)
    diff = res.diff(ref, res.normalize({}), res.normalize(RAW_BY_PATH[ecos_path]))
    result = res.apply(_ctx(settings), diff)
    assert not result.ok
    assert "not persisted" in result.message
    assert result.jobs and result.jobs[0].state != "SUCCESS"


@respx.mock
@pytest.mark.parametrize(("cls", "ecos_path"), APPLIANCE_RESOURCES)
def test_rollback_restores_snapshot_then_saves(cls: Any, ecos_path: str, settings: Any) -> None:
    proxy = _mock_save(key="k2")
    res = cls()
    snapshot = RAW_BY_PATH[ecos_path]

    result = res.rollback(_ctx(settings), _ref(res.kind), snapshot)
    assert result.ok, result.message
    assert _json.loads(proxy.calls.last.request.content) == res.normalize(snapshot)


@pytest.mark.parametrize(("cls", "_ecos"), APPLIANCE_RESOURCES)
def test_rollback_refuses_an_absent_snapshot(cls: Any, _ecos: str, settings: Any) -> None:
    # Replaying an absent snapshot as "POST an empty document" would clear the
    # whole settings section instead of restoring it.
    result = cls().rollback(_ctx(settings), _ref(cls().kind), None)
    assert not result.ok
    assert "refusing" in result.message


@respx.mock
def test_schedule_timezone_apply_posts_sdk_body(settings: Any) -> None:
    route = respx.post(TIMEZONE_URL).mock(return_value=httpx.Response(204))
    res = ScheduleTimezone()
    ref = Ref(kind=res.kind, name="global")
    diff = res.diff(
        ref,
        res.normalize({"defaultTimezone": "UTC"}),
        res.normalize({"defaultTimezone": "US/East-Indiana"}),
    )

    result = res.apply(_ctx(settings), diff)
    assert result.ok, result.message
    # SDK body shape, not the spec's bare string.
    assert _json.loads(route.calls.last.request.content) == {"defaultTimezone": "US/East-Indiana"}
    # Orchestrator scope: no appliance save-changes involved.
    assert result.jobs == []


@respx.mock
def test_schedule_timezone_refuses_to_write_empty(settings: Any) -> None:
    route = respx.post(TIMEZONE_URL).mock(return_value=httpx.Response(204))
    res = ScheduleTimezone()
    ref = Ref(kind=res.kind, name="global")
    diff = res.diff(ref, res.normalize({"defaultTimezone": "UTC"}), res.normalize({}))
    result = res.apply(_ctx(settings), diff)
    assert not result.ok
    assert "refusing" in result.message
    assert route.call_count == 0


def test_schedule_timezone_has_no_template_owner(settings: Any) -> None:
    res = ScheduleTimezone()
    assert res.managed_by(_ctx(settings), Ref(kind=res.kind, name="global")) is None


# == e2e against the bundled mock =============================================


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, Any]]:
    pytest.importorskip("pyecsdwan.mock.server")
    from pyecsdwan.mock.server import run_in_thread

    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, Any]) -> dict[str, Any]:
    from pyecsdwan.resolver import Resolver

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
    candidate = CandidateStore(settings.host)
    return {"ctx": ctx, "settings": settings, "candidate": candidate, "state": state}


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


def _appliance(state: Any, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


@pytest.mark.parametrize(("cls", "_ecos"), APPLIANCE_RESOURCES)
def test_e2e_idempotent_round_trip_against_seeded_state(
    cls: Any, _ecos: str, world: dict[str, Any]
) -> None:
    res = cls()
    ref = _ref(res.kind, "HUB1-EC")
    current = res.normalize(res.fetch(world["ctx"], ref))
    assert isinstance(current, dict) and current

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


@pytest.mark.parametrize(("cls", "_ecos"), APPLIANCE_RESOURCES)
def test_e2e_unseeded_appliance_normalizes_to_defaults_and_round_trips(
    cls: Any, _ecos: str, world: dict[str, Any]
) -> None:
    # 5.NE (BR2-EC) is deliberately unseeded for every #38 setting group, so
    # the proxy answers {} and normalize() must produce the documented
    # defaults — and re-planning those defaults must still diff empty.
    res = cls()
    ref = _ref(res.kind, "BR2-EC")
    current = res.normalize(res.fetch(world["ctx"], ref))
    assert current == res.normalize({})

    world["candidate"].set_desired(ref, current)
    assert _plan_is_empty(world)


def test_e2e_snmp_apply_persists_replans_empty_then_rolls_back(world: dict[str, Any]) -> None:
    state = world["state"]
    candidate = world["candidate"]
    ctx = world["ctx"]
    ref = _ref("appliance/snmp", "BR1-EC")  # 3.NE, seeded with SNMP disabled

    before = Snmp().normalize(Snmp().fetch(ctx, ref))
    assert isinstance(before, dict)
    desired = {
        **before,
        "listen": {"enable": True},
        "access": {"rocommunity": "s3cret"},
        "syslocation": "Branch-1 rack 4",
    }
    candidate.set_desired(ref, desired)
    report = _commit(world)
    assert report.ok, report.messages

    stored = state.appliance_ecos["3.NE"]["snmp"]
    assert stored["listen"]["enable"] is True
    assert stored["access"]["rocommunity"] == "s3cret"
    # Live-only keys survived the full-object replace.
    assert stored["hash_algs"] == ["MD5", "SHA"]
    # apply() saved: persisted, not just running config.
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    saves_after_apply = len(state.actions)

    # Idempotency: replanning identical intent is an empty plan and no save.
    candidate.set_desired(ref, desired)
    assert _plan_is_empty(world)
    assert len(state.actions) == saves_after_apply

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert Snmp().normalize(state.appliance_ecos["3.NE"]["snmp"]) == before
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False
    assert len(state.actions) == saves_after_apply + 1


def test_e2e_logging_apply_and_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    ctx = world["ctx"]
    ref = _ref("appliance/logging", "BR1-EC")

    before = Logging().normalize(Logging().fetch(ctx, ref))
    assert isinstance(before, dict)
    world["candidate"].set_desired(ref, {**before, "min_priority": "Debug", "keep_number": 90})
    assert _commit(world).ok

    stored = state.appliance_ecos["3.NE"]["logging/config"]
    assert stored["min_priority"] == "Debug"
    assert stored["keep_number"] == 90
    assert stored["ids"] == "local3"  # spec-absent key preserved

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert Logging().normalize(state.appliance_ecos["3.NE"]["logging/config"]) == before


def test_e2e_mgmt_services_apply_drops_self_echo_on_the_wire(world: dict[str, Any]) -> None:
    state = world["state"]
    ctx = world["ctx"]
    ref = _ref("appliance/mgmt-services", "BR1-EC")

    before = MgmtServices().normalize(MgmtServices().fetch(ctx, ref))
    assert isinstance(before, dict)
    assert state.appliance_ecos["3.NE"]["mgmtServices"]["sshd"]["self"] == "sshd"

    world["candidate"].set_desired(
        ref, {**before, "sshd": {"displayname": "SSH", "srcinf": "lan0"}}
    )
    assert _commit(world).ok

    stored = state.appliance_ecos["3.NE"]["mgmtServices"]
    assert stored["sshd"] == {"displayname": "SSH", "srcinf": "lan0"}
    # The read-side echo is not re-sent (the POST body type has no 'self').
    assert all("self" not in service for service in stored.values())

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert MgmtServices().normalize(state.appliance_ecos["3.NE"]["mgmtServices"]) == before


def test_e2e_banners_apply_preserves_exact_text(world: dict[str, Any]) -> None:
    state = world["state"]
    ref = _ref("appliance/banners", "BR1-EC")
    motd = "  Maintenance window Sat 02:00-04:00 UTC\n\n"

    world["candidate"].set_desired(ref, {"motd": motd, "issue": "Authorized users only."})
    assert _commit(world).ok
    assert state.appliance_ecos["3.NE"]["banners"]["motd"] == motd


def test_e2e_schedule_timezone_apply_replan_and_rollback(world: dict[str, Any]) -> None:
    state = world["state"]
    ctx = world["ctx"]
    ref = Ref(kind="schedule-timezone", name="global")
    assert state.schedule_timezone == {"defaultTimezone": "UTC"}

    world["candidate"].set_desired(ref, {"defaultTimezone": "US/East-Indiana"})
    assert _commit(world).ok
    assert state.schedule_timezone == {"defaultTimezone": "US/East-Indiana"}

    world["candidate"].set_desired(ref, {"defaultTimezone": "US/East-Indiana"})
    assert _plan_is_empty(world)

    restore = txn.rollback_history_txn(ctx, default_registry, world["settings"], n=1)
    assert restore.ok, restore.messages
    assert state.schedule_timezone == {"defaultTimezone": "UTC"}


def test_e2e_whole_resource_delete_is_refused(world: dict[str, Any]) -> None:
    # deletable=False: there is no "SNMP does not exist" state, and POSTing an
    # empty document would wipe the section rather than delete the resource.
    world["candidate"].delete(_ref("appliance/snmp", "BR1-EC"))
    with pytest.raises(txn.CommitError, match="singleton"):
        txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()


# -- managed_by(): both the no-template and template-owned paths --------------


@pytest.mark.parametrize(
    ("cls", "section"),
    [
        (Snmp, "snmp"),
        (Logging, "logging"),
        (MgmtServices, "mgmtServices"),
        (Banners, "banners"),
    ],
)
def test_e2e_managed_by_reports_owning_template_group(
    cls: Any, section: str, world: dict[str, Any]
) -> None:
    state = world["state"]
    ctx = world["ctx"]
    res = cls()

    # No association yet: nothing owns this section.
    assert res.managed_by(ctx, _ref(res.kind, "BR1-EC")) is None

    state.template_groups["NetStd"] = {"name": "NetStd", "templates": []}
    state.template_selection["NetStd"] = [section, "dns"]
    state.template_association["3.NE"] = ["NetStd"]

    assert res.managed_by(ctx, _ref(res.kind, "BR1-EC")) == "template-group NetStd"
    # An unassociated appliance is still unowned.
    assert res.managed_by(ctx, _ref(res.kind, "BR2-EC")) is None


def test_e2e_plan_warns_on_template_owned_section(world: dict[str, Any]) -> None:
    state = world["state"]
    state.template_groups["NetStd"] = {"name": "NetStd", "templates": []}
    state.template_selection["NetStd"] = ["snmp"]
    state.template_association["3.NE"] = ["NetStd"]

    ref = _ref("appliance/snmp", "BR1-EC")
    current = Snmp().normalize(Snmp().fetch(world["ctx"], ref))
    assert isinstance(current, dict)
    world["candidate"].set_desired(ref, {**current, "syslocation": "moved"})

    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    world["candidate"].clear()
    assert any("managed-by: template-group NetStd" in w for w in plan.warnings)


def test_managed_by_requires_an_appliance_on_the_ref(settings: Any) -> None:
    with pytest.raises(ValueError, match="appliance"):
        Snmp().managed_by(_ctx(settings), Ref(kind="appliance/snmp", name="global"))


# -- optional live smoke test --------------------------------------------------


@pytest.mark.live
def test_live_read_only_smoke() -> None:
    """Read-only: fetch+normalize each setting group from a real Orchestrator.

    Gated on ``ECSDWAN_ORCH_URL``; credentials come from the normal config
    chain (env/keyring), never from this file. Nothing here writes.
    """
    import os

    orch_url = os.environ.get("ECSDWAN_ORCH_URL")
    if not orch_url:
        pytest.skip("ECSDWAN_ORCH_URL not set")
    appliance = os.environ.get("ECSDWAN_LIVE_APPLIANCE")
    if not appliance:
        pytest.skip("ECSDWAN_LIVE_APPLIANCE not set")

    from pyecsdwan.resolver import Resolver

    settings = config.Settings.load()
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))

    for cls, _ecos in APPLIANCE_RESOURCES:
        res = cls()
        ref = _ref(res.kind, appliance)
        once = res.normalize(res.fetch(ctx, ref))
        assert res.normalize(once) == once

    tz = ScheduleTimezone()
    tz_ref = Ref(kind=tz.kind, name="global")
    once_tz = tz.normalize(tz.fetch(ctx, tz_ref))
    assert tz.normalize(once_tz) == once_tz
