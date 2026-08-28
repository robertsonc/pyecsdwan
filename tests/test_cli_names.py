"""User-facing CLI nouns, separate from internal registry kinds (#77).

The operator should never have to type `appliance/nat-maps` after the command
has already established the appliance scope. `kind` stays the stable internal
identifier — candidate store, journal, API contracts — and the CLI noun is a
separate contract, so a nicer name never becomes a state migration.
"""

from __future__ import annotations

import pytest

import pyecsdwan.resources  # noqa: F401 - registers the built-in plugins
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
    assert reg.resolve_cli("zones", Scope.APPLIANCE).kind == "appliance/zones"
    assert reg.resolve_cli("zones", Scope.ORCHESTRATOR).kind == "zones"


def test_an_unscoped_collision_is_refused_with_both_candidates() -> None:
    with pytest.raises(AliasError) as excinfo:
        reg.resolve_cli("zones")
    message = str(excinfo.value)
    assert "appliance/zones" in message and "zones" in message


def test_the_registry_key_still_resolves_but_is_flagged_legacy() -> None:
    resolution = reg.resolve_cli("appliance/nat-maps", Scope.APPLIANCE)
    assert resolution.kind == "appliance/nat-maps"
    assert resolution.legacy is True
    assert reg.resolve_cli("nat-maps", Scope.APPLIANCE).legacy is False


def test_cli_name_round_trips_for_every_kind() -> None:
    for kind in reg.kinds():
        noun = reg.cli_name(kind)
        assert reg.resolve_cli(noun, reg.get(kind).scope).kind == kind


def test_non_curated_stubs_are_resolvable_but_not_offered() -> None:
    """Completing a name whose `normalize()` raises is offering a dead end."""
    stubs = [k for k in reg.kinds() if reg.get(k).tier < Tier.CURATED]
    assert stubs, "expected at least one Tier-1 generated stub"
    for kind in stubs:
        assert kind not in reg.cli_names(reg.get(kind).scope)
        assert reg.resolve_cli(kind, reg.get(kind).scope).kind == kind


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
    assert fresh.resolve_cli("zones", Scope.APPLIANCE).kind == "appliance/zones"
    assert fresh.resolve_cli("zones", Scope.ORCHESTRATOR).kind == "zones"


def test_an_explicit_alias_resolves_alongside_the_primary_name() -> None:
    fresh = Registry()
    fresh.register(_plugin("appliance/nat-maps", cli_aliases=("nat",)))
    assert fresh.resolve_cli("nat", Scope.APPLIANCE).kind == "appliance/nat-maps"
    assert fresh.resolve_cli("nat-maps", Scope.APPLIANCE).kind == "appliance/nat-maps"


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
