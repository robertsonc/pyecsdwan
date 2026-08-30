"""The evidence ladder, and the bookkeeping that keeps it honest (#66).

What this file can check and what it cannot is the whole design. It cannot
check that anyone actually ran the live protocol — only a real fabric can, and
this repository must never assume one. What it *can* check is that the ledger
never claims more than it carries: that every curated kind has a row, that a
live claim carries the versions it was made against, and that mock evidence
cannot reach the top rung no matter how the file is edited.

That distinction is the point of the ladder. "Shipped" used to cover a resource
implemented against a spec, a resource green against the mock, and a resource
whose writes had been run and rolled back on real gear. Those are three
different promises to an operator, and only the last one is worth anything when
the change is going into production tonight.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import evidence
from pyecsdwan.cli.main import app
from pyecsdwan.contract import Tier
from pyecsdwan.registry import default_registry

runner = CliRunner()
OFFLINE = ["--orch-url", "https://nowhere.invalid"]


@pytest.fixture(autouse=True)
def _fresh_ledger() -> Any:
    """The loader caches; tests that repoint it must not leak into the next."""
    evidence.clear_cache()
    yield
    evidence.clear_cache()


def _write_ledger(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any]) -> None:
    (tmp_path / "ledger.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv(evidence.ENV_EVIDENCE_DIR, str(tmp_path))
    evidence.clear_cache()


# -- the ladder itself --------------------------------------------------------


def test_the_levels_are_the_ones_the_issue_names() -> None:
    """#66 names six levels by these exact strings, and the labels are what
    the CLI prints and the ledger stores — so a rename here is a data
    migration, not a cosmetic edit."""
    assert [level.label for level in evidence.Evidence] == [
        "implemented",
        "mock-verified",
        "live-read-verified",
        "live-no-op-write-verified",
        "live-change-and-rollback-verified",
        "production-supported",
    ]


def test_the_levels_are_ordered_so_comparisons_mean_something() -> None:
    """Every consumer asks `>= some floor`. An unordered enum would make each
    of those a membership test someone has to keep in step."""
    assert evidence.Evidence.MOCK_VERIFIED < evidence.LIVE_FLOOR
    assert evidence.LIVE_FLOOR < evidence.WRITE_SUPPORTED_FLOOR
    assert evidence.WRITE_SUPPORTED_FLOOR < evidence.Evidence.PRODUCTION_SUPPORTED


def test_required_behaviors_are_cumulative() -> None:
    """A claim at level 5 carries level 3's and 4's obligations. A rollback
    nobody watched persist is not a rollback anyone can rely on."""
    required = evidence.required_behaviors(evidence.WRITE_SUPPORTED_FLOOR)
    assert evidence.LIVE_READ in required
    assert evidence.NO_OP_ROUND_TRIP in required
    assert evidence.ROLLBACK in required
    assert evidence.SAVE_PERSISTENCE in required
    # ...and not the level-6 failure paths.
    assert evidence.PERMISSION_DENIED not in required
    assert set(evidence.required_behaviors(evidence.Evidence.PRODUCTION_SUPPORTED)) == set(
        evidence.BEHAVIORS
    )


def test_nothing_below_the_live_floor_requires_a_fabric() -> None:
    """The two rungs this repository can reach on its own must be reachable
    without gear — otherwise `make check` could never leave level 1."""
    for level in (evidence.Evidence.IMPLEMENTED, evidence.Evidence.MOCK_VERIFIED):
        assert evidence.required_behaviors(level) == ()


# -- a record cannot claim more than it carries -------------------------------


def test_a_live_claim_without_a_version_is_refused() -> None:
    """The finding this whole issue rests on. This repository already had live
    observations — payload shapes captured against a real lab Orchestrator —
    and none of them can promote anything, because nobody wrote down the
    version. So the rule is enforced, not documented."""
    record = evidence.Record(
        kind="x",
        level=evidence.Evidence.LIVE_READ_VERIFIED,
        behaviors=(evidence.LIVE_READ,),
    )
    with pytest.raises(evidence.LedgerError) as exc:
        record.validate()
    for field in ("orchestrator", "ecos", "auth_mode", "observed", "source"):
        assert field in str(exc.value)


def test_a_live_claim_with_every_version_is_accepted() -> None:
    """The other side of the gate, so the rule above is not simply refusing
    everything."""
    evidence.Record(
        kind="x",
        level=evidence.Evidence.LIVE_READ_VERIFIED,
        orchestrator="9.4.2",
        ecos="9.3.1.40100",
        auth_mode="api-key",
        observed="2026-09-14",
        source="docs/sitrep/whatever.md",
        behaviors=(evidence.LIVE_READ,),
    ).validate()


def test_a_claim_without_its_behaviors_is_refused() -> None:
    record = evidence.Record(
        kind="x",
        level=evidence.WRITE_SUPPORTED_FLOOR,
        orchestrator="9.4.2",
        ecos="9.3.1",
        auth_mode="api-key",
        observed="2026-09-14",
        source="s",
        behaviors=(evidence.LIVE_READ,),  # the rest were never run
    )
    with pytest.raises(evidence.LedgerError) as exc:
        record.validate()
    assert evidence.ROLLBACK in str(exc.value)
    assert evidence.SAVE_PERSISTENCE in str(exc.value)


def test_an_invented_behavior_is_refused() -> None:
    """The behaviors are constants precisely so a ledger cannot claim "tested"
    in prose nobody can check."""
    with pytest.raises(evidence.LedgerError) as exc:
        evidence.Record(
            kind="x", level=evidence.Evidence.MOCK_VERIFIED, behaviors=("looked-fine",)
        ).validate()
    assert "looked-fine" in str(exc.value)


def test_mock_evidence_cannot_reach_production_supported() -> None:
    """#66's acceptance criterion, and it holds structurally rather than by
    policy: the top rung demands four behaviors the bundled mock cannot
    witness on anyone's behalf, plus versions it does not have."""
    everything_the_mock_could_show = (
        evidence.LIVE_READ,
        evidence.NO_OP_ROUND_TRIP,
        evidence.REAL_CHANGE,
        evidence.POST_APPLY_VERIFICATION,
        evidence.ROLLBACK,
        evidence.SAVE_PERSISTENCE,
    )
    with pytest.raises(evidence.LedgerError):
        evidence.Record(
            kind="x",
            level=evidence.Evidence.PRODUCTION_SUPPORTED,
            orchestrator="mock",
            ecos="mock",
            auth_mode="api-key",
            observed="2026-09-14",
            source="s",
            behaviors=everything_the_mock_could_show,
        ).validate()


