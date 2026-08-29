"""`ec-cli apply --from <dir>` — declarative apply as one transaction (epic #8).

The whole design claim is that this needed *no second transaction engine*. A
declared change and a hand-staged one differ only in where the intent came
from; from `build_plan` onward they are the same objects going through the same
guards into the same journal. So the tests that matter here are not "does it
write" — it is `txn.commit`, which is covered elsewhere — but:

* the two intent sources really do produce the same plan;
* the guards are still on the path, not bypassed by the new entry point;
* `--dry-run` writes nothing and exits the way a CI gate expects;
* two sources of intent cannot be silently merged into one transaction.

That last one is a decision, not a mechanism. A non-empty candidate is someone's
in-progress work, and folding it into a declarative apply would commit changes
the directory never declared with no way to see which came from where.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, desired, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli import main as cli_main
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ownership, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

runner = CliRunner()

KIND = "appliance/banners"
REF = Ref(KIND, "global", appliance="BR1-EC")
DECLARED = "appliances/BR1-EC/banners/global.yaml"


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(
        orch_url=base_url,
        api_key="test-key",
        job_timeout=5.0,
        job_poll_initial=0.01,
        job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "settings": settings,
        "state": state,
        "port": base_url.rsplit(":", 1)[1],
    }


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _cli(world: dict[str, Any], *args: str) -> Any:
    return runner.invoke(cli_main.app, ["--mock", world["port"], *args])


def _live_banners(world: dict[str, Any]) -> Any:
    return world["state"].appliance_ecos["3.NE"]["banners"]


# -- the two intent sources agree --------------------------------------------


def test_a_directory_and_a_candidate_build_the_same_plan(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The claim that lets declarative apply reuse the transaction engine.

    If these diverged, `drift --from` would report one thing and `apply --from`
    would do another — the failure mode the shared `IntentSource` exists to
    make impossible.
    """
    ctx = world["ctx"]
    _write(tmp_path, DECLARED, "issue: declared banner\n")

    staged = CandidateStore(world["settings"].host)
    staged.set_desired(REF, {"issue": "declared banner"})

    from_dir = txn.build_plan(ctx, default_registry, desired.load(default_registry, tmp_path))
    from_candidate = txn.build_plan(ctx, default_registry, staged)

    assert [i.ref.key() for i in from_dir.items] == [i.ref.key() for i in from_candidate.items]
    assert [i.diff.entries for i in from_dir.items] == [
        i.diff.entries for i in from_candidate.items
    ]
    assert from_dir.changed_items and from_candidate.changed_items


# -- --dry-run ---------------------------------------------------------------


def test_dry_run_exits_one_and_writes_nothing(
    world: dict[str, Any], tmp_path: Path
) -> None:
    before = dict(_live_banners(world))
    _write(tmp_path, DECLARED, "issue: not what the box has\n")

    result = _cli(world, "apply", "--from", str(tmp_path), "--dry-run")

    assert result.exit_code == 1, result.output
    assert _live_banners(world) == before, "--dry-run wrote to the fabric"


