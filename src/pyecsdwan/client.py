"""Orchestrator HTTP client (httpx).

Facts sourced from the vendored pyedgeconnect SDK and EC_SD-WAN_Expert
(core/orchestrator.py); where live Orchestrator behavior could differ the
client stays tolerant (unknown fields pass through untouched — responses are
returned as parsed JSON, never remodeled here).

* Base URL: ``https://{host}/gms/rest`` (Orchestrator 9.x REST).
* Sessionless auth: ``X-Auth-Token: <api key>`` header on every request
  (cloud-hosted Orchestrators; also accepted on-prem 9.x).
* Session auth: POST ``/authentication/login`` (interactive user/password),
  cookie-based; POST ``/authentication/logout`` to end.
* Appliance-level config goes through the Orchestrator appliance proxy:
  ``/appliance/rest?nePk={nePk}&url={ecosPath}`` — never direct-to-appliance
  in v1 (no RBAC there; a separate gateway project covers that).
* Retries: bounded, with backoff, on connection errors and 5xx — and only
  for GET. Non-idempotent writes are never blindly retried; the transaction
  engine re-fetches and re-diffs instead.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import structlog

from pyecsdwan.config import Settings

log = structlog.get_logger("pyecsdwan.client")

_NE_PK_RE = re.compile(r"^\d{1,10}\.\w{1,10}$")
# The Orchestrator's own REST UI appends this; harmless but not required.
# pyedgeconnect sends it on every call — kept here as documentation only.
_API_SOURCE_PARAM = "menu_rest_apis_id"

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class OrchApiError(Exception):
    """API call failed; carries status and the Orchestrator's error text."""

    def __init__(self, method: str, path: str, status_code: int | None, detail: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        status = status_code if status_code is not None else "connection error"
        super().__init__(f"{method} {path} failed ({status}): {detail[:500]}")


def validate_ne_pk(ne_pk: str) -> str:
    """Appliance primary keys look like ``3.NE``."""
    if not _NE_PK_RE.match(ne_pk):
        raise ValueError(f"invalid appliance nePk: {ne_pk!r} (expected e.g. '3.NE')")
    return ne_pk


class OrchClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        base = settings.orch_url
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        base = base.rstrip("/")
        if not base.endswith("/gms/rest"):
            base = f"{base}/gms/rest"
        headers = {"Accept": "application/json"}
        if settings.api_key:
            headers["X-Auth-Token"] = settings.api_key
        if not settings.verify_tls:
            log.warning(
                "tls_verification_disabled",
                hint="open to man-in-the-middle; use --insecure only against lab gear",
            )
        self._http = httpx.Client(
            base_url=base,
            headers=headers,
            verify=settings.verify_tls,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.read_timeout,
                pool=settings.connect_timeout,
            ),
            transport=transport,
        )
        self._authenticated_session = False

    # -- auth ----------------------------------------------------------------

    def login(self, user: str, password: str) -> None:
        """Interactive session login (used when no API key is configured)."""
        resp = self._http.post(
            "/authentication/login",
            json={"user": user, "password": password, "token": ""},
        )
        if resp.status_code != 200:
            raise OrchApiError(
                "POST", "/authentication/login", resp.status_code,
                "authentication failed (check username/password)",
            )
        self._authenticated_session = True

    def logout(self) -> None:
        if not self._authenticated_session:
            return
        try:
            self._http.post("/authentication/logout")
        except httpx.HTTPError:
            pass
        self._authenticated_session = False

    def close(self) -> None:
        self.logout()
        self._http.close()

    # -- core request --------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """One API call; returns parsed JSON (or None for empty bodies).

        GETs retry on connection errors / 5xx with exponential backoff.
        Writes never blindly retry — a lost response to a landed POST would
        double-apply; callers re-fetch and re-diff instead.
        """
        method = method.upper()
        attempts = self.settings.max_retries + 1 if method == "GET" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(min(2 ** (attempt - 1), 10))
            started = time.monotonic()
            try:
                resp = self._http.request(method, path, json=json_body, params=params)
            except httpx.HTTPError as exc:
                last_error = exc
                log.debug(
                    "api_call_error", method=method, path=path,
                    attempt=attempt + 1, error=str(exc),
                )
                continue
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            log.debug(
                "api_call", method=method, path=path,
                status=resp.status_code, elapsed_ms=elapsed_ms,
            )
            if resp.status_code in expected:
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            if method == "GET" and resp.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                last_error = OrchApiError(method, path, resp.status_code, resp.text)
                continue
            raise OrchApiError(method, path, resp.status_code, resp.text)
        assert last_error is not None
        if isinstance(last_error, OrchApiError):
            raise last_error
        raise OrchApiError(method, path, None, str(last_error)) from last_error

    def get(self, path: str, *, params: dict[str, Any] | None = None,
            expected: tuple[int, ...] = (200, 204)) -> Any:
        return self.request("GET", path, params=params, expected=expected)

    def post(self, path: str, json_body: Any = None, *,
             params: dict[str, Any] | None = None,
             expected: tuple[int, ...] = (200, 201, 204)) -> Any:
        return self.request("POST", path, json_body=json_body, params=params, expected=expected)

    def put(self, path: str, json_body: Any = None, *,
            params: dict[str, Any] | None = None,
            expected: tuple[int, ...] = (200, 201, 204)) -> Any:
        return self.request("PUT", path, json_body=json_body, params=params, expected=expected)

    def delete(self, path: str, *, params: dict[str, Any] | None = None,
               expected: tuple[int, ...] = (200, 204)) -> Any:
        return self.request("DELETE", path, params=params, expected=expected)

    # -- appliance proxy -----------------------------------------------------

    def appliance_request(
        self,
        method: str,
        ne_pk: str,
        ecos_path: str,
        *,
        json_body: Any = None,
        expected: tuple[int, ...] = (200, 201, 204),
    ) -> Any:
        """Call an appliance (ECOS) API through the Orchestrator proxy."""
        validate_ne_pk(ne_pk)
        clean = "/" + ecos_path.lstrip("/")
        if not re.match(r"^[a-zA-Z0-9/_.?=&-]+$", clean):
            raise ValueError(f"invalid ECOS path: {ecos_path!r}")
        return self.request(
            method,
            "/appliance/rest",
            json_body=json_body,
            params={"nePk": ne_pk, "url": clean},
            expected=expected,
        )
