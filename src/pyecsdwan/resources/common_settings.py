"""Appliance-common settings — SNMP, logging, management services, banners,
and the Orchestrator schedule timezone (Phase 3, #38).

These are the "device housekeeping" knobs the Orchestrator UI normally manages
*through templates*. Modeling them as first-class resources is only half the
point: the other half is ``managed_by()``, so an operator who sets one of them
per-appliance gets a template-conflict warning up front instead of watching the
next template push silently revert the change. See the ownership section below.

Why one module for five resources
---------------------------------
Every setting group here is a *singleton settings object*: one flat-ish JSON
document per appliance (or per Orchestrator, for the timezone), read with a
GET, replaced wholesale with a POST. They share the same plumbing almost
exactly — enough that a shared base class (:class:`_ApplianceSetting`) carries
``fetch``/``apply``/``rollback``/``list_refs``/``managed_by`` and each concrete
resource contributes only its ECOS path and its ``normalize()``. Splitting them
across five files would have duplicated that plumbing five times; keeping them
together makes the one thing that actually differs — the canonical shape —
easy to read side by side.

Endpoint correction (verified against both vendored specs + a read-only live
probe this session)
------------------------------------------------------------------------
Issue #38 named Orchestrator paths, but on the **orchestrator** API these are
GET-only: ``/snmp``, ``/logging``, ``/banners``, ``/mgmtServices``,
``/dnsProxy/config``, ``/appliance/dnsCache/config``
(``specs/orchestrator-openapi-7.2.0.json`` declares only ``get`` for each).
The write surface is the **appliance (ECOS) API through the Orchestrator
proxy** — ``GET/POST /appliance/rest?nePk=<pk>&url=<ecosPath>`` — the same
channel ``resources/routes.py``, ``resources/vrrp.py`` and
``resources/appliance_zones.py`` use. ``specs/appliance-openapi-7.2.0.json``
declares ``get``+``post`` for ``/snmp``, ``/banners``, ``/logging/config``,
``/logging/remote``, ``/mgmtServices``, ``/dnsProxy/config`` and
``/dnsCache/config``. So four of the five resources here are
``Scope.APPLIANCE``.

The one genuinely Orchestrator-writable member of the group is
``/gms/scheduleTimezone`` (GET + POST), which is why
:class:`ScheduleTimezone` is ``Scope.ORCHESTRATOR`` and — being Orchestrator
state, not appliance running-config — never calls ``ctx.save_changes()``.

Live-captured shapes (read-only, this session — all populated, high
confidence)
-----------------------------------------------------------------------
``snmp`` (10 top-level keys)::

    {"access": {"rocommunity": "", ...}, "hash_algs": ..., "listen": {...},
     "priv_algs": ..., "syscontact": ..., "sysdescr": ..., "syslocation": ...,
     "traps": {...}, ...}

``banners``::

    {"motd": "", "issue": ""}

``logging/config`` (11 top-level keys)::

    {"min_priority": "Notice", "threshold_size": ..., "keep_number": ...,
     "auditlog": ..., "flow": ..., "system": ..., "ids": ...,
     "logStatefulWanDrops": ..., ...}

``mgmtServices`` (9 keys, note the ``self`` echoes)::

    {"aaa": {"self": "aaa", "displayname": ..., "srcinf": ...},
     "dhcrelay": {...}, "netflowd": {...}, "node": {...}, "ntpd": {...},
     "other": {...}, "snmpd": {...}, "sshd": {...}, ...}

Orchestrator ``/gms/scheduleTimezone``::

    {"defaultTimezone": "UTC"}

Spec-vs-live divergence, recorded rather than papered over:

* ``snmp`` carries ``hash_algs`` and ``priv_algs`` live; neither appears in
  the vendored ``SNMP`` schema. They ride the unknown-key passthrough.
* ``logging/config`` carries ``ids`` live; the vendored ``LoggingConfig``
  schema does not declare it. Same passthrough — it is *not* validated
  against the facility enum, because nothing confirms it is one.
* ``POST /gms/scheduleTimezone``'s request body is typed ``string`` in the
  Orchestrator spec, but the vendored SDK
  (``pyedgeconnect/orch/_schedule_timezone.py``) posts
  ``{"defaultTimezone": tz}``. This resource follows the SDK — that is the
  shape a working client actually sends, and it round-trips with the GET.

Every resource here does a **full-object replace**, so unknown top-level keys
are preserved verbatim by ``normalize()``: dropping one would silently reset
server config on the next POST. This is the same read-modify-write contract
``resources/internal_subnets.py`` documents — desired state is the *complete*
object, and ``build_plan`` merges a partial ``set`` onto the fetched current
state before diffing.

``self``-echo handling — what is reused and what deliberately is not
-------------------------------------------------------------------
``resources/security_policy.py`` already solves the ``self``-echo problem for
ECOS keyed maps (``_strip_meta`` drops ``self``/``gms_marked`` recursively on
read; ``_inject_self`` re-adds them on write). This module reuses
``_strip_meta`` for ``mgmtServices``, whose per-service ``self`` echo is
exactly that pattern — and reuses **only** that half, on purpose:

* ``_inject_self`` is shaped for ``security_policy``'s map/zonePair/prio
  nesting and would not fit ``mgmtServices``' flat ``serviceId -> config``
  shape. It is also not needed: the vendored appliance spec's POST body type
  (``MgmtServicesPostConfig`` -> ``serviceConfigSave``) declares only
  ``displayname`` and ``srcinf`` — ``self`` is a read-side echo the write side
  does not want. So ``mgmtServices`` strips on read and simply does not
  re-inject on write.
* ``snmp``'s ``trapsink.sink`` and ``v3.users`` sub-maps are also keyed maps
  and *may* carry ``self`` echoes, but this session's live probe captured only
  the top-level key list, not their interiors. Rather than guess a strip/inject
  convention for them (which, if wrong, breaks writes), they are passed through
  verbatim: a full-object round-trip re-sends exactly what the server reported,
  which is idempotent either way. Revisit when a populated trapsink/v3 sample
  is captured.

Ownership (feeds #20)
---------------------
Template section names were probed live this session against a real Default
Template Group's selected-section list and came back as::

    adminDistance, cli, dns, datetime, logging, mgmtServices,
    routes, secureWebServicesConfig, shaper, snmp, webconfig

so ``snmp`` -> ``snmp``, ``logging`` -> ``logging`` and ``mgmtServices`` ->
``mgmtServices`` are **live-confirmed**, not guesses, and are marked as such in
``ownership.KIND_TO_TEMPLATE_SECTIONS`` (most existing entries there are
flagged UNVERIFIED; that distinction is worth preserving). ``banners`` is
*not* in the confirmed list, so ``appliance/banners`` is added UNVERIFIED like
the pre-existing guesses. ``ScheduleTimezone`` is Orchestrator scope and has no
owner — it keeps the base ``managed_by()`` returning ``None``.

Deliberately not implemented here
---------------------------------
``dnsProxy/config`` and ``dnsCache/config`` (both GET+POST on the appliance
API) are left for a follow-up: the DNS proxy config is segment-keyed with
nested profiles/domain-groups/maps (the spec's ``Segment`` schema), which is a
different and much larger normalization job than these flat singletons, and no
populated live sample was captured this session. ``logging/remote`` is
likewise deferred — note for whoever picks it up that its ``self`` key is a
*nested settings object* (``RemoteReceiverSelf``: ``fac``, ``min_severity``,
``port``, ``protocol``, ...), **not** an id echo, so ``_strip_meta`` must NOT
be used on it. Per-appliance NTP/``datetime`` (appliance ``/datetime``) is the
natural claimant of the confirmed ``datetime`` template section and is also a
follow-up.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

import structlog

from pyecsdwan import ownership
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    Ownership,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register
from pyecsdwan.resources.security_policy import _strip_meta

log = structlog.get_logger("pyecsdwan.resources.common_settings")

_SNMP_PATH = "snmp"
_BANNERS_PATH = "banners"
_LOGGING_CONFIG_PATH = "logging/config"
_MGMT_SERVICES_PATH = "mgmtServices"
_SCHEDULE_TIMEZONE_PATH = "/gms/scheduleTimezone"


# -- small shared coercions ---------------------------------------------------
#
# Every one of these raises ValueError naming the offending field: a settings
# object that a human hand-edited in YAML is the common input here, and a
# silent coercion of the wrong type is exactly the phantom-drift the contract
# forbids.


def _document(raw: RawState, what: str) -> dict[str, Any]:
    """Coerce a fetched/round-tripped settings document to a plain dict.

    ``None`` (absent) and ``{}`` (the appliance proxy's answer for an unseeded
    path) both mean "nothing configured yet" and normalize to the defaults.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"{what} must be a mapping of settings, got {type(raw).__name__}: {raw!r}")
    return {str(key): value for key, value in raw.items()}


