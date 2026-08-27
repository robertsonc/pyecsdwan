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


@pytest.fixture
def lock_holder(state_home, tmp_path):
    """Spawn a *separate process* holding a host-scoped lock (issue #63).

    Holding the lock in-process proves nothing: locks are re-entrant per
    process on purpose, so an in-process holder nests instead of blocking.
    Exclusion is a cross-process property, so testing it takes a real
    second process.
    """
    import os
    import subprocess
    import sys
    import time

    procs = []

    def _hold(host: str, scope: str = "commit", ready_timeout: float = 20.0):
        ready = tmp_path / f"holder-{host}-{scope}.ready"
        body = f"""
import time
from pathlib import Path
from pyecsdwan.locking import HostLock
with HostLock({host!r}, {scope!r}, timeout=10.0):
    Path({str(ready)!r}).write_text("ready")
    time.sleep(300)
"""
        proc = subprocess.Popen(
            [sys.executable, "-c", body],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, ECSDWAN_HOME=str(state_home)),
        )
        procs.append(proc)
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if ready.exists():
                return proc
            if proc.poll() is not None:
                raise AssertionError(f"lock holder died: {proc.communicate()[1]}")
            time.sleep(0.02)
        raise AssertionError("lock holder never acquired the lock")

    yield _hold

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
