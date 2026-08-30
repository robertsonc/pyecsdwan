"""Async jobs fail closed on anything they cannot confirm (#64).

Four holes, all the same shape — *absence of evidence read as evidence*:

1. A terminal record was a success unless its ``result`` contained one of five
   English failure words. "Completed" + "Rejected" was a success. So was every
   localized result, and every wording a future release invents.
2. A keyless ``saveChanges`` returned SUCCESS having checked nothing.
3. The keyless action-log waiter took the newest guid in its window, so a
   concurrent push became this transaction's outcome.
4. ``TemplateAssociation.rollback()`` reported success from a 204, never
   polling at all — the revert, running after something already went wrong,
   was the one operation that never checked.

Each is now evidence-gated, and each test below both proves the gate and, by
its fixture, shows what used to slip through: the shapes here are the ones the
old rules called success.

The mock is the other half of this. It used to answer every finished action
with ``result: "mock apply complete"`` — a string no Orchestrator emits, and
harmless only because success was inferred from absence. Under the allowlist
it failed 81 tests at once. A fixture that models what the API does is not a
detail; it is the thing that decides whether these tests can fail.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, jobs, txn
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.journal import TxnJournal, TxnState
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import SUCCESS_RESULT, MockState, run_in_thread


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
    ctx = Ctx(client=client, resolver=Resolver(client))
    return {
        "ctx": ctx,
        "settings": settings,
        "state": state,
        "client": client,
        "candidate": CandidateStore(settings.origin),
    }


def _commit(world: dict[str, Any]) -> Any:
    plan = txn.build_plan(world["ctx"], default_registry, world["candidate"])
    return txn.commit(world["ctx"], default_registry, plan, world["settings"])


def _appliance(state: MockState, ne_pk: str) -> dict[str, Any]:
    return next(a for a in state.appliances if a["nePk"] == ne_pk)


# -- 1. the classifier: version shapes, and what the old rule let through -----


#: Terminal records by the shape they wear, with the class each must land in.
#:
#: The `unknown-*` rows are the acceptance criteria stated literally: every one
#: of them passed the old absence-of-failure test, because none of them
#: contains fail/error/denied/unable/cannot/invalid/reject/refused. They are
#: the reason success is now an allowlist rather than a denylist.
SHAPES: list[tuple[str, dict[str, Any], str]] = [
    # -- observed successes (docs/research/job-shapes.md) --
    ("upper COMPLETED + Success", {"taskStatus": "COMPLETED", "result": "Success"}, "SUCCESS"),
    ("mixed-case Completed", {"taskStatus": "Completed", "result": "Success"}, "SUCCESS"),
    ("Success plus detail", {"taskStatus": "Completed", "result": "Success: 3 of 3"}, "SUCCESS"),
    ("empty result on a done status", {"taskStatus": "Completed", "result": ""}, "SUCCESS"),
    ("no result field at all", {"taskStatus": "Done"}, "SUCCESS"),
    # -- explicit failures --
    ("FAILED status", {"taskStatus": "FAILED", "result": "Success"}, "FAILED"),
    ("failure text, done status", {"taskStatus": "Completed", "result": "error: denied"}, "FAILED"),
    ("Invalid configuration", {"taskStatus": "Completed", "result": "Invalid config"}, "FAILED"),
    ("Rejected", {"taskStatus": "Completed", "result": "Rejected by appliance"}, "FAILED"),
    ("Cancelled status", {"taskStatus": "Cancelled", "result": ""}, "FAILED"),
    # -- unrecognised: neither list matches, so the answer is "cannot tell" --
    ("localized success", {"taskStatus": "Completed", "result": "Configuracion OK"}, "UNKNOWN"),
    ("plausible English prose", {"taskStatus": "Completed", "result": "pushed"}, "UNKNOWN"),
    ("terminal but unknown status", {"taskStatus": "Wibbled", "result": "Success"}, "UNKNOWN"),
    ("completionStatus no tiebreak", {"taskStatus": "Wibbled", "completionStatus": 1}, "UNKNOWN"),
]


@pytest.mark.parametrize("label,record,expected", SHAPES, ids=[s[0] for s in SHAPES])
def test_terminal_record_classification(label: str, record: dict[str, Any], expected: str) -> None:
    assert jobs._record_state(record) == expected, label


def test_the_two_shapes_the_issue_names_are_not_success() -> None:
    """#64's acceptance criteria, spelled out rather than left to the table.

    Both were success under the old rule for the same dull reason: neither
    "Invalid configuration" nor "Rejected" contains any of the five failure
    words it looked for. `invalid` and `reject` are on the failure list now,
    but that is not what makes this safe — the allowlist is. Delete the
    additions and the localized cases below still fail closed.
    """
    completed = {"taskStatus": "Completed"}
    assert jobs._record_state({**completed, "result": "Invalid configuration"}) == "FAILED"
    assert jobs._record_state({**completed, "result": "Rejected"}) == "FAILED"


def test_the_allowlist_is_what_makes_unknown_shapes_fail_closed() -> None:
    """Guards the guard: verified by deleting the guard.

    With the allowlist emptied, an unrecognised result on a done status must
    NOT become a success. If widening `SUCCESS_RESULT_SHAPES` to nothing still
    lets a shape through, the allowlist is not the thing deciding.
    """
    localized = {"taskStatus": "Completed", "result": "Configuracion aplicada"}
    assert jobs._record_state(localized) == "UNKNOWN"

    original = jobs.SUCCESS_RESULT_SHAPES
    try:
        jobs.SUCCESS_RESULT_SHAPES = ()
        assert jobs._record_state({"taskStatus": "Completed", "result": "Success"}) == "UNKNOWN"
    finally:
        jobs.SUCCESS_RESULT_SHAPES = original
    # ...and restoring it restores the success, so the test really moved it.
    assert jobs._record_state({"taskStatus": "Completed", "result": "Success"}) == "SUCCESS"


def test_failure_outranks_unknown_across_records() -> None:
    """A group with one failure and one unrecognised record is FAILED: the
    definite answer wins over the ambiguous one, and the detail names the
    Orchestrator's own text rather than this poller's uncertainty."""
    outcome = jobs._terminal_outcome(
        "g",
        [
            {"nepk": "1.NE", "endTime": 1, "taskStatus": "Completed", "result": "who knows"},
            {"nepk": "2.NE", "endTime": 1, "taskStatus": "Failed", "result": "boom"},
        ],
    )
    assert outcome is not None
    assert outcome.state == "FAILED"
    assert "boom" in outcome.detail


def test_an_unknown_outcome_quotes_the_shape_it_did_not_recognise() -> None:
    """The detail is the mechanism by which docs/research/job-shapes.md grows:
    an operator cannot report a shape the tool did not show them."""
    outcome = jobs._terminal_outcome(
        "g", [{"nepk": "1.NE", "endTime": 1, "taskStatus": "Completed", "result": "Voltooid"}]
    )
    assert outcome is not None
    assert outcome.state == "UNKNOWN"
    assert "Voltooid" in outcome.detail
    assert "taskStatus='Completed'" in outcome.detail
    assert "cannot be confirmed" in outcome.detail


def test_the_mock_emits_a_shape_the_field_has_actually_seen() -> None:
    """The fixture that decides whether every other test here can fail.

    `SUCCESS_RESULT` must be a shape the allowlist accepts *for the reason the
    allowlist accepts it* — not a string chosen to make tests pass. Asserting
    the classifier's verdict on the mock's own constant ties the two together,
    so re-inventing "mock apply complete" fails here rather than silently
    turning every apply test into a test of the UNKNOWN path.
    """
    assert jobs._record_state({"taskStatus": "Completed", "result": SUCCESS_RESULT}) == "SUCCESS"
    assert SUCCESS_RESULT.lower().startswith(jobs.SUCCESS_RESULT_SHAPES[0])


def test_every_consumer_makes_a_new_job_state_fail_closed() -> None:
    """Why adding UNKNOWN needed no changes at ~20 call sites — and a guard so
    that stays true.

    Every consumer asks `state != "SUCCESS"` (or `== "SUCCESS"`), so a state
    they have never heard of fails. A consumer that instead enumerated the
    failures — `state in ("FAILED", "TIMEOUT")` — would treat UNKNOWN as
    success, silently, at whichever single call site adopted the habit. This
    reads the source because that is where the property lives: no runtime test
    can visit every branch of every resource's apply() and rollback().
    """
    import re

    root = pathlib.Path(jobs.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Membership against *string* values only: `TxnState` enums are a
            # different `.state`, and an unknown member of a closed enum
            # cannot appear at runtime anyway.
            if re.search(r"""\.state\s+(?:not\s+)?in\s*[\(\[{]\s*["']""", line):
                offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
            if re.search(r'\.state\s*[!=]=\s*"(?:FAILED|TIMEOUT|UNKNOWN)"', line):
                offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    assert not offenders, (
        "these compare a job state against a list of known-bad values, so a job "
        "state added later passes them: " + "; ".join(offenders)
    )
    # Guards the guard: the scan must actually be reading the source, and the
    # pattern must still match the thing it is looking for.
    assert len(list(root.rglob("*.py"))) > 50
    assert re.search(r"""\.state\s+(?:not\s+)?in\s*[\(\[{]\s*["']""", 'if o.state in ("FAILED",):')
    assert re.search(r'\.state\s*[!=]=\s*"(?:FAILED|TIMEOUT|UNKNOWN)"', 'if o.state == "FAILED":')


# -- 2. a keyless save is verified against the fabric --------------------------


def test_keyless_save_confirms_via_the_unsaved_flag(world: dict[str, Any]) -> None:
    client, state, ctx = world["client"], world["state"], world["ctx"]
    state.save_changes_keyless = True
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 9000})
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True

    outcome = ctx.save_changes(["3.NE"])
    assert outcome.state == "SUCCESS"
    assert "persistence confirmed" in outcome.detail
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is False


def test_keyless_save_that_did_not_persist_is_failure(world: dict[str, Any]) -> None:
    """The case the old code could not distinguish from the one above: the
    Orchestrator accepted the request and nothing was written to flash."""
    client, state, ctx = world["client"], world["state"], world["ctx"]
    state.save_changes_keyless = True
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 9000})
    state.fail_next_action = True  # the mock leaves the flag set, like a real failed save

    outcome = ctx.save_changes(["3.NE"])
    assert outcome.state == "FAILED"
    assert "not persisted" in outcome.detail
    assert "3.NE" in outcome.detail
    assert _appliance(state, "3.NE")["hasUnsavedChanges"] is True


