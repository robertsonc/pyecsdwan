"""The convention that persisted state is keyed by origin, held over the source.

#63: two Orchestrators that differ only in scheme or base path collapsed onto
one identity, and then shared a candidate store, a lock, a resolver cache and
a rollback history. The fix is that everything which persists or compares
identity uses `Settings.origin` / `TxnMeta.orch_origin`, and `Settings.host` /
`TxnMeta.orch_host` are display-only.

That cannot be a type — both are `str`, and the wrong one type-checks
perfectly — and it cannot be a runtime assertion, because the whole failure
mode is that the lossy value looks entirely valid. So it is a test over the
source: every read of the lossy field is either on this file's allowlist,
with a reason, or the test fails.

Below the source check are behavioural tests for the three collision classes
the old identity actually had.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from pyecsdwan import config, journal
from pyecsdwan.candidate import CandidateFormatError, CandidateStore
from pyecsdwan.contract import Ref
from pyecsdwan.locking import HostLock, LockBusy

ROOT = Path(__file__).resolve().parents[1]
#: Every tree the gate lints. Tests are included deliberately: a test that
#: stages by the display host while the code under test keys by the origin
#: passes while asserting nothing, which is how 19 of them ended up green
#: against two different state files.
TREES = ("src", "tests", "tools", "contrib")

#: Reads of a display-only identity that are correct, and why. Keyed by
#: ``relative/path.py:attribute``; the value is the justification, which is
#: the point of the allowlist — adding an entry means writing down why the
#: lossy value is the right one there.
ALLOWED = {
    "src/pyecsdwan/journal.py:orch_host": (
        "targets()/lock_origin() fall back to the display host for format-1 "
        "journals, which is the only identity those files carry"
    ),
    "src/pyecsdwan/audit.py:orch_host": (
        "the export carries both fields; this is the one being exported"
    ),
    "src/pyecsdwan/txn.py:orch_host": (
        "reported inside PROVENANCE_UNVERIFIED, which exists to say the match "
        "was made on a host rather than an origin"
    ),
    "src/pyecsdwan/client.py:host": (
        "a local from the request URL, used only in the plaintext-URL warning"
    ),
    "src/pyecsdwan/mock/__main__.py:host": "the mock server's bind address, not an Orchestrator",
    "tests/test_journal.py:orch_host": "asserts the display host is still recorded",
    "tests/test_origin_identity.py:orch_host": "this file's own tests of the fallback",
}


def _display_reads(path: Path) -> list[tuple[int, str]]:
    """Every ``<something>.host`` / ``<something>.orch_host`` read in a file."""
    tree = ast.parse(path.read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("host", "orch_host"):
            out.append((node.lineno, node.attr))
    return sorted(out)


def _sources() -> list[Path]:
    return sorted(p for tree in TREES for p in (ROOT / tree).rglob("*.py"))


def test_no_unreviewed_read_of_a_display_only_identity():
    offenders = []
    for path in _sources():
        rel = str(path.relative_to(ROOT))
        for lineno, attr in _display_reads(path):
            if f"{rel}:{attr}" not in ALLOWED:
                offenders.append(f"{rel}:{lineno} reads .{attr}")
    assert offenders == [], (
        "these read a display-only identity. Persisted state and identity "
        "comparisons must use `.origin` / `.orch_origin` (#63). If a read is "
        "genuinely display-only, add it to ALLOWED in this file with the "
        "reason:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_has_no_stale_entries():
    """An allowlist that outlives its call site stops being a review record."""
    live = {
        f"{path.relative_to(ROOT)}:{attr}"
        for path in _sources()
        for _lineno, attr in _display_reads(path)
    }
    assert set(ALLOWED) - live == set()


def test_every_allowlist_entry_carries_a_reason():
    assert [k for k, v in ALLOWED.items() if not v.strip()] == []


# -- the collisions themselves ---------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # Two tenants behind one hostname.
        ("https://orch.example.com/tenant-a", "https://orch.example.com/tenant-b"),
        # Plaintext and TLS on one name: different trust, one old key.
        ("http://orch.example.com", "https://orch.example.com"),
        # Different ports, which the display host dropped entirely.
        ("https://orch.example.com:8443", "https://orch.example.com:9443"),
        # The lossy filename sanitizer mapped both of these to `orch_443`.
        ("https://orch:443/a", "https://orch_443/a"),
    ],
)
def test_distinct_targets_get_distinct_identities(a, b):
    assert config.canonical_origin(a) != config.canonical_origin(b)
    assert config.origin_slug(config.canonical_origin(a)) != config.origin_slug(
        config.canonical_origin(b)
    )


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://orch.example.com", "HTTPS://Orch.Example.COM"),
        ("https://orch.example.com", "https://orch.example.com/"),
        ("https://orch.example.com", "https://orch.example.com:443"),
        ("http://orch.example.com", "http://orch.example.com:80"),
        ("orch.example.com", "https://orch.example.com"),
    ],
)
def test_one_target_spelled_differently_is_one_identity(a, b):
    assert config.canonical_origin(a) == config.canonical_origin(b)


def test_an_empty_authority_is_refused():
    with pytest.raises(ValueError):
        config.canonical_origin("https:///nope")


def test_two_tenants_do_not_share_a_candidate_store(tmp_path):
    a = CandidateStore("https://orch.example.com/tenant-a", root=tmp_path)
    b = CandidateStore("https://orch.example.com/tenant-b", root=tmp_path)
    assert a.path != b.path
    a.set_desired(Ref("interface_label", "L1"), {"name": "from-a"})
    b.reload()
    assert b.items == {}


def test_two_tenants_do_not_share_a_commit_lock(tmp_path):
    a = HostLock("https://orch.example.com/tenant-a", "commit", root=tmp_path, timeout=0.0)
    b = HostLock("https://orch.example.com/tenant-b", "commit", root=tmp_path, timeout=0.0)
    assert a.path != b.path
    with a:
        with b:  # must not block: they are different Orchestrators
            pass


def test_two_tenants_do_not_share_a_rollback_history(tmp_path):
    for origin in ("https://orch.example.com/tenant-a", "https://orch.example.com/tenant-b"):
        txn = journal.TxnJournal.create(origin, [Ref("interface_label", "L1")], root=tmp_path)
        txn.set_state(journal.TxnState.CONFIRMED)
    a = journal.committed_history(root=tmp_path, origin="https://orch.example.com/tenant-a")
    assert [t.meta.orch_origin for t in a] == ["https://orch.example.com/tenant-a"]


def test_a_journal_records_the_origin_and_a_display_host(tmp_path):
    txn = journal.TxnJournal.create(
        "HTTPS://Orch.Example.COM:443/tenant-a", [Ref("interface_label", "L1")], root=tmp_path
    )
    assert txn.meta.orch_origin == "https://orch.example.com/tenant-a"
    assert txn.meta.orch_host == "orch.example.com"
    assert not journal.is_legacy(txn)


def test_a_format_one_journal_is_matched_on_its_host_and_marked_legacy(tmp_path):
    """Upgrading must not orphan an operator's rollback history."""
    txn = journal.TxnJournal.create(
        "https://orch.example.com", [Ref("interface_label", "L1")], root=tmp_path
    )
    meta = json.loads((txn.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (txn.dir / "meta.json").write_text(json.dumps(meta))

    reopened = journal.TxnJournal.open(txn.dir)
    assert journal.is_legacy(reopened)
    assert journal.targets(reopened, "https://orch.example.com")
    # Matched on the host, so a different scheme matches too — that ambiguity
    # is what format 1 recorded, and `is_legacy` is how a caller can say so.
    assert journal.targets(reopened, "http://orch.example.com")
    assert not journal.targets(reopened, "https://other.example.com")
    assert journal.lock_origin(reopened) == "https://orch.example.com"


def test_a_format_two_journal_is_matched_exactly(tmp_path):
    txn = journal.TxnJournal.create(
        "https://orch.example.com/tenant-a", [Ref("interface_label", "L1")], root=tmp_path
    )
    assert journal.targets(txn, "https://orch.example.com/tenant-a")
    assert not journal.targets(txn, "https://orch.example.com/tenant-b")
    assert not journal.targets(txn, "http://orch.example.com/tenant-a")
    # Not matched on the display host, which both tenants share.
    assert not journal.targets(txn, "https://orch.example.com")


def test_a_format_one_candidate_file_is_adopted_by_this_session(tmp_path):
    store = CandidateStore("https://orch.example.com", root=tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "format": 1,
                "items": [
                    {
                        "ref_key": "interface_label/L1",
                        "mode": "replace",
                        "intent": {"name": "L1"},
                        "delete_paths": [],
                    }
                ],
            }
        )
    )
    store.reload()
    assert list(store.items) == ["interface_label/L1"]
    # Not rewritten on read; the origin lands on the next ordinary save.
    assert "origin" not in json.loads(store.path.read_text())
    store.set_desired(Ref("interface_label", "L2"), {"name": "L2"})
    assert json.loads(store.path.read_text())["origin"] == "https://orch.example.com"


