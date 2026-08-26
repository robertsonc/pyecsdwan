"""Unit tests for pyecsdwan.client: respx-mocked Orchestrator HTTP client."""

import time

import httpx
import pytest
import respx

from pyecsdwan.client import OrchApiError, OrchClient, validate_ne_pk

BASE = "https://orch.example.com/gms/rest"


@respx.mock
def test_get_parses_json_list_and_sends_auth_token(settings):
    route = respx.get(f"{BASE}/appliance").mock(
        return_value=httpx.Response(200, json=[{"hostName": "edge1", "nePk": "3.NE"}])
    )
    client = OrchClient(settings)
    result = client.get("/appliance")
    assert result == [{"hostName": "edge1", "nePk": "3.NE"}]
    assert route.calls.last.request.headers["X-Auth-Token"] == "test-key"


@respx.mock
def test_404_raises_orch_api_error_with_status(settings):
    respx.get(f"{BASE}/appliance/nope").mock(
        return_value=httpx.Response(404, text="no such appliance")
    )
    client = OrchClient(settings)
    with pytest.raises(OrchApiError) as excinfo:
        client.get("/appliance/nope")
    assert excinfo.value.status_code == 404
    assert "no such appliance" in excinfo.value.detail
    assert "404" in str(excinfo.value)


@respx.mock
def test_get_retries_on_503_then_succeeds(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    route = respx.get(f"{BASE}/appliance").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=[]),
        ]
    )
    client = OrchClient(settings)
    assert settings.max_retries == 3
    assert client.get("/appliance") == []
    assert route.call_count == 3


@respx.mock
def test_post_does_not_retry_on_503(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    route = respx.post(f"{BASE}/gms/interfaceLabels").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={}),  # must never be reached
        ]
    )
    client = OrchClient(settings)
    with pytest.raises(OrchApiError) as excinfo:
        client.post("/gms/interfaceLabels", {"wan": {}, "lan": {}})
    assert excinfo.value.status_code == 503
    assert route.call_count == 1


@respx.mock
def test_empty_204_body_returns_none(settings):
    respx.post(f"{BASE}/appliance/save").mock(return_value=httpx.Response(204))
    client = OrchClient(settings)
    assert client.post("/appliance/save", {"x": 1}) is None


@respx.mock
def test_appliance_request_builds_proxy_params(settings):
    route = respx.get(f"{BASE}/appliance/rest").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = OrchClient(settings)
    result = client.appliance_request("GET", "3.NE", "interface/state")
    assert result == {"ok": True}
    url = route.calls.last.request.url
    assert url.params["nePk"] == "3.NE"
    # The proxy url param carries the path after rest/json/ with no leading slash.
    assert url.params["url"] == "interface/state"


def test_validate_ne_pk():
    assert validate_ne_pk("3.NE") == "3.NE"
    with pytest.raises(ValueError, match="invalid appliance nePk"):
        validate_ne_pk("foo")
