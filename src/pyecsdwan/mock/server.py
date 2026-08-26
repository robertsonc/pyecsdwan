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
            "reachabilityStatus": 1,
        },
        {
            "nePk": "3.NE",
            "id": "3.NE",
            "hostName": "BR1-EC",
            "site": "Branch-1",
            "model": "EC-S",
            "reachabilityStatus": 1,
        },
        {
            "nePk": "5.NE",
            "id": "5.NE",
            "hostName": "BR2-EC",
            "site": "Branch-2",
            "model": "EC-S",
            "reachabilityStatus": 1,
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
    actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)
    next_overlay_id: int = 2

    def reset(self) -> None:
        """Restore every field to its seeded default (in place)."""
        fresh = MockState()
        for spec in fields(self):
            setattr(self, spec.name, getattr(fresh, spec.name))

    def new_action(self, key: str | None = None, ne_pks: list[str] | None = None) -> str:
        """Register an async action; it finishes after ``action_delay_polls`` polls.

        A pending ``fail_next_action`` flag is consumed by the next action
        created, which then finishes as a failure (``completionStatus`` false,
        result ``"mock failure"``).
        """
        action_key = key or str(uuid.uuid4())
        self.actions[action_key] = {
            "polls": 0,
            "delay_polls": self.action_delay_polls,
            "fail": self.fail_next_action,
            "ne_pks": list(ne_pks) if ne_pks else [],
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
    records: list[dict[str, Any]] = []
    for ne_pk in action.get("ne_pks") or [""]:
        if not finished:
            records.append(
                {
                    "guid": key,
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
                    "guid": key,
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
                    "guid": key,
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
        await _json_body(request)  # {user, password, token} — any credentials accepted
        session_id = uuid.uuid4().hex
        mock.sessions.add(session_id)
        response = JSONResponse({"status": "logged in"})
        response.set_cookie(_SESSION_COOKIE, session_id, httponly=True)
        return response

    @auth.post("/authentication/logout")
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
        mock.new_action(ne_pks=[nePk])
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

    # -- security maps -------------------------------------------------------

    @api.get("/securityMaps")
    async def get_security_maps(nePk: str | None = None, cached: bool | None = None) -> Any:
        return mock.security_maps

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
