"""User-facing CLI nouns, separate from internal registry kinds (#77).

The operator should never have to type `appliance/nat-maps` after the command
has already established the appliance scope. `kind` stays the stable internal
identifier — candidate store, journal, API contracts — and the CLI noun is a
separate contract, so a nicer name never becomes a state migration.
"""

from __future__ import annotations

import pytest
import structlog
from typer.testing import CliRunner

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
from pyecsdwan import config
from pyecsdwan.cli import main as cli_main
from pyecsdwan.contract import RESERVED_CLI_WORDS, Resource, Scope, Tier, default_cli_name
from pyecsdwan.registry import AliasError, Registry, UnknownKind
from pyecsdwan.registry import default_registry as reg


def _plugin(kind: str, scope: Scope = Scope.APPLIANCE, **kw: object) -> Resource:
    r = Resource()
    r.kind = kind
    r.scope = scope
    for key, value in kw.items():
        setattr(r, key, value)
    return r


# -- derivation --------------------------------------------------------------


def test_the_appliance_scope_prefix_is_dropped() -> None:
    assert default_cli_name("appliance/nat-maps") == "nat-maps"
    assert default_cli_name("appliance/zones") == "zones"


def test_a_kind_without_a_scope_prefix_is_unchanged() -> None:
    assert default_cli_name("bio") == "bio"
    assert default_cli_name("template-group") == "template-group"


def test_the_generated_prefix_is_deliberately_kept() -> None:
    """`generated/` marks *tier*, not scope, and beneath it is a raw operation
    id. Stripping it would promote that id to the "friendly" name, which is not
    an improvement — a Tier-1 stub has no curated noun to offer yet."""
    kind = "generated/appliance_post_virtualif_vti_by_vti_name"
    assert default_cli_name(kind) == kind


# -- the live registry -------------------------------------------------------


def test_no_registry_key_leaks_into_the_offerable_nouns() -> None:
    for scope in (Scope.APPLIANCE, Scope.ORCHESTRATOR):
        leaked = [n for n in reg.cli_names(scope) if "/" in n]
        assert not leaked, f"{scope.value}: {leaked}"


def test_the_zones_collision_resolves_by_scope() -> None:
    """`zones` names two genuinely different objects — appliance firewall zones
    and the Orchestrator's zone definitions. This is why the namespace is per
    scope rather than flat."""
    assert reg.resolve_cli("zones", Scope.APPLIANCE) == "appliance/zones"
    assert reg.resolve_cli("zones", Scope.ORCHESTRATOR) == "zones"


def test_an_unscoped_collision_is_refused_with_both_candidates() -> None:
    with pytest.raises(AliasError) as excinfo:
        reg.resolve_cli("zones")
    message = str(excinfo.value)
    assert "appliance/zones" in message and "zones" in message


def test_a_registry_key_is_not_a_command_token() -> None:
    """`appliance/nat-maps` shipped as a warned alias in #82; #74 withdrew it.

    A key the operator can type is a key that reaches scripts, and then it is a
    compatibility surface whether or not it was ever documented. `kind` is
    keyed on by the candidate store, the journal and the API contracts — which
    is precisely why it must not also be a thing operators type.
    """
    with pytest.raises(UnknownKind):
        reg.resolve_cli("appliance/nat-maps", Scope.APPLIANCE)
    assert reg.resolve_cli("nat-maps", Scope.APPLIANCE) == "appliance/nat-maps"


def test_cli_name_round_trips_for_every_kind() -> None:
    for kind in reg.kinds():
        noun = reg.cli_name(kind)
        assert reg.resolve_cli(noun, reg.get(kind).scope) == kind


def test_non_curated_stubs_are_resolvable_but_not_offered() -> None:
    """Completing a name whose `normalize()` raises is offering a dead end."""
    stubs = [k for k in reg.kinds() if reg.get(k).tier < Tier.CURATED]
    assert stubs, "expected at least one Tier-1 generated stub"
    for kind in stubs:
        assert kind not in reg.cli_names(reg.get(kind).scope)
        assert reg.resolve_cli(kind, reg.get(kind).scope) == kind


def test_an_unknown_noun_lists_the_valid_ones_for_that_scope() -> None:
    with pytest.raises(UnknownKind) as excinfo:
        reg.resolve_cli("no-such-thing", Scope.APPLIANCE)
    message = str(excinfo.value)
    assert "bgp" in message
    assert "bio" not in message, "orchestrator nouns offered for an appliance-scope miss"