def test_a_candidate_file_staged_against_another_origin_is_refused(tmp_path):
    a = CandidateStore("https://orch.example.com/tenant-a", root=tmp_path)
    a.set_desired(Ref("interface_label", "L1"), {"name": "from-a"})

    # Simulate the file being moved or restored under another origin's name.
    b = CandidateStore("https://orch.example.com/tenant-b", root=tmp_path)
    b.path.write_text(a.path.read_text())
    with pytest.raises(CandidateFormatError) as excinfo:
        b.reload()
    assert "tenant-a" in str(excinfo.value)
    assert "has not been modified" in str(excinfo.value)
    assert json.loads(b.path.read_text())["origin"] == "https://orch.example.com/tenant-a"


# -- the guard that stops a snapshot crossing fabrics ------------------------


def _ctx_for(origin: str):
    import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
    from pyecsdwan.client import OrchClient
    from pyecsdwan.contract import Ctx
    from pyecsdwan.resolver import Resolver

    client = OrchClient(config.Settings(orch_url=origin, api_key="test-key"))
    return Ctx(client=client, resolver=Resolver(client))


def _unconfirmed(origin: str) -> journal.TxnJournal:
    txn = journal.TxnJournal.create(origin, [Ref("interface-labels", "global")])
    txn.record_snapshot(Ref("interface-labels", "global"), {"wan": {}, "lan": {}})
    txn.append("APPLY_START", ref="interface-labels:global")
    txn.set_state(journal.TxnState.CONFIRMED)
    txn.set_state(journal.TxnState.APPLIED_UNCONFIRMED)
    return txn