def test_dry_run_exits_zero_when_already_in_sync(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """Guards the guard: a dry run that always exited 1 would fail every CI
    gate forever and the test above would still pass."""
    live = _live_banners(world)
    body = "".join(f"{k}: {v!r}\n" for k, v in live.items())
    _write(tmp_path, DECLARED, body)

    result = _cli(world, "apply", "--from", str(tmp_path), "--dry-run")
    assert result.exit_code == 0, result.output


# -- the real thing ----------------------------------------------------------


def test_apply_commits_the_declared_state(world: dict[str, Any], tmp_path: Path) -> None:
    _write(tmp_path, DECLARED, "issue: applied from git\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert _live_banners(world)["issue"] == "applied from git"


def test_applying_twice_is_a_no_op(world: dict[str, Any], tmp_path: Path) -> None:
    """The idempotency the whole contract rests on, exercised through the new
    entry point rather than assumed from `commit`'s own tests."""
    _write(tmp_path, DECLARED, "issue: applied from git\n")
    assert _cli(world, "apply", "--from", str(tmp_path)).exit_code == 0

    again = _cli(world, "apply", "--from", str(tmp_path))
    assert again.exit_code == 0, again.output
    assert "no changes" in again.output


def test_a_malformed_directory_is_fatal_and_writes_nothing(
    world: dict[str, Any], tmp_path: Path
) -> None:
    before = dict(_live_banners(world))
    _write(tmp_path, DECLARED, "issue: fine\n")
    _write(tmp_path, "appliances/BR2-EC/banners/global.yaml", "issue: [\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0
    assert _live_banners(world) == before, "wrote part of a declaration it could not read"


def test_a_failed_apply_exits_nonzero(world: dict[str, Any], tmp_path: Path) -> None:
    """The exit code has to reflect what happened on the fabric.

    A pipeline that runs `apply` reads the exit code and nothing else. If a
    write is rejected and this still exits 0, the pipeline reports a successful
    deployment over a failed one — "absence of evidence read as evidence", the
    failure this project keeps finding. The mutation sweep caught this: hard-
    coding `Exit(0)` left every other test here green.
    """
    _write(tmp_path, DECLARED, "issue: this write will be rejected\n")
    world["state"].fail_next_action = True  # the apply's own save-changes fails

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0, result.output


# -- two sources of intent are never merged ----------------------------------


def test_a_non_empty_candidate_refuses_the_apply(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """A decision, not a mechanism: the directory and the operator's staged
    work are two different intents, and one transaction carrying both would
    commit changes the directory never declared."""
    before = dict(_live_banners(world))
    _write(tmp_path, DECLARED, "issue: from git\n")

    staged = CandidateStore(world["settings"].host)
    staged.set_path(Ref(KIND, "global", appliance="BR2-EC"), ["issue"], "hand-staged")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0
    assert "staged in the candidate" in result.output
    # And it names a way out that exists.
    assert "discard" in result.output
    assert _live_banners(world) == before


def test_the_named_escape_hatches_are_real_commands() -> None:
    """The refusal above tells the operator to run `commit` or `discard`.
    A message naming a command that does not exist is worse than no message."""
    names = {c.name or c.callback.__name__ for c in cli_main.app.registered_commands}
    assert "commit" in names
    assert "discard" in names


# -- one transaction owns the fabric at a time (#100) -------------------------


def _pending_window(world: dict[str, Any]) -> Any:
    """A transaction that has written and is waiting to be confirmed."""
    from pyecsdwan.journal import TxnJournal, TxnState

    journal = TxnJournal.create(world["settings"].host, [REF])
    journal.set_state(TxnState.APPLIED_UNCONFIRMED)
    return journal


def test_apply_refuses_during_an_active_confirm_window(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """Issue #100, and the most serious thing in this PR.

    An APPLIED_UNCONFIRMED transaction ends by restoring the snapshot it took
    *before* it wrote. A second transaction that lands inside that window is
    therefore not merely racing — it is guaranteed to be erased when the
    window expires, with no error and no journal entry explaining where the
    change went.

    The `commit` command had this guard. `apply --from` called `txn.commit`
    directly, so it did not: the check was a property of one entry point
    rather than of the engine, which is exactly the kind of guard a new entry
    point walks around. It lives in `_commit_locked` now, so every caller —
    apply, `commit`, the shell, any library user — gets it.
    """
    before = dict(_live_banners(world))
    _pending_window(world)
    _write(tmp_path, DECLARED, "issue: would be erased when the window expires\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0
    assert _live_banners(world) == before, "wrote inside another transaction's window"


def test_the_refusal_is_the_engine_not_the_command(world: dict[str, Any]) -> None:
    """Stated against `txn.commit` directly, because a test that only drove
    the CLI would pass again the moment someone adds a third entry point."""
    _pending_window(world)
    staged = CandidateStore(world["settings"].host)
    staged.set_desired(REF, {"issue": "from a library caller"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    with pytest.raises(txn.CommitError) as caught:
        txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert "APPLIED_UNCONFIRMED" in str(caught.value)
    # ... and it names both ways out, which is what the operator does next.
    assert "confirm" in str(caught.value)
    assert "rollback --pending" in str(caught.value)


def test_nothing_is_journaled_for_a_refused_commit(world: dict[str, Any]) -> None:
    """The guard sits before `TxnJournal.create`, so a refused commit leaves
    no half-transaction behind for the orphan scan to puzzle over."""
    from pyecsdwan.journal import list_txns

    _pending_window(world)
    before = {t.meta.txn_id for t in list_txns()}
    staged = CandidateStore(world["settings"].host)
    staged.set_desired(REF, {"issue": "refused"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    with pytest.raises(txn.CommitError):
        txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert {t.meta.txn_id for t in list_txns()} == before


def test_a_settled_transaction_does_not_block_anything(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """Guards the guard. Refusing whenever *any* transaction exists would pass
    every test above and make the tool single-use."""
    from pyecsdwan.journal import TxnJournal, TxnState

    TxnJournal.create(world["settings"].host, [REF]).set_state(TxnState.CONFIRMED)
    _write(tmp_path, DECLARED, "issue: after a confirmed transaction\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code == 0, result.output
    assert _live_banners(world)["issue"] == "after a confirmed transaction"


def test_another_host_does_not_block_this_one(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The journal is shared across Orchestrators; the constraint is not.
    An unconfirmed window on one fabric says nothing about another."""
    from pyecsdwan.journal import TxnJournal, TxnState

    other = TxnJournal.create("some-other-orchestrator.example.com", [REF])
    other.set_state(TxnState.APPLIED_UNCONFIRMED)
    _write(tmp_path, DECLARED, "issue: different fabric entirely\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code == 0, result.output


# -- a lost snapshot must not become a delete (#110) --------------------------


def test_a_ref_with_no_recorded_snapshot_is_refused_not_deleted(
    world: dict[str, Any],
) -> None:
    """`rollback(ctx, ref, None)` means "this did not exist before, remove it".

    `snapshots()` returns a dict, so "no snapshot recorded" and "recorded as
    absent" are genuinely different — but `_revert_items` read it with
    `.get()`, which collapses them to None. A snapshot the journal lost (#110's
    torn tail was one way) therefore told the revert to *delete* a resource
    that had been there all along: the failure mode is not an incomplete
    rollback but a destructive one.

    Now the missing case is refused and reported, and the item is left in its
    modified state rather than removed — the safe direction, because a change
    an operator can see and undo beats a deletion they cannot.
    """
    from pyecsdwan.journal import TxnJournal

    journal = TxnJournal.create(world["settings"].host, [REF])
    # APPLY_START without the SNAPSHOT that should precede it.
    journal.append("APPLY_START", ref=REF.key())
    before = dict(_live_banners(world))

    staged = CandidateStore(world["settings"].host)
    staged.set_desired(REF, {"issue": "x"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)
    report = txn.CommitReport(ok=False, txn_id=journal.meta.txn_id)

    txn._revert_items(world["ctx"], journal, plan.items, report)

    assert report.state == "REVERT_FAILED"
    assert any("nothing to restore from" in m for m in report.messages)
    assert _live_banners(world) == before, "deleted a resource it could not restore"


# -- the guards are still on the path ----------------------------------------


def test_ownership_still_refuses_through_apply(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new entry point must not become a way around the safety guards.

    Ownership is the one to prove, because it is the guard #20 exists for and
    the one an operator would most regret losing. Forced UNKNOWN rather than
    OWNED: UNKNOWN is the state the tri-state added, so it is the one a
    reintroduced fail-open would drop first.
    """
    _write(tmp_path, DECLARED, "issue: should never land\n")
    before = dict(_live_banners(world))

    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "managed_by",
        lambda self, ctx, ref: Ownership.unknown("template selection unreadable (403)"),
    )

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0, result.output
    assert _live_banners(world) == before, "wrote despite unknown ownership"

    # ... and the documented break-glass still works, so the refusal is the
    # guard firing rather than the command being broken.
    ok = _cli(world, "apply", "--from", str(tmp_path), "--override-template")
    assert ok.exit_code == 0, ok.output
    assert _live_banners(world)["issue"] == "should never land"
