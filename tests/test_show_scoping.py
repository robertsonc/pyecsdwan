"""Appliance-scoped instance discovery and terminal-state rendering (#76, #78).

#78 reports that `show appliance <name> appliance/banners` produces no visible
result. Against the bundled mock it produces a *visible* result — but a useless
one, for a reason #78 does not name and #76 does:

    appliance/banners: name required; instances: global, global, global

`list_refs()` enumerates the whole fabric, so an appliance-scoped singleton
offers one identical name per appliance and then refuses to act on any of them.
The operator has already named the appliance; the ambiguity is manufactured.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from rich.console import Console

from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli.shell import ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import AmbiguousInstances, default_registry, scoped_instances
from pyecsdwan.resolver import Resolver

#: Every appliance in the mock fabric holds one `banners` object named `global`.
SINGLETON_KIND = "appliance/banners"
#: What the operator types for it. A separate constant on purpose: `kind` is
#: the internal identifier that appears in refs and the candidate store, and
#: #74 withdrew the registry key as a command token — so the two are not
#: interchangeable, and a test that spells one where it means the other proves
#: nothing (#77).
SINGLETON_NOUN = "banners"


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    yield
    structlog.reset_defaults()


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def shell_state(state_home: Any, mock_server: tuple[str, MockState]) -> ShellState:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(
        orch_url=base_url, api_key="test-key",
        job_timeout=5.0, job_poll_initial=0.01, job_poll_max=0.02,
    )
    client = OrchClient(settings)
    return ShellState(
        ctx=Ctx(client=client, resolver=Resolver(client)),
        registry=default_registry,
        settings=settings,
        console=Console(record=True, width=200),
        candidate=CandidateStore(settings.host),
    )


def _shell(state: ShellState, line: str) -> str:
    state.console = Console(record=True, width=200)
    dispatch_operational(line, state)
    return state.console.export_text()


# -- the reproduction --------------------------------------------------------


def test_the_fabric_really_does_hold_one_identical_name_per_appliance() -> None:
    """The precondition the bug rests on — asserted, not assumed."""
    import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins

    resource = default_registry.get(SINGLETON_KIND)
    assert resource.scope.value == "appliance"


def test_naming_the_appliance_is_enough_to_resolve_a_singleton(
    shell_state: ShellState,
) -> None:
    """The #76/#78 fix: scope discovery to the appliance already named.

    Before this, the command refused with three identical candidate names and
    no indication that `global` was the answer.
    """
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert "name required" not in out, out
    assert "global, global" not in out, out
    # It resolved and rendered the object.
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


def test_the_explicit_instance_form_still_works(shell_state: ShellState) -> None:
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN} global")
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


def test_discovery_does_not_offer_instances_from_other_appliances(
    shell_state: ShellState,
) -> None:
    """Whatever it resolves, it must belong to the appliance that was named."""
    out = _shell(shell_state, f"show appliance BR2-EC {SINGLETON_NOUN}")
    assert ":BR2-EC:" in out, out
    for other in ("BR1-EC", "HUB1-EC"):
        assert f":{other}:" not in out, out


# -- distinct terminal states (#78) -----------------------------------------


def test_an_unknown_appliance_says_so_rather_than_reporting_no_instances(
    shell_state: ShellState,
) -> None:
    """"No instances on X" is the wrong answer when X does not exist.

    This is the literal command from #78's report; in the mock fabric that
    appliance is absent, so it exercises the unknown-target path.
    """
    out = _shell(shell_state, f"show appliance S1-ecv-01 {SINGLETON_NOUN}")
    assert "unknown appliance" in out and "S1-ecv-01" in out, out
    assert "no instances" not in out, out


def test_an_appliance_scoped_kind_without_an_appliance_says_what_to_type(
    shell_state: ShellState,
) -> None:
    """This message must not be buried behind fabric-wide enumeration."""
    out = _shell(shell_state, f"show {SINGLETON_NOUN}")
    assert "appliance-scoped" in out, out
    # The remedy is spelled with the user-facing noun, not the registry key
    # the operator happened to type (#77).
    assert "show appliance <name> banners" in out, out


def test_every_terminal_state_produces_visible_output(shell_state: ShellState) -> None:
    """R8: a renderer may never reduce a result to zero visible characters.

    The operator must always be able to tell the command finished, whatever
    the outcome — that is #78's actual complaint, independent of which branch
    produced it.
    """
    lines = [
        f"show appliance BR1-EC {SINGLETON_NOUN}",          # ok
        f"show appliance BR1-EC {SINGLETON_NOUN} global",   # ok, explicit
        f"show appliance S1-ecv-01 {SINGLETON_NOUN}",       # unknown appliance
        f"show {SINGLETON_NOUN}",                           # scope error
        "show appliance BR1-EC no-such-kind",               # unknown kind
        "show appliance BR1-EC",                            # incomplete
    ]
    for line in lines:
        out = _shell(shell_state, line).strip()
        assert out, f"zero visible output for: {line}"


def test_the_prompt_survives_every_failure(shell_state: ShellState) -> None:
    """Each failure returns control; none raises out of the dispatcher."""
    for line in (
        f"show appliance S1-ecv-01 {SINGLETON_NOUN}",
        "show appliance BR1-EC no-such-kind",
        "show appliance",
        f"show {SINGLETON_NOUN}",
    ):
        dispatch_operational(line, shell_state)  # must not raise
    # Still usable afterwards.
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


# -- empty is an answer, and a different one from absent --------------------


def test_an_empty_configuration_is_reported_explicitly(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{}` rendered as YAML is a bare `{}` — indistinguishable in a scrollback
    from the command having done nothing. It gets a sentence instead."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: {})
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN} global")
    assert "empty" in out.lower(), out
    assert out.strip(), out


def test_absent_and_empty_are_different_answers(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: None)
    absent = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN} global")
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: {})
    empty = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN} global")
    assert "not present" in absent.lower(), absent
    assert "empty" in empty.lower(), empty
    assert absent.strip() != empty.strip()


# -- the case the mock's fixtures cannot reach ------------------------------
#
# Every appliance-scoped kind in the bundled mock is a per-appliance singleton
# named `global`, so deduplication alone resolves them and the appliance filter
# never has to do any work. That makes the mock unable to exercise the case the
# filter exists for: a kind whose instance *names differ per appliance*.
#
# Without a synthetic fixture this code path would be covered by tests that
# pass whether or not the filter is present — which is the same trap as the
# `/gms/versions` fixture in the #54 epic, where `installed[0] == current`
# meant the wrong implementation passed every assertion.


def _multi_instance_refs(appliance_names: dict[str, list[str]]) -> Any:
    """A `list_refs` that returns different instance names on each appliance."""
    from pyecsdwan.contract import Ref

    def list_refs(self: Any, ctx: Any) -> Any:
        for appliance, names in appliance_names.items():
            for name in names:
                yield Ref(kind=SINGLETON_KIND, name=name, appliance=appliance)

    return list_refs


def test_discovery_never_offers_another_appliances_instance_names(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The appliance filter, isolated.

    With per-appliance-distinct names, dropping the filter makes BR2-EC's
    instances appear as candidates on BR1-EC — and picking one would build a
    ref pairing BR1-EC with a name that only exists on BR2-EC.
    """
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(
        type(resource),
        "list_refs",
        _multi_instance_refs({"BR1-EC": ["only-on-br1"], "BR2-EC": ["only-on-br2"]}),
    )
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert "only-on-br2" not in out, out


