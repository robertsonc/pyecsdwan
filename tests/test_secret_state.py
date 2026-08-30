"""#106's acceptance criteria, run end to end against the real state files.

Every test here works the way the issue is worded: seed a *sentinel* secret
through a real path — staged candidate, journal snapshot, Tier-0 API call —
then read what actually landed on disk or came out of an export, and assert
the sentinel is not in it. The sweep reads every file under the state root
rather than the files we expect to exist, because the leak that matters is
always in the file nobody thought of.

The other half of the criteria is that protection must not cost correctness:
an encrypted snapshot still restores byte-identically, a sealed candidate
still materializes the real intent, and a lost key is a loud, recoverable
refusal — never a quiet empty read that a revert would interpret as "the
resource did not exist".
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from pyecsdwan import audit, vault
from pyecsdwan.candidate import CandidateStore, materialize_desired
from pyecsdwan.cli.main import app
from pyecsdwan.contract import Diff, Ref
from pyecsdwan.diffing import render_diff_lines, structural_diff
from pyecsdwan.journal import JournalCorrupt, TxnJournal, TxnState
from tests.test_vault import FakeKeyring

runner = CliRunner()

ORIGIN = "https://orch.example.com"
BASE = f"{ORIGIN}/gms/rest"

#: One sentinel per protocol family the issue names. Unique strings, so a
#: sweep hit identifies *which* path leaked.
BGP_SENTINEL = "sentinel-bgp-Xj9q"
OSPF_SENTINEL = "sentinel-ospf-Km2w"
OSPF_MD5_SENTINEL = "sentinel-ospf-md5-Tn4c"
SNMP_SENTINEL = "sentinel-snmp-Vb7d"
PROXY_SENTINEL = "sentinel-proxy-Qs5e"
PARAM_SENTINEL = "sentinel-param-Zr8f"
SENTINELS = (
    BGP_SENTINEL,
    OSPF_SENTINEL,
    OSPF_MD5_SENTINEL,
    SNMP_SENTINEL,
    PROXY_SENTINEL,
    PARAM_SENTINEL,
)

BGP_REF = Ref(kind="appliance/bgp", name="config", appliance="BR1-EC")
OSPF_REF = Ref(kind="appliance/ospf", name="config", appliance="BR1-EC")
SNMP_REF = Ref(kind="appliance/snmp", name="config", appliance="BR1-EC")
PROXY_REF = Ref(kind="proxy-config", name="global")

BGP_BODY = {
    "asn": 65000,
    "neighbors": [{"ip": "10.0.0.1", "remote_as": 65001, "password": BGP_SENTINEL}],
}
OSPF_BODY = {
    "interfaces": {
        "lan0": {"area": "0.0.0.0", "authKey": OSPF_SENTINEL, "md5Password": OSPF_MD5_SENTINEL}
    }
}
SNMP_BODY = {"v2c": {"community": SNMP_SENTINEL}, "enabled": True}
PROXY_BODY = {"endpoint": "https://siem.example.com", "integrationToken": PROXY_SENTINEL}


def _sweep(root: Path) -> list[tuple[Path, str]]:
    """Every (file, sentinel) pair found anywhere under the state root."""
    hits: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for sentinel in SENTINELS:
            if sentinel in text:
                hits.append((path, sentinel))
    return hits


def _stage_and_snapshot(state_home: Path) -> tuple[CandidateStore, TxnJournal]:
    store = CandidateStore(ORIGIN)
    store.set_desired(BGP_REF, BGP_BODY)
    store.set_desired(OSPF_REF, OSPF_BODY)
    store.set_desired(SNMP_REF, SNMP_BODY)
    store.set_desired(PROXY_REF, PROXY_BODY)
    journal = TxnJournal.create(ORIGIN, [BGP_REF, OSPF_REF, SNMP_REF, PROXY_REF])
    journal.record_snapshot(BGP_REF, BGP_BODY)
    journal.record_snapshot(OSPF_REF, OSPF_BODY)
    journal.record_snapshot(SNMP_REF, SNMP_BODY)
    journal.record_snapshot(PROXY_REF, PROXY_BODY)
    journal.append("APPLY_START", ref=BGP_REF.key())
    journal.set_state(TxnState.CONFIRMED)
    return store, journal


# -- the sweep ----------------------------------------------------------------


def test_no_sentinel_survives_into_any_state_file(state_home: Path) -> None:
    """The headline criterion, verbatim: "seeded sentinel secrets are absent
    from every plaintext state file". Candidates and journal snapshots go
    through their real write paths; then every byte under the state root is
    read back."""
    _stage_and_snapshot(state_home)

    hits = _sweep(state_home)
    assert hits == [], f"sentinels leaked into: {hits}"
    # Guard against a vacuous pass: the state that *should* exist, does — the
    # non-secret halves of the same objects are on disk and findable.
    all_text = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(state_home.rglob("*"))
        if p.is_file()
    )
    assert "10.0.0.1" in all_text          # the BGP neighbor, minus its password
    assert "siem.example.com" in all_text  # the proxy endpoint, minus its token


def test_the_raw_api_journal_keeps_param_names_and_masked_values(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion three: secret-looking raw parameters retain only names plus
    hashes/redacted values — while the request itself still carries the real
    value, because redaction protects the record, not the call."""
    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    with respx.mock:
        route = respx.get(f"{BASE}/snmp/config").mock(
            return_value=httpx.Response(200, json={})
        )
        result = runner.invoke(
            app,
            [
                "--orch-url", ORIGIN, "api", "get", "/snmp/config",
                "--param", f"community={PARAM_SENTINEL}",
                "--param", "rows=5",
            ],
        )
    assert result.exit_code == 0, result.output
    sent = route.calls[0].request.url
    assert PARAM_SENTINEL in str(sent), "the real request must carry the real value"

    assert _sweep(state_home) == []
    events = [
        json.loads(line)
        for path in state_home.rglob("events.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    raw = next(e for e in events if e.get("event") == "RAW_API")
    assert raw["params"]["rows"] == "5"
    assert raw["params"]["community"].startswith("<redacted")


def test_an_inline_query_string_is_masked_in_the_journal_too(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--param` is not the only way a secret reaches a Tier-0 call: the path
    argument itself can carry `?community=...`, and the journal records the
    path verbatim otherwise."""
    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    with respx.mock:
        respx.get(f"{BASE}/snmp/config", params={"community": PARAM_SENTINEL}).mock(
            return_value=httpx.Response(200, json={})
        )
        result = runner.invoke(
            app,
            ["--orch-url", ORIGIN, "api", "get",
             f"/snmp/config?community={PARAM_SENTINEL}"],
        )
    assert result.exit_code == 0, result.output
    assert _sweep(state_home) == []


def test_api_error_text_masks_query_values() -> None:
    """An exception's text is the one string guaranteed to travel — into
    logs, the journal's response_summary, a pasted bug report — so the
    masking happens at construction, not at any rendering site."""
    from pyecsdwan.client import OrchApiError

    exc = OrchApiError(
        "GET", f"/snmp/config?community={PARAM_SENTINEL}&rows=5", 500, "boom"
    )
    assert PARAM_SENTINEL not in str(exc)
    assert PARAM_SENTINEL not in exc.path
    assert "rows=5" in exc.path


# -- rollback still works ------------------------------------------------------


def test_encrypted_snapshots_restore_byte_identically(state_home: Path) -> None:
    """Criterion two. The snapshot store is now split and partly encrypted;
    what a revert reads out of it must still be exactly what fetch put in."""
    _, journal = _stage_and_snapshot(state_home)
    reopened = TxnJournal.open(journal.dir)
    snaps = reopened.snapshots()
    assert snaps[BGP_REF.key()] == BGP_BODY
    assert snaps[OSPF_REF.key()] == OSPF_BODY
    assert snaps[SNMP_REF.key()] == SNMP_BODY
    assert snaps[PROXY_REF.key()] == PROXY_BODY


def test_a_tampered_snapshot_body_is_refused_not_restored(state_home: Path) -> None:
    """The digest in the event log is a commitment, not a decoration: a body
    that no longer matches it must never be what a revert writes to a fabric."""
    journal = TxnJournal.create(ORIGIN, [SNMP_REF])
    journal.record_snapshot(SNMP_REF, {"enabled": True, "rows": 5})  # no secrets: plaintext record

    private = journal.dir / "private.jsonl"
    lines = private.read_text(encoding="utf-8").splitlines()
    doctored = [
        line.replace('"rows": 5', '"rows": 6').replace('"rows":5', '"rows":6')
        for line in lines
    ]
    assert doctored != lines
    private.write_text("\n".join(doctored) + "\n", encoding="utf-8")

    with pytest.raises(JournalCorrupt, match="does not match the digest"):
        TxnJournal.open(journal.dir).snapshots()


def test_a_missing_private_store_is_refused_not_read_as_absent(state_home: Path) -> None:
    """A missing snapshot must never read as "the resource did not exist
    before" — that is the reading that makes a revert *delete* something."""
    journal = TxnJournal.create(ORIGIN, [SNMP_REF])
    journal.record_snapshot(SNMP_REF, {"enabled": True})
    (journal.dir / "private.jsonl").unlink()

    with pytest.raises(JournalCorrupt, match="no private snapshot"):
        TxnJournal.open(journal.dir).snapshots()


def test_pre_split_journals_with_inline_bodies_still_restore(state_home: Path) -> None:
    """Journals written before #106 carry the body inside the SNAPSHOT event;
    they are history, and history must keep restoring."""
    journal = TxnJournal.create(ORIGIN, [SNMP_REF])
    journal.append("SNAPSHOT", ref=SNMP_REF.key(), exists=True, raw=SNMP_BODY)

    snaps = TxnJournal.open(journal.dir).snapshots()
    assert snaps[SNMP_REF.key()] == SNMP_BODY


# -- candidate round trip ------------------------------------------------------


def test_sealed_candidate_intent_round_trips_and_materializes(state_home: Path) -> None:
    _stage_and_snapshot(state_home)
    fresh = CandidateStore(ORIGIN)
    item = fresh.item_for(BGP_REF)
    assert item is not None
    assert item.intent == BGP_BODY
    assert materialize_desired(item, None) == BGP_BODY


# -- exports and rendering -----------------------------------------------------


def test_both_export_modes_are_sentinel_free(state_home: Path) -> None:
    _, journal = _stage_and_snapshot(state_home)
    for include in (False, True):
        records = list(audit.events([journal], include_snapshots=include))
        text = "\n".join(audit.to_ndjson(records))
        for sentinel in SENTINELS:
            assert sentinel not in text, f"{sentinel} leaked (include={include})"
    # And with bodies included, the export is really carrying the bodies.
    included = list(audit.events([journal], include_snapshots=True))
    bgp = next(r for r in included if r.get("ref") == BGP_REF.key())
    assert bgp["raw"]["neighbors"][0]["ip"] == "10.0.0.1"


def test_rendered_diffs_mask_secret_values(state_home: Path) -> None:
    current = {"asn": 65000, "neighbors": [{"ip": "10.0.0.1", "password": "old-secret"}]}
    desired = {"asn": 65001, "neighbors": [{"ip": "10.0.0.1", "password": BGP_SENTINEL}]}
    diff = Diff(ref=BGP_REF, entries=structural_diff(current, desired), desired=desired)
    text = "\n".join(line for _, line in render_diff_lines(diff))
    assert "old-secret" not in text
    assert BGP_SENTINEL not in text
    # The diff still says what changed: the non-secret field with its values,
    # the secret field by name with a change hint.
    assert "asn: 65001" in text
    assert "password" in text
    assert "<redacted" in text


def test_show_candidate_masks_secret_values(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    _stage_and_snapshot(state_home)
    result = runner.invoke(
        app, ["--orch-url", ORIGIN, "show", "configuration", "candidate"]
    )
    assert result.exit_code == 0, result.output
    for sentinel in SENTINELS:
        assert sentinel not in result.output
    assert "10.0.0.1" in result.output  # the rest of the object still renders


# -- fail closed, recover safely ----------------------------------------------


def _break_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)

    class Broken:
        @staticmethod
        def get_password(service: str, user: str) -> str:
            raise RuntimeError("no backend")

    monkeypatch.setitem(sys.modules, "keyring", Broken)


def test_without_a_key_secret_state_is_refused_before_it_is_written(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion four, the write half: keyring-unavailable fails *safely* —
    the save raises, and nothing secret has touched the disk in any form."""
    _break_keyring(monkeypatch)

    store = CandidateStore(ORIGIN)
    with pytest.raises(vault.VaultUnavailable):
        store.set_desired(BGP_REF, BGP_BODY)

    journal = TxnJournal.create(ORIGIN, [BGP_REF])
    with pytest.raises(vault.VaultUnavailable):
        journal.record_snapshot(BGP_REF, BGP_BODY)
    # The refusal came before the event log claimed a snapshot existed.
    assert all(e["event"] != "SNAPSHOT" for e in journal.events())

    assert _sweep(state_home) == []


def test_a_lost_key_is_a_loud_refusal_and_a_restored_key_recovers(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion four, the read half. Losing the key must not read as "no
    snapshot"; getting the key back must be a full recovery, because the
    blobs were never touched."""
    _, journal = _stage_and_snapshot(state_home)
    saved = str(Path(state_home))  # keep fixture referenced; the state is under it

    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", FakeKeyring())
    with pytest.raises(vault.VaultOpenError):
        TxnJournal.open(journal.dir).snapshots()
    with pytest.raises(vault.VaultOpenError):
        CandidateStore(ORIGIN)

    monkeypatch.setenv(
        vault.ENV_ENVELOPE_KEY, base64.b64encode(b"\x07" * vault.KEY_BYTES).decode()
    )
    assert TxnJournal.open(journal.dir).snapshots()[BGP_REF.key()] == BGP_BODY
    assert CandidateStore(ORIGIN).item_for(BGP_REF).intent == BGP_BODY
    assert saved


# -- rotation, end to end ------------------------------------------------------


def test_rotation_reseals_the_state_directory(
    state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage and snapshot under keyring key one, rotate, and prove three
    things: the report counted real files, everything still opens under key
    two alone, and the sweep stays clean throughout."""
    ring = FakeKeyring()
    monkeypatch.delenv(vault.ENV_ENVELOPE_KEY, raising=False)
    monkeypatch.setitem(sys.modules, "keyring", ring)

    _, journal = _stage_and_snapshot(state_home)
    key_one = ring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)]

    report = vault.rotate_key()

    assert ring.store[(vault.KEYRING_SERVICE, vault.KEYRING_USER)] != key_one
    assert (vault.KEYRING_SERVICE, vault.KEYRING_USER_PREVIOUS) not in ring.store
    assert report.candidate_files == 1
    assert report.journal_files == 1
    assert report.blobs >= 8  # four secret-bearing items in each store

    assert _sweep(state_home) == []
    assert TxnJournal.open(journal.dir).snapshots()[BGP_REF.key()] == BGP_BODY
    assert CandidateStore(ORIGIN).item_for(BGP_REF).intent == BGP_BODY
