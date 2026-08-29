"""A desired-state directory as the source of intent (epic #8, GitOps read half).

`drift` against the candidate store answers "does the fabric match what I typed
since the last commit?". A CI pipeline wants a different question: does it match
the declaration in git — reviewed, versioned, the same on every run.

This is the read half only. It reads; it never writes. Landing it first is
deliberate: a layout mistake here costs a rename, and the same mistake
underneath a write path costs a fabric.

Two properties matter more than the parsing, and both are tested against the
real registry rather than a fixture:

* **The directory names are user-facing nouns, not registry kinds.** The kind
  for a per-appliance banner is `appliance/banners` — a path separator inside a
  directory name — so a tree keyed on kinds would silently split into two
  levels. #77 settled that registry keys don't reach surfaces operators type,
  and a checked-in tree is one.
* **A malformed declaration is fatal, never partial.** Reading half a directory
  would report the rest of the fabric as `undeclared`, which looks like a
  smaller fabric rather than a broken input — the exact collapse
  `reports/drift.py` exists to prevent.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config, desired
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Ref
from pyecsdwan.registry import default_registry
from pyecsdwan.reports import drift
from pyecsdwan.resolver import Resolver

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

KIND = "appliance/banners"
REF = Ref(KIND, "global", appliance="BR1-EC")


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def world(state_home: Any, mock_server: tuple[str, MockState]) -> dict[str, Any]:
    base_url, state = mock_server
    state.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    client = OrchClient(settings)
    return {
        "ctx": Ctx(client=client, resolver=Resolver(client)),
        "settings": settings,
        "state": state,
    }


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# -- the layout --------------------------------------------------------------


def test_a_declared_tree_resolves_to_refs(tmp_path: Path) -> None:
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: hello\n")
    _write(tmp_path, "fabric/interface-labels/global.yaml", "wan: {}\n")

    declared = desired.load(default_registry, tmp_path)

    assert len(declared) == 2
    assert declared.item_for(REF) is not None
    assert declared.item_for(Ref("interface-labels", "global")) is not None
    # And nothing else got swept in.
    assert declared.item_for(Ref(KIND, "global", appliance="BR2-EC")) is None


def test_the_tree_is_keyed_on_nouns_not_registry_kinds(tmp_path: Path) -> None:
    """The load-bearing one. `appliance/banners` contains a separator, so a
    kind-keyed tree splits into two directory levels and stops meaning what it
    says — quite apart from #77 forbidding keys on operator-facing surfaces."""
    _write(tmp_path, "appliances/BR1-EC/appliance/banners/global.yaml", "login: x\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)
    # It fails on the shape, and says what shape it wanted.
    assert "appliances/<appliance>/<noun>/<instance>.yaml" in str(caught.value)

    # ... while the noun spelling of the same resource works.
    other = tmp_path / "ok"
    _write(other, "appliances/BR1-EC/banners/global.yaml", "login: x\n")
    assert len(desired.load(default_registry, other)) == 1


def test_the_instance_name_comes_from_the_file_name(tmp_path: Path) -> None:
    _write(tmp_path, "fabric/bio/CorpFabric.yaml", "name: CorpFabric\n")
    declared = desired.load(default_registry, tmp_path)
    assert declared.item_for(Ref("bio", "CorpFabric")) is not None


def test_yml_is_accepted_too(tmp_path: Path) -> None:
    _write(tmp_path, "appliances/BR1-EC/banners/global.yml", "login: hello\n")
    assert len(desired.load(default_registry, tmp_path)) == 1


def test_non_yaml_files_are_ignored(tmp_path: Path) -> None:
    """A real GitOps repo has a README and a CI config in it. Those are not
    declarations and must not become parse errors."""
    _write(tmp_path, "README.md", "# our fabric\n")
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: hello\n")
    assert len(desired.load(default_registry, tmp_path)) == 1


def test_declaring_nothing_is_not_an_error(tmp_path: Path) -> None:
    """An empty declaration is a legitimate starting point — every instance
    then reports `undeclared`, which is the honest answer and exactly what a
    team adopting this incrementally should see."""
    assert len(desired.load(default_registry, tmp_path)) == 0


# -- malformed input is fatal, never partial ---------------------------------


@pytest.mark.parametrize(
    "rel,body,expect",
    [
        ("appliances/BR1-EC/banners/global.yaml", "login: [\n", "invalid YAML"),
        ("appliances/BR1-EC/banners/global.yaml", "", "is empty"),
        ("appliances/BR1-EC/banners/global.yaml", "- a\n- b\n", "must be a mapping"),
        ("appliances/BR1-EC/nonsense/global.yaml", "a: 1\n", "nonsense"),
        ("elsewhere/banners/global.yaml", "a: 1\n", "top-level directory"),
        ("fabric/global.yaml", "a: 1\n", "fabric/<noun>/<instance>.yaml"),
        # Too *deep*, not just too shallow: a `< 3` check would accept this and
        # then read parts[1]/parts[2] as the noun and file name, quietly
        # declaring something the path does not say. The sweep found this gap.
        ("fabric/bio/extra/CorpFabric.yaml", "a: 1\n", "fabric/<noun>/<instance>.yaml"),
        ("appliances/BR1-EC/banners/extra/global.yaml", "a: 1\n", "<appliance>/<noun>/"),
    ],
    ids=[
        "bad-yaml", "empty", "not-a-mapping", "unknown-noun", "wrong-root",
        "too-shallow", "fabric-too-deep", "appliance-too-deep",
    ],
)
def test_a_malformed_declaration_raises(
    tmp_path: Path, rel: str, body: str, expect: str
) -> None:
    _write(tmp_path, rel, body)
    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)
    assert expect in str(caught.value)