def test_a_single_instance_on_the_named_appliance_resolves_without_asking(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One candidate after filtering is not ambiguous, however many exist
    elsewhere in the fabric."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(
        type(resource),
        "list_refs",
        _multi_instance_refs(
            {"BR1-EC": ["only-on-br1"], "BR2-EC": ["a", "b"], "HUB1-EC": ["c", "d"]}
        ),
    )
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert "name required" not in out, out
    assert "only-on-br1" in out, out


def test_genuine_ambiguity_on_one_appliance_still_asks(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filter must not turn a real choice into a silent guess — and the
    names it offers must all be on the appliance that was named."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(
        type(resource),
        "list_refs",
        _multi_instance_refs({"BR1-EC": ["wan1", "wan2"], "BR2-EC": ["elsewhere"]}),
    )
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert "name required" in out and "BR1-EC" in out, out
    assert "wan1" in out and "wan2" in out, out
    assert "elsewhere" not in out, out


# -- user-facing nouns in the shell (#77) -----------------------------------


def test_the_noun_works_without_the_registry_prefix(shell_state: ShellState) -> None:
    """The headline of #77: `banners`, not `appliance/banners`."""
    out = _shell(shell_state, "show appliance BR1-EC banners")
    assert "appliance/banners:BR1-EC:global" in out, out
    assert "internal registry key" not in out, out


