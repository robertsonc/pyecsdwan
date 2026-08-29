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
import re
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
    "src/pyecsdwan/cli/main.py:orch_host": (
        "`adopt` reports the hostname an unadopted journal recorded, which is "
        "the thing the operator is being asked to disambiguate"
    ),
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
        # The file-name sanitizer maps both of these to `https___orch_8443_a`.
        # A *non-default* port, deliberately: 443 is normalized away before the
        # sanitizer ever sees it, so that pair never exercised the collision —
        # the mutation sweep is what showed the case was decorative.
        ("https://orch:8443/a", "https://orch_8443/a"),
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


def test_the_digest_is_what_keeps_a_sanitized_name_unique():
    """Named separately from the parametrized pair above, because it is the
    digest specifically that does the work: the readable halves are equal, and
    a slug that were only the readable half would put two Orchestrators'
    staging, locks and caches in one file."""
    a = config.canonical_origin("https://orch:8443/a")
    b = config.canonical_origin("https://orch_8443/a")
    readable = re.compile(r"[^A-Za-z0-9._-]")
    assert readable.sub("_", a)[-48:] == readable.sub("_", b)[-48:]
    assert config.origin_slug(a) != config.origin_slug(b)


def test_two_origins_alike_in_their_last_48_characters_still_differ():
    """The readable half is truncated, so long origins that share a tail are
    distinguished by the digest alone."""
    tail = "x" * 60
    a = config.canonical_origin(f"https://a.example.com/{tail}")
    b = config.canonical_origin(f"https://b.example.com/{tail}")
    assert config.origin_slug(a)[:-33] == config.origin_slug(b)[:-33]
    assert config.origin_slug(a) != config.origin_slug(b)


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
    # Listed: broad, because anything on that hostname might be it.
    assert journal.targets(reopened, "https://orch.example.com")
    assert journal.targets(reopened, "http://orch.example.com")
    assert not journal.targets(reopened, "https://other.example.com")
    # Authorized: nothing. A hostname is shared by both schemes on that name
    # and by every tenant path under it, so it proves nothing about the target.
    assert not journal.authorizes(reopened, "https://orch.example.com")
    assert not journal.authorizes(reopened, "http://orch.example.com")
    assert not journal.authorizes(reopened, "https://orch.example.com/tenant-a")


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


def test_a_legacy_journal_cannot_authorize_a_restore(state_home):
    """The review's finding: warning *after* authorizing does not help, because
    by then another fabric's snapshots have gone out. A journal whose target
    cannot be proven refuses until an operator adopts it."""
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
    assert not report.ok
    assert "refusing to restore" in report.messages[0]
    assert "ec-cli adopt --txn" in report.messages[0]
    # Refused without touching the transaction at all.
    assert (
        journal.TxnJournal.open(staged_txn.dir).meta.state
        == journal.TxnState.APPLIED_UNCONFIRMED
    )


