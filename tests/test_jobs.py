"""Unit tests for pyecsdwan.jobs: async action-key and keyless window polling."""

import time

import httpx
import respx

from pyecsdwan.client import OrchClient
from pyecsdwan.jobs import wait_for_action, wait_for_recent_action

STATUS_URL = "https://orch.example.com/gms/rest/action/status"
ACTION_LOG_URL = "https://orch.example.com/gms/rest/action"


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


# -- keyless waiter: GET /action window polling -------------------------------


def _log_record(**overrides):
    """One terminal-success ActionLog record; override fields per test."""
    record = {
        "guid": "guid-A",
        "nepk": "3.NE",
        "name": "template push",
        "taskStatus": "Completed",
        "percentComplete": 100,
        "completionStatus": True,
        "startTime": 1724680000500,
        "endTime": 1724680001000,
        "result": "template pushed",
    }
    record.update(overrides)
    return record


@respx.mock
def test_keyless_window_polls_until_terminal(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    in_flight = _log_record(
        taskStatus="In Progress", percentComplete=50,
        completionStatus=False, endTime=0, result="",
    )
    route = respx.get(ACTION_LOG_URL).mock(
        side_effect=[
            httpx.Response(200, json=[]),  # the action log can lag the 204
            httpx.Response(200, json=[in_flight]),
            httpx.Response(200, json=[_log_record()]),
        ]
    )
    outcome = wait_for_recent_action(OrchClient(settings), settings, "3.NE", 1724680000000)
    assert outcome.state == "SUCCESS"
    assert outcome.key == "guid-A"
    assert outcome.detail == "template pushed"
    assert outcome.per_appliance == {"3.NE": "template pushed"}
    assert route.call_count == 3
    params = route.calls.last.request.url.params
    assert params["appliance"] == "3.NE"
    assert params["startTime"] == str(1724680000000 - 1000)  # 1s clock-skew slack
    assert params["logLevel"] == "1"
    assert int(params["endTime"]) >= 1724680000000


@respx.mock
def test_keyless_window_maps_failed_record(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    respx.get(ACTION_LOG_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _log_record(
                    taskStatus="Failed", completionStatus=False,
                    result="boom: policy rejected",
                )
            ],
        )
    )
    outcome = wait_for_recent_action(OrchClient(settings), settings, "3.NE", 1724680000000)
    assert outcome.state == "FAILED"
    assert outcome.key == "guid-A"
    assert outcome.per_appliance == {"3.NE": "boom: policy rejected"}
    assert "boom: policy rejected" in outcome.detail


@respx.mock
def test_keyless_window_picks_newest_guid(settings, monkeypatch):
    """An older push in the window (e.g. an operator's) must not be mistaken
    for ours: the guid with the newest startTime wins."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    stale = _log_record(
        guid="guid-old", startTime=1724679999200,
        taskStatus="Failed", completionStatus=False, result="stale operator push",
    )
    respx.get(ACTION_LOG_URL).mock(
        return_value=httpx.Response(
            200, json=[stale, _log_record(guid="guid-new", startTime=1724680000900)]
        )
    )
    outcome = wait_for_recent_action(OrchClient(settings), settings, "3.NE", 1724680000000)
    assert outcome.state == "SUCCESS"
    assert outcome.key == "guid-new"
    assert outcome.per_appliance == {"3.NE": "template pushed"}


@respx.mock
def test_keyless_window_times_out_when_log_stays_empty(settings, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)
    route = respx.get(ACTION_LOG_URL).mock(return_value=httpx.Response(200, json=[]))
    outcome = wait_for_recent_action(
        OrchClient(settings), settings, "3.NE", 1724680000000, description="push"
    )
    assert outcome.state == "TIMEOUT"
    assert outcome.key == ""
    assert outcome.detail == "push: no action-log records appeared within 5.0s"
    assert route.call_count > 1


@respx.mock
def test_keyless_window_times_out_while_in_flight(settings, monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", fake_sleep)
    respx.get(ACTION_LOG_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _log_record(
                    taskStatus="In Progress", percentComplete=40,
                    completionStatus=False, endTime=0, result="",
                )
            ],
        )
    )
    outcome = wait_for_recent_action(OrchClient(settings), settings, "3.NE", 1724680000000)
    assert outcome.state == "TIMEOUT"
    assert outcome.key == "guid-A"  # the last-seen guid is carried for triage
    assert outcome.detail == "job did not finish within 5.0s"