def test_the_registry_key_is_not_a_command_token(shell_state: ShellState) -> None:
    """#82 accepted it with a warning; #74 withdrew both.

    compatibility.md rule 1: removed means removed — the ordinary unknown-token
    error, not a deprecation path. Rule 2: assert the old spelling is *gone*,
    so the removal cannot regress into a half-supported form.
    """
    out = _shell(shell_state, "show appliance BR1-EC appliance/banners")
    assert "unknown resource kind" in out, out
    assert "still works" not in out, out
    # The ref rendering is untouched — `kind` is still the internal identifier,
    # which is the whole reason it must not also be a thing operators type.
    assert "appliance/banners:BR1-EC:global" not in out, out


def test_the_two_zones_are_different_objects_reached_by_scope(
    shell_state: ShellState,
) -> None:
    """The collision that forced a per-scope namespace, exercised end to end."""
    appliance_side = _shell(shell_state, "show appliance BR1-EC zones")
    assert "appliance/zones:BR1-EC" in appliance_side, appliance_side
    assert "internal registry key" not in appliance_side, appliance_side


def test_set_and_show_resolve_a_noun_identically(shell_state: ShellState) -> None:
    """Principle IV: one grammar across interfaces."""
    from pyecsdwan.cli.shell import _parse_ref

    ref, _rest = _parse_ref(
        ["appliance", "BR1-EC", "banners", "global", "motd", "hi"],
        shell_state,
        "usage",
    )
    assert ref.kind == SINGLETON_KIND
    assert ref.appliance == "BR1-EC"


def test_an_orchestrator_noun_typed_without_a_scope_still_works(
    shell_state: ShellState,
) -> None:
    """An absent scope noun means the Orchestrator — it is not ambiguity."""
    out = _shell(shell_state, "show interface-labels")
    assert "unknown resource kind" not in out, out
    assert "ambiguous" not in out, out


# -- #76 at the scale it was reported ---------------------------------------
#
# Everything above runs on the seeded three-appliance fabric. #76's report is
# six, and three is enough to *have* the bug but not enough to show what made
# it unusable: six identical names in one error line, none of them wrong.


def _wide(shell_state: ShellState, mstate: MockState, grow: Any, **kw: Any) -> list[str]:
    """Grow the fabric to six and re-point the resolver at it."""
    added = grow(mstate, 6, **kw)
    shell_state.ctx.resolver.refresh()
    return added


def test_six_appliances_is_the_precondition_the_report_describes(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """Asserted, not assumed: `list_refs()` really does yield six `global`s."""
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric)
    resource = default_registry.get(SINGLETON_KIND)
    refs = list(resource.list_refs(shell_state.ctx))
    assert len(refs) == 6, refs
    assert {r.name for r in refs} == {"global"}, refs
    assert len({r.appliance for r in refs}) == 6, refs


def test_a_singleton_resolves_on_every_one_of_six_appliances(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """The headline acceptance criterion, at the reported scale.

    Six candidates before scoping, one after — on each appliance in turn, so a
    filter that happened to work for the first name is not mistaken for one
    that works.
    """
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric, seed_ecos={"banners": {"motd": "hi", "issue": ""}})
    for name in shell_state.ctx.resolver.appliance_names():
        out = _shell(shell_state, f"show appliance {name} banners")
        assert "name required" not in out, out
        assert f"appliance/banners:{name}:global" in out, out


def test_six_identical_names_never_reach_the_operator(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """The literal output #76 pasted, asserted absent."""
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric)
    out = _shell(shell_state, "show appliance BR1-EC nat-maps")
    assert "global, global" not in out, out


def test_fabric_wide_enumeration_keeps_one_attributed_ref_per_appliance(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """Scoping narrows the selector, not just the list — and the unscoped
    selector is (appliance, name).

    #76 asks for both halves: another appliance's refs must never appear in a
    scoped view, *and* fabric-wide enumeration must still preserve one
    attributed ref per appliance. Collapsing on the bare name satisfies the
    first and destroys the second: all six `deployment` refs are one name, and
    a helper that treats that as a collision refuses to enumerate the fabric
    at all.
    """
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric)
    resource = default_registry.get("appliance/deployment")
    refs = scoped_instances(resource, shell_state.ctx, None)
    assert len(refs) == 6, refs
    assert {r.name for r in refs} == {"deployment"}, refs
    assert len({r.appliance for r in refs}) == 6, refs