# -- validation happens at registration, not at operator runtime ------------


def test_a_reserved_word_is_refused_at_registration() -> None:
    for word in sorted(RESERVED_CLI_WORDS):
        fresh = Registry()
        with pytest.raises(AliasError, match="reserve"):
            fresh.register(_plugin(f"appliance/{word}"))


def test_a_collision_within_one_scope_is_refused_at_registration() -> None:
    fresh = Registry()
    fresh.register(_plugin("appliance/thing"))
    with pytest.raises(AliasError) as excinfo:
        fresh.register(_plugin("appliance/other", cli_name="thing"))
    assert "thing" in str(excinfo.value)


def test_the_same_name_in_two_scopes_is_allowed() -> None:
    fresh = Registry()
    fresh.register(_plugin("appliance/zones", scope=Scope.APPLIANCE))
    fresh.register(_plugin("zones", scope=Scope.ORCHESTRATOR))
    assert fresh.resolve_cli("zones", Scope.APPLIANCE) == "appliance/zones"
    assert fresh.resolve_cli("zones", Scope.ORCHESTRATOR) == "zones"


def test_an_explicit_alias_resolves_alongside_the_primary_name() -> None:
    fresh = Registry()
    fresh.register(_plugin("appliance/nat-maps", cli_aliases=("nat",)))
    assert fresh.resolve_cli("nat", Scope.APPLIANCE) == "appliance/nat-maps"
    assert fresh.resolve_cli("nat-maps", Scope.APPLIANCE) == "appliance/nat-maps"


def test_an_alias_colliding_with_another_primary_is_refused() -> None:
    fresh = Registry()
    fresh.register(_plugin("appliance/bgp"))
    with pytest.raises(AliasError):
        fresh.register(_plugin("appliance/other", cli_aliases=("bgp",)))


def test_internal_kinds_are_untouched_by_any_of_this() -> None:
    """Renaming the CLI surface must not become a state migration — the
    candidate store, journal and API contracts key off `kind`."""
    assert "appliance/nat-maps" in reg.kinds()
    assert reg.get("appliance/nat-maps").kind == "appliance/nat-maps"


# -- the same noun in the scriptable CLI (#74, Principle IV) -----------------
#
# The shell learned nouns and the scriptable CLI did not, so `banners` worked
# at the prompt while `ec-cli plugin promote banners` answered "unknown
# resource kind" — and then listed the registry keys, which is the leak #77 is
# about. One grammar across interfaces means the resolution rule lives in one
# place and both surfaces call it.

_runner = CliRunner()


@pytest.fixture
def _reset_structlog_after() -> object:
    yield
    # The app callback binds structlog to CliRunner's capture stream, which is
    # closed on exit; leaving it bound kills the next test that logs.
    structlog.reset_defaults()


@pytest.fixture
def cli(state_home: object, monkeypatch: pytest.MonkeyPatch, _reset_structlog_after: object):
    """Invoke the scriptable CLI against an Orchestrator that is never reached.

    `set` and `delete` only stage into the candidate store, so the resolution
    these tests are about happens before any network call would.
    """
    monkeypatch.setenv(config.ENV_API_KEY, "test-key")

    def _invoke(*args: str):
        return _runner.invoke(cli_main.app, ["--orch-url", "https://nowhere.invalid", *args])

    return _invoke


def test_the_scriptable_cli_takes_the_same_noun_as_the_shell(cli) -> None:
    result = cli("set", "banners", "global", "motd", "hi", "--appliance", "BR1-EC")
    assert result.exit_code == 0, result.output
    # The noun is a CLI contract; the ref keeps the internal kind, so a nicer
    # name never becomes a candidate-store migration.
    assert "appliance%2Fbanners:BR1-EC:global" in result.output, result.output


def test_the_scriptable_cli_rejects_a_registry_key_exactly_as_the_shell_does(cli) -> None:
    """Removed means removed (compatibility.md rule 1): the ordinary
    unknown-token error, not a special deprecation path."""
    result = cli("set", "appliance/banners", "global", "motd", "hi", "--appliance", "BR1-EC")
    assert result.exit_code == 2, result.output
    assert "unknown resource kind" in result.output, result.output
    assert "still works" not in result.output, result.output


