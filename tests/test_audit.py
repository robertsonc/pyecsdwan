"""`show journal --events` — the journal as an exportable audit trail (epic #9).

The journal already recorded everything an auditor needs; it had no way out of
`~/.pyecsdwan/journal/` except reading the files. What is worth testing about
the way out is not that JSON serializes — it is the two claims the export makes
about itself:

* **it discloses no more than it must.** The journal is 0600 because snapshots
  embed whole server objects. Exporting is distribution, so a snapshot body is
  redacted to a digest and a size unless someone asks for it by name;
* **it describes the whole journal.** A directory too corrupt to open is
  dropped by `list_txns`, and an export that stayed silent about that would be
  a smaller, cleaner journal than the one on disk — "absence of evidence read
  as evidence", which is the bug this project keeps finding.

Both are tested in the destructive direction too: the redaction test is paired
with one proving `--include-snapshots` really does disclose, so a redactor
that had been wired to *always* fire would not pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from pyecsdwan import audit
from pyecsdwan.cli import main as cli_main
from pyecsdwan.contract import Ref
from pyecsdwan.journal import TxnJournal, TxnState

runner = CliRunner()

SECRET = {"snmp": {"community": "public-but-not-really"}, "users": ["admin"]}
REF = Ref(kind="appliance/banners", name="global", appliance="BR1-EC")


def _journal(host: str = "orch.example.com") -> TxnJournal:
    j = TxnJournal.create(host, [REF])
    j.record_snapshot(REF, SECRET)
    j.append("APPLY_START", ref=REF.key())
    j.set_state(TxnState.CONFIRMED)
    return j


def _cli(*args: str) -> Any:
    return runner.invoke(cli_main.app, ["show", "journal", *args])


def _lines(stream: str) -> list[dict[str, Any]]:
    """Parse an NDJSON stream. Always pass `result.stdout`, never
    `result.output`: the latter folds stderr in, so a test using it would
    pass only for as long as nothing warned — and warning without corrupting
    the stream is exactly what these tests are checking."""
    return [json.loads(line) for line in stream.splitlines() if line.strip()]


# -- redaction ---------------------------------------------------------------


def test_a_snapshot_body_is_not_in_the_default_export(state_home: Path) -> None:
    """The whole reason this defaults the way it does: `journal.append` opens
    events.jsonl 0600 because snapshots embed server config, and a default that
    shipped those bodies to a log aggregator would undo that without anyone
    deciding to."""
    _journal()

    result = _cli("--events")

    assert result.exit_code == 0, result.output
    assert "public-but-not-really" not in result.output
    snap = [r for r in _lines(result.stdout) if r["event"] == "SNAPSHOT"]
    assert len(snap) == 1
    assert snap[0]["raw"] == {
        "redacted": True,
        "sha256": audit.digest(SECRET),
        "bytes": len(json.dumps(SECRET, sort_keys=True, separators=(",", ":"))),
    }


def test_include_snapshots_really_discloses(state_home: Path) -> None:
    """Guards the guard. A redactor hard-wired to always fire would leave the
    test above green and this whole feature useless — `--include-snapshots`
    exists precisely for the restore-verification case an auditor needs."""
    _journal()

    result = _cli("--events", "--include-snapshots")

    assert result.exit_code == 0, result.output
    snap = [r for r in _lines(result.stdout) if r["event"] == "SNAPSHOT"]
    assert snap[0]["raw"] == SECRET


def test_disclosing_bodies_announces_itself_off_stream(state_home: Path) -> None:
    """The one call in this feature that distributes config says so, the way
    `api` announces Tier-0 — and says it on stderr, so it reaches the operator
    running the command without landing in the file they redirected into."""
    _journal()

    result = _cli("--events", "--include-snapshots")

    assert "may contain credentials" in result.stderr
    _lines(result.stdout)  # the stream itself is still clean NDJSON
    assert "may contain credentials" not in result.stdout


def test_redaction_keeps_enough_to_prove_two_exports_agree(state_home: Path) -> None:
    """Redaction is not deletion. The digest is over canonical JSON, so it is
    stable across exports and comparable between journals — an auditor can show
    a restore matched what was captured without ever seeing the config."""
    _journal()
    first = _cli("--events").stdout
    second = _cli("--events").stdout

    def sha(out: str) -> str:
        return next(r["raw"]["sha256"] for r in _lines(out) if r["event"] == "SNAPSHOT")

    assert sha(first) == sha(second)
    # ... and it is the digest of the body, not of the marker that replaced it.
    assert sha(first) == audit.digest(SECRET)
    assert sha(first) != audit.digest({"snmp": {"community": "something-else"}})


def test_the_digest_does_not_depend_on_key_order(state_home: Path) -> None:
    """The half of the digest claim the other tests cannot reach.

    They compare against `audit.digest` itself, so they would pass just as
    happily over non-canonical JSON — the mutation sweep found exactly that.
    What "comparable between two journals" actually rests on is this: the same
    state serialized in a different key order is the same digest. Two journals
    holding the same appliance config need not have built the dict the same
    way, and an auditor comparing them must not see a false mismatch.

    Written to compare two bodies rather than to a literal digest, so it says
    the property instead of pinning a hash nobody can check by eye.
    """
    one = {"alpha": 1, "beta": {"x": 1, "y": 2}}
    other = {"beta": {"y": 2, "x": 1}, "alpha": 1}
    assert list(one) != list(other), "the fixture must actually differ in order"
    assert audit.digest(one) == audit.digest(other)
    assert audit.digest(one) != audit.digest({"alpha": 1, "beta": {"x": 1, "y": 3}})


def test_only_body_bearing_events_are_touched(state_home: Path) -> None:
    """Redaction is keyed on the event kind. If it were keyed on the field name
    instead, any future event with a `raw` field would be silently rewritten,
    and any renamed snapshot field would silently leak."""
    _journal()

    records = _lines(_cli("--events").stdout)
    begin = next(r for r in records if r["event"] == "TXN_BEGIN")
    assert begin["items"] == [REF.key()]
    assert begin["host"] == "orch.example.com"
    assert [r["event"] for r in records] == [
        "TXN_BEGIN", "SNAPSHOT", "APPLY_START", "STATE",
    ]


def test_redact_does_not_mutate_its_input() -> None:
    """The same event object is also what a human-readable render may print,
    so redacting for export must not quietly edit it for everyone else."""
    event = {"event": "SNAPSHOT", "ref": REF.key(), "raw": SECRET}
    audit.redact(event)
    assert event["raw"] == SECRET


# -- every line stands alone -------------------------------------------------


def test_each_record_carries_its_transaction_and_host(state_home: Path) -> None:
    """An NDJSON line lands in a SIEM with no surrounding context. A record
    that only made sense next to the file it came from would be unattributable
    the moment it was shipped."""
    journal = _journal()

    for record in _lines(_cli("--events").stdout):
        assert record["txn_id"] == journal.meta.txn_id
        assert record["orch_host"] == "orch.example.com"
        # And the unambiguous identity, not only the display host: two tenants
        # under one hostname share the latter, so an auditor reading shipped
        # lines could not tell which fabric a change landed on (#63).
        assert record["orch_origin"] == "https://orch.example.com"
        assert record["ts"]


def test_a_long_record_stays_one_line(state_home: Path) -> None:
    """rich wraps at the terminal width by default, and a wrapped NDJSON line
    is two records, neither of which parses. This is why the export prints with
    soft_wrap rather than through the ordinary console path."""
    journal = TxnJournal.create("orch.example.com", [REF])
    journal.append("APPLY_START", ref=REF.key(), note="x" * 4000)

    result = _cli("--events")

    assert result.exit_code == 0, result.output
    records = _lines(result.stdout)  # raises if any line was torn in half
    assert any(r.get("note") == "x" * 4000 for r in records)


def test_events_run_oldest_transaction_first(state_home: Path) -> None:
    """It is a log. The table is newest-first because that is what an operator
    scanning it wants; a stream appended to a SIEM wants the other order."""
    first = _journal()
    second = _journal()

    seen = [r["txn_id"] for r in _lines(_cli("--events").stdout)]
    assert seen.index(first.meta.txn_id) < seen.index(second.meta.txn_id)


# -- selecting one transaction -----------------------------------------------


def test_txn_filters_to_one_transaction(state_home: Path) -> None:
    wanted = _journal()
    _journal()

    records = _lines(_cli("--events", "--txn", wanted.meta.txn_id).stdout)
    assert records
    assert {r["txn_id"] for r in records} == {wanted.meta.txn_id}


def test_an_unknown_txn_id_fails_rather_than_exporting_nothing(state_home: Path) -> None:
    """Zero lines is a valid answer to "export the journal" and a dangerous one
    to "export transaction X": a pipeline reading the stream would record that
    nothing happened, when what actually happened is a typo."""
    _journal()

    result = _cli("--events", "--txn", "20260101-000000.0-deadbeef")

    assert result.exit_code != 0
    assert result.stdout.strip() == ""


# -- summaries ---------------------------------------------------------------


def test_json_gives_summaries_without_bodies(state_home: Path) -> None:
    journal = _journal()

    result = _cli("--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["unreadable"] == []
    (summary,) = payload["transactions"]
    assert summary["txn_id"] == journal.meta.txn_id
    assert summary["state"] == TxnState.CONFIRMED
    assert summary["items"] == [REF.key()]
    assert summary["orch_host"] == "orch.example.com"
    assert summary["orch_origin"] == "https://orch.example.com"
    assert "public-but-not-really" not in result.output


# -- the journal it cannot read ----------------------------------------------


def _corrupt(state_home: Path, name: str = "20260101-000000.0-corrupt") -> str:
    """A transaction directory `list_txns` will silently drop."""
    bad = state_home / "journal" / name
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("{not json", encoding="utf-8")
    return name


def test_a_torn_journal_directory_is_named_not_dropped(state_home: Path) -> None:
    """`list_txns` skips a directory whose meta.json is torn. Reporting the
    survivors as the whole journal would make an audit export quietly describe
    a smaller history than the one on disk."""
    _journal()
    name = _corrupt(state_home)

    payload = json.loads(_cli("--json").output)
    assert payload["unreadable"] == [name]
    assert len(payload["transactions"]) == 1

    table = _cli()
    assert name in table.output
    assert "could not be read" in table.output


def test_the_events_warning_stays_off_the_stream(state_home: Path) -> None:
    """The warning has to be said, and it cannot be said on stdout: one
    non-JSON line in the middle of an NDJSON stream breaks every consumer."""
    _journal()
    name = _corrupt(state_home)

    result = runner.invoke(cli_main.app, ["show", "journal", "--events"])

    assert result.exit_code == 0
    _lines(result.stdout)  # every stdout line still parses as JSON
    assert name not in result.stdout


def test_pending_says_so_even_when_it_finds_nothing(state_home: Path) -> None:
    """"No pending transactions" is the answer an operator acts on by going
    home, so it is the worst possible place for an unreadable directory —
    which could be the stuck transaction — to stay quiet."""
    _corrupt(state_home)

    result = runner.invoke(cli_main.app, ["--mock", "0", "show", "pending"])

    assert "no pending transactions" in result.output
    assert "could not be read" in result.output


# -- flag combinations -------------------------------------------------------


@pytest.mark.parametrize(
    "args, expected",
    [
        (["--events", "--json"], "already newline-delimited"),
        (["--include-snapshots"], "only applies to --events"),
        (["--include-snapshots", "--json"], "only applies to --events"),
    ],
)
def test_meaningless_combinations_are_refused(
    state_home: Path, args: list[str], expected: str
) -> None:
    """A flag that does nothing is a lie, and `--include-snapshots` is the
    worst one to lie about: an operator who typed it and saw no bodies would
    conclude the bodies had been disclosed."""
    _journal()

    result = _cli(*args)

    assert result.exit_code != 0
    assert expected in result.output


def test_the_shell_does_not_silently_ignore_the_export_flags(state_home: Path) -> None:
    """Principle IV cuts both ways: the same words must not quietly mean two
    different things on two surfaces. The shell has no pipe, so it says so
    rather than rendering a table and dropping the flag on the floor."""
    from pyecsdwan.cli import shell

    with pytest.raises(ValueError, match="ec-cli show journal --events"):
        shell._show_operational(["journal", "--events"], object())  # type: ignore[arg-type]
