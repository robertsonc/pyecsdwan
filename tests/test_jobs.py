"""Unit tests for pyecsdwan.jobs: async action-key polling."""

import time

import httpx
import respx

from pyecsdwan.client import OrchClient
from pyecsdwan.jobs import wait_for_action

STATUS_URL = "https://orch.example.com/gms/rest/action/status"


@respx.mock
def test_single_record_success(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "taskStatus": "Done",
                "percentComplete": 100,
                "completionStatus": True,
                "result": "template pushed",
                "endTime": 1724680000000,
            },
        )
    )
    outcome = wait_for_action(OrchClient(settings), "guid-1", settings)
    assert outcome.state == "SUCCESS"
    assert outcome.key == "guid-1"
    assert outcome.detail == "template pushed"
    assert outcome.per_appliance == {}
    assert route.calls.last.request.url.params["key"] == "guid-1"


@respx.mock
def test_per_appliance_records_one_failure(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nepk": "1.NE",
                    "endTime": 1724680000000,
                    "completionStatus": True,
                    "result": "push ok",
                },
                {
                    "nepk": "2.NE",
                    "endTime": 1724680000001,
                    "completionStatus": False,
                    "result": "boom: policy rejected",
                },
            ],
        )
    )
    outcome = wait_for_action(OrchClient(settings), "guid-2", settings)
    assert outcome.state == "FAILED"
    assert outcome.per_appliance == {"1.NE": "push ok", "2.NE": "boom: policy rejected"}
    assert "boom: policy rejected" in outcome.detail


@respx.mock
def test_never_finishing_records_time_out(settings, monkeypatch):
    # Fake clock: sleep advances monotonic time so the deadline is reached fast.
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)
    route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json={"taskStatus": "in progress", "percentComplete": 40, "endTime": 0},
        )
    )
    outcome = wait_for_action(OrchClient(settings), "guid-3", settings, description="push")
    assert outcome.state == "TIMEOUT"
    assert outcome.detail == "push: job did not finish within 5.0s"
    assert route.call_count > 1
