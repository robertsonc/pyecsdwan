"""Fake Orchestrator: FastAPI app factory, in-memory state, and a thread runner.

Behavior mirrors the real Orchestrator 9.3+ endpoints as documented in the
vendored pyedgeconnect SDK docstrings. Every handler is tolerant: unknown
body fields pass through into state untouched, and responses come straight
from state (never remodeled), matching the pass-through philosophy of
``pyecsdwan.client``.

All routes are served under ``/gms/rest``. Authentication accepts either an
``X-Auth-Token`` header with any non-empty value, or a session cookie issued
by ``POST /gms/rest/authentication/login``. Set ``MockState.require_auth``
to ``False`` to disable the check entirely (useful in tests).
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from typing import Any

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

_SESSION_COOKIE = "pyecsdwanMockSession"


# -- seed data ---------------------------------------------------------------


def _seed_appliances() -> list[dict[str, Any]]:
    return [
        {
            "nePk": "1.NE",
            "id": "1.NE",
            "hostName": "HUB1-EC",
            "site": "HQ",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
        {
            "nePk": "3.NE",
            "id": "3.NE",
            "hostName": "BR1-EC",
            "site": "Branch-1",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
        {
            "nePk": "5.NE",
            "id": "5.NE",
            "hostName": "BR2-EC",
            "site": "Branch-2",
            "model": "EC-S",
            "state": 1,
            "reachabilityChannel": 2,
            "hasUnsavedChanges": False,
            "rebootRequired": False,
        },
    ]


def _seed_interface_labels() -> dict[str, Any]:
    return {
        "wan": {
            "1": {"name": "MPLS1", "active": True, "topology": 0},
            "2": {"name": "INET1", "active": True, "topology": 0},
        },
        "lan": {
            "4": {"name": "Voice", "active": True, "topology": 0},
            "5": {"name": "Data", "active": True, "topology": 0},
        },
    }


def _seed_overlays() -> dict[str, dict[str, Any]]:
    return {"1": {"id": 1, "name": "CorpFabric", "modifiedTime": 0}}


def _seed_overlay_association() -> dict[str, list[str]]:
    return {"1": ["1.NE"]}


def _seed_security_maps() -> dict[str, Any]:
    return {"mapsByVrf": {"0": {"zoneMap": {}}}}


def _seed_zones() -> dict[str, Any]:
    return {"0": {"name": "Default"}}


def _seed_vrf_zones_map() -> dict[str, Any]:
    return {"0": {"0": {"id": 0, "name": "Default"}}}


def _seed_zone_list_meta() -> dict[str, Any]:
    return {
        "1.NE": {"zones": ["Default"]},
        "3.NE": {"zones": ["Default"]},
        "5.NE": {"zones": ["Default"]},
    }


# -- deployment (#12) ---------------------------------------------------------
#
# Genericized capture of a real GET /appliance/rest?...&url=deployment
# response against a lab Orchestrator this session (docs/research/
# appliance-config.md + resources/deployment.py docstring). Six interfaces,
# one running a DHCP server, matching the sampled appliance's shape.


def _seed_deployment() -> dict[str, Any]:
    def _ip(
        devnum_role: str,
        ip: str,
        mask: str,
        *,
        label: str,
        lan_side: bool,
        wan_side: bool,
        wan_nexthop: str = "",
        zone: int = 0,
        dhcpd: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "ip": ip,
            "mask": mask,
            "wanNexthop": wan_nexthop,
            "dhcp": False,
            "lanSide": lan_side,
            "wanSide": wan_side,
            "label": label,
            "harden": 0,
            "behindNAT": False,
            "maxBW": {"inbound": 0, "outbound": 0},
            "zone": zone,
            "comment": "",
            "vrf": 0,
            "role": devnum_role,
            "proxy_arp": False,
            "dhcpd": dhcpd
            if dhcpd is not None
            else {"type": "none", "server": {}, "relay": {}},
        }

    return {
        # Read-only hardware/license limits — Deployment.normalize() strips
        # this key entirely; kept here only so the mock's GET response
        # matches the real appliance shape byte-for-byte.
        "scalars": {
            "maxWanBandwidth": 1000000,
            "defaultMaxWanBandwidth": 1000000,
            "maxRxTargetBandwidth": 1000000,
            "maxTunnels": 500,
            "maxIKETunnels": 500,
            "minMtu": 576,
            "maxMtu": 9216,
            "maxRouteMapEntries": 512,
            "maxOptMapEntries": 256,
            "maxQoSMapEntries": 256,
            "maxNatMapEntries": 512,
            "isPortalLicensed": True,
            "portalLicenseType": "boost",
            "supportServerMode": True,
            "isLicenseRequired": False,
            "isDynamicLimits": False,
            "isDynamicInterface": True,
            "isModel4Port": False,
            "isModel10G": False,
            "isModelSingleDisk": True,
            "isModelPowerCycle": False,
            "num1GigPorts": 8,
            "num1GigFiberPorts": 0,
            "numMgmtPorts": 1,
            "num10GigPorts": 0,
        },
        "sysConfig": {
            "mode": "router",
            "useMgmt0": True,
            "tenG": False,
            "bonding": False,
            "maxBW": 100000,
            "propagateLinkDown": False,
            "singleBridge": False,
            "inline": False,
            "vrfEnable": False,
            "serverPerSegment": False,
            "ifLabels": {"lan": ["Data", "Voice"], "wan": ["MPLS1", "INET1"]},
            "haIf": "",
            "zones": [],
            "vrfs": [],
            "roles": [],
            "vrfZonesMap": {},
            "maxInBW": 100000,
            "maxInSysBW": 100000,
            "maxObSysBW": 100000,
            "license": "boost",
        },
        "mgmtIfData": {
            "mgmt0": {
                "dhcp": False,
                "ip": "10.0.0.10",
                "mask": "255.255.255.0",
                "nexthop": "10.0.0.1",
            },
        },
        "modeIfs": [
            {
                "devNum": "rtr1",
                "ifName": "lan0",
                "applianceIPs": [
                    _ip(
                        "lan",
                        "10.1.1.1",
                        "255.255.255.0",
                        label="Data",
                        lan_side=True,
                        wan_side=False,
                        zone=0,
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "lan1",
                "applianceIPs": [
                    _ip(
                        "lan",
                        "10.1.2.1",
                        "255.255.255.0",
                        label="Voice",
                        lan_side=True,
                        wan_side=False,
                        zone=0,
                        dhcpd={
                            "type": "server",
                            "server": {
                                "prefix": "10.1.2.0/24",
                                "ipStart": "10.1.2.100",
                                "ipEnd": "10.1.2.200",
                                "gw": ["10.1.2.1"],
                                "dns": ["10.1.2.1"],
                                "ntpd": [],
                                "netbios": [],
                                "netbiosNodeType": 0,
                                "maxLease": 86400,
                                "defaultLease": 43200,
                                "ip_range": {},
                                "options": {},
                                "host": {},
                                "failover": "",
                            },
                            "relay": {},
                        },
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "wan0",
                "applianceIPs": [
                    _ip(
                        "wan",
                        "203.0.113.10",
                        "255.255.255.0",
                        label="MPLS1",
                        lan_side=False,
                        wan_side=True,
                        wan_nexthop="203.0.113.1",
                        zone=0,
                    )
                ],
            },
            {
                "devNum": "rtr1",
                "ifName": "wan1",
                "applianceIPs": [
                    _ip(
                        "wan",
                        "198.51.100.10",
                        "255.255.255.0",
                        label="INET1",
                        lan_side=False,
                        wan_side=True,
                        wan_nexthop="198.51.100.1",
                        zone=0,
                    )
                ],
            },
            {"devNum": "rtr1", "ifName": "wan2", "applianceIPs": []},
            {"devNum": "rtr1", "ifName": "lan2", "applianceIPs": []},
        ],
        "dpRoutes": [],
        # Real top-level key (issue text didn't mention it) — pass-through
        # unknown config, never dropped by Deployment.normalize().
        "vifs": {"pppoe": [], "bondedIfs": []},
        "dhcpFailover": {},
    }


def _validate_deployment(body: Any, force_fail: bool) -> dict[str, Any]:
    """Minimal mock of ``deployment/validate``: ``{err, rebootRequired}``.

    ``force_fail`` lets a test arm one deterministic failure (mirrors
    ``fail_next_action``); otherwise a body missing a mask for a configured
    IP is the one condition rejected, so the happy path is always empty.
    """
    if force_fail:
        return {
            "err": "mock validation failure: conflicting IP allocation",
            "rebootRequired": False,
        }
    if isinstance(body, dict):
        for iface in body.get("modeIfs") or []:
            if not isinstance(iface, dict):
                continue
            for entry in iface.get("applianceIPs") or []:
                if isinstance(entry, dict) and entry.get("ip") and not entry.get("mask"):
                    return {
                        "err": (
                            f"{iface.get('devNum')}.{iface.get('ifName')}: "
                            f"ip {entry.get('ip')} missing mask"
                        ),
                        "rebootRequired": False,
                    }
    return {"err": "", "rebootRequired": False}


# -- state -------------------------------------------------------------------


@dataclass
class MockState:
    """Resettable in-memory Orchestrator state shared with the FastAPI app.

    Tests reach into this directly (e.g. ``state.actions`` for action keys,
    ``state.fail_next_action`` to force a failing job, ``state.require_auth``
    to disable auth checks).
    """

    require_auth: bool = True
    action_delay_polls: int = 1
    fail_next_action: bool = False
    appliances: list[dict[str, Any]] = field(default_factory=_seed_appliances)
    interface_labels: dict[str, Any] = field(default_factory=_seed_interface_labels)
    template_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    template_selection: dict[str, list[str]] = field(default_factory=dict)
    template_association: dict[str, list[str]] = field(default_factory=dict)
    overlays: dict[str, dict[str, Any]] = field(default_factory=_seed_overlays)
    overlay_association: dict[str, list[str]] = field(default_factory=_seed_overlay_association)
    security_maps: dict[str, Any] = field(default_factory=_seed_security_maps)
    #: Segment-pair keyed security policy data ("0_0" -> SecurityMaps object).
    security_policies: dict[str, Any] = field(default_factory=dict)
    #: Orchestrator firewall zone table ({zone_id: {"name": ...}}) plus the
    #: monotonic id allocator and the end-to-end ZBFW flag.
    zones: dict[str, Any] = field(default_factory=_seed_zones)
    zones_next_id: int = 1
    zones_ee_enable: bool = False
    #: Read-only views: segment<->zone map and per-appliance cached zone lists.
    vrf_zones_map: dict[str, Any] = field(default_factory=_seed_vrf_zones_map)
    zone_list_meta: dict[str, Any] = field(default_factory=_seed_zone_list_meta)
    #: Per-appliance ECOS store reached via the /appliance/rest proxy:
    #: {nePk: {ecosPath: payload}}.
    appliance_ecos: dict[str, dict[str, Any]] = field(default_factory=dict)
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)
    next_overlay_id: int = 2
    # -- deployment (#12) --
    #: Consumed by the next POST .../url=deployment/validate call, like
    #: fail_next_action: forces one deterministic {err: ...} response.
    deployment_fail_validate: bool = False

    def reset(self) -> None:
        """Restore every field to its seeded default (in place)."""
        fresh = MockState()
        for spec in fields(self):
            setattr(self, spec.name, getattr(fresh, spec.name))

    def new_action(
        self, key: str | None = None, ne_pks: list[str] | None = None, name: str = ""
    ) -> str:
        """Register an async action; it finishes after ``action_delay_polls`` polls.

        A poll is one GET that returns the action's records — via either
        ``/action/status?key=`` or the ``/action`` listing; a single waiter
        only ever uses one of the two, so ``action_delay_polls`` means the
        same thing on both paths. A pending ``fail_next_action`` flag is
        consumed by the next action created, which then finishes as a failure
        (``completionStatus`` false, result ``"mock failure"``).
        """
        action_key = key or str(uuid.uuid4())
        self.actions[action_key] = {
            "polls": 0,
            "delay_polls": self.action_delay_polls,
            "fail": self.fail_next_action,
            "ne_pks": list(ne_pks) if ne_pks else [],
            "name": name,
            "startTime": int(time.time() * 1000),
        }
        self.fail_next_action = False
        return action_key


# -- app factory -------------------------------------------------------------


async def _json_body(request: Request) -> Any:
    """Parse the request body as JSON; tolerate empty/invalid bodies as None."""
    raw = await request.body()
    if not raw:
        return None
    try:
        return await request.json()
    except (ValueError, UnicodeDecodeError):
        return None


def _action_records(key: str, action: dict[str, Any]) -> list[dict[str, Any]]:
    finished = action["polls"] >= max(int(action.get("delay_polls", 1)), 1)
    failed = bool(action.get("fail"))
    base = {
        "guid": key,
        "name": str(action.get("name", "")),
        "startTime": int(action.get("startTime", 0)),
    }
    records: list[dict[str, Any]] = []
    for ne_pk in action.get("ne_pks") or [""]:
        if not finished:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "In Progress",
                    "percentComplete": 50,
                    "completionStatus": False,
                    "endTime": 0,
                    "result": "",
                }
            )
        elif failed:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "Failed",
                    "percentComplete": 100,
                    "completionStatus": False,
                    "endTime": int(time.time() * 1000),
                    "result": "mock failure",
                }
            )
        else:
            records.append(
                {
                    **base,
                    "nepk": ne_pk,
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": int(time.time() * 1000),
                    "result": "mock apply complete",
                }
            )
    return records


def create_app(state: MockState | None = None) -> FastAPI:
    """Build the fake-Orchestrator FastAPI app around the given (or fresh) state."""
    mock = state if state is not None else MockState()
    app = FastAPI(title="pyecsdwan fake Orchestrator", docs_url=None, redoc_url=None)
    app.state.mock = mock

    def require_auth(request: Request) -> None:
        if not mock.require_auth:
            return
        if request.headers.get("X-Auth-Token"):
            return
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie and cookie in mock.sessions:
            return
        raise HTTPException(status_code=401, detail="authentication required")

    auth = APIRouter(prefix="/gms/rest")
    api = APIRouter(prefix="/gms/rest", dependencies=[Depends(require_auth)])

    # -- authentication ------------------------------------------------------

    @auth.post("/authentication/login")
    async def login(request: Request) -> Response:
        await _json_body(request)  # {user, password, loginType} — any accepted
        session_id = uuid.uuid4().hex
        mock.sessions.add(session_id)
        response = JSONResponse({"status": "logged in"})
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True)
        # Real Orchestrator also sets the CSRF cookie the client echoes back.
        response.set_cookie("orchCsrfToken", uuid.uuid4().hex)
        return response

    # Logout is a GET on the real Orchestrator (the client uses GET too).
    @auth.get("/authentication/logout")
    async def logout(request: Request) -> Response:
        cookie = request.cookies.get(_SESSION_COOKIE)
        if cookie:
            mock.sessions.discard(cookie)
        response = Response(status_code=200)
        response.delete_cookie(_SESSION_COOKIE)
        return response

    # -- appliances ----------------------------------------------------------

    @api.get("/appliance")
    async def list_appliances() -> Any:
        return mock.appliances

    # -- interface labels ----------------------------------------------------

    @api.get("/gms/interfaceLabels")
    async def get_interface_labels(active: bool | None = None) -> Any:
        labels = mock.interface_labels
        if active is None:
            return labels
        out = dict(labels)
        for side in ("wan", "lan"):
            entries = labels.get(side) or {}
            out[side] = {
                label_id: entry
                for label_id, entry in entries.items()
                if isinstance(entry, dict) and bool(entry.get("active")) == active
            }
        return out

    @api.post("/gms/interfaceLabels")
    async def replace_interface_labels(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a wan/lan label object"}, status_code=400)
        mock.interface_labels = body
        return Response(status_code=200)

    # -- template groups -----------------------------------------------------

    @api.get("/template/templateGroups")
    async def get_template_groups(templateGroup: str | None = None) -> Any:
        if templateGroup is None:
            return list(mock.template_groups.values())
        group = mock.template_groups.get(templateGroup)
        if group is None:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        return group

    @api.post("/template/templateGroups")
    async def update_template_group(request: Request, templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a template group object"}, status_code=400)
        mock.template_groups[templateGroup] = {**body, "name": templateGroup}
        return Response(status_code=204)

    @api.post("/template/templateCreate")
    async def create_template_group(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or not body.get("name"):
            return JSONResponse({"error": "body must include a group name"}, status_code=400)
        name = str(body["name"])
        group = {**body, "name": name}
        group.setdefault("templates", [])
        mock.template_groups[name] = group
        mock.template_selection.setdefault(name, [])
        if "templates" not in body:
            return Response(status_code=204)
        return JSONResponse(group, status_code=200)

    @api.delete("/template/templateGroups")
    async def delete_template_group(templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        del mock.template_groups[templateGroup]
        mock.template_selection.pop(templateGroup, None)
        for ne_pk, groups in mock.template_association.items():
            mock.template_association[ne_pk] = [g for g in groups if g != templateGroup]
        return Response(status_code=204)

    @api.get("/template/templateSelection")
    async def get_template_selection(templateGroup: str) -> Any:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        return mock.template_selection.get(templateGroup, [])

    @api.post("/template/templateSelection")
    async def set_template_selection(request: Request, templateGroup: str) -> Response:
        if templateGroup not in mock.template_groups:
            return JSONResponse(
                {"error": f"no template group {templateGroup!r}"}, status_code=404
            )
        body = await _json_body(request)
        if not isinstance(body, list):
            return JSONResponse({"error": "body must be a list of template names"}, status_code=400)
        mock.template_selection[templateGroup] = [str(name) for name in body]
        return Response(status_code=204)

    @api.get("/template/applianceAssociation")
    async def get_appliance_association(nePk: str | None = None) -> Any:
        if nePk is None:
            out: dict[str, list[str]] = {
                str(a.get("nePk") or a.get("id")): [] for a in mock.appliances
            }
            for ne_pk, groups in mock.template_association.items():
                out[ne_pk] = list(groups)
            return out
        return {"templateIds": list(mock.template_association.get(nePk, []))}

    @api.post("/template/applianceAssociation")
    async def set_appliance_association(request: Request, nePk: str) -> Response:
        body = await _json_body(request)
        template_ids = body.get("templateIds") if isinstance(body, dict) else None
        if not isinstance(template_ids, list):
            return JSONResponse({"error": "body must include templateIds list"}, status_code=400)
        mock.template_association[nePk] = [str(g) for g in template_ids]
        # Fire-and-204, like the real endpoint: the push's per-appliance
        # results exist only as action-log records — no key in the response.
        mock.new_action(ne_pks=[nePk], name="template push")
        return Response(status_code=204)

    @api.get("/template/history/groupList")
    async def get_applied_group_list(nePk: str) -> Any:
        return list(mock.template_association.get(nePk, []))

    # -- overlays ------------------------------------------------------------

    @api.get("/gms/overlays/config")
    async def get_overlays(overlayId: str | None = None) -> Any:
        if overlayId is None:
            return list(mock.overlays.values())
        overlay = mock.overlays.get(str(overlayId))
        if overlay is None:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        return overlay

    @api.post("/gms/overlays/config")
    async def create_overlay(request: Request) -> Any:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an overlay object"}, status_code=400)
        overlay_id = mock.next_overlay_id
        mock.next_overlay_id += 1
        overlay = {**body, "id": overlay_id, "modifiedTime": int(time.time() * 1000)}
        mock.overlays[str(overlay_id)] = overlay
        return JSONResponse({"id": overlay_id}, status_code=200)

    @api.put("/gms/overlays/config")
    async def modify_overlay(request: Request, overlayId: str) -> Any:
        existing = mock.overlays.get(str(overlayId))
        if existing is None:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an overlay object"}, status_code=400)
        overlay = {**body, "id": existing["id"], "modifiedTime": int(time.time() * 1000)}
        mock.overlays[str(overlayId)] = overlay
        return JSONResponse(overlay, status_code=200)

    @api.delete("/gms/overlays/config")
    async def delete_overlay(overlayId: str) -> Response:
        if str(overlayId) not in mock.overlays:
            return JSONResponse({"error": f"no overlay {overlayId!r}"}, status_code=404)
        del mock.overlays[str(overlayId)]
        mock.overlay_association.pop(str(overlayId), None)
        return Response(status_code=204)

    @api.get("/gms/overlays/association")
    async def get_overlay_association() -> Any:
        out: dict[str, list[str]] = {overlay_id: [] for overlay_id in mock.overlays}
        for overlay_id, ne_pks in mock.overlay_association.items():
            out[overlay_id] = list(ne_pks)
        return out

    @api.post("/gms/overlays/association")
    async def add_overlay_association(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map overlay id -> nePk list"}, status_code=400
            )
        for overlay_id, ne_pks in body.items():
            if not isinstance(ne_pks, list):
                continue
            current = mock.overlay_association.setdefault(str(overlay_id), [])
            for ne_pk in ne_pks:
                if str(ne_pk) not in current:
                    current.append(str(ne_pk))
        return Response(status_code=204)

    @api.post("/gms/overlays/association/remove")
    async def remove_overlay_association(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map overlay id -> nePk list"}, status_code=400
            )
        for overlay_id, ne_pks in body.items():
            if not isinstance(ne_pks, list):
                continue
            current = mock.overlay_association.get(str(overlay_id), [])
            drop = {str(ne_pk) for ne_pk in ne_pks}
            mock.overlay_association[str(overlay_id)] = [n for n in current if n not in drop]
        return Response(status_code=204)

    # -- action log ----------------------------------------------------------

    @api.get("/action/status")
    async def get_action_status(key: str | None = None) -> Any:
        if not key or key not in mock.actions:
            return JSONResponse({"error": f"unknown action key {key!r}"}, status_code=404)
        action = mock.actions[key]
        action["polls"] += 1
        return _action_records(key, action)

    @api.get("/action")
    async def list_actions(
        startTime: int,
        endTime: int,
        logLevel: int = 1,
        limit: int = 100,
        appliance: str | None = None,
    ) -> Any:
        """Action/audit log listing: epoch-ms window (required, like the real
        API) plus the ``appliance`` nePk filter. Every emitted action counts
        the call as one poll, so ``action_delay_polls`` drives the keyless
        waiter exactly as ``/action/status`` drives the keyed one."""
        records: list[dict[str, Any]] = []
        for key, action in mock.actions.items():
            if appliance is not None and appliance not in (action.get("ne_pks") or []):
                continue
            if not startTime <= int(action.get("startTime", 0)) <= endTime:
                continue
            action["polls"] += 1
            records.extend(_action_records(key, action))
        return records[: max(limit, 0)]

    # -- security maps -------------------------------------------------------

    @api.get("/securityMaps")
    async def get_security_maps(nePk: str | None = None, cached: bool | None = None) -> Any:
        return mock.security_maps

    # -- security policy orchestration (segment-pair scoped) ----------------

    @api.get("/vrf/config/securityPolicies")
    async def get_security_policies(map: str) -> Any:
        data = mock.security_policies.get(map)
        if data is None:
            return Response(status_code=204)
        return {"data": data}

    @api.post("/vrf/config/securityPolicies")
    async def set_security_policies(request: Request, map: str) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "data" not in body:
            raise HTTPException(status_code=400, detail="body must carry a 'data' object")
        mock.security_policies[map] = body["data"]
        return Response(status_code=204)

    # -- zones (orchestrator scope) ------------------------------------------

    @api.get("/zones")
    async def get_zones(allVRFZones: bool = False) -> Any:
        # The mock keeps one unique-names table; allVRFZones=true would add
        # per-segment duplicates on a real Orchestrator.
        return mock.zones

    @api.post("/zones")
    async def replace_zones(request: Request, deleteDependencies: bool) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "body must map zone id -> zone object"}, status_code=400
            )
        mock.zones = {str(zone_id): zone for zone_id, zone in body.items()}
        # Real Orchestrator behavior: the Default zone is re-added to any
        # table posted without it.
        mock.zones.setdefault("0", {"name": "Default"})
        return Response(status_code=204)

    @api.get("/zones/nextId")
    async def get_zone_next_id() -> Any:
        return {"nextId": mock.zones_next_id}

    @api.post("/zones/nextId")
    async def set_zone_next_id(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "nextId" not in body:
            return JSONResponse({"error": "body must carry nextId"}, status_code=400)
        mock.zones_next_id = int(body["nextId"])
        return Response(status_code=204)

    @api.get("/zones/eeEnable")
    async def get_zones_ee_enable() -> Any:
        return {"enable": mock.zones_ee_enable}

    @api.post("/zones/eeEnable")
    async def set_zones_ee_enable(request: Request) -> Response:
        body = await _json_body(request)
        if not isinstance(body, dict) or "enable" not in body:
            return JSONResponse({"error": "body must carry enable"}, status_code=400)
        mock.zones_ee_enable = bool(body["enable"])
        return Response(status_code=204)

    @api.get("/zones/vrfZonesMap")
    async def get_vrf_zones_map() -> Any:
        return mock.vrf_zones_map

    @api.get("/appliance/zoneListMeta")
    async def get_zone_list_meta(nePk: str | None = None) -> Any:
        if nePk is None:
            return mock.zone_list_meta
        return {nePk: mock.zone_list_meta.get(nePk, {"zones": []})}

    # -- appliance proxy + save-changes -------------------------------------

    @api.api_route("/appliance/rest", methods=["GET", "POST", "PUT", "DELETE"])
    async def appliance_proxy(request: Request, nePk: str, url: str) -> Any:
        """Proxy to a per-appliance ECOS store keyed by (nePk, url). Writes set
        hasUnsavedChanges on the appliance until saveChanges clears it."""
        store = mock.appliance_ecos.setdefault(nePk, {})
        # -- deployment (#12) --
        # deployment/validate is a virtual ECOS endpoint: it never touches
        # the ecos store, it only inspects the candidate body and reports
        # {err, rebootRequired} per the real validate-then-apply contract.
        if url == "deployment/validate":
            body = await _json_body(request)
            force_fail = mock.deployment_fail_validate
            mock.deployment_fail_validate = False
            return _validate_deployment(body, force_fail)
        if url == "deployment" and request.method == "GET" and url not in store:
            # Seed realistic interface/IP fixture data on first read so
            # tests exercise the real captured shape, not an empty object.
            return _seed_deployment()
        # -- end deployment (#12) --
        if request.method == "GET":
            return store.get(url, {})
        if request.method == "DELETE":
            store.pop(url, None)
        else:
            body = await _json_body(request)
            store[url] = body
        for appliance in mock.appliances:
            if appliance.get("nePk") == nePk:
                appliance["hasUnsavedChanges"] = True
        return Response(status_code=204)

    @api.post("/appliance/saveChanges")
    async def save_changes(request: Request, nePk: str | None = None) -> Any:
        body = await _json_body(request)
        ne_pks = [nePk] if nePk else (body.get("nePks", []) if isinstance(body, dict) else [])
        # A save armed to fail (fail_next_action) persists nothing: the
        # hasUnsavedChanges flag stays set, like a real failed save. Checked
        # before new_action(), which consumes the flag.
        if not mock.fail_next_action:
            for appliance in mock.appliances:
                if appliance.get("nePk") in ne_pks:
                    appliance["hasUnsavedChanges"] = False
        return {
            "clientKey": mock.new_action(
                ne_pks=[str(p) for p in ne_pks], name="save changes"
            )
        }

    # -- catch-all (must be registered last on this router) -----------------

    @api.api_route("/{_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def catch_all(_path: str) -> Any:
        return JSONResponse({"error": "no such endpoint in mock"}, status_code=404)

    app.include_router(auth)
    app.include_router(api)
    return app


# -- thread runner -----------------------------------------------------------


def run_in_thread(port: int = 0) -> tuple[str, MockState, Callable[[], None]]:
    """Start the mock in a daemon thread on 127.0.0.1; return (base_url, state, shutdown).

    ``port=0`` (the default) binds an ephemeral port; the returned ``base_url``
    carries the real port. Works with a plain sync httpx client. Call the
    returned ``shutdown()`` to stop the server and join the thread.
    """
    state = MockState()
    app = create_app(state)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="pyecsdwan-mock", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("mock server thread exited before startup completed")
        if time.monotonic() > deadline:
            raise RuntimeError("mock server did not start within 15s")
        time.sleep(0.01)
    sockets = server.servers[0].sockets
    real_port = sockets[0].getsockname()[1]
    base_url = f"http://127.0.0.1:{real_port}"

    def shutdown() -> None:
        server.should_exit = True
        thread.join(timeout=10.0)

    return base_url, state, shutdown