def test_scoped_and_fabric_wide_disagree_only_about_scope(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """Each appliance's scoped view is exactly its slice of the fabric view."""
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric)
    resource = default_registry.get("appliance/deployment")
    everything = scoped_instances(resource, shell_state.ctx, None)
    rebuilt = [
        ref
        for name in shell_state.ctx.resolver.appliance_names()
        for ref in scoped_instances(resource, shell_state.ctx, name)
    ]
    assert sorted(str(r) for r in rebuilt) == sorted(str(r) for r in everything)


# -- one address, two objects ------------------------------------------------


class _DuplicateRefs:
    """A resource whose list_refs() yields the same ref twice.

    Nothing in the shipped registry does this (verified against every curated
    kind on the mock fabric), so the guard needs a resource built to break it.
    """

    kind = SINGLETON_KIND

    def list_refs(self, ctx: Any) -> Iterator[Any]:
        from pyecsdwan.contract import Ref

        yield Ref(kind=SINGLETON_KIND, name="global", appliance="BR1-EC")
        yield Ref(kind=SINGLETON_KIND, name="global", appliance="BR1-EC")


def test_one_address_matching_two_instances_is_reported_not_guessed(
    shell_state: ShellState,
) -> None:
    """Within a scope the ref *is* the selector, so a repeated ref is an
    address that cannot address anything.

    Either the resource yielded a harmless duplicate or it flattened two
    distinct objects onto one address; from here those are indistinguishable
    and only the second is dangerous. Keeping whichever arrived first makes
    `fetch()` a coin toss and says nothing.
    """
    with pytest.raises(AmbiguousInstances) as excinfo:
        scoped_instances(_DuplicateRefs(), shell_state.ctx, "BR1-EC")
    message = str(excinfo.value)
    assert "global" in message and "BR1-EC" in message, message
    # Names the culprit: this is a resource defect, not an operator error.
    assert "_DuplicateRefs" in message and "list_refs" in message, message


def test_the_shell_reports_the_defect_instead_of_picking_one(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it stays a shell error — the prompt survives it."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "list_refs", _DuplicateRefs.list_refs)
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_NOUN}")
    assert "more than one instance" in out, out
    assert ":BR1-EC:global" not in out, out


# -- deterministic order (the promotion checklist samples one) --------------


