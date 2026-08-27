"""Unit tests for pyecsdwan.jobs: action-key polling, keyless window polling,
and the save-changes primitive."""

import json
import time

import httpx
import pytest
import respx

from pyecsdwan.client import OrchClient
from pyecsdwan.jobs import (
    cancel_action,
    save_changes,
    wait_for_action,
    wait_for_preconfig_apply,
    wait_for_recent_action,
)

STATUS_URL = "https://orch.example.com/gms/rest/action/status"
ACTION_LOG_URL = "https://orch.example.com/gms/rest/action"
SAVE_URL = "https://orch.example.com/gms/rest/appliance/saveChanges"
PRECONFIG_APPLY_URL = "https://orch.example.com/gms/rest/gms/appliance/preconfiguration/apply"


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


# -- save_changes (issue #11) --------------------------------------------------


@respx.mock
def test_save_changes_batches_dedupes_and_awaits(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    save_route = respx.post(SAVE_URL).mock(
        return_value=httpx.Response(200, json={"clientKey": "sk-1"})
    )
    status_route = respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "nepk": "1.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1724680000000,
                    "result": "Success",
                },
                {
                    "nepk": "3.NE",
                    "taskStatus": "Completed",
                    "percentComplete": 100,
                    "completionStatus": True,
                    "endTime": 1724680000001,
                    "result": "Success",
                },
            ],
        )
    )
    outcome = save_changes(OrchClient(settings), ["3.NE", "1.NE", "3.NE"], settings)
    assert outcome.state == "SUCCESS"
    assert outcome.key == "sk-1"
    # One batched POST: duplicates collapsed, order deterministic.
    assert save_route.call_count == 1
    assert json.loads(save_route.calls.last.request.content) == {"nePks": ["1.NE", "3.NE"]}
    assert status_route.calls.last.request.url.params["key"] == "sk-1"
    assert outcome.per_appliance == {"1.NE": "Success", "3.NE": "Success"}


@respx.mock
def test_save_changes_failed_action_is_reported(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    respx.post(SAVE_URL).mock(return_value=httpx.Response(200, json={"clientKey": "sk-2"}))
    respx.get(STATUS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "taskStatus": "Failed",
                "percentComplete": 100,
                "completionStatus": False,
                "endTime": 1724680000000,
                "result": "disk full",
            },
        )
    )
    outcome = save_changes(OrchClient(settings), ["3.NE"], settings)
    assert outcome.state == "FAILED"
    assert "disk full" in outcome.detail


def test_save_changes_empty_list_is_noop(settings):
    # No respx routes active: any HTTP request would error the test.
    outcome = save_changes(OrchClient(settings), [], settings)
    assert outcome.state == "SUCCESS"
    assert outcome.key == ""
    assert "no appliances" in outcome.detail


def test_save_changes_rejects_invalid_ne_pk(settings):
    with pytest.raises(ValueError, match="invalid appliance nePk"):
        save_changes(OrchClient(settings), ["BR1-EC"], settings)


@respx.mock
def test_save_changes_keyless_response_tolerated(settings):
    # Off-spec 204 with no clientKey: accepted-but-unawaited, not a failure.
    # No /action/status route is registered, so any poll attempt would fail.
    route = respx.post(SAVE_URL).mock(return_value=httpx.Response(204))
    outcome = save_changes(OrchClient(settings), ["3.NE"], settings)
    assert route.call_count == 1
    assert outcome.state == "SUCCESS"
    assert "not awaited" in outcome.detail


# -- preconfig apply: numeric taskStatus channel (#23) -----------------------


@respx.mock
def test_preconfig_apply_success(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    respx.get(PRECONFIG_APPLY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "taskStatus": 2,
                "completionStatus": True,
                "guid": "guid-precfg-1",
                "result": [
                    {"nePk": "1.NE", "taskStatus": 2, "completionStatus": True, "result": "ok"},
                ],
            },
        )
    )
    outcome = wait_for_preconfig_apply(OrchClient(settings), "preconfig-1", settings)
    assert outcome.state == "SUCCESS"
    assert outcome.key == "preconfig-1"
    assert outcome.per_appliance == {"1.NE": "ok"}


@respx.mock
def test_preconfig_apply_failure(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    respx.get(PRECONFIG_APPLY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "taskStatus": 2,
                "completionStatus": False,
                "result": [
                    {
                        "nePk": "1.NE",
                        "taskStatus": 2,
                        "completionStatus": False,
                        "result": "yaml parse error at line 4",
                    },
                ],
            },
        )
    )
    outcome = wait_for_preconfig_apply(OrchClient(settings), "preconfig-2", settings)
    assert outcome.state == "FAILED"
    assert outcome.per_appliance == {"1.NE": "yaml parse error at line 4"}


@respx.mock
def test_preconfig_apply_keeps_polling_while_in_progress(settings, monkeypatch):
    """taskStatus 0/1 must not be read as terminal even if completionStatus
    happens to be present and falsy — only taskStatus==2 is terminal (the
    API's own 'completionStatus valid only when taskStatus==2' caveat)."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    responses = iter(
        [
            httpx.Response(200, json={"taskStatus": 0, "completionStatus": False}),
            httpx.Response(200, json={"taskStatus": 1, "completionStatus": False}),
            httpx.Response(
                200,
                json={"taskStatus": 2, "completionStatus": True, "result": []},
            ),
        ]
    )
    route = respx.get(PRECONFIG_APPLY_URL).mock(side_effect=lambda request: next(responses))
    outcome = wait_for_preconfig_apply(OrchClient(settings), "preconfig-3", settings)
    assert outcome.state == "SUCCESS"
    assert route.call_count == 3


@respx.mock
def test_preconfig_apply_timeout(settings, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    real_monotonic = time.monotonic
    calls = iter([0.0, settings.job_timeout + 1])
    monkeypatch.setattr(
        time, "monotonic", lambda: next(calls, real_monotonic())
    )
    respx.get(PRECONFIG_APPLY_URL).mock(
        return_value=httpx.Response(200, json={"taskStatus": 1})
    )
    outcome = wait_for_preconfig_apply(OrchClient(settings), "preconfig-4", settings)
    assert outcome.state == "TIMEOUT"
    assert outcome.key == "preconfig-4"
    assert "did not finish" in outcome.detail


# -- job cancellation (#24) ---------------------------------------------------


@respx.mock
def test_cancel_action_success(settings):
    route = respx.post("https://orch.example.com/gms/rest/action/cancel").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    assert cancel_action(OrchClient(settings), "guid-cancel-1") is True
    assert route.calls.last.request.url.params["key"] == "guid-cancel-1"


@respx.mock
def test_cancel_action_reads_falsy_response_field(settings):
    respx.post("https://orch.example.com/gms/rest/action/cancel").mock(
        return_value=httpx.Response(200, json={"cancelled": False})
    )
    assert cancel_action(OrchClient(settings), "guid-cancel-2") is False


@respx.mock
def test_cancel_action_empty_response_treated_as_accepted(settings):
    # A bare 204/empty body — no boolean field to read — is treated as
    # accepted, matching the "Orchestrator's own response governs" default.
    respx.post("https://orch.example.com/gms/rest/action/cancel").mock(
        return_value=httpx.Response(204)
    )
    assert cancel_action(OrchClient(settings), "guid-cancel-3") is True