def test_one_bad_file_fails_the_whole_load(tmp_path: Path) -> None:
    """Not "skip it and carry on". A partially-read declaration reports the
    remainder as `undeclared`, which renders as a smaller fabric rather than a
    broken input — and `undeclared` is exit 0."""
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: fine\n")
    _write(tmp_path, "appliances/BR2-EC/banners/global.yaml", "login: [\n")

    with pytest.raises(desired.DesiredError):
        desired.load(default_registry, tmp_path)


def test_two_files_cannot_declare_one_instance(tmp_path: Path) -> None:
    """`.yaml` and `.yml` for the same instance is the realistic way this
    happens, and whichever won would depend on sort order."""
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: one\n")
    _write(tmp_path, "appliances/BR1-EC/banners/global.yml", "login: two\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)
    assert "both" in str(caught.value) and "declare" in str(caught.value)


def test_a_missing_directory_is_an_error(tmp_path: Path) -> None:
    """A mistyped path must not read as "nothing declared", which would report
    a whole fabric as undeclared and exit 0."""
    with pytest.raises(desired.DesiredError, match="not a directory"):
        desired.load(default_registry, tmp_path / "typo")


# -- through drift -----------------------------------------------------------


def test_a_declaration_that_matches_reports_in_sync(
    world: dict[str, Any], tmp_path: Path
) -> None:
    ctx = world["ctx"]
    resource = default_registry.get(KIND)
    live = resource.normalize(resource.fetch(ctx, REF))
    assert isinstance(live, dict) and live

    body = "".join(f"{k}: {v!r}\n" for k, v in live.items())
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", body)

    report = drift.collect(
        ctx, default_registry, desired.load(default_registry, tmp_path), kinds=[KIND]
    )
    row = next(r for r in report.rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.IN_SYNC, row
    assert report.exit_code == drift.EXIT_OK


def test_a_declaration_that_differs_reports_drift(
    world: dict[str, Any], tmp_path: Path
) -> None:
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: not what the box has\n")

    report = drift.collect(
        world["ctx"],
        default_registry,
        desired.load(default_registry, tmp_path),
        kinds=[KIND],
    )
    row = next(r for r in report.rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.DRIFT
    assert report.exit_code == drift.EXIT_DRIFT
    # Undeclared neighbours stay undeclared — a declaration for one appliance
    # says nothing about the others.
    assert {r.status for r in report.rows if r.appliance != "BR1-EC"} == {
        drift.Status.UNDECLARED
    }


def test_the_directory_and_the_candidate_agree_on_the_same_intent(
    world: dict[str, Any], tmp_path: Path, state_home: Any
) -> None:
    """The property that makes two intent sources safe.

    Both go through the same `materialize_desired`, so the same declared value
    must produce the same verdict whichever way it arrives. If these ever
    diverge, `drift` reports something `commit` would not do — worse than
    either source being wrong on its own.
    """
    ctx = world["ctx"]
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: same value\n")

    candidate = CandidateStore(world["settings"].host)
    candidate.set_desired(REF, {"login": "same value"})

    from_dir = drift.collect(
        ctx, default_registry, desired.load(default_registry, tmp_path), kinds=[KIND]
    )
    from_candidate = drift.collect(
        ctx, default_registry, drift.CandidateIntent(candidate), kinds=[KIND]
    )
    assert from_dir.counts == from_candidate.counts
    assert [r.status for r in from_dir.rows] == [r.status for r in from_candidate.rows]
    assert from_dir.exit_code == from_candidate.exit_code


def test_an_undeclared_non_default_value_is_drift(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The safety property replace mode buys, stated precisely.

    An omitted key does *not* mean "leave whatever is there" — it takes the
    value `normalize()` supplies for an absent key. So a box holding a
    non-default value for something nobody declared is drift. Under merge it
    would take the live value and report in sync, which would make an
    incomplete declaration indistinguishable from a complete one.

    Written against a *deliberately non-default* live value, because banners'
    `normalize()` fills omitted keys with `""` and the mock's seed is `""` too
    — declaring a subset of an all-default object is genuinely in sync, and a
    test that used it would assert the opposite of the truth.
    """
    ctx, state = world["ctx"], world["state"]
    state.appliance_ecos["3.NE"]["banners"] = {"login": "declared", "motd": "nobody asked for this"}

    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "login: declared\n")

    report = drift.collect(
        ctx, default_registry, desired.load(default_registry, tmp_path), kinds=[KIND]
    )
    row = next(r for r in report.rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.DRIFT, (
        "an undeclared non-default value must show as drift; merge would hide it"
    )
    assert "motd" in row.detail


def test_declaring_a_subset_of_an_all_default_object_is_in_sync(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """The other side, and the reason the test above needs its non-default
    fixture: where `normalize()` fills an omitted key with the same value the
    appliance reports, a partial declaration really is complete. Minimal files
    are a feature — replace mode does not force an operator to restate every
    default.

    Note `issue`, not `login`: the mock's banners object carries `issue` and
    `motd`, and declaring a key it does not have *adds* one — drift for a real
    and different reason."""
    _write(tmp_path, "appliances/BR1-EC/banners/global.yaml", "issue: ''\n")

    report = drift.collect(
        world["ctx"],
        default_registry,
        desired.load(default_registry, tmp_path),
        kinds=[KIND],
    )
    row = next(r for r in report.rows if r.appliance == "BR1-EC")
    assert row.status is drift.Status.IN_SYNC, row


def test_materialized_intent_does_not_alias_the_declaration(tmp_path: Path) -> None:
    """`Declared.desired_for` delegates to `materialize_desired`, which deep-copies.

    A shallow copy would pass every other test here — the flat values compare
    equal either way, which is why the mutation sweep reported this as
    unprotected. It would still be a real bug: `normalize()` implementations
    receive the desired state and are free to shape it in place, so a shared
    nested object would let one instance's normalization corrupt the
    declaration the next instance is compared against.
    """
    _write(
        tmp_path,
        "appliances/BR1-EC/banners/global.yaml",
        "login: hi\nnested:\n  inner: original\n",
    )
    declared = desired.load(default_registry, tmp_path)
    item = declared.item_for(REF)
    assert item is not None

    materialized = declared.desired_for(item, None)
    assert materialized == item.intent
    materialized["nested"]["inner"] = "mutated by a consumer"
    assert item.intent["nested"]["inner"] == "original", "the declaration was aliased"

    # And a second read is unaffected by the first consumer.
    assert declared.desired_for(item, None)["nested"]["inner"] == "original"