def test_a_legacy_journal_cannot_be_confirmed_either(state_home):
    """Confirming writes the marker, kills the watchdog and marks CONFIRMED —
    it is what stops the fabric being put back, so it needs the same proof."""
    from pyecsdwan import txn as txn_mod

    staged_txn = _unconfirmed("https://orch.example.com")
    meta = json.loads((staged_txn.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (staged_txn.dir / "meta.json").write_text(json.dumps(meta))

    report = txn_mod.confirm_pending(
        config.Settings(orch_url="https://orch.example.com", api_key="k")
    )
    assert not report.ok
    assert "refusing to confirm" in report.messages[0]


def test_adoption_makes_a_legacy_journal_executable_and_is_recorded(state_home):
    """The escape hatch has to exist, or preserving the history is worthless.
    The operator is the proof, so the claim goes in the event log."""
    staged_txn = _unconfirmed("https://orch.example.com")
    meta = json.loads((staged_txn.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (staged_txn.dir / "meta.json").write_text(json.dumps(meta))

    reopened = journal.TxnJournal.open(staged_txn.dir)
    journal.adopt(reopened, "https://orch.example.com/tenant-a")

    rebound = journal.TxnJournal.open(staged_txn.dir)
    assert rebound.meta.orch_origin == "https://orch.example.com/tenant-a"
    assert not journal.is_legacy(rebound)
    assert journal.authorizes(rebound, "https://orch.example.com/tenant-a")
    # And only that one: adoption records a target, it does not widen anything.
    assert not journal.authorizes(rebound, "https://orch.example.com/tenant-b")
    assert "ORIGIN_ADOPTED" in [e["event"] for e in rebound.events()]


def test_adoption_cannot_re_target_an_already_bound_journal(state_home):
    txn = _unconfirmed("https://orch.example.com/tenant-a")
    with pytest.raises(ValueError, match="already bound"):
        journal.adopt(txn, "https://orch.example.com/tenant-b")


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


def _legacy_candidate(root, name="L1"):
    legacy = root / "orch.example.com.json"
    legacy.write_text(
        json.dumps(
            {
                "format": 1,
                "items": [
                    {
                        "ref_key": f"interface_label/{name}",
                        "mode": "replace",
                        "intent": {"name": name},
                        "delete_paths": [],
                    }
                ],
            }
        )
    )
    return legacy


def test_work_staged_by_an_older_build_is_surfaced_but_not_claimed(tmp_path):
    """The review's P1: the old file is keyed by a display host that several
    origins share, so first-reader adoption turns unknown provenance into
    asserted provenance — and two tenants would each copy it into their own
    store. Surfaced instead, so nothing is lost and nothing is assumed."""
    legacy = _legacy_candidate(tmp_path)
    store = CandidateStore("https://orch.example.com", root=tmp_path)

    assert store.items == {}
    assert store.unadopted_legacy == legacy
    assert store.legacy_pending() == ["interface_label/L1"]
    assert legacy.exists()
    assert not store.path.exists()


def test_two_origins_cannot_both_claim_one_legacy_candidate(tmp_path):
    """Both compute the same legacy path, because the old name carried only
    the display host. Whoever adopts first takes it; the second finds nothing
    rather than a second copy of the first one's work."""
    _legacy_candidate(tmp_path)
    a = CandidateStore("https://orch.example.com/tenant-a", root=tmp_path)
    b = CandidateStore("https://orch.example.com/tenant-b", root=tmp_path)
    assert a._legacy_path == b._legacy_path

    assert a.adopt_legacy() == ["interface_label/L1"]
    assert b.adopt_legacy() == []
    assert list(a.items) == ["interface_label/L1"]
    assert CandidateStore("https://orch.example.com/tenant-b", root=tmp_path).items == {}


def test_adopting_a_legacy_candidate_retires_the_file(tmp_path):
    legacy = _legacy_candidate(tmp_path)
    store = CandidateStore("https://orch.example.com", root=tmp_path)
    store.adopt_legacy()
    assert not legacy.exists()
    assert json.loads(store.path.read_text())["origin"] == "https://orch.example.com"
    assert store.unadopted_legacy is None


def test_a_commit_with_only_unadopted_staging_refuses_rather_than_saying_no_changes(
    tmp_path,
):
    """"No changes" to an operator who staged twelve of them reads as "it was
    lost", and they do the work again. The staging is right there; only its
    target is unknown."""
    from pyecsdwan import txn as txn_mod

    _legacy_candidate(tmp_path)
    store = CandidateStore("https://orch.example.com", root=tmp_path)
    with pytest.raises(txn_mod.CommitError) as excinfo:
        txn_mod._guard_unadopted_staging(store)
    assert "adopt --candidate" in str(excinfo.value)
    assert "1 change(s) staged by an older build" in str(excinfo.value)


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


# -- one endpoint, one identity (review P0-1) --------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # `OrchClient` appends the fixed API suffix when it is absent, so both
        # of these talk to exactly the same URL. Deriving identity from the
        # typed string instead made them two of everything: #63 inverted.
        ("https://orch.example.com", "https://orch.example.com/gms/rest"),
        ("https://orch.example.com/", "https://orch.example.com/gms/rest/"),
        ("orch.example.com", "https://orch.example.com/gms/rest"),
        ("https://orch/tenant-a", "https://orch/tenant-a/gms/rest"),
    ],
)
def test_one_effective_endpoint_is_one_identity(a, b):
    assert config.canonical_origin(a) == config.canonical_origin(b)
    assert config.api_base(a) == config.api_base(b)


def test_identity_and_the_client_agree_on_what_the_endpoint_is():
    """Two definitions of URL equivalence is the defect. One function, so the
    client's base URL and the identity keying its state cannot drift apart."""
    from pyecsdwan.client import OrchClient

    for spelling in ("https://orch.example.com", "https://orch.example.com/gms/rest"):
        settings = config.Settings(orch_url=spelling, api_key="k")
        client = OrchClient(settings)
        assert str(client._http.base_url).rstrip("/") == config.api_base(spelling)


def test_a_deeper_path_is_still_a_distinct_target():
    """Normalizing the suffix must not swallow a genuinely different base."""
    assert config.canonical_origin("https://orch/gms/rest/gms/rest") != config.canonical_origin(
        "https://orch"
    )


@pytest.mark.parametrize(
    "a,b",
    [
        ("https://[2001:db8::1]", "https://[2001:0db8:0:0:0:0:0:1]"),
        ("https://[::1]:8443", "https://[0:0:0:0:0:0:0:1]:8443"),
    ],
)
def test_equivalent_ipv6_literals_are_one_identity(a, b):
    """Re-bracketing fixes the port ambiguity and says nothing about a literal
    written two ways; `ipaddress` gives the one compressed form."""
    assert config.canonical_origin(a) == config.canonical_origin(b)


def test_a_capitalized_scheme_is_not_treated_as_a_hostname():
    """Schemes are case-insensitive, and operators paste capitalized URLs. A
    case-sensitive check prepended a second scheme."""
    assert config.canonical_origin("HTTPS://Orch.Example.COM") == "https://orch.example.com"
    assert config.canonical_origin("HtTp://orch") == "http://orch"


# -- the rolling upgrade (review P0-3b) --------------------------------------


def test_a_pre_63_lock_is_held_alongside_the_new_one(state_home):
    """Locks moved from the sanitized display host to an origin digest, so a
    surviving pre-#63 process — a detached watchdog, say — and a new one take
    different files and neither excludes the other."""
    from pyecsdwan.locking import HostLock

    root = config.lock_root()
    root.mkdir(parents=True, exist_ok=True)
    legacy = root / "orch.example.com.commit.lock"
    legacy.write_text("")  # a build before #63 has run here

    lock = HostLock("https://orch.example.com", "commit", timeout=0.2)
    with lock:
        assert [b.path for b in lock._barriers] == [legacy]
    assert lock._barriers == []


def test_the_barrier_excludes_a_pre_63_holder(state_home):
    """Across processes, since that is the only place exclusion is real."""
    from pyecsdwan.locking import HostLock, LockBusy

    root = config.lock_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "orch.example.com.commit.lock").write_text("")

    holder = _spawn_legacy_holder(state_home, "orch.example.com", "commit",
                                  state_home / "old.ready")
    try:
        with pytest.raises(LockBusy):
            HostLock("https://orch.example.com", "commit", timeout=0.5).acquire()
    finally:
        holder.kill()
        holder.wait(timeout=10)


def test_no_barrier_where_no_older_build_has_run(state_home):
    """Otherwise two origins sharing a display host would serialize forever,
    which is the #63 acceptance criterion this whole change exists to meet."""
    from pyecsdwan.locking import HostLock

    lock = HostLock("https://orch.example.com/tenant-a", "commit", timeout=0.2)
    assert lock._legacy_barrier() is None
    with lock:
        # A different tenant is unaffected.
        with HostLock("https://orch.example.com/tenant-b", "commit", timeout=0.5):
            pass


_LEGACY_HOLDER = """
import time
from pathlib import Path
from pyecsdwan.locking import lock_root, _safe
import os, sys
# A pre-#63 build's lock: the sanitized display host, no digest.
path = lock_root() / (_safe(sys.argv[1]) + "." + sys.argv[2] + ".lock")
fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
import fcntl
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
Path(sys.argv[3]).write_text("ready")
time.sleep(120)
"""


def _spawn_legacy_holder(state_home, host, scope, ready):
    import os
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", _LEGACY_HOLDER, host, scope, str(ready)],
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
            raise AssertionError(f"legacy holder died: {proc.communicate()[1]}")
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("legacy holder never took the lock")


def test_the_under_lock_recheck_refuses_an_unproven_journal(state_home):
    """`revert_txn_dir` refuses a legacy journal before it takes the lock, so
    the inner guard is unreachable through that door. It is still the one that
    matters: the inner function re-reads the journal *inside* the lock, and
    everything past that point acts on what was read there, not on what was
    true when the outer check ran. Tested against the inner function directly,
    which is where that invariant lives."""
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed("https://orch.example.com")
    meta = json.loads((staged_txn.dir / "meta.json").read_text())
    del meta["orch_origin"]
    meta["format"] = 1
    (staged_txn.dir / "meta.json").write_text(json.dumps(meta))

    report = txn_mod._revert_txn_dir_locked(
        journal.TxnJournal.open(staged_txn.dir),
        reason="test",
        ctx=_ctx_for("https://orch.example.com"),
        registry=default_registry,
    )
    assert not report.ok
    assert "refusing to restore" in report.messages[0]


def test_the_under_lock_recheck_still_refuses_a_different_origin(state_home):
    """The other half of the same guard: a bound journal from another target."""
    from pyecsdwan import txn as txn_mod
    from pyecsdwan.registry import default_registry

    staged_txn = _unconfirmed("https://orch.example.com/tenant-a")
    report = txn_mod._revert_txn_dir_locked(
        journal.TxnJournal.open(staged_txn.dir),
        reason="test",
        ctx=_ctx_for("https://orch.example.com/tenant-b"),
        registry=default_registry,
    )
    assert not report.ok
    assert "refusing: transaction targets" in report.messages[0]


def test_the_legacy_claim_is_serialized_on_the_shared_namespace(state_home):
    """Sequential adoption proves the unlink, not the lock. The race is between
    two *different* origins that compute the same legacy path, so a lock keyed
    by either origin would not exclude the other — and both would copy the same
    staging into their own store. Held from another process, since a lock is
    re-entrant within one."""
    from pyecsdwan.locking import LockBusy

    root = config.candidate_root()
    root.mkdir(parents=True, exist_ok=True)
    _legacy_candidate(root)

    holder = _spawn_claim_holder(state_home, "orch.example.com", state_home / "claim.ready")
    try:
        other = CandidateStore(
            "https://orch.example.com/tenant-b", root=root, lock_timeout=0.5
        )
        assert other.unadopted_legacy is not None
        with pytest.raises(LockBusy):
            other.adopt_legacy()
    finally:
        holder.kill()
        holder.wait(timeout=10)


_CLAIM_HOLDER = """
import sys, time
from pathlib import Path
from pyecsdwan.locking import HostLock
with HostLock(sys.argv[1], "candidate-legacy", timeout=10.0):
    Path(sys.argv[2]).write_text("ready")
    time.sleep(120)
"""


def _spawn_claim_holder(state_home, host, ready):
    import os
    import subprocess
    import sys

    proc = subprocess.Popen(
        [sys.executable, "-c", _CLAIM_HOLDER, host, str(ready)],
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
            raise AssertionError(f"claim holder died: {proc.communicate()[1]}")
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("claim holder never took the lock")


def test_the_real_commit_path_refuses_rather_than_saying_no_changes(state_home):
    """Testing the guard is not testing that anything calls it — the exact
    failure that let `clear()` survive on the shell path (#100). Driven through
    `commit_candidate`, which is the one cycle both interfaces use."""
    from pyecsdwan import txn as txn_mod

    root = config.candidate_root()
    root.mkdir(parents=True, exist_ok=True)
    _legacy_candidate(root)
    store = CandidateStore("https://orch.example.com", root=root)
    assert store.items == {}

    with pytest.raises(txn_mod.CommitError) as excinfo:
        txn_mod.commit_candidate(
            _ctx_for("https://orch.example.com"),
            __import__("pyecsdwan.registry", fromlist=["x"]).default_registry,
            store,
            config.Settings(orch_url="https://orch.example.com", api_key="k"),
        )
    assert "adopt --candidate" in str(excinfo.value)
