"""Concurrent candidate writers must not lose staged state (#63).

The host-scoped lock already serialized read-modify-write cycles, so two
`set` calls could not clobber each other. What it did not cover is the *last
act of a successful commit*: `commit` called `candidate.clear()`, which takes
the lock, re-reads the file — picking up whatever another shell staged while
the commit was running — and then wipes all of it.

So shell A committing X destroyed shell B's unrelated Y, silently, at the
moment A reported success. The lock made that worse rather than better: it
guaranteed A saw B's work before deleting it.

Driven with real processes. Two `CandidateStore` objects in one process share
nothing that matters here — the store is a file, and the failure is about what
two independent readers of that file do to each other.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import txn as txn_mod
from pyecsdwan.candidate import CandidateItem, CandidateStore
from pyecsdwan.contract import Ref

HOST = "orch.example.com"
X = Ref("appliance/banners", "global", appliance="BR1-EC")
Y = Ref("appliance/banners", "global", appliance="BR2-EC")

_STAGE = """
import sys
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ref
host, appliance, issue = sys.argv[1:4]
store = CandidateStore(host)
store.set_desired(Ref("appliance/banners", "global", appliance=appliance), {"issue": issue})
"""


@pytest.fixture
def mock_fabric(state_home: Any) -> Any:
    """A live client against the bundled mock, for paths that really commit."""
    pytest.importorskip("pyecsdwan.mock.server")
    import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
    from pyecsdwan import config
    from pyecsdwan.client import OrchClient
    from pyecsdwan.mock.server import run_in_thread

    base_url, state, shutdown = run_in_thread()
    state.reset()
    settings = config.Settings(
        orch_url=base_url, api_key="k", job_timeout=5.0,
        job_poll_initial=0.01, job_poll_max=0.02,
    )
    yield settings, OrchClient(settings)
    shutdown()


@pytest.fixture
def stage_elsewhere(state_home: Any) -> Any:
    """Stage an item from a *separate process*, as a second shell would."""

    def _stage(appliance: str, issue: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", _STAGE, HOST, appliance, issue],
            env=dict(os.environ, ECSDWAN_HOME=str(state_home)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    return _stage


def _keys(store: CandidateStore | None = None) -> set[str]:
    return {i.ref_key for i in (store or CandidateStore(HOST)).ordered_items()}


def test_a_successful_commit_keeps_work_staged_by_another_shell(
    state_home: Any, stage_elsewhere: Any
) -> None:
    """The bug, end to end. A stages X and plans it; B stages Y from another
    process; A's commit succeeds and acknowledges what it committed."""
    shell_a = CandidateStore(HOST)
    shell_a.set_desired(X, {"issue": "A's change"})
    planned = shell_a.ordered_items()

    stage_elsewhere("BR2-EC", "B's change, staged during A's commit")

    kept = shell_a.clear_committed(planned)

    assert _keys() == {Y.key()}, "B's unrelated staged work was destroyed"
    assert kept == []


def test_an_item_rewritten_after_planning_survives(
    state_home: Any, stage_elsewhere: Any
) -> None:
    """Same ref, changed content. B's rewrite is intent nobody has committed,
    so acknowledging A's *older* version of it would discard a change that
    never reached the fabric."""
    shell_a = CandidateStore(HOST)
    shell_a.set_desired(X, {"issue": "A's change"})
    planned = shell_a.ordered_items()

    stage_elsewhere("BR1-EC", "B rewrote it mid-flight")

    kept = shell_a.clear_committed(planned)

    assert _keys() == {X.key()}
    assert CandidateStore(HOST).items[X.key()].intent == {"issue": "B rewrote it mid-flight"}
    assert kept == [X.key()], "the operator is not told their item was kept"


def test_what_the_commit_did_apply_is_actually_cleared(
    state_home: Any,
) -> None:
    """Guards the guard. An acknowledgement that kept everything would pass
    both tests above and leave the candidate permanently dirty, so the next
    commit would re-apply changes that already landed."""
    shell_a = CandidateStore(HOST)
    shell_a.set_desired(X, {"issue": "A's change"})

    assert shell_a.clear_committed(shell_a.ordered_items()) == []
    assert _keys() == set()


def test_an_item_deleted_by_another_shell_is_not_resurrected(
    state_home: Any, stage_elsewhere: Any
) -> None:
    """The acknowledgement removes; it must never add. Writing back the
    planned snapshot would restore an item another shell had dropped."""
    shell_a = CandidateStore(HOST)
    shell_a.set_desired(X, {"issue": "A's change"})
    planned = shell_a.ordered_items()

    CandidateStore(HOST).drop(X)

    shell_a.clear_committed(planned)

    assert _keys() == set()


def test_discard_still_wipes_everything(state_home: Any) -> None:
    """`clear()` is the operator's explicit `discard` and must stay a blind
    wipe — the per-item acknowledgement is for commit, not for someone who
    asked to throw their staging away."""
    store = CandidateStore(HOST)
    store.set_desired(X, {"issue": "x"})
    store.set_desired(Y, {"issue": "y"})

    store.clear()

    assert _keys() == set()