def _submap(where: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a mapping, got {type(value).__name__}: {value!r}")
    return {str(key): copy.deepcopy(inner) for key, inner in value.items()}


def _text(where: str, value: Any, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {type(value).__name__}: {value!r}")
    return value


def _flag(where: str, value: Any, default: bool = False) -> bool:
    """Booleans, tolerating the ``"true"``/``"false"``/``0``/``1`` spellings
    ECOS endpoints and hand-written YAML both produce."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"{where} must be a boolean, got {type(value).__name__}: {value!r}")


def _count(where: str, value: Any, default: int, minimum: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{where} must be an integer, got {type(value).__name__}: {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{where} must be an integer, got {value!r}") from None
    if number < minimum:
        raise ValueError(f"{where} must be >= {minimum}, got {number}")
    return number


def _choice(where: str, value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = _text(where, value, default)
    if text not in allowed:
        raise ValueError(f"{where} must be one of {allowed}, got {text!r}")
    return text


def _passthrough(data: Mapping[str, Any], known: frozenset[str]) -> dict[str, Any]:
    """Top-level keys this resource does not model, preserved verbatim.

    Applies are full-object replaces, so a key dropped here would be silently
    reset on the server. Deep-copied so a caller mutating the canonical state
    cannot reach back into the fetched raw document.
    """
    return {
        str(key): copy.deepcopy(value) for key, value in data.items() if str(key) not in known
    }


def _ordered(state: Mapping[str, Any]) -> dict[str, Any]:
    """Key-sorted copy — dict equality ignores order, but a stable order keeps
    diff/plan rendering and journal snapshots readable and reproducible."""
    return {key: state[key] for key in sorted(state)}


# == shared appliance-singleton plumbing ======================================


class _ApplianceSetting(Resource):
    """Base for the appliance-scope singleton settings documents.

    Subclasses set ``kind``/``ecos_path``/``label`` and implement
    ``normalize()``. Everything else — the proxy GET, the full-object POST, the
    mandatory batched ``save_changes``, ref enumeration and the template
    ownership join — is identical across all four and lives here.
    """

    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: These are singleton settings documents: there is no "SNMP does not
    #: exist on this appliance" state to delete into. Turning a feature off is
    #: a field value (``listen.enable = false``), not a whole-resource delete,
    #: and POSTing an empty document would wipe the section instead.
    deletable = False

    #: ECOS path behind ``/appliance/rest?url=...``.
    ecos_path: str = ""
    #: Human label for log lines, save-changes descriptions and messages.
    label: str = ""

    @classmethod
    def _ne_pk(cls, ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = self._ne_pk(ctx, ref)
        raw = ctx.client.appliance_request("GET", ne_pk, self.ecos_path)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected {self.label} response shape from {ref}: {raw!r}")

    # -- write side -----------------------------------------------------------

    def payload(self, canonical: Mapping[str, Any]) -> Any:
        """Canonical state -> POST body. Identity for every resource whose
        canonical shape *is* the wire shape; overridden where it is not."""
        return dict(canonical)

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired
        if not isinstance(desired, Mapping):
            # Whole-resource delete is refused by the engine (deletable=False),
            # so a non-mapping desired here means a caller bypassed the plan.
            return ApplyResult(
                ok=False,
                message=f"no desired {self.label} settings; refusing to POST an empty document",
            )
        return self._replace(ctx, diff.ref, desired, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent (a degenerate GET at commit time)
            # must never replay as "POST an empty settings document" — that
            # would clear the whole section rather than restore it. Refuse
            # loudly, same as internal_subnets.py.
            return ApplyResult(
                ok=False,
                message=f"no usable {self.label} snapshot; refusing to POST an empty document",
            )
        restored = self.normalize(snapshot)
        if not isinstance(restored, Mapping):
            return ApplyResult(
                ok=False,
                message=f"{self.label} snapshot did not normalize to a settings document",
            )
        return self._replace(ctx, ref, restored, "rollback")

    def _replace(
        self, ctx: Ctx, ref: Ref, canonical: Mapping[str, Any], verb: str
    ) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        ctx.client.appliance_request(
            "POST", ne_pk, self.ecos_path, json_body=self.payload(canonical)
        )
        log.debug("common_settings_replace", ref=str(ref), path=self.ecos_path, verb=verb)
        # Mandatory for every appliance-proxy write (docs/plugin-promotion.md):
        # one batched save per operation, and a non-SUCCESS outcome fails it.
        outcome = ctx.save_changes([ne_pk], f"{self.label} {verb}: {ref}")
        message = f"{self.label} {verb} on {ne_pk}"
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"{message} — not persisted, "
                    f"save-changes {outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(ok=True, jobs=[outcome], message=f"{message}, persisted")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]

    # -- ownership ------------------------------------------------------------

    def managed_by(self, ctx: Ctx, ref: Ref) -> Ownership:
        ne_pk = self._ne_pk(ctx, ref)
        return ownership.owning_group(ctx, self.kind, ne_pk)


# == SNMP =====================================================================

_SNMP_KNOWN: frozenset[str] = frozenset(
    {
        "access",
        "auto_launch",
        "listen",
        "syscontact",
        "sysdescr",
        "syslocation",
        "traps",
        "trapsink",
        "v3",
    }
)


class Snmp(_ApplianceSetting):
    kind = "appliance/snmp"
    ecos_path = _SNMP_PATH
    label = "snmp"
    desired_state_doc = (
        "access: {rocommunity: str}. listen: {enable: bool} — the SNMP agent "
        "itself. traps: {enable: bool, trap_community: str}. trapsink: {sink: "
        "{...}} and v3: {users: {...}} are opaque keyed maps, passed through "
        "verbatim. auto_launch: bool. syscontact/sysdescr/syslocation: str. "
        "Full-object replace against ECOS 'snmp'; unknown keys (live-only "
        "hash_algs/priv_algs) are preserved."
    )
    endpoints = (
        "appliance GET /snmp",
        "appliance POST /snmp",
    )

    def normalize(self, raw: RawState) -> CanonicalState:
        data = _document(raw, "snmp")

        access = _submap("snmp.access", data.get("access"))
        access["rocommunity"] = _text("snmp.access.rocommunity", access.get("rocommunity"))

        listen = _submap("snmp.listen", data.get("listen"))
        listen["enable"] = _flag("snmp.listen.enable", listen.get("enable"))

        traps = _submap("snmp.traps", data.get("traps"))
        traps["enable"] = _flag("snmp.traps.enable", traps.get("enable"))
        traps["trap_community"] = _text("snmp.traps.trap_community", traps.get("trap_community"))

        # trapsink.sink / v3.users are keyed maps whose self-echo convention was
        # not captured live this session — opaque passthrough, see module docs.
        trapsink = _submap("snmp.trapsink", data.get("trapsink"))
        trapsink["sink"] = _submap("snmp.trapsink.sink", trapsink.get("sink"))
        v3 = _submap("snmp.v3", data.get("v3"))
        v3["users"] = _submap("snmp.v3.users", v3.get("users"))

        out: dict[str, Any] = {
            "access": _ordered(access),
            "auto_launch": _flag("snmp.auto_launch", data.get("auto_launch")),
            "listen": _ordered(listen),
            "syscontact": _text("snmp.syscontact", data.get("syscontact")),
            "sysdescr": _text("snmp.sysdescr", data.get("sysdescr")),
            "syslocation": _text("snmp.syslocation", data.get("syslocation")),
            "traps": _ordered(traps),
            "trapsink": _ordered(trapsink),
            "v3": _ordered(v3),
        }
        out.update(_passthrough(data, _SNMP_KNOWN))
        return _ordered(out)


# == logging ==================================================================

#: syslog severities, per the vendored LoggingConfig schema's enum.
_SEVERITIES: tuple[str, ...] = (
    "None",
    "Emergency",
    "Alert",
    "Critical",
    "Error",
    "Warning",
    "Notice",
    "Info",
    "Debug",
)
#: syslog facilities, same source.
_FACILITIES: tuple[str, ...] = tuple(f"local{n}" for n in range(8))
#: Documented masking widths (0 = mask all, then /8, /16, /24).
_MASK_WIDTHS: tuple[int, ...] = (0, 8, 16, 24)

_LOGGING_KNOWN: frozenset[str] = frozenset(
    {
        "auditlog",
        "flow",
        "system",
        "min_priority",
        "threshold_size",
        "keep_number",
        "logStatefulWanDrops",
        "mask_enable",
        "mask_ipv4",
        "format_log_enable",
    }
)


class Logging(_ApplianceSetting):
    kind = "appliance/logging"
    ecos_path = _LOGGING_CONFIG_PATH
    label = "logging"
    desired_state_doc = (
        "min_priority: minimum severity, one of None|Emergency|Alert|Critical|"
        "Error|Warning|Notice|Info|Debug. auditlog/flow/system: syslog "
        "facility local0-local7. threshold_size: bytes before rotating (int). "
        "keep_number: how many rotated files to keep (int). "
        "logStatefulWanDrops/mask_enable/format_log_enable: bool. mask_ipv4: "
        "one of 0|8|16|24. Full-object replace against ECOS 'logging/config'; "
        "unknown keys (live-only 'ids') are preserved. Remote receivers "
        "(ECOS 'logging/remote') are a separate, not-yet-modeled resource."
    )
    endpoints = (
        "appliance GET /logging/config",
        "appliance POST /logging/config",
    )

    def normalize(self, raw: RawState) -> CanonicalState:
        data = _document(raw, "logging config")
        mask_ipv4 = _count("logging.mask_ipv4", data.get("mask_ipv4"), 24, minimum=0)
        if mask_ipv4 not in _MASK_WIDTHS:
            raise ValueError(f"logging.mask_ipv4 must be one of {_MASK_WIDTHS}, got {mask_ipv4}")

        out: dict[str, Any] = {
            "min_priority": _choice(
                "logging.min_priority", data.get("min_priority"), _SEVERITIES, "Error"
            ),
            "auditlog": _choice("logging.auditlog", data.get("auditlog"), _FACILITIES, "local0"),
            "flow": _choice("logging.flow", data.get("flow"), _FACILITIES, "local1"),
            "system": _choice("logging.system", data.get("system"), _FACILITIES, "local2"),
            "threshold_size": _count("logging.threshold_size", data.get("threshold_size"), 50),
            "keep_number": _count("logging.keep_number", data.get("keep_number"), 30),
            "logStatefulWanDrops": _flag(
                "logging.logStatefulWanDrops", data.get("logStatefulWanDrops")
            ),
            "mask_enable": _flag("logging.mask_enable", data.get("mask_enable")),
            "mask_ipv4": mask_ipv4,
            "format_log_enable": _flag(
                "logging.format_log_enable", data.get("format_log_enable")
            ),
        }
        # 'ids' is live-present but spec-absent: preserved, deliberately NOT
        # validated against _FACILITIES (nothing confirms it is a facility).
        out.update(_passthrough(data, _LOGGING_KNOWN))
        return _ordered(out)


# == management services ======================================================

class MgmtServices(_ApplianceSetting):
    kind = "appliance/mgmt-services"
    ecos_path = _MGMT_SERVICES_PATH
    label = "mgmt-services"
    desired_state_doc = (
        "A map of serviceId -> {displayname: str, srcinf: str}: which "
        "interface each management service (aaa, dhcrelay, netflowd, node, "
        "ntpd, other, snmpd, sshd, ...) sources its traffic from. The read-side "
        "'self' echo is stripped on read and not re-sent on write (the API's "
        "POST body type declares only displayname/srcinf). Full-object replace "
        "against ECOS 'mgmtServices'."
    )
    endpoints = (
        "appliance GET /mgmtServices",
        "appliance POST /mgmtServices",
    )

    def normalize(self, raw: RawState) -> CanonicalState:
        data = _document(raw, "mgmt services")
        services: dict[str, Any] = {}
        for service_id, config in data.items():
            where = f"mgmtServices.{service_id}"
            entry = _submap(where, config)
            # Reused verbatim from security_policy.py: drops the 'self' id echo
            # (and 'gms_marked', absent here) at every nesting level. Its
            # _inject_self counterpart is deliberately NOT used — see module doc.
            stripped = _strip_meta(entry)
            assert isinstance(stripped, dict)
            stripped["displayname"] = _text(f"{where}.displayname", stripped.get("displayname"))
            stripped["srcinf"] = _text(f"{where}.srcinf", stripped.get("srcinf"))
            # Anything else the service record carries stays put: the POST is a
            # full-object replace, so a dropped key would be reset server-side.
            services[str(service_id)] = _ordered(stripped)
        return _ordered(services)


# == banners ==================================================================

_BANNER_KNOWN: frozenset[str] = frozenset({"motd", "issue"})


class Banners(_ApplianceSetting):
    kind = "appliance/banners"
    ecos_path = _BANNERS_PATH
    label = "banners"
    desired_state_doc = (
        "motd: message of the day, shown after login. issue: the login banner, "
        "shown before it. Both are free text and are stored verbatim — no "
        "trimming or newline rewriting, because a banner's exact whitespace is "
        "what the operator sees. Full-object replace against ECOS 'banners'."
    )
    endpoints = (
        "appliance GET /banners",
        "appliance POST /banners",
    )

    def normalize(self, raw: RawState) -> CanonicalState:
        data = _document(raw, "banners")
        out: dict[str, Any] = {
            "motd": _text("banners.motd", data.get("motd")),
            "issue": _text("banners.issue", data.get("issue")),
        }
        out.update(_passthrough(data, _BANNER_KNOWN))
        return _ordered(out)


# == Orchestrator schedule timezone ===========================================

_TIMEZONE_KNOWN: frozenset[str] = frozenset({"defaultTimezone"})


class ScheduleTimezone(Resource):
    """The timezone Orchestrator runs scheduled jobs and reports in.

    Orchestrator scope, not appliance scope: this is the one member of the #38
    group with a real Orchestrator write path (``GET``+``POST
    /gms/scheduleTimezone``). It therefore uses ``ctx.client.get/post``
    directly, never the appliance proxy, and never calls ``save_changes()`` —
    there is no appliance running-config involved. Being Orchestrator config it
    also has no template owner, so it keeps the base ``managed_by()``.
    """

    kind = "schedule-timezone"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: Singleton: there is no "no timezone configured" state to delete into.
    deletable = False
    desired_state_doc = (
        "defaultTimezone: the timezone scheduled jobs and reports run in, in "
        "Country/Location form (e.g. 'US/East-Indiana') or 'UTC'. Applies as a "
        "full-object POST to /gms/scheduleTimezone."
    )
    endpoints = (
        "orchestrator GET /gms/scheduleTimezone",
        "orchestrator POST /gms/scheduleTimezone",
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_SCHEDULE_TIMEZONE_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        data = _document(raw, "schedule timezone")
        # An empty value is not raised on here: a value the server itself
        # reported must never make fetch() explode. apply()/rollback() refuse
        # to *write* an empty timezone instead.
        out: dict[str, Any] = {
            "defaultTimezone": _text(
                "scheduleTimezone.defaultTimezone", data.get("defaultTimezone")
            ).strip()
        }
        out.update(_passthrough(data, _TIMEZONE_KNOWN))
        return _ordered(out)

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired
        if not isinstance(desired, Mapping):
            return ApplyResult(ok=False, message="no desired schedule timezone to apply")
        return self._write(ctx, diff.ref, desired, "set")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            return ApplyResult(
                ok=False, message="no usable schedule-timezone snapshot; refusing to write"
            )
        restored = self.normalize(snapshot)
        if not isinstance(restored, Mapping):
            return ApplyResult(ok=False, message="schedule-timezone snapshot did not normalize")
        return self._write(ctx, ref, restored, "restored")

    def _write(
        self, ctx: Ctx, ref: Ref, canonical: Mapping[str, Any], verb: str
    ) -> ApplyResult:
        timezone = str(canonical.get("defaultTimezone", ""))
        if not timezone:
            return ApplyResult(
                ok=False,
                message=(
                    "refusing to write an empty schedule timezone; set "
                    "defaultTimezone to e.g. 'UTC' or 'US/East-Indiana'"
                ),
            )
        # Body shape follows the vendored SDK ({"defaultTimezone": tz}), not
        # the spec's bare-string request type — see the module docstring.
        ctx.client.post(_SCHEDULE_TIMEZONE_PATH, dict(canonical))
        log.debug("schedule_timezone_write", ref=str(ref), verb=verb, timezone=timezone)
        return ApplyResult(ok=True, message=f"schedule timezone {verb} to {timezone}")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(Snmp())
register(Logging())
register(MgmtServices())
register(Banners())
register(ScheduleTimezone())