@pytest.mark.parametrize(
    "staged,session",
    [
        ("https://orch.example.com/tenant-a", "https://orch.example.com/tenant-b"),
        ("http://orch.example.com", "https://orch.example.com"),
        ("https://orch.example.com:8443", "https://orch.example.com:9443"),
    ],
)
def test_a_snapshot_is_not_restored_into_a_different_origin(state_home, staged, session):
    """The single worst thing this tool can do, and the display host let it
    through: all three of these pairs used to compare equal (#63)."""
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed(staged)
    report = txn_mod.revert_txn_dir(
        staged_txn.dir, reason="test", ctx=_ctx_for(session), registry=default_registry
    )
    assert not report.ok
    assert "refusing" in report.messages[0].lower()
    assert staged in report.messages[0]
    assert config.canonical_origin(session) in report.messages[0]


def test_a_matching_origin_is_not_refused(state_home):
    """The guard has to let the legitimate case through, or it is just a break.

    Asserted on the journal, not only on the absence of a message: the guard
    returns *before* the transaction is touched, so a state that has moved on
    is proof the restore was actually attempted.
    """
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed("https://orch.example.com/tenant-a")
    report = txn_mod.revert_txn_dir(
        staged_txn.dir,
        reason="test",
        ctx=_ctx_for("https://orch.example.com/tenant-a"),
        registry=default_registry,
    )
    assert "refusing: transaction targets" not in " ".join(report.messages)
    reopened = journal.TxnJournal.open(staged_txn.dir)
    assert reopened.meta.state != journal.TxnState.APPLIED_UNCONFIRMED