# -- the vendored ledger ------------------------------------------------------


def test_the_shipped_ledger_loads_and_validates() -> None:
    led = evidence.ledger()
    assert led.available
    assert led.records


def test_every_registered_kind_has_a_record() -> None:
    """"unrecorded" is a bookkeeping failure, not an answer — a kind with no
    row would print blank next to kinds that print mock-verified, and read as
    less rather than unknown."""
    missing = sorted(k for k in default_registry.kinds() if evidence.ledger().get(k) is None)
    assert not missing, f"registered but absent from the evidence ledger: {missing}"


def test_the_ledger_names_no_kind_that_does_not_exist() -> None:
    """The other direction: a row for a kind that was renamed or deleted is a
    claim about nothing, and it would keep the count looking healthy."""
    stray = sorted(set(evidence.ledger().records) - set(default_registry.kinds()))
    assert not stray, f"in the evidence ledger but not registered: {stray}"


def test_a_generated_stub_claims_only_implemented() -> None:
    """A stub's normalize() raises, so it cannot even be planned against the
    mock. Claiming mock-verified for one would be claiming a test that cannot
    have run."""
    for kind in default_registry.kinds():
        if default_registry.get(kind).tier is not Tier.CURATED:
            record = evidence.ledger().get(kind)
            assert record is not None
            assert record.level is evidence.Evidence.IMPLEMENTED, kind


def test_the_support_matrix_is_empty_and_that_is_the_honest_answer() -> None:
    """Today nothing has been verified against a recorded version.

    This test is expected to *change* when someone runs the protocol — that is
    what it is for. It fails loudly rather than letting the matrix quietly
    gain entries nobody reviewed, and the failure message says what to do.
    """
    matrix = evidence.ledger().support
    assert matrix.spec_baseline, "the spec baseline is known even when nothing is verified"
    assert not matrix.orchestrator and not matrix.ecos, (
        "the support matrix gained a verified version. If real evidence was recorded, "
        "update this test and docs/ROADMAP.md's parity table together — the roadmap's "
        "claims and the ledger are supposed to be the same claim."
    )


def test_the_ledger_note_says_what_the_live_reads_do_and_do_not_buy() -> None:
    """The ledger carries its own reasoning, and the reasoning changed.

    It used to explain why the 2026-08-26 live reads counted for nothing: none
    recorded the Orchestrator version. The 2026-08-30 sweep recorded it, so 38
    resources are `live-read-verified` — and the note now has a harder job,
    because a reader who sees "live" anywhere is one step from believing the
    write paths were tested. They were not. The note must say both.
    """
    note = evidence.ledger().note
    assert "live-read-verified" in note
    assert "docs/live-validation.md" in note
    # The limit, stated in the file rather than left to be inferred.
    assert "no write path" in note.lower()


def test_no_record_claims_a_write_level() -> None:
    """The claim the parity map makes, enforced against the data.

    Level 4 and above are claims that a write reached real gear and was
    verified. Nothing here has earned one, and a ledger that quietly acquired
    one would make `show coverage --evidence` lie to an operator."""
    written = [
        r.kind
        for r in evidence.ledger().records.values()
        if r.level >= evidence.Evidence.LIVE_NO_OP_WRITE_VERIFIED
    ]
    assert written == [], f"{written} claim a verified write path"