def test_an_unknown_noun_never_answers_with_registry_keys(cli) -> None:
    """The headline of #77, in the error that was leaking them."""
    result = cli("set", "not-a-kind", "global", "motd", "hi")
    assert result.exit_code == 2
    known = result.output.split("known kinds:", 1)[1]
    assert "appliance/" not in known, known
    assert "banners" in known, known


def test_the_scope_error_names_the_noun_not_the_token(cli) -> None:
    result = cli("set", "banners", "global", "motd", "hi")
    assert result.exit_code == 2
    assert "banners is appliance-scoped" in result.output, result.output
    # Asserting the noun is present proves nothing on its own —
    # "appliance/banners is appliance-scoped" contains that substring too.
    # The key's absence is the claim.
    assert "appliance/banners" not in result.output, result.output


def test_a_scope_collision_resolves_by_command_shape_in_both_surfaces(cli) -> None:
    """`zones` names two different objects; --appliance is what picks."""
    orch = cli("set", "zones", "z", "x", "1")
    assert orch.exit_code == 0, orch.output
    assert "zones:z" in orch.output and "appliance%2Fzones" not in orch.output, orch.output
    appl = cli("set", "zones", "z", "x", "1", "--appliance", "BR1-EC")
    assert appl.exit_code == 0, appl.output
    assert "appliance%2Fzones:BR1-EC:z" in appl.output, appl.output


def test_delete_and_load_resolve_the_noun_too(cli, tmp_path) -> None:
    """One resolver, so this is not four independent behaviours — asserted
    because "resolution lives in one place" is the claim being made."""
    assert cli("delete", "banners", "global", "--appliance", "BR1-EC").exit_code == 0
    doc = tmp_path / "banners.yaml"
    doc.write_text("motd: hi\nissue: ''\n", encoding="utf-8")
    result = cli("load", "banners", "global", str(doc), "--appliance", "BR1-EC", "--merge")
    assert result.exit_code == 0, result.output
    assert "appliance%2Fbanners:BR1-EC:global" in result.output, result.output


def test_the_shell_and_the_scriptable_cli_resolve_a_token_identically() -> None:
    """Both call `Registry.resolve_cli` with the scope the command implies, so
    a token can never mean one thing at the prompt and another in a script."""
    for token, appliance, expected in (
        ("banners", "BR1-EC", "appliance/banners"),
        ("zones", "BR1-EC", "appliance/zones"),
        ("zones", None, "zones"),
        # An appliance-scoped noun with no appliance still resolves, so the
        # command can answer "that is appliance-scope" rather than the much
        # less useful "unknown kind".
        ("banners", None, "appliance/banners"),
        # The withdrawn registry key resolves in neither scope.
        ("appliance/banners", "BR1-EC", None),
    ):
        scope = Scope.APPLIANCE if appliance is not None else Scope.ORCHESTRATOR
        resolved: str | None
        try:
            resolved = reg.resolve_cli(token, scope)
        except UnknownKind:
            try:
                resolved = reg.resolve_cli(token, Scope.APPLIANCE)
            except UnknownKind:
                resolved = None
        assert resolved == expected, (token, appliance, resolved)


def test_the_unknown_token_error_spans_both_scopes(cli) -> None:
    """Scope is a flag here, not a position, so both scopes are reachable
    without moving the token — listing only one would hide the noun the
    operator meant behind a flag they have not typed yet. (The shell lists the
    position's nouns instead, because there position *is* scope.)"""
    result = cli("set", "not-a-kind", "global", "motd", "hi")
    known = result.output.split("known kinds:", 1)[1]
    assert "banners" in known, known  # appliance scope
    assert "interface-labels" in known, known  # orchestrator scope


def test_replace_mode_load_resolves_the_noun_once(cli, tmp_path) -> None:
    """Replace mode looks the resource up a second time to warn about omitted
    sections; it reuses the kind already resolved into the ref rather than
    re-resolving the operator's token."""
    doc = tmp_path / "banners.yaml"
    doc.write_text("motd: hi\n", encoding="utf-8")
    result = cli("load", "banners", "global", str(doc), "--appliance", "BR1-EC")
    assert result.exit_code == 0, result.output
    # rich wraps the warning, so match a fragment that cannot straddle a break.
    assert "section(s) issue" in result.output, result.output
    assert "appliance/banners" not in result.output, result.output