def test_a_legacy_journal_records_that_its_provenance_was_not_verified(state_home):
    """A format-1 journal can only be matched on a hostname. It is still
    recoverable — losing an operator's way back is worse — but the journal has
    to say the target was inferred rather than checked."""
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed("https://orch.example.com")
    meta = json.loads((staged_txn.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (staged_txn.dir / "meta.json").write_text(json.dumps(meta))

    report = txn_mod.revert_txn_dir(
        staged_txn.dir,
        reason="test",
        ctx=_ctx_for("https://orch.example.com"),
        registry=default_registry,
    )
    reopened = journal.TxnJournal.open(staged_txn.dir)
    kinds = [e["event"] for e in reopened.events()]
    assert "PROVENANCE_UNVERIFIED" in kinds, kinds
    # And the operator deciding whether to accept the restore has to see it:
    # the journal is the durable record, the report is what they read.
    assert any("predates origin recording" in m for m in report.messages), report.messages


def test_a_format_two_journal_records_no_such_caveat(state_home):
    """Deleting the `is_legacy` guard must fail a test, not just the one above."""
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed("https://orch.example.com")
    txn_mod.revert_txn_dir(
        staged_txn.dir,
        reason="test",
        ctx=_ctx_for("https://orch.example.com"),
        registry=default_registry,
    )
    reopened = journal.TxnJournal.open(staged_txn.dir)
    assert "PROVENANCE_UNVERIFIED" not in [e["event"] for e in reopened.events()]


# -- spellings the parser itself could collapse ------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # `urlsplit` strips the brackets, and without them the port separator
        # is the same character as the address separator.
        ("https://[::1]:8443", "https://[::1:8443]"),
        ("https://[2001:db8::1]:443", "https://[2001:db8::1:443]"),
    ],
)
def test_two_ipv6_targets_are_not_collapsed_by_the_parser(a, b):
    assert config.canonical_origin(a) != config.canonical_origin(b)


def test_an_ipv6_origin_keeps_its_brackets():
    """Or the display host, the legacy match and the readable half of the file
    name all inherit the same ambiguity."""
    origin = config.canonical_origin("https://[::1]:8443")
    assert origin == "https://[::1]:8443"
    assert config.display_host(origin) == "[::1]:8443"


def test_an_ipv6_default_port_still_normalizes():
    assert config.canonical_origin("https://[::1]:443") == config.canonical_origin(
        "https://[::1]"
    )


@pytest.mark.parametrize(
    "spelling",
    [
        "https://münchen.example.com/x",
        "https://xn--mnchen-3ya.example.com/x",
        "https://MÜNCHEN.example.com/x",
    ],
)
def test_the_spellings_of_an_internationalized_name_are_one_identity(spelling):
    assert config.canonical_origin(spelling) == "https://xn--mnchen-3ya.example.com/x"


def test_a_name_the_idna_codec_refuses_is_still_an_identity():
    """Folding equivalent spellings is the job; validating a hostname is not.
    A name that cannot be encoded still has to key state rather than raise."""
    label = "ü" * 70
    origin = config.canonical_origin(f"https://{label}.example.com")
    assert label in origin
    assert config.origin_slug(origin)


# -- upgrading with work already staged --------------------------------------


