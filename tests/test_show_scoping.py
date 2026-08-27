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
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver

#: Every appliance in the mock fabric holds one `banners` object named `global`.
SINGLETON_KIND = "appliance/banners"


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
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND}")
    assert "name required" not in out, out
    assert "global, global" not in out, out
    # It resolved and rendered the object.
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


def test_the_explicit_instance_form_still_works(shell_state: ShellState) -> None:
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND} global")
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


def test_discovery_does_not_offer_instances_from_other_appliances(
    shell_state: ShellState,
) -> None:
    """Whatever it resolves, it must belong to the appliance that was named."""
    out = _shell(shell_state, f"show appliance BR2-EC {SINGLETON_KIND}")
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
    out = _shell(shell_state, f"show appliance S1-ecv-01 {SINGLETON_KIND}")
    assert "unknown appliance" in out and "S1-ecv-01" in out, out
    assert "no instances" not in out, out


def test_an_appliance_scoped_kind_without_an_appliance_says_what_to_type(
    shell_state: ShellState,
) -> None:
    """This message must not be buried behind fabric-wide enumeration."""
    out = _shell(shell_state, f"show {SINGLETON_KIND}")
    assert "appliance-scoped" in out, out
    assert f"show appliance <name> {SINGLETON_KIND}" in out, out


def test_every_terminal_state_produces_visible_output(shell_state: ShellState) -> None:
    """R8: a renderer may never reduce a result to zero visible characters.

    The operator must always be able to tell the command finished, whatever
    the outcome — that is #78's actual complaint, independent of which branch
    produced it.
    """
    lines = [
        f"show appliance BR1-EC {SINGLETON_KIND}",          # ok
        f"show appliance BR1-EC {SINGLETON_KIND} global",   # ok, explicit
        f"show appliance S1-ecv-01 {SINGLETON_KIND}",       # unknown appliance
        f"show {SINGLETON_KIND}",                           # scope error
        "show appliance BR1-EC no-such-kind",               # unknown kind
        "show appliance BR1-EC",                            # incomplete
    ]
    for line in lines:
        out = _shell(shell_state, line).strip()
        assert out, f"zero visible output for: {line}"


def test_the_prompt_survives_every_failure(shell_state: ShellState) -> None:
    """Each failure returns control; none raises out of the dispatcher."""
    for line in (
        f"show appliance S1-ecv-01 {SINGLETON_KIND}",
        "show appliance BR1-EC no-such-kind",
        "show appliance",
        f"show {SINGLETON_KIND}",
    ):
        dispatch_operational(line, shell_state)  # must not raise
    # Still usable afterwards.
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND}")
    assert f"{SINGLETON_KIND}:BR1-EC:global" in out, out


# -- empty is an answer, and a different one from absent --------------------


def test_an_empty_configuration_is_reported_explicitly(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`{}` rendered as YAML is a bare `{}` — indistinguishable in a scrollback
    from the command having done nothing. It gets a sentence instead."""
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: {})
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND} global")
    assert "empty" in out.lower(), out
    assert out.strip(), out


def test_absent_and_empty_are_different_answers(
    shell_state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    resource = default_registry.get(SINGLETON_KIND)
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: None)
    absent = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND} global")
    monkeypatch.setattr(type(resource), "normalize", lambda self, raw: {})
    empty = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND} global")
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
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND}")
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
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND}")
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
    out = _shell(shell_state, f"show appliance BR1-EC {SINGLETON_KIND}")
    assert "name required" in out and "BR1-EC" in out, out
    assert "wan1" in out and "wan2" in out, out
    assert "elsewhere" not in out, out