def test_keyless_save_with_no_readable_flag_is_unknown_not_success(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Orchestrator that reports no `hasUnsavedChanges` leaves the question
    unanswerable. Unanswerable is not "fine" — that inference is the whole
    bug. UNKNOWN rather than FAILED, because the save may well have worked.
    """
    client, state, ctx = world["client"], world["state"], world["ctx"]
    state.save_changes_keyless = True
    client.appliance_request("POST", "3.NE", "virtualif/ip", json_body={"mtu": 9000})

    real_get = OrchClient.get

    def strip_flag(self: Any, path: str, **kwargs: Any) -> Any:
        raw = real_get(self, path, **kwargs)
        if path == "/appliance" and isinstance(raw, list):
            return [{k: v for k, v in a.items() if k != jobs.UNSAVED_FIELD} for a in raw]
        return raw

    monkeypatch.setattr(OrchClient, "get", strip_flag)
    outcome = ctx.save_changes(["3.NE"])
    assert outcome.state == "UNKNOWN"
    assert jobs.UNSAVED_FIELD in outcome.detail


def test_an_appliance_missing_from_the_inventory_is_unknown(world: dict[str, Any]) -> None:
    """"Not in the list" answers nothing about persistence, and an appliance
    that vanished mid-save is exactly when a wrong answer costs most."""
    state, client = world["state"], world["client"]
    state.save_changes_keyless = True
    pending = jobs._unsaved_appliances(client, ["3.NE", "99.NE"])
    assert pending is None


def test_a_keyless_save_cannot_confirm_a_transaction(world: dict[str, Any]) -> None:
    """#64's acceptance criterion, end to end: a keyless save with no
    persistence evidence must not produce a CONFIRMED commit."""
    state, candidate = world["state"], world["candidate"]
    state.save_changes_keyless = True
    state.fail_next_action = True

    ref = Ref("appliance/banners", "banners", appliance="BR1-EC")
    candidate.set_path(ref, ["login"], "authorized use only")
    report = _commit(world)

    assert not report.ok
    assert report.state in (TxnState.REVERTED, TxnState.REVERT_FAILED)
    assert any("not persisted" in m for m in report.messages), report.messages


# -- 3. concurrent action-log noise cannot be attributed to us ----------------


def test_a_concurrent_push_makes_the_window_ambiguous(world: dict[str, Any]) -> None:
    """#64: "Concurrent unrelated action-log records cannot be attributed to
    the transaction."

    The noise is created *after* the pre-write snapshot, so it is genuinely
    indistinguishable from our own push: same appliance, same window, newer.
    Under the old newest-guid rule it would simply have become our outcome.
    """
    state, ctx, settings = world["state"], world["ctx"], world["settings"]
    since_ms = 0  # a window wide enough to hold both
    before = jobs.action_log_guids(ctx.client, "3.NE", since_ms)
    state.new_action(ne_pks=["3.NE"], name="somebody else's push")
    state.new_action(ne_pks=["3.NE"], name="our push")

    outcome = jobs.wait_for_recent_action(
        ctx.client, settings, "3.NE", since_ms, "template push", ignore_guids=before
    )
    assert outcome.state == "UNKNOWN"
    assert outcome.key == ""
    assert "concurrent activity" in outcome.detail


def test_noise_that_predates_the_write_is_not_ambiguity(world: dict[str, Any]) -> None:
    """The other half, and the reason correlation compares identity rather
    than timestamps: an operator's *earlier* push, and this appliance's own
    previous push, are both already in the log when we write. Refusing on
    those would fail every revert that follows an apply."""
    state, ctx, settings = world["state"], world["ctx"], world["settings"]
    state.new_action(ne_pks=["3.NE"], name="an earlier push")
    since_ms = 0
    before = jobs.action_log_guids(ctx.client, "3.NE", since_ms)
    assert len(before) == 1

    ours = state.new_action(ne_pks=["3.NE"], name="our push")
    outcome = jobs.wait_for_recent_action(
        ctx.client, settings, "3.NE", since_ms, "template push", ignore_guids=before
    )
    assert outcome.state == "SUCCESS"
    assert outcome.key == ours


def test_without_the_snapshot_the_same_fabric_is_ambiguous(world: dict[str, Any]) -> None:
    """Guards the guard, by deleting it: drop `ignore_guids` and the test above
    becomes the ambiguous case. That is what proves the snapshot — not the
    timestamps, which are identical either way — is doing the work."""
    state, ctx, settings = world["state"], world["ctx"], world["settings"]
    state.new_action(ne_pks=["3.NE"], name="an earlier push")
    state.new_action(ne_pks=["3.NE"], name="our push")

    outcome = jobs.wait_for_recent_action(ctx.client, settings, "3.NE", 0, "template push")
    assert outcome.state == "UNKNOWN"


def test_records_for_another_appliance_are_not_our_noise(world: dict[str, Any]) -> None:
    """Correlation's first dimension. A busy fabric is busy everywhere; only
    activity on *this* appliance can be confused with this push."""
    state, ctx, settings = world["state"], world["ctx"], world["settings"]
    before = jobs.action_log_guids(ctx.client, "3.NE", 0)
    state.new_action(ne_pks=["5.NE"], name="a push somewhere else")
    ours = state.new_action(ne_pks=["3.NE"], name="our push")

    outcome = jobs.wait_for_recent_action(
        ctx.client, settings, "3.NE", 0, "template push", ignore_guids=before
    )
    assert outcome.state == "SUCCESS"
    assert outcome.key == ours


def test_the_operation_name_filter_narrows_the_window(world: dict[str, Any]) -> None:
    """`action_name` is the "operation type" dimension #64 asks for. No caller
    passes one yet — no `name` string has been observed in the field, and
    guessing would filter out the record being awaited — so this proves the
    mechanism works for whoever records one in docs/research/job-shapes.md."""
    state, ctx, settings = world["state"], world["ctx"], world["settings"]
    before = jobs.action_log_guids(ctx.client, "3.NE", 0)
    state.new_action(ne_pks=["3.NE"], name="appliance backup")
    ours = state.new_action(ne_pks=["3.NE"], name="template push")

    outcome = jobs.wait_for_recent_action(
        ctx.client, settings, "3.NE", 0, "", action_name="template push", ignore_guids=before
    )
    assert outcome.state == "SUCCESS"
    assert outcome.key == ours


# -- 4. a revert confirms its own push ----------------------------------------


def test_a_template_revert_polls_its_own_push(world: dict[str, Any]) -> None:
    """`rollback()` used to POST and return ok=True from the 204. The revert is
    the operation running after something already went wrong; it is the worst
    place in the system to assume."""
    state, candidate = world["state"], world["candidate"]
    state.template_groups["G1"] = {"name": "G1", "templates": []}
    state.template_groups["G2"] = {"name": "G2", "templates": []}
    state.template_association["3.NE"] = ["G1"]
    state.fail_next_action = True  # fails the apply's push; the revert's succeeds

    ref = Ref("template-association", "BR1-EC")
    candidate.set_desired(ref, {"template_groups": ["G1", "G2"]})
    report = _commit(world)

    assert not report.ok
    assert report.state == TxnState.REVERTED
    assert [j.state for j in report.jobs] == ["FAILED", "SUCCESS"]
    assert state.template_association["3.NE"] == ["G1"]

    assert report.txn_id is not None
    journal = TxnJournal.open(config.journal_root() / report.txn_id)
    reverts = [e for e in journal.events() if e.get("event") == "REVERT_RESULT"]
    assert len(reverts) == 1 and reverts[0]["ok"] is True


def test_an_unconfirmable_push_is_not_a_confirmed_association(world: dict[str, Any]) -> None:
    """A SUCCESS action-log outcome that names no appliance confirms the
    control-plane association and nothing about the appliance — #64's last
    work item. Driven by clearing the mock's per-appliance records, which is
    what a control-plane-only log looks like from here."""
    outcome = jobs.JobOutcome(key="g", state="SUCCESS", detail="", per_appliance={})
    from pyecsdwan.resources.templates import TemplateAssociation

    result = TemplateAssociation()._confirmed(
        Ref("template-association", "BR1-EC"), "3.NE", outcome, "set to ['G1']"
    )
    assert not result.ok
    assert "no action-log record names 3.NE" in result.message
    assert "not the push to the appliance" in result.message


def test_the_same_outcome_with_appliance_evidence_is_confirmed() -> None:
    """The other side of the gate, so the check above is not simply refusing
    everything."""
    from pyecsdwan.resources.templates import TemplateAssociation

    outcome = jobs.JobOutcome(
        key="g", state="SUCCESS", detail="", per_appliance={"3.NE": SUCCESS_RESULT}
    )
    result = TemplateAssociation()._confirmed(
        Ref("template-association", "BR1-EC"), "3.NE", outcome, "set to ['G1']"
    )
    assert result.ok
    assert "template push confirmed" in result.message
