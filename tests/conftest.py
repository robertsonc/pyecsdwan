"""Shared fixtures for the pyecsdwan unit test suite."""

import pytest

from pyecsdwan import config


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    """Point ECSDWAN_HOME at a per-test tmp dir so all state roots live there."""
    monkeypatch.setenv(config.ENV_HOME, str(tmp_path))
    config.ensure_dirs()
    return tmp_path


@pytest.fixture
def settings():
    """Settings against a fake Orchestrator, with short job-poll knobs."""
    return config.Settings(
        orch_url="https://orch.example.com",
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