def test_two_processes_staging_at_once_both_survive(
    state_home: Any, stage_elsewhere: Any
) -> None:
    """The property the lock already provided, pinned here because this file
    is where someone will look for it — and because the fix above must not
    regress it."""
    stage_elsewhere("BR1-EC", "from one shell")
    stage_elsewhere("BR2-EC", "from another")

    assert _keys() == {X.key(), Y.key()}


# -- the acknowledgement is actually on the commit path ----------------------


def test_the_commit_command_acknowledges_rather_than_wiping(
    state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation sweep found this missing.

    Every test above drives `clear_committed` directly, so reverting the call
    site to `candidate.clear()` left them all green — the guard existed and
    nothing put it on the path. This runs the real `commit` command with
    another shell staging mid-flight.
    """
    pytest.importorskip("pyecsdwan.mock.server")
    from typer.testing import CliRunner

    import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
    from pyecsdwan import txn
    from pyecsdwan.cli import main as cli_main
    from pyecsdwan.mock.server import run_in_thread

    base_url, _state, shutdown = run_in_thread()
    try:
        port = base_url.rsplit(":", 1)[1]
        host = f"127.0.0.1:{port}"
        shell_a = CandidateStore(host)
        shell_a.set_desired(Ref("appliance/banners", "global", appliance="BR1-EC"),
                            {"issue": "A's change"})

        real_commit = txn.commit

        def commit_while_b_stages(*args: Any, **kwargs: Any) -> Any:
            # B stages an unrelated item while A's transaction is in flight.
            CandidateStore(host).set_desired(
                Ref("appliance/banners", "global", appliance="BR2-EC"),
                {"issue": "B's change"},
            )
            return real_commit(*args, **kwargs)

        monkeypatch.setattr(cli_main.txn, "commit", commit_while_b_stages)
        result = CliRunner().invoke(cli_main.app, ["--mock", port, "commit"])
        assert result.exit_code == 0, result.output

        remaining = {i.ref_key for i in CandidateStore(host).ordered_items()}
        assert remaining == {"appliance%2Fbanners:BR2-EC:global"}, (
            "commit did not acknowledge per item: B's staged work is gone"
        )
    finally:
        shutdown()


def test_the_shell_commit_also_acknowledges_rather_than_wiping(
    state_home: Any, mock_fabric: Any
) -> None:
    """The interactive shell is the *primary* Junos-style interface, and it had
    its own copy of this cycle: build a plan, commit, `state.candidate.clear()`.

    Fixing the scriptable path left the shell destroying another shell's work,
    and no test noticed, because the integration test drove `cli_main.app
    commit` rather than `dispatch_config`. Both now run the same
    `txn.commit_candidate`, and this drives the shell one so a third copy
    cannot reappear unseen.
    """
    from rich.console import Console

    from pyecsdwan.cli.shell import ShellState, dispatch_config
    from pyecsdwan.contract import Ctx
    from pyecsdwan.resolver import Resolver

    settings, client = mock_fabric
    host = settings.host
    shell_a = CandidateStore(host)
    shell_a.set_desired(Ref("appliance/banners", "global", appliance="BR1-EC"),
                        {"issue": "A's change"})
    state = ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=__import__("pyecsdwan.registry", fromlist=["x"]).default_registry,
        settings=settings,
        console=Console(record=True),
        candidate=shell_a,
    )

    real_commit = txn_mod.commit

    def commit_while_b_stages(*args: Any, **kwargs: Any) -> Any:
        CandidateStore(host).set_desired(
            Ref("appliance/banners", "global", appliance="BR2-EC"), {"issue": "B's change"}
        )
        return real_commit(*args, **kwargs)

    txn_mod.commit = commit_while_b_stages  # type: ignore[assignment]
    try:
        dispatch_config("commit", state)
    finally:
        txn_mod.commit = real_commit  # type: ignore[assignment]

    remaining = {i.ref_key for i in CandidateStore(host).ordered_items()}
    assert remaining == {"appliance%2Fbanners:BR2-EC:global"}, (
        "the shell wiped the candidate instead of acknowledging per item"
    )


def test_the_snapshot_is_compared_by_value_not_identity(state_home: Any) -> None:
    """`clear_committed` deep-copies what it is given, so a caller holding the
    same objects the store later reloads cannot make an item look unchanged
    when it is not."""
    store = CandidateStore(HOST)
    store.set_desired(X, {"issue": "original"})
    planned = [CandidateItem(ref_key=X.key(), mode="replace", intent={"issue": "original"})]

    raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
    raw["items"][0]["intent"] = {"issue": "changed on disk"}
    Path(store.path).write_text(json.dumps(raw), encoding="utf-8")

    assert store.clear_committed(planned) == [X.key()]
