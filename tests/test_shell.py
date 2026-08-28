"""Shell command dispatch: `show [appliance <name>] <kind> [<instance>]`
scope-mismatch validation and appliance-qualified completion (#48, #49)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from rich.console import Console

pytest.importorskip("pyecsdwan.mock.server")

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.candidate import CandidateStore
from pyecsdwan.cli.shell import ShellCompleter, ShellState, dispatch_operational
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.mock.server import MockState, run_in_thread
from pyecsdwan.registry import default_registry
from pyecsdwan.resolver import Resolver


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def state(state_home: Any, mock_server: tuple[str, MockState]) -> ShellState:
    base_url, mstate = mock_server
    mstate.reset()
    settings = config.Settings(orch_url=base_url, api_key="test-key", job_timeout=5.0)
    client = OrchClient(settings)
    ctx = Ctx(client=client, resolver=Resolver(client))
    return ShellState(
        ctx=ctx,
        registry=default_registry,
        settings=settings,
        console=Console(record=True),
        candidate=CandidateStore(settings.host),
    )


def _run(state: ShellState, line: str) -> str:
    dispatch_operational(line, state)
    return state.console.export_text()


# -- #48: orchestrator-scoped kind + spurious `appliance <name>` prefix -----


def test_show_appliance_alone_gives_usage_not_unknown_kind(state: ShellState) -> None:
    out = _run(state, "show appliance")
    assert "unknown resource kind" not in out
    assert "usage: show" in out


def test_show_appliance_name_alone_gives_usage(state: ShellState) -> None:
    out = _run(state, "show appliance S1-ecv-01")
    assert "usage: show" in out


def test_show_appliance_name_orchestrator_kind_rejected(state: ShellState) -> None:
    out = _run(state, "show appliance S1-ecv-01 bio")
    assert "orchestrator-scope" in out
    assert "(not present)" not in out


def test_show_appliance_name_orchestrator_kind_instance_rejected(state: ShellState) -> None:
    # The exact failure mode from #48: this used to silently drop the
    # appliance qualifier and print a misleading "(not present)".
    out = _run(state, "show appliance S1-ecv-01 bio DEFAULT")
    assert "orchestrator-scope" in out
    assert "(not present)" not in out


def test_show_orchestrator_kind_without_appliance_still_works(state: ShellState) -> None:
    # Regression check: the ordinary no-appliance form is untouched.
    out = _run(state, "show coverage")
    assert "bio-association" in out


# -- #49: tab completion for `show appliance <name> <kind>` -----------------


def test_completer_offers_appliance_names_after_show_appliance(
    state: ShellState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deterministic regardless of mock seed data: before the fix, "show" was
    # simply absent from the appliance-completion branch, so this returned []
    # no matter what the resolver had.
    monkeypatch.setattr(state.ctx.resolver, "appliance_names", lambda: ["S1-ecv-01"])
    completer = ShellCompleter(state)
    assert completer._options(["show", "appliance"]) == ["S1-ecv-01"]


def test_completer_offers_kinds_after_show_appliance_name(state: ShellState) -> None:
    """After an appliance name, completion offers appliance-scope nouns only.

    This used to assert `bio` was offered — but `bio` is *orchestrator*-scope,
    so the old completer was suggesting kinds that cannot be used in that
    position at all, alongside registry keys like `appliance/bgp` that repeat
    scope the operator has already given (#77).
    """
    completer = ShellCompleter(state)
    options = completer._options(["show", "appliance", "S1-ecv-01"])
    assert "bgp" in options and "banners" in options
    assert "bio" not in options, "orchestrator-scope kind offered in appliance position"
    assert not [o for o in options if "/" in o], f"registry keys leaked: {options}"


def test_completer_set_appliance_unaffected(state: ShellState) -> None:
    """The set/delete completion path resolves names exactly as `show` does.

    Previously this asserted the whole registry-key list; both surfaces now
    offer the same appliance-scope nouns, which is Principle IV's "one grammar
    across interfaces" made testable.
    """
    from pyecsdwan.contract import Scope

    completer = ShellCompleter(state)
    options = completer._options(["set", "appliance", "S1-ecv-01"])
    assert options == default_registry.cli_names(Scope.APPLIANCE)
    assert options == completer._options(["show", "appliance", "S1-ecv-01"])
    assert not [o for o in options if "/" in o], f"registry keys leaked: {options}"
