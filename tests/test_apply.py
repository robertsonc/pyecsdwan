"""`ec-cli apply --from <dir>` — declarative apply as one transaction (epic #8).

The design claim is that this needs *no second transaction engine*: a declared
change and a hand-staged one differ only in where the intent came from, and
from `build_plan` onward they are the same objects going through the same
guards into the same journal.

**The write half is currently disabled.** Under the ratified spec a
declaration is typed *partial* intent, and a resource must prove it can build a
complete target without erasing unknown, unmodeled or write-only fields before
one is written (D7/D8) — no resource has that proof yet (T8). So `apply --from`
previews and refuses to write, and the engine guards it shares are exercised
here through `commit`, which is the surface that actually writes today. Testing
them through a path that cannot reach them would be theatre.

What this file covers:

* the two intent sources really do produce the same plan;
* `--dry-run` writes nothing and exits the way a CI gate expects;
* the write path refuses, and its commit-only flags refuse rather than being
  ignored;
* invalid input costs no connection at all;
* the engine guards — ownership, confirm windows, lost snapshots — still fire.

Two sources of intent are never silently merged: a non-empty candidate is
someone's in-progress work, and folding it in would commit changes the
directory never declared with no way to see which came from where.
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


def _envelope(body: str, state: str = "present") -> str:
    """Wrap a bare spec body in the ratified declaration envelope (T7).

    The tests read better naming only the values they care about, and the
    envelope is fixed boilerplate that would otherwise be repeated in every
    fixture. Malformed bodies stay malformed once indented, which is what the
    invalid-input tests rely on.
    """
    lines = body.strip("\n").splitlines()
    spec = "".join(f"  {line}\n" for line in lines) if lines else "  {}\n"
    return f"apiVersion: {desired.API_VERSION}\nstate: {state}\nspec:\n{spec}"


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_envelope(body), encoding="utf-8")
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

    staged = CandidateStore(world["settings"].origin)
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


def test_apply_refuses_to_write_until_materialization_is_proven(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The ratified spec calls a declaration *typed partial intent* (D7), and
    requires a resource to prove it can build a complete target without
    erasing unknown, unmodeled or write-only fields before one is written
    (D8/R12). No resource has that proof yet — T8.

    Until it does, this path would send a partial declaration as a full
    replacement. It refuses rather than writing under semantics the spec does
    not sanction; `--dry-run` stays open because a preview writes nothing.
    """
    before = dict(_live_banners(world))
    _write(tmp_path, DECLARED, "issue: applied from git\n")

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0
    assert "cannot write yet" in result.output
    assert _live_banners(world) == before


@pytest.mark.parametrize(
    "flag", ["--confirm-minutes", "--force", "--override-template", "--rebase"]
)
def test_commit_only_flags_are_refused_not_ignored(
    world: dict[str, Any], tmp_path: Path, flag: str
) -> None:
    """A flag that cannot do anything is the lie this project keeps removing:
    an operator who passed `--confirm-minutes` and saw a plan would reasonably
    believe a confirm window had been armed."""
    _write(tmp_path, DECLARED, "issue: x\n")
    args = ["apply", "--from", str(tmp_path), "--dry-run", flag]
    if flag == "--confirm-minutes":
        args.append("10")

    result = _cli(world, *args)

    assert result.exit_code != 0
    assert "only affect committing" in result.output


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
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "this write will be rejected"})
    world["state"].fail_next_action = True  # the commit's own save-changes fails

    result = _cli(world, "commit")

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

    staged = CandidateStore(world["settings"].origin)
    staged.set_path(Ref(KIND, "global", appliance="BR2-EC"), ["issue"], "hand-staged")

    result = _cli(world, "apply", "--from", str(tmp_path), "--dry-run")

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


# -- invalid input costs no connection (R2) -----------------------------------