# -- degradation --------------------------------------------------------------


def test_a_missing_ledger_degrades_rather_than_crashing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same rule as the vendored specs: a read-only report must not be the
    thing that crashes on a partial install."""
    monkeypatch.setenv(evidence.ENV_EVIDENCE_DIR, str(tmp_path / "nope"))
    evidence.clear_cache()
    led = evidence.ledger()
    assert not led.available
    assert led.records == {}


def test_an_over_claiming_ledger_raises_rather_than_being_ignored(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed file is not the same as a missing one. Quietly ignoring a
    ledger that claims production support without versions is exactly the
    failure this module exists to prevent, so it is loud."""
    _write_ledger(
        tmp_path,
        monkeypatch,
        {"records": [{"kind": "bio", "level": "production-supported"}]},
    )
    with pytest.raises(evidence.LedgerError):
        evidence.ledger()


def test_a_duplicated_kind_raises(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rows for one kind means one of them is silently ignored, and which
    one depends on file order."""
    _write_ledger(
        tmp_path,
        monkeypatch,
        {
            "records": [
                {"kind": "bio", "level": "mock-verified"},
                {"kind": "bio", "level": "implemented"},
            ]
        },
    )
    with pytest.raises(evidence.LedgerError):
        evidence.ledger()


def test_an_unknown_level_label_names_the_valid_ones() -> None:
    with pytest.raises(ValueError) as exc:
        evidence.Evidence.from_label("pretty-sure")
    assert "mock-verified" in str(exc.value)


# -- the version warning ------------------------------------------------------


def test_an_empty_support_matrix_warns_about_every_fabric() -> None:
    """The state today, and the important case: a matrix that stayed silent
    while empty would be indistinguishable from one that had verified the
    world."""
    warning = evidence.version_warning("9.4.2", "9.3.1.40100")
    assert "9.4.2" in warning and "9.3.1.40100" in warning
    assert "no version has been recorded" in warning


def test_a_verified_version_does_not_warn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ledger(
        tmp_path,
        monkeypatch,
        {"support": {"orchestrator": ["9.4.2"], "ecos": ["9.3.1"]}, "records": []},
    )
    assert evidence.version_warning("9.4.2", "9.3.1") == ""
    # ...and a neighbouring version still does.
    assert "9.4.3" in evidence.version_warning("9.4.3", "9.3.1")


def test_no_versions_at_hand_means_nothing_to_warn_about() -> None:
    """`show fabric version` calls this with "" when the Orchestrator version
    could not be read. Warning that "" is unverified would be noise on top of
    an error the operator has already been shown."""
    assert evidence.version_warning("", "") == ""


# -- what the CLI actually prints ---------------------------------------------


def test_show_coverage_prints_the_evidence_column() -> None:
    result = runner.invoke(app, [*OFFLINE, "show", "coverage", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert all("evidence" in row for row in payload["kinds"])
    assert payload["evidence"]["available"] is True
    assert payload["evidence"]["support"]["spec_baseline"]


def test_show_coverage_warns_that_no_write_path_is_live_verified() -> None:
    """#66's "demote or visibly warn". Demotion would be wrong — the code is
    genuinely curated — so the warning carries it, and it names the count
    rather than gesturing at "some"."""
    result = runner.invoke(app, [*OFFLINE, "show", "coverage"])
    assert result.exit_code == 0, result.output
    assert "no live change-and-rollback evidence" in result.output


def test_the_evidence_view_shows_the_support_matrix() -> None:
    result = runner.invoke(app, [*OFFLINE, "show", "coverage", "--evidence"])
    assert result.exit_code == 0, result.output
    assert "Orchestrator verified" in result.output
    assert "spec baseline" in result.output
    # The self-referential pointer is suppressed inside the view it points at.
    assert "`show coverage --evidence` for the detail" not in result.output


def test_the_evidence_view_can_be_filtered_by_level() -> None:
    result = runner.invoke(
        app, [*OFFLINE, "show", "coverage", "--evidence", "--level", "implemented", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["kinds"]
    assert all(row["evidence"] == "implemented" for row in payload["kinds"])
    # ...and that is the generated stubs, not everything.
    assert len(payload["kinds"]) < len(default_registry.kinds())


def test_an_unknown_level_filter_names_the_valid_ones() -> None:
    result = runner.invoke(app, [*OFFLINE, "show", "coverage", "--level", "pretty-sure"])
    assert result.exit_code != 0
    assert "mock-verified" in result.output


def test_the_json_carries_tier_and_evidence_together() -> None:
    """They answer different questions and reading one without the other is
    how "shipped" came to mean five things. A script asking "can I use this?"
    must not be able to see one and miss the other."""
    result = runner.invoke(app, [*OFFLINE, "show", "coverage", "--json"])
    row = json.loads(result.stdout)["kinds"][0]
    assert {"tier", "evidence"} <= set(row)
