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


def test_an_empty_declaration_set_is_invalid(tmp_path: Path) -> None:
    """D6, and it reverses what this test used to assert.

    An empty directory read as "declare nothing" is indistinguishable from a
    mistyped path, a failed checkout, or a template that rendered nothing —
    and the cost of guessing wrong is an apply that reports success having
    done nothing at all. The ratified spec makes it invalid, with no
    `--allow-empty` escape hatch in v1.
    """
    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "no declarations" in str(caught.value)


def test_a_directory_of_non_yaml_is_also_empty(tmp_path: Path) -> None:
    """The realistic version of D6: the checkout succeeded, but nothing in it
    is a declaration. Same answer, for the same reason."""
    (tmp_path / "README.md").write_text("not a declaration", encoding="utf-8")

    with pytest.raises(desired.DesiredError):
        desired.load(default_registry, tmp_path)


# -- malformed input is fatal, never partial ---------------------------------


@pytest.mark.parametrize(
    "rel,body,expect",
    [
        ("appliances/BR1-EC/banners/global.yaml", "login: [\n", "invalid YAML"),
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
        "bad-yaml", "not-a-mapping", "unknown-noun", "wrong-root",
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
        ctx, default_registry, candidate, kinds=[KIND]
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


def test_the_same_directory_always_builds_the_same_order(tmp_path: Path) -> None:
    """Deterministic, because the files are read in sorted order — a plan built
    from a directory must not depend on filesystem iteration order."""
    for rel in (
        "appliances/BR2-EC/banners/global.yaml",
        "fabric/interface-labels/global.yaml",
        "appliances/BR1-EC/banners/global.yaml",
    ):
        _write(tmp_path, rel, "login: x\n" if "banners" in rel else "wan: {}\n")

    first = [i.ref_key for i in desired.load(default_registry, tmp_path).ordered_items()]
    second = [i.ref_key for i in desired.load(default_registry, tmp_path).ordered_items()]
    assert first == second
    assert len(first) == 3


# -- the versioned envelope (T7, spec 003 ratified 1.0.0) --------------------


def _raw(root: Path, rel: str, text: str) -> Path:
    """Write a declaration file verbatim, envelope and all."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


BANNERS = "appliances/BR1-EC/banners/global.yaml"


def test_the_envelope_is_required_and_says_how_to_add_it(tmp_path: Path) -> None:
    """A bare mapping was the pre-T7 format. It is refused rather than assumed
    to be v1: the whole point of an explicit version is that a future format
    can be told apart from this one instead of guessed at.

    The assertion is on the *guidance*, not merely on a refusal. A missing
    version is caught by the version comparison anyway — the separate branch
    exists solely to say "add this line", which is the migration instruction
    for every file written before this change. The mutation sweep found that
    a laxer assertion could not tell the two apart, so the branch would have
    been free to rot into `apiVersion None is not 'pyecsdwan/v1'`.
    """
    _raw(tmp_path, BANNERS, "issue: no envelope\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert f"Add `apiVersion: {desired.API_VERSION}`" in str(caught.value)


def test_a_future_api_version_fails_closed_without_rewriting(tmp_path: Path) -> None:
    """Same rule the candidate store follows (#108): a newer format is not
    damaged, and the tool that wrote it must still be able to use it."""
    path = _raw(
        tmp_path, BANNERS, "apiVersion: pyecsdwan/v99\nstate: present\nspec:\n  issue: x\n"
    )
    before = path.read_bytes()

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "v99" in str(caught.value)
    assert path.read_bytes() == before


def test_state_is_required(tmp_path: Path) -> None:
    """The lifecycle is explicit because absence never means deletion (D4).
    Defaulting it would make `state` decorative on the one file where it is
    load-bearing."""
    _raw(tmp_path, BANNERS, f"apiVersion: {desired.API_VERSION}\nspec:\n  issue: x\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "state must be one of" in str(caught.value)


def test_absent_is_parsed_and_refused_for_now(tmp_path: Path) -> None:
    """`absent` is the only v1 deletion mechanism (D4/R5) and is gated on
    per-resource deletion and rollback evidence (D16, T11). Until a resource
    has that, declaring a deletion the tool cannot prove it can undo is the
    one thing this format must not accept — so it parses, and refuses."""
    _raw(tmp_path, BANNERS, f"apiVersion: {desired.API_VERSION}\nstate: absent\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "not supported yet" in str(caught.value)


def test_absent_may_not_carry_a_spec(tmp_path: Path) -> None:
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: absent\nspec:\n  issue: x\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "must not carry a spec" in str(caught.value)


def test_present_requires_a_spec(tmp_path: Path) -> None:
    """`spec: {}` declares an empty object; a missing spec is a file that
    forgot to say anything, and the two must not be the same."""
    _raw(tmp_path, BANNERS, f"apiVersion: {desired.API_VERSION}\nstate: present\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "requires a `spec`" in str(caught.value)


def test_an_empty_spec_is_a_valid_declaration(tmp_path: Path) -> None:
    """Guards the guard for the rule above."""
    _raw(tmp_path, BANNERS, f"apiVersion: {desired.API_VERSION}\nstate: present\nspec: {{}}\n")

    declared = desired.load(default_registry, tmp_path)

    assert len(declared) == 1


def test_an_unknown_envelope_key_is_refused(tmp_path: Path) -> None:
    """A typo'd `speec:` silently ignored would apply an empty object over a
    live one. Refusing an unknown key costs a rename; ignoring it costs a
    resource."""
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspeec:\n  issue: x\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "unknown envelope key" in str(caught.value)


# -- identity: the document is the authority ---------------------------------


def test_a_document_may_restate_its_own_address(tmp_path: Path) -> None:
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: present\n"
        f"kind: banners\nappliance: BR1-EC\nname: global\nspec:\n  issue: x\n",
    )

    declared = desired.load(default_registry, tmp_path)

    assert list(declared.items) == [
        Ref("appliance/banners", "global", appliance="BR1-EC").key()
    ]


@pytest.mark.parametrize(
    "line,expect",
    [
        ("appliance: BR2-EC", "appliance"),
        ("name: motd", "name"),
        ("kind: bgp", "kind"),
    ],
)
def test_a_document_disagreeing_with_its_path_is_invalid(
    tmp_path: Path, line: str, expect: str
) -> None:
    """Neither one wins, because either winner is a trap: silently preferring
    the path would apply a file whose contents say BR1-EC to BR2-EC, and
    silently preferring the document would make the tree a lie."""
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: present\n{line}\nspec:\n  issue: x\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert expect in str(caught.value)
    assert "refusing rather than choosing" in str(caught.value)


# -- loading is all or nothing -----------------------------------------------


def test_every_problem_is_reported_at_once(tmp_path: Path) -> None:
    """A CI gate that surfaces one typo per run costs a round trip each time.
    Fatal either way — this is about how much the operator learns per run."""
    _raw(tmp_path, BANNERS, "issue: no envelope\n")
    _raw(tmp_path, "appliances/BR2-EC/nonsense/global.yaml",
         f"apiVersion: {desired.API_VERSION}\nstate: present\nspec: {{}}\n")

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    message = str(caught.value)
    assert "2 problem(s)" in message
    assert "apiVersion" in message
    assert "nonsense" in message


def test_a_symlink_out_of_the_tree_is_refused(tmp_path: Path) -> None:
    """`rglob` follows symlinks, so a link inside the tree can pull in another
    checkout — or /etc — and have it read as declared intent. The declaration
    set has to be exactly the reviewed directory."""
    outside = tmp_path.parent / "outside-the-tree"
    outside.mkdir(exist_ok=True)
    (outside / "global.yaml").write_text(
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n  issue: smuggled\n",
        encoding="utf-8",
    )
    root = tmp_path / "desired"
    (root / "appliances" / "BR1-EC" / "banners").mkdir(parents=True)
    _raw(root, "appliances/BR2-EC/banners/global.yaml",
         f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n  issue: real\n")
    (root / "appliances" / "BR1-EC" / "banners" / "global.yaml").symlink_to(
        outside / "global.yaml"
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, root)

    assert "outside" in str(caught.value)


# -- the declaration set has a stable identity -------------------------------


def test_the_digest_is_stable_across_loads(tmp_path: Path) -> None:
    _write(tmp_path, BANNERS, "issue: x\n")

    first = desired.load(default_registry, tmp_path).digest
    second = desired.load(default_registry, tmp_path).digest

    assert first == second
    assert len(first) == 64


def test_the_digest_does_not_depend_on_where_the_checkout_lives(
    tmp_path: Path
) -> None:
    """Two checkouts of the same reviewed declarations are the same desired
    state. A digest that disagreed could not be used to say "this is the plan
    that was approved"."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, BANNERS, "issue: x\n")
    _write(b, BANNERS, "issue: x\n")

    assert desired.load(default_registry, a).digest == desired.load(default_registry, b).digest


def test_a_changed_value_changes_the_digest(tmp_path: Path) -> None:
    """Guards the guard: a constant digest would satisfy both tests above."""
    a, b = tmp_path / "a", tmp_path / "b"
    _write(a, BANNERS, "issue: x\n")
    _write(b, BANNERS, "issue: y\n")

    assert desired.load(default_registry, a).digest != desired.load(default_registry, b).digest


# -- adversarial input (2026-08-29 review) ------------------------------------


@pytest.mark.parametrize(
    "doc,what",
    [
        (
            "apiVersion: pyecsdwan/v1\nstate: present\nspec:\n  issue: REVIEWED\n"
            "spec:\n  issue: SILENTLY WINS\n",
            "duplicate spec",
        ),
        (
            "apiVersion: pyecsdwan/v1\napiVersion: pyecsdwan/v99\n"
            "state: present\nspec:\n  issue: x\n",
            "duplicate apiVersion",
        ),
        (
            "apiVersion: pyecsdwan/v1\nstate: present\nstate: absent\nspec:\n  issue: x\n",
            "duplicate state",
        ),
        (
            "apiVersion: pyecsdwan/v1\nstate: present\nspec:\n  issue: a\n  issue: b\n",
            "duplicate nested key",
        ),
        (
            "apiVersion: pyecsdwan/v1\nstate: present\nname: global\nname: motd\n"
            "spec:\n  issue: x\n",
            "duplicate identity key",
        ),
    ],
    ids=["spec", "apiVersion", "state", "nested", "identity"],
)
def test_duplicate_keys_are_refused(tmp_path: Path, doc: str, what: str) -> None:
    """`yaml.safe_load` is last-key-wins, so two `spec:` blocks parse cleanly
    and the *second* is applied.

    That defeats every other check in this module: the unknown-key guard, the
    identity reconciliation, and the human review all inspect a document that
    is not the one that would be written. A duplicate key is never intentional
    in a reviewed declaration, and silently picking one is the worst available
    answer — so the loader rejects duplicates at every mapping level.
    """
    _raw(tmp_path, BANNERS, doc)

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "duplicate key" in str(caught.value), what


def test_an_ordinary_document_is_not_mistaken_for_a_duplicate(tmp_path: Path) -> None:
    """Guards the guard: the same key under *different* parents is normal, and
    a check that flagged it would reject most real declarations."""
    _raw(
        tmp_path, "fabric/interface-labels/global.yaml",
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n"
        f"  wan:\n    '3':\n      name: LTE\n  lan:\n    '4':\n      name: DMZ\n",
    )

    assert len(desired.load(default_registry, tmp_path)) == 1


def test_an_unquoted_date_is_refused(tmp_path: Path) -> None:
    """YAML infers `2026-08-29` as a `datetime.date`. Stringified for the
    digest it becomes `"2026-08-29"` — identical to the quoted string, which
    is a different declaration. Two distinct inputs must not share one
    identity, so the inferred type is refused rather than coerced."""
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n  expires: 2026-08-29\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "Quote it" in str(caught.value)


def test_the_quoted_form_is_accepted(tmp_path: Path) -> None:
    """Guards the guard, and shows the fix the message asks for."""
    _raw(
        tmp_path, BANNERS,
        f'apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n  expires: "2026-08-29"\n',
    )

    assert len(desired.load(default_registry, tmp_path)) == 1


def test_a_non_string_key_is_refused_at_load_not_at_digest(tmp_path: Path) -> None:
    """A YAML integer key used to survive `load()` and then raise `TypeError`
    from `digest()` — after the caller had been told the directory was fine.
    Failing at the boundary is the difference between an input error and a
    crash somewhere downstream."""
    _raw(
        tmp_path, BANNERS,
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n  1: int key\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "non-string key" in str(caught.value)


def test_nested_values_are_validated_too(tmp_path: Path) -> None:
    """The check is recursive: an inferred type three levels down is exactly as
    ambiguous as one at the top."""
    _raw(
        tmp_path, "fabric/interface-labels/global.yaml",
        f"apiVersion: {desired.API_VERSION}\nstate: present\nspec:\n"
        f"  wan:\n    '3':\n      renewed: 2026-08-29\n",
    )

    with pytest.raises(desired.DesiredError) as caught:
        desired.load(default_registry, tmp_path)

    assert "wan.3.renewed" in str(caught.value)


def test_the_schema_version_is_inside_the_digest_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same values under a different envelope version are a different
    contract. If the version were outside the digest, a future v2 that
    reinterpreted the same YAML would produce an identical identity — and
    "this is the plan that was approved" would stop being true."""
    _write(tmp_path, BANNERS, "issue: x\n")
    first = desired.load(default_registry, tmp_path).digest

    monkeypatch.setattr(desired, "API_VERSION", "pyecsdwan/v2")
    _raw(
        tmp_path, BANNERS,
        "apiVersion: pyecsdwan/v2\nstate: present\nspec:\n  issue: x\n",
    )
    second = desired.load(default_registry, tmp_path).digest

    assert first != second