def test_an_empty_directory_never_builds_a_client(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2/D6. A mistyped `--from` path should cost nothing: no credentials, no
    resolver call, no round trip to an Orchestrator to be told the directory
    was wrong. Asserted by making bootstrap explode — if the command reaches
    it, this fails with the wrong error.
    """
    from pyecsdwan import runtime

    monkeypatch.setattr(
        runtime, "bootstrap",
        lambda **kw: pytest.fail("a client was constructed for an invalid directory"),
    )

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0
    assert "no declarations" in result.output


def test_a_malformed_directory_never_builds_a_client(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the guard's other half: the check must fire on invalid content,
    not only on emptiness."""
    from pyecsdwan import runtime

    _write(tmp_path, DECLARED, "issue: fine\n")
    (tmp_path / "appliances" / "BR2-EC" / "banners").mkdir(parents=True)
    (tmp_path / "appliances" / "BR2-EC" / "banners" / "global.yaml").write_text(
        "issue: [\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        runtime, "bootstrap",
        lambda **kw: pytest.fail("a client was constructed for an invalid directory"),
    )

    result = _cli(world, "apply", "--from", str(tmp_path))

    assert result.exit_code != 0


def test_drift_from_a_bad_directory_never_builds_a_client(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`drift --from` is the CI entry point, so it is the one most likely to be
    pointed at a path that does not exist yet."""
    from pyecsdwan import runtime

    monkeypatch.setattr(
        runtime, "bootstrap",
        lambda **kw: pytest.fail("a client was constructed for an invalid directory"),
    )

    result = _cli(world, "drift", "--from", str(tmp_path), "--yes")

    assert result.exit_code != 0


# -- one transaction owns the fabric at a time (#100) -------------------------


def _pending_window(world: dict[str, Any]) -> Any:
    """A transaction that has written and is waiting to be confirmed."""
    from pyecsdwan.journal import TxnJournal, TxnState

    journal = TxnJournal.create(world["settings"].origin, [REF])
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
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "would be erased when the window expires"})

    result = _cli(world, "commit")

    assert result.exit_code != 0
    assert _live_banners(world) == before, "wrote inside another transaction's window"


def test_the_refusal_is_the_engine_not_the_command(world: dict[str, Any]) -> None:
    """Stated against `txn.commit` directly, because a test that only drove
    the CLI would pass again the moment someone adds a third entry point."""
    _pending_window(world)
    staged = CandidateStore(world["settings"].origin)
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
    staged = CandidateStore(world["settings"].origin)
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

    TxnJournal.create(world["settings"].origin, [REF]).set_state(TxnState.CONFIRMED)
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "after a confirmed transaction"})

    result = _cli(world, "commit")

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
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "different fabric entirely"})

    result = _cli(world, "commit")

    assert result.exit_code == 0, result.output


# -- an unverifiable write is a failed one (#103) -----------------------------


def test_a_raising_verify_reverts_instead_of_escaping(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#103. `apply` was inside the try; `verify` on the next line was not.

    A read timeout or an odd response while *confirming* a write therefore
    propagated straight out of `commit()`: the caller got a raw exception, the
    fabric kept the change, and the transaction sat in APPLYING with no revert
    and no terminal state — the one shape commit-confirm exists to make
    impossible. Reproduced before fixing.

    Verify runs after a write has landed, which is exactly why it must not be
    the unguarded step: an unverifiable write is a failed one, because not
    getting to assume is the entire point of verifying.
    """
    before = dict(_live_banners(world))
    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "verify",
        lambda self, ctx, ref, desired: (_ for _ in ()).throw(
            TimeoutError("read timed out confirming the write")
        ),
    )
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "written, then verify blew up"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert not report.ok
    assert report.state in ("REVERTED", "REVERT_FAILED"), report.state
    assert _live_banners(world) == before, "left the fabric modified"


def test_the_verify_failure_journals_its_typed_cause(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Verify failed" and "verify could not run" are different incidents, and
    an operator reading the journal afterwards needs to tell them apart."""
    from pyecsdwan.journal import list_txns

    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "verify",
        lambda self, ctx, ref, desired: (_ for _ in ()).throw(TimeoutError("read timed out")),
    )
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "x"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    txn.commit(world["ctx"], default_registry, plan, world["settings"])

    events = list_txns()[0].events()
    failed = next(e for e in events if e["event"] == "VERIFY_FAILED")
    assert "TimeoutError" in failed["error"]


def test_a_verify_that_returns_false_still_reverts(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the guard: the pre-existing path must keep working, and a fix
    that swallowed every verify result would satisfy the tests above."""
    before = dict(_live_banners(world))
    resource = default_registry.get(KIND)
    monkeypatch.setattr(type(resource), "verify", lambda self, ctx, ref, desired: False)
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "verify says no"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert not report.ok
    assert _live_banners(world) == before


# -- a rollback is not believed on its own word (#103) ------------------------


def _force_revert(monkeypatch: pytest.MonkeyPatch, marker: str) -> None:
    """Fail the post-apply verify for the forward change only.

    Deliberately selective. `_confirm_restored` calls `verify` too — with the
    *snapshot* as desired rather than the staged intent — so a blanket `False`
    would fail the restore confirmation as well, and a fixture that sabotages
    the thing it is setting up proves nothing. This first version did exactly
    that and made a passing implementation look broken.
    """
    resource = default_registry.get(KIND)
    real = type(resource).verify

    def selective(self: Any, ctx: Any, ref: Any, desired: Any) -> bool:
        if isinstance(desired, dict) and desired.get("issue") == marker:
            return False
        return bool(real(self, ctx, ref, desired))

    monkeypatch.setattr(type(resource), "verify", selective)


def test_a_rollback_that_restored_nothing_is_not_reported_as_restored(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#103's second criterion. `rollback()`'s own `ok` was the whole evidence.

    A plugin returning success without restoring anything produced "fabric
    restored to pre-commit snapshot" over a fabric that still held the change.
    The report a transaction hands back is the operator's only account of what
    state the network is in — it cannot be a restatement of what the write
    path claimed about itself.
    """
    from pyecsdwan.contract import ApplyResult

    _force_revert(monkeypatch, "the change that should be undone")
    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "rollback",
        lambda self, ctx, ref, snapshot: ApplyResult(ok=True, message="restored"),
    )
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "the change that should be undone"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert report.state == "REVERT_FAILED"
    assert not report.reverted
    assert any("does not match its pre-change snapshot" in m for m in report.messages)
    assert not any("fabric restored" in m for m in report.messages)


def test_an_unreadable_resource_after_rollback_is_not_a_restore(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """"I could not check" is not "it worked" — the same inference #64 removed
    from the job poller, applied to the last place a transaction speaks.

    Asserted on `_confirm_restored` rather than through a whole commit: making
    `fetch` raise globally breaks the snapshot-before-write long before any
    rollback happens, so the commit would fail for the wrong reason and the
    test would pass without exercising this at all.
    """
    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "verify",
        lambda self, ctx, ref, desired: (_ for _ in ()).throw(
            TimeoutError("appliance unreachable")
        ),
    )

    ok, detail = txn._confirm_restored(
        world["ctx"], _plan_item(world), {"issue": "before"}, "restored"
    )

    assert not ok
    assert "could not be confirmed" in detail
    assert "TimeoutError" in detail


def test_a_real_rollback_still_reports_restored(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the guard, and it is the one that matters here: a confirmation
    step that could never pass would turn every auto-revert into
    REVERT_FAILED and send operators hunting for damage that is not there."""
    before = dict(_live_banners(world))
    _force_revert(monkeypatch, "applied, then reverted for real")
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "applied, then reverted for real"})
    plan = txn.build_plan(world["ctx"], default_registry, staged)

    report = txn.commit(world["ctx"], default_registry, plan, world["settings"])

    assert report.state == "REVERTED", report.messages
    assert report.reverted == [REF.key()]
    assert _live_banners(world) == before


def test_a_deletion_rollback_says_it_is_unconfirmed(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest gap. A snapshot of None means the resource did not exist, so
    rollback deleted it — and confirming a deletion means reading an absence,
    which a raising fetch does not prove (it is equally a timeout). Allowed
    through, but the message says it was not confirmed rather than implying it
    was."""
    ok, detail = txn._confirm_restored(
        world["ctx"], _plan_item(world), None, "deleted"
    )

    assert ok
    assert "not independently confirmed" in detail


def _plan_item(world: dict[str, Any]) -> Any:
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "x"})
    return txn.build_plan(world["ctx"], default_registry, staged).items[0]


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

    journal = TxnJournal.create(world["settings"].origin, [REF])
    # APPLY_START without the SNAPSHOT that should precede it.
    journal.append("APPLY_START", ref=REF.key())
    before = dict(_live_banners(world))

    staged = CandidateStore(world["settings"].origin)
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
    before = dict(_live_banners(world))
    staged = CandidateStore(world["settings"].origin)
    staged.set_desired(REF, {"issue": "should never land"})

    resource = default_registry.get(KIND)
    monkeypatch.setattr(
        type(resource),
        "managed_by",
        lambda self, ctx, ref, diff=None: Ownership.unknown(
            "template selection unreadable (403)"
        ),
    )

    result = _cli(world, "commit")

    assert result.exit_code != 0, result.output
    assert _live_banners(world) == before, "wrote despite unknown ownership"

    # ... and the documented break-glass still works, so the refusal is the
    # guard firing rather than the command being broken.
    ok = _cli(world, "commit", "--override-template")
    assert ok.exit_code == 0, ok.output
    assert _live_banners(world)["issue"] == "should never land"


def test_a_dry_run_shows_a_blocked_reference_and_exits_nonzero(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The command-level seam of T8: a directory whose only reference is a
    kind that cannot be materialized must not preview as "nothing to do".
    The blocker is printed where the plan is, and the exit code says the
    directory asked for something this build refuses (Principle II)."""
    _write(tmp_path, "fabric/interface-labels/global.yaml", "wan: {}")

    result = _cli(world, "apply", "--from", str(tmp_path), "--dry-run")

    assert result.exit_code != 0, result.output
    assert "[blocked interface-labels:global]" in result.output
    assert "not declaratively writable" in result.output
