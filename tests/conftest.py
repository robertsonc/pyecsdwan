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

    def _hold(origin: str, scope: str = "commit", ready_timeout: float = 20.0):
        # The origin is a URL, so it cannot go into a file name unescaped —
        # `https://` would silently make `holder-https:/` a directory (#63).
        stem = config.origin_slug(config.as_origin(origin))
        ready = tmp_path / f"holder-{stem}-{scope}.ready"
        body = f"""
import time
from pathlib import Path
from pyecsdwan.locking import HostLock
with HostLock({origin!r}, {scope!r}, timeout=10.0):
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


@pytest.fixture
def wide_fabric():
    """Grow a mock fabric past the three seeded appliances (issue #76).

    #76 reports the bug at six appliances::

        appliance/nat-maps: name required; instances: global, global, global,
                                                      global, global, global

    Three is enough to *have* the bug but not enough to show it at the scale
    that made it unusable, and the seeded fabric is three. Seventy test files
    reference that fabric and several assert its size, so this grows it per
    test rather than moving the seed: a changed count there would surface as
    dozens of unrelated failures, which is exactly where a real regression
    hides in the noise.

    ``MockState.appliances`` is a plain list field, so appending is all it
    takes. Returns the names added. Call before anything populates the
    resolver cache, or refresh it afterwards.
    """

    def _grow(state, total: int = 6, seed_ecos: dict | None = None):
        added = []
        # Seeded nePks are odd (1.NE, 3.NE, 5.NE); keep the pattern going so a
        # generated nePk can never collide with a hand-written fixture's.
        next_pk = max(int(a["nePk"].split(".")[0]) for a in state.appliances) + 2
        branch = sum(1 for a in state.appliances if a["hostName"].startswith("BR"))
        while len(state.appliances) < total:
            branch += 1
            ne_pk, name = f"{next_pk}.NE", f"BR{branch}-EC"
            state.appliances.append(
                {
                    "nePk": ne_pk,
                    "id": ne_pk,
                    "hostName": name,
                    "site": f"Branch-{branch}",
                    "model": "EC-S",
                    "state": 1,
                    "reachabilityChannel": 2,
                    "hasUnsavedChanges": False,
                    "rebootRequired": False,
                }
            )
            if seed_ecos:
                state.appliance_ecos.setdefault(ne_pk, {}).update(seed_ecos)
            added.append(name)
            next_pk += 2
        return added

    return _grow