def test_work_staged_by_an_older_build_is_adopted_not_lost(tmp_path):
    """The store moved to an origin-keyed name. Reading only the new name
    would report an empty candidate to an operator who has work staged —
    which reads as 'nothing to commit', and the work is re-done or lost."""
    legacy = tmp_path / "orch.example.com.json"
    legacy.write_text(
        json.dumps(
            {
                "format": 1,
                "items": [
                    {
                        "ref_key": "interface_label/L1",
                        "mode": "replace",
                        "intent": {"name": "L1"},
                        "delete_paths": [],
                    }
                ],
            }
        )
    )
    store = CandidateStore("https://orch.example.com", root=tmp_path)
    assert list(store.items) == ["interface_label/L1"]

    # Retired on the next save, not on read: a read-only session must not
    # mutate state, and leaving it would let a later session read it back.
    store.set_desired(Ref("interface_label", "L2"), {"name": "L2"})
    assert not legacy.exists()
    assert json.loads(store.path.read_text())["origin"] == "https://orch.example.com"


def test_the_new_name_wins_over_a_legacy_file(tmp_path):
    """Otherwise the fallback would quietly undo the fix for anyone who has
    both, which is everyone mid-migration."""
    store = CandidateStore("https://orch.example.com", root=tmp_path)
    store.set_desired(Ref("interface_label", "NEW"), {"name": "NEW"})
    (tmp_path / "orch.example.com.json").write_text(
        json.dumps({"format": 1, "items": [
            {"ref_key": "interface_label/OLD", "mode": "replace",
             "intent": {}, "delete_paths": []}
        ]})
    )
    assert list(CandidateStore("https://orch.example.com", root=tmp_path).items) == [
        Ref("interface_label", "NEW").key()
    ]


# -- across processes, which is the only place exclusion is real -------------

_STAGE_AND_HOLD = """
import sys, time
from pathlib import Path
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.contract import Ref
from pyecsdwan.locking import HostLock
origin, name, ready = sys.argv[1:4]
store = CandidateStore(origin)
store.set_desired(Ref("interface_label", name), {"name": name})
with HostLock(origin, "commit", timeout=10.0):
    Path(ready).write_text("held")
    time.sleep(120)
"""


def _spawn(state_home, origin: str, name: str, ready):
    import os
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", _STAGE_AND_HOLD, origin, name, str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ, ECSDWAN_HOME=str(state_home)),
    )
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if ready.exists():
            return proc
        if proc.poll() is not None:
            raise AssertionError(f"holder died: {proc.communicate()[1]}")
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("holder never took the lock")


def test_two_tenants_do_not_contend_across_processes(state_home):
    """In-process proves nothing: locks are re-entrant per process on purpose,
    so an in-process second holder nests instead of blocking. Exclusion — and
    its absence between distinct origins — is a cross-process property."""
    from pyecsdwan.locking import HostLock

    a = _spawn(state_home, "https://orch.example.com/tenant-a", "A", state_home / "a.ready")
    try:
        # A different tenant on the same hostname must not be blocked by A...
        with HostLock("https://orch.example.com/tenant-b", "commit", timeout=1.0):
            pass
        # ...and the same tenant must be.
        with pytest.raises(LockBusy):
            HostLock("https://orch.example.com/tenant-a", "commit", timeout=0.5).acquire()

        # Their staged work is in two files, neither seeing the other.
        assert list(CandidateStore("https://orch.example.com/tenant-a").items) == [
            Ref("interface_label", "A").key()
        ]
        assert CandidateStore("https://orch.example.com/tenant-b").items == {}
    finally:
        a.kill()
        a.wait(timeout=10)


def test_a_scheme_distinct_target_does_not_contend_across_processes(state_home):
    """A plaintext and a TLS endpoint on one name were one lock and one store."""
    from pyecsdwan.locking import HostLock

    a = _spawn(state_home, "https://orch.example.com", "TLS", state_home / "tls.ready")
    try:
        with HostLock("http://orch.example.com", "commit", timeout=1.0):
            pass
        assert CandidateStore("http://orch.example.com").items == {}
        assert list(CandidateStore("https://orch.example.com").items) == [
            Ref("interface_label", "TLS").key()
        ]
    finally:
        a.kill()
        a.wait(timeout=10)
