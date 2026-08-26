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


def _guard_relative_path(path: str) -> None:
    """Reject anything that could escape the ``/gms/rest`` base.

    httpx leaves an absolute URL untouched (base_url is ignored), so a path
    like ``http://evil/steal`` would ship the ``X-Auth-Token`` header to an
    attacker host; ``../`` segments climb out of ``/gms/rest``. Both are
    refused before the request is built.
    """
    url = httpx.URL(path)
    if url.is_absolute_url:
        raise ValueError(f"path must be relative to /gms/rest, not an absolute URL: {path!r}")
    if path.startswith("//"):
        raise ValueError(f"path must not start with '//' (host-relative): {path!r}")
    if ".." in url.path.split("/"):
        raise ValueError(f"path must not contain '..' segments: {path!r}")


class OrchClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        base = settings.orch_url
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        base = base.rstrip("/")
        if not base.endswith("/gms/rest"):
            base = f"{base}/gms/rest"
        if base.startswith("http://"):
            host = httpx.URL(base).host
            if host not in ("127.0.0.1", "::1", "localhost"):
                log.warning(
                    "plaintext_orchestrator_url",
                    hint=f"http:// sends the API key in cleartext to {host}; use https://",
                )
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

    def login(self, user: str, password: str, auth_mode: str = "local") -> None:
        """Interactive session login (used when no API key is configured).

        Mirrors the pyedgeconnect SDK: a successful login sets an
        ``orchCsrfToken`` cookie, whose value must be echoed as the
        ``X-XSRF-TOKEN`` header on every subsequent write — the Orchestrator's
        CSRF filter rejects state-changing requests without it. A 200 without
        that cookie is a failure, not a success.
        """
        login_type = {"local": 0, "radius": 1, "tacacs": 2}.get(auth_mode, 0)
        resp = self._http.post(
            "/authentication/login",
            json={"user": user, "password": password, "loginType": login_type},
        )
        if resp.status_code != 200:
            raise OrchApiError(
                "POST", "/authentication/login", resp.status_code,
                "authentication failed (check username/password)",
            )
        csrf = resp.cookies.get("orchCsrfToken")
        if not csrf:
            raise OrchApiError(
                "POST", "/authentication/login", resp.status_code,
                "login returned 200 but no CSRF token cookie; session not established",
            )
        self._http.headers["X-XSRF-TOKEN"] = csrf
        self._authenticated_session = True

    def logout(self) -> None:
        if not self._authenticated_session:
            return
        try:
            # The Orchestrator logout endpoint is a GET (per the SDK/spec).
            self.request("GET", "/authentication/logout", expected=(200, 204))
        except (OrchApiError, httpx.HTTPError):
            pass
        self._http.headers.pop("X-XSRF-TOKEN", None)
        self._authenticated_session = False

    def _scrub(self, text: str) -> str:
        """Remove the API key from any text before it is raised or journaled —
        error pages can reflect request headers."""
        key = self.settings.api_key
        if key and key in text:
            return text.replace(key, "***REDACTED***")
        return text

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
        _guard_relative_path(path)
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
                last_error = OrchApiError(method, path, resp.status_code, self._scrub(resp.text))
                continue
            raise OrchApiError(method, path, resp.status_code, self._scrub(resp.text))
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
        # The proxy `url` param carries the path *after* rest/json/ with no
        # leading slash (per the SDK: url="securityMaps"); a leading slash
        # would resolve to rest/json//securityMaps on the appliance.
        clean = ecos_path.strip("/")
        if not re.match(r"^[a-zA-Z0-9/_.?=&-]+$", clean):
            raise ValueError(f"invalid ECOS path: {ecos_path!r}")
        return self.request(
            method,
            "/appliance/rest",
            json_body=json_body,
            params={"nePk": ne_pk, "url": clean},
            expected=expected,
        )
