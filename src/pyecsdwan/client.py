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

from pyecsdwan import config, redaction
from pyecsdwan.config import Settings
from pyecsdwan.retry import Retry, effective_policy

log = structlog.get_logger("pyecsdwan.client")

_NE_PK_RE = re.compile(r"^\d{1,10}\.\w{1,10}$")
# The Orchestrator's own REST UI appends this; harmless but not required.
# pyedgeconnect sends it on every call — kept here as documentation only.
_API_SOURCE_PARAM = "menu_rest_apis_id"

_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})


class OrchApiError(Exception):
    """API call failed; carries status and the Orchestrator's error text.

    The recorded path has secret-named query values masked (#106): an
    exception's text is the one string guaranteed to travel — into logs, the
    journal's ``response_summary``, a pasted bug report — and an appliance
    proxy path can carry credentials folded in as query parameters. Masked
    here, at construction, so no rendering site has to remember to.
    """

    def __init__(self, method: str, path: str, status_code: int | None, detail: str):
        self.method = method
        self.path = redaction.redact_query(path)
        self.status_code = status_code
        self.detail = detail
        status = status_code if status_code is not None else "connection error"
        super().__init__(f"{method} {self.path} failed ({status}): {detail[:500]}")


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


#: How many recent calls the latency estimate averages over.
_LATENCY_WINDOW = 8


class OrchClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        #: Recent call latencies in ms; see `_record_latency`.
        self._latencies: list[float] = []
        # The one definition of the effective endpoint, shared with
        # `config.canonical_origin` so the client and the identity that keys
        # its state cannot disagree about what "the same Orchestrator" means.
        base = config.api_base(settings.orch_url)
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
        retry_policy: Retry = Retry.NEVER,
        scope: str = "orchestrator",
    ) -> Any:
        """One API call; returns parsed JSON (or None for empty bodies).

        Retries are **opt-in and vetoable** (#67). This used to retry every
        GET on the usual assumption that GET is idempotent; it is not on this
        API, whose own spec describes GETs that clear idle time, generate a sys
        dump, log out a session and delete segment BGP state. `retry.py` holds
        the classification and the evidence for each entry.

        ``retry_policy`` defaults to NEVER so a caller that has not thought
        about it gets one attempt: the failure mode of a missing retry is a
        reported error the caller already handles, and the failure mode of an
        unwanted one is a mutation applied twice. :func:`retry.effective_policy`
        can still override BOUNDED down to NEVER, never the other way.

        Writes never retry at all — a lost response to a landed POST would
        double-apply; callers re-fetch and re-diff instead.
        """
        method = method.upper()
        _guard_relative_path(path)
        policy, reason = effective_policy(method, path, retry_policy, scope=scope)
        attempts = self.settings.max_retries + 1 if policy is Retry.BOUNDED else 1
        if policy is Retry.NEVER and retry_policy is Retry.BOUNDED:
            # The veto fired: a caller asked to replay something the spec says
            # mutates. Logged rather than silent — it is the interesting case,
            # and the reason carries the spec's own words.
            log.debug("retry_vetoed", method=method, path=path, reason=reason)
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
            self._record_latency(elapsed_ms)
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
            if (
                policy is Retry.BOUNDED
                and resp.status_code in _RETRYABLE_STATUS
                and attempt + 1 < attempts
            ):
                last_error = OrchApiError(method, path, resp.status_code, self._scrub(resp.text))
                continue
            raise OrchApiError(method, path, resp.status_code, self._scrub(resp.text))
        assert last_error is not None
        if isinstance(last_error, OrchApiError):
            raise last_error
        raise OrchApiError(method, path, None, str(last_error)) from last_error

    def _record_latency(self, elapsed_ms: float) -> None:
        """Keep a short window of call latencies.

        Read by the fan-out warning (grammar.md Decision 7), which has to say
        how long a per-appliance command will take *before* running it. A
        constant would be wrong on every fabric; this is at least wrong in the
        direction of the Orchestrator actually in front of the operator.

        Bounded to the last few calls on purpose: an estimate for the fan-out
        about to start should reflect the link as it is now, not an average
        dragged around by a slow call from ten minutes ago.
        """
        self._latencies.append(elapsed_ms)
        if len(self._latencies) > _LATENCY_WINDOW:
            del self._latencies[:-_LATENCY_WINDOW]

    @property
    def observed_latency_ms(self) -> float | None:
        """Mean of the recent call latencies, or None before any call.

        None is a real answer and not zero: it means "no basis for an
        estimate", and a caller that renders it as a duration would be
        inventing one.
        """
        if not self._latencies:
            return None
        return sum(self._latencies) / len(self._latencies)

    def get(self, path: str, *, params: dict[str, Any] | None = None,
            expected: tuple[int, ...] = (200, 204),
            retry_policy: Retry = Retry.BOUNDED) -> Any:
        """A curated read. Bounded retries by default (#67).

        This is the seam the policy hangs on: everything that reaches a fabric
        through a registered plugin comes through here, and those GETs are
        reviewed at promotion. Tier-0 raw passthrough deliberately does *not* —
        it calls :meth:`request` directly, which defaults to NEVER, so an
        arbitrary path an operator typed is never replayed.
        """
        return self.request(
            "GET", path, params=params, expected=expected, retry_policy=retry_policy
        )

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
        retry_policy: Retry = Retry.BOUNDED,
    ) -> Any:
        """Call an appliance (ECOS) API through the Orchestrator proxy.

        The retry policy is resolved against the **ECOS** path, not against
        ``/appliance/rest`` (#67). Every proxied call wears the same transport
        path, so classifying on it would make the whole appliance API one
        undifferentiated endpoint — and the two worst read-shaped mutations in
        the specs, ``GET /bgp/vrfs/{vrfId}/state`` ("Delete specific/all
        segment BGP state") and ``GET /debugFiles/debugDump/generate``, are
        both appliance-scope and reachable only through here.
        """
        validate_ne_pk(ne_pk)
        # The proxy `url` param carries the path *after* rest/json/ with no
        # leading slash (per the SDK: url="securityMaps"); a leading slash
        # would resolve to rest/json//securityMaps on the appliance.
        clean = ecos_path.strip("/")
        if not re.match(r"^[a-zA-Z0-9/_.?=&-]+$", clean):
            raise ValueError(f"invalid ECOS path: {ecos_path!r}")
        policy, reason = effective_policy(method, clean, retry_policy, scope="appliance")
        if policy is Retry.NEVER and retry_policy is Retry.BOUNDED:
            log.debug("retry_vetoed", method=method.upper(), path=clean, reason=reason)
        return self.request(
            method,
            "/appliance/rest",
            json_body=json_body,
            params={"nePk": ne_pk, "url": clean},
            expected=expected,
            retry_policy=policy,
        )