def test_the_instance_sampled_is_the_same_whatever_order_it_arrives_in(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`plugin promote` runs its checklist against the first ref, so "first"
    has to mean something. Raw list_refs() order is whatever the resource
    happened to build."""
    resource = default_registry.get(SINGLETON_KIND)
    order_a = _multi_instance_refs({"BR1-EC": ["wan2", "wan1", "wan3"]})
    order_b = _multi_instance_refs({"BR1-EC": ["wan3", "wan1", "wan2"]})

    monkeypatch.setattr(type(resource), "list_refs", order_a)
    first_a = scoped_instances(resource, shell_state.ctx, "BR1-EC")[0]
    monkeypatch.setattr(type(resource), "list_refs", order_b)
    first_b = scoped_instances(resource, shell_state.ctx, "BR1-EC")[0]
    assert first_a == first_b == first_a.__class__(
        kind=SINGLETON_KIND, name="wan1", appliance="BR1-EC"
    )


# -- completion consumes the same scoped results (#49, #76) -----------------
#
# #76: "Ensure dynamic completion consumes the same scoped/deduplicated
# results" and "another appliance's refs never appear in scoped help,
# completion, or error output". Before this, completion stopped at the kind
# noun: the only way to learn an instance name was to run the command without
# one and read the names out of the error — which for a singleton was six
# copies of `global` (#78).


def _completer(state: ShellState) -> Any:
    from pyecsdwan.cli.shell import ShellCompleter

    return ShellCompleter(state)


def test_completion_offers_instance_names_after_an_appliance_scoped_kind(
    shell_state: ShellState, mock_server: tuple[str, MockState], wide_fabric: Any
) -> None:
    """The position that used to offer nothing at all."""
    _base, mstate = mock_server
    _wide(shell_state, mstate, wide_fabric)
    options = _completer(shell_state)._options(["show", "appliance", "BR1-EC", "banners"])
    assert options == ["global"], options


def test_completion_offers_instance_names_after_an_orchestrator_kind(
    shell_state: ShellState,
) -> None:
    """Same position, other scope — `show <kind> <TAB>` with no appliance."""
    options = _completer(shell_state)._options(["show", "region"])
    assert options == ["Default", "EMEA"], options


def test_completion_offers_the_same_names_for_set_and_show(
    shell_state: ShellState,
) -> None:
    """Principle IV: one grammar across interfaces — including completion."""
    completer = _completer(shell_state)
    for prior in (["show", "appliance", "BR1-EC", "banners"], ["show", "region"]):
        assert completer._options(prior) == completer._options(["set", *prior[1:]])


def test_completion_never_offers_another_appliances_instances(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mock's singletons are all named `global`, so scoping and not
    scoping produce the same list and a passing test would prove nothing.
    Distinct per-appliance names make the filter observable."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(
        type(resource),
        "list_refs",
        _multi_instance_refs({"BR1-EC": ["only-on-br1"], "BR2-EC": ["only-on-br2"]}),
    )
    options = _completer(shell_state)._options(["show", "appliance", "BR1-EC", "banners"])
    assert options == ["only-on-br1"], options


def test_completion_offers_exactly_what_the_command_accepts(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completion the command then rejects is worse than no completion.

    Both surfaces go through the same `scoped_instances`, so this holds by
    construction — asserted because the construction is the point of #76.
    """
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(
        type(resource),
        "list_refs",
        _multi_instance_refs({"BR1-EC": ["wan1", "wan2"], "BR2-EC": ["elsewhere"]}),
    )
    offered = _completer(shell_state)._options(["show", "appliance", "BR1-EC", "banners"])
    assert offered == ["wan1", "wan2"], offered
    for name in offered:
        out = _shell(shell_state, f"show appliance BR1-EC banners {name}")
        assert f"appliance/banners:BR1-EC:{name}" in out, out
    # And the names it withheld are exactly the ones the command refuses to
    # pair with this appliance.
    assert "elsewhere" not in offered


def test_completion_does_not_offer_an_appliance_kind_at_the_bare_position(
    shell_state: ShellState,
) -> None:
    """The bare position is Orchestrator scope, so an appliance-scoped noun has
    nothing to offer there — the command rejects that form anyway, and a
    completion the command then refuses is worse than no completion."""
    assert _completer(shell_state)._options(["show", SINGLETON_NOUN]) == []
    assert _completer(shell_state)._options(["show", SINGLETON_KIND]) == []
    out = _shell(shell_state, f"show {SINGLETON_NOUN}")
    assert "appliance-scoped" in out, out


def test_completion_degrades_to_nothing_when_the_orchestrator_is_unreachable(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#74: dynamic completion degrades safely when the resolver/API is down.

    A raised exception here reaches prompt_toolkit's redraw, not a try/except
    the operator can see — it takes the prompt down mid-session.
    """
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("orchestrator unreachable")

    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "list_refs", boom)
    completer = _completer(shell_state)
    assert completer._options(["show", "appliance", "BR1-EC", "banners"]) == []
    document = Document("show appliance BR1-EC banners ")
    assert list(completer.get_completions(document, CompleteEvent())) == []


def test_completion_survives_a_resource_that_cannot_address_its_instances(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate-ref defect is a loud error at the command, and silence at
    the prompt — completion is not the place to report it."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "list_refs", _DuplicateRefs.list_refs)
    assert _completer(shell_state)._options(["show", "appliance", "BR1-EC", "banners"]) == []


def test_completion_leaves_the_special_show_forms_alone(
    shell_state: ShellState,
) -> None:
    """Instance completion sits after the special forms, which occupy the same
    token position: `show run <TAB>` must still offer sections, not instances."""
    completer = _completer(shell_state)
    assert "appliance" in completer._options(["show", "run"])
    assert completer._options(["show", "transactions"]) == ["pending"]
    assert completer._options(["show", "flows"]) == ["summary"]
    assert completer._options(["show", "journal"]) == []
