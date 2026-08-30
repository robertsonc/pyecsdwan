"""Template ownership fails closed (#20).

The old join answered "nothing owns this" — the one state that permits a
direct write — in at least five situations where it had established nothing at
all: a kind missing from the section map, an unreadable association, an
unreadable selection, a response of the wrong shape, and a section name nobody
has ever seen on a real Orchestrator. A direct write to a template-owned
section is silently reverted by the next template push, so each of those was a
quiet route to losing an operator's change.

Every one of them is exercised here against the wire, not against a stub of
`owning_group`: the point is that the *join* fails closed, and a test that
mocked the function under test would only prove the mock does.

The tests are ordered as the join is: the mapping, the association read, the
selection read, then the verification status of the name being compared.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from pyecsdwan import config, ownership
from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx, Owned, Ownership
from pyecsdwan.resolver import Resolver

BASE = "https://orch.example.com/gms/rest"
NE_PK = "3.NE"

ASSOCIATION = f"{BASE}/template/applianceAssociation"
SELECTION = f"{BASE}/template/templateSelection"
GROUPS = f"{BASE}/template/templateGroups"

#: A kind whose section names are live-confirmed, and one whose names are
#: spelled after the ECOS path and never observed. The difference between them
#: is the whole point of `Sections.verified`.
VERIFIED_KIND = "appliance/routes"
GUESSED_KIND = "appliance/vrrp"


@pytest.fixture
def ctx(tmp_path: Path) -> Iterator[Ctx]:
    """A resolver with a per-test cache directory.

    `Resolver` persists to `~/.pyecsdwan/cache/<origin>.json` and holds entries
    for its TTL, so without this every test in the file shares one cached
    template vocabulary — the first to run decides what the rest see — and the
    run writes into the developer's real state directory. Both were latent
    until ownership started reading a cached section.
    """
    settings = config.Settings(orch_url="https://orch.example.com", api_key="k")
    client = OrchClient(settings)
    yield Ctx(client=client, resolver=Resolver(client, cache_dir=tmp_path))


def _associated(*groups: str) -> None:
    respx.get(ASSOCIATION).mock(
        return_value=httpx.Response(200, json={"templateIds": list(groups)})
    )


def _selects(*sections: str) -> None:
    respx.get(SELECTION).mock(return_value=httpx.Response(200, json=list(sections)))


def _vocabulary(*names: str) -> None:
    """What the Orchestrator says its template sections are *called*.

    Read only on a non-match, where a name the fabric has never heard of and a
    section nobody selected produce the same silence. These tests are about
    the second case, so the vocabulary here contains every name under test —
    a kind whose names are absent is `test_a_name_the_fabric_never_heard_of_
    is_unknown`.
    """
    respx.get(GROUPS).mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "g", "templates": [{"name": n} for n in names]}],
        )
    )


def _text(verdict) -> str:
    """Every human-facing string on an Ownership, whatever field carries it."""
    import dataclasses
    return " ".join(str(v) for v in dataclasses.asdict(verdict).values() if v)


# -- the table itself --------------------------------------------------------


def test_the_two_kinds_this_module_contrasts_really_do_differ() -> None:
    """Guards every "unverified" assertion below. If both kinds were verified
    (or neither were), the UNKNOWN-vs-UNOWNED pairs would be testing the same
    branch twice and passing for the wrong reason."""
    assert ownership.SECTION_MAP[VERIFIED_KIND].verified
    assert not ownership.SECTION_MAP[GUESSED_KIND].verified


def test_every_verified_name_comes_from_the_live_probe() -> None:
    """`verified` means one thing: a live `GET /template/templateSelection`
    returned this name. Nothing may claim it by resembling an ECOS path, which
    is exactly how three entries came to be commented "CONFIRMED real"."""
    for kind, entry in ownership.SECTION_MAP.items():
        if not entry.verified:
            continue
        assert entry.names, kind
        assert any(n in ownership.LIVE_CONFIRMED_SECTIONS for n in entry.names), (
            f"{kind} claims verification but names none of the live-probed sections"
        )


def test_the_legacy_view_still_matches_the_table() -> None:
    """`KIND_TO_TEMPLATE_SECTIONS` is derived, and deliberately drops the
    verification status — so nothing in the decision may read it."""
    assert ownership.KIND_TO_TEMPLATE_SECTIONS == {
        k: v.names for k, v in ownership.SECTION_MAP.items()
    }


# -- 1. a kind with no mapping ----------------------------------------------


@respx.mock
def test_an_unmapped_kind_is_unknown_not_unowned(ctx: Ctx) -> None:
    """A generated stub reaches this every time: no entry, so nothing was
    compared. Answering "unowned" here is answering a question nobody asked."""
    owns = ownership.owning_group(ctx, "appliance/nothing-maps-this", NE_PK)
    assert owns.state is Owned.UNKNOWN
    assert owns.blocks_write
    assert "no template-section mapping" in owns.reason


# -- 2. the association read -------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500, 502])
def test_an_unreadable_association_is_unknown(ctx: Ctx, status: int) -> None:
    """403 is the one that matters most: a credential without template-read
    permission used to make every appliance look unowned. 404 is included on
    purpose — the baseline types the empty case as a 200 carrying
    ``{"templateIds": []}``, so a 404 is a different fact, and the previous
    code turned it into "no groups"."""
    respx.get(ASSOCIATION).mock(return_value=httpx.Response(status, json={"e": "no"}))
    owns = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert owns.state is Owned.UNKNOWN
    assert owns.blocks_write


@respx.mock
@pytest.mark.parametrize("body", [[], "nope", {"templateIds": "Branch-Std"}], ids=str)
def test_an_association_of_the_wrong_shape_is_unknown(ctx: Ctx, body: Any) -> None:
    """A 200 whose body is not the documented object — or whose templateIds is
    a bare string rather than a list — means the read did not do what we think
    it did. `[str(g) for g in "Branch-Std"]` would have quietly produced ten
    single-character group names."""
    respx.get(ASSOCIATION).mock(return_value=httpx.Response(200, json=body))
    assert ownership.owning_group(ctx, VERIFIED_KIND, NE_PK).state is Owned.UNKNOWN


@respx.mock
def test_no_associated_group_is_a_real_unowned(ctx: Ctx) -> None:
    """The one clean negative, and the reason unverified section names do not
    make every appliance UNKNOWN: ownership needs an associated group, and an
    empty list says there is none whatever the section is called."""
    _associated()
    owns = ownership.owning_group(ctx, GUESSED_KIND, NE_PK)
    assert owns.state is Owned.UNOWNED
    assert not owns.blocks_write


# -- 3. the selection read ---------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [403, 404, 500])
def test_an_unreadable_selection_is_unknown(ctx: Ctx, status: int) -> None:
    """This one used to be a bare `continue`, so a group whose selection could
    not be read contributed nothing and the loop fell through to "unowned"."""
    _associated("Branch-Std")
    respx.get(SELECTION).mock(return_value=httpx.Response(status, json={"e": "no"}))
    owns = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert owns.state is Owned.UNKNOWN
    assert "Branch-Std" in owns.reason


@respx.mock
def test_a_selection_of_the_wrong_shape_is_unknown(ctx: Ctx) -> None:
    respx.get(ASSOCIATION).mock(
        return_value=httpx.Response(200, json={"templateIds": ["Branch-Std"]})
    )
    respx.get(SELECTION).mock(return_value=httpx.Response(200, json={"sections": ["routes"]}))
    assert ownership.owning_group(ctx, VERIFIED_KIND, NE_PK).state is Owned.UNKNOWN


@respx.mock
def test_a_match_wins_over_an_unreadable_sibling(ctx: Ctx) -> None:
    """OWNED short-circuits: a matched section name is a matched section name,
    however uncertain the rest of the join is. Without this the answer would
    degrade to UNKNOWN and an operator would be told to break glass over a
    section we positively know is owned.

    Two groups are associated; the *first* one read fails and the second
    matches, so the ordering is exercised rather than assumed.
    """
    _associated("Broken", "Branch-Std")
    respx.get(SELECTION, params={"templateGroup": "Broken"}).mock(
        return_value=httpx.Response(500, json={"e": "no"})
    )
    respx.get(SELECTION, params={"templateGroup": "Branch-Std"}).mock(
        return_value=httpx.Response(200, json=["routes"])
    )
    owns = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Branch-Std"


# -- 4. the verification status of the name being compared -------------------


@respx.mock
def test_a_non_match_on_a_verified_name_is_unowned(ctx: Ctx) -> None:
    """"routes" came back from a live template group, so a group that does not
    select it genuinely does not own static routes."""
    _associated("Branch-Std")
    _selects("dns", "snmp")
    _vocabulary("subnets", "routes", "bgp", "dns", "snmp")
    owns = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert owns.state is Owned.UNOWNED
    assert not owns.blocks_write


@respx.mock
def test_a_non_match_on_a_guessed_name_is_unknown(ctx: Ctx) -> None:
    """The subtle one. "vrrp" is spelled after the ECOS path and has never
    been seen in a *selected* list, so a group that does not select it may
    still own VRRP under a name we never compared against. "Not selected" and
    "wrong name" are indistinguishable from here.

    The vocabulary deliberately contains "vrrp": the fabric knows the name, so
    this is the unverified-non-match branch and not the stale-name one. (On
    9.7 that is exactly true — `vrrp` is a real section that no group on the
    lab fabric happened to select.)"""
    _associated("Branch-Std")
    _selects("dns", "snmp")
    _vocabulary("subnets", "routes", "vrrp", "dns", "snmp")
    owns = ownership.owning_group(ctx, GUESSED_KIND, NE_PK)
    assert owns.state is Owned.UNKNOWN
    assert owns.blocks_write
    assert "unverified" in owns.reason


@respx.mock
def test_a_match_on_a_guessed_name_is_still_owned(ctx: Ctx) -> None:
    """Asymmetry on purpose: a guess that matches was a correct guess. Only the
    negative answer depends on the name being right."""
    _associated("Branch-Std")
    _selects("vrrp")
    owns = ownership.owning_group(ctx, GUESSED_KIND, NE_PK)
    assert owns.state is Owned.OWNED
    assert owns.owner == "template-group Branch-Std"


@respx.mock
def test_section_names_match_case_insensitively(ctx: Ctx) -> None:
    _associated("Branch-Std")
    _selects("ROUTES")
    assert ownership.owning_group(ctx, VERIFIED_KIND, NE_PK).state is Owned.OWNED


# -- the type's own contract -------------------------------------------------


def test_unknown_blocks_exactly_as_owned_does() -> None:
    """`blocks_write` is the only thing callers may branch on. A guard that
    reads `state is OWNED` has reintroduced the whole bug, so the property has
    to be true for both."""
    assert Ownership.owned("template-group X").blocks_write
    assert Ownership.unknown("who knows").blocks_write
    assert not Ownership.unowned("checked").blocks_write


def test_the_label_names_the_state_an_operator_must_act_on() -> None:
    assert Ownership.owned("template-group X").label == "managed-by: template-group X"
    assert Ownership.unknown("403").label == "ownership-unknown: 403"
    # Nothing to say, and nothing printed: an empty label is what lets a
    # reader take "no ownership line" as "nothing owns this".
    assert Ownership.unowned("checked").label == ""


# -- the surfaces an operator actually reads ---------------------------------
#
# `compare` prints it, `show configuration` prints it, and `--json` carries it
# (#20). The states are not symmetrical on purpose: UNOWNED prints nothing,
# which is what lets a reader take a bare diff as "nothing owns this".


def test_the_plan_renderer_shows_both_blocking_states() -> None:
    """`compare` is where an operator sees a change before making it, so an
    UNKNOWN that printed nothing there would be a refusal arriving as a
    surprise at commit time."""
    import io

    from rich.console import Console

    from pyecsdwan import txn
    from pyecsdwan.cli import render
    from pyecsdwan.contract import Diff, DiffEntry, DiffOp, Ref, Resource

    def _plan(own: Ownership) -> txn.Plan:
        ref = Ref("appliance/bgp", "global", appliance="BR1-EC")
        return txn.Plan(
            items=[
                txn.PlanItem(
                    ref=ref,
                    resource=Resource(),
                    delete=False,
                    current_raw=None,
                    current=None,
                    desired={"a": 1},
                    diff=Diff(
                        ref=ref,
                        entries=[DiffEntry(DiffOp.ADD, ("a",), None, 1)],
                        desired={"a": 1},
                        current=None,
                    ),
                    ownership=own,
                )
            ]
        )

    def _render(own: Ownership) -> str:
        buf = io.StringIO()
        render.render_plan(Console(file=buf, width=200, no_color=True), _plan(own))
        return buf.getvalue()

    assert "managed-by: template-group X" in _render(Ownership.owned("template-group X"))
    assert "ownership-unknown: 403 on selection" in _render(
        Ownership.unknown("403 on selection")
    )
    # And nothing at all for the state that needs no action.
    unowned = _render(Ownership.unowned("no group associated"))
    assert "managed-by" not in unowned and "ownership-unknown" not in unowned


# -- a name the fabric never heard of (#20, live-found on 9.7) ---------------


@respx.mock
def test_a_name_the_fabric_never_heard_of_is_unknown(ctx: Ctx) -> None:
    """The fail-open this closes, found live.

    `appliance/optimization-map` looked for a section called
    `optimizationMaps`. The real name on Orchestrator 9.7 is `optmap`, that
    section is selected, and it demonstrably pushes to the appliance — its four
    entries match the live config byte-for-byte by comment.

    A name nothing can match produces exactly the same non-match as a resource
    no template governs, and the old code answered both with "unowned". One of
    those answers permits a write the template silently reverts, which is the
    operator surprise ownership exists to prevent.
    """
    _associated("Branch-Std")
    _selects("optmap", "qosMaps")
    _vocabulary("optmap", "qosMaps", "bgp")

    # `appliance/zones` looks for a section called `zones`. The live 9.7
    # vocabulary has 46 names and none of them is `zones`, so the mapping can
    # never match — the same silence a resource no template governs produces.
    verdict = ownership.owning_group(ctx, "appliance/zones", NE_PK)
    assert verdict.state is Owned.UNKNOWN
    assert "no section by any of those names" in _text(verdict)


@respx.mock
def test_an_unreadable_vocabulary_does_not_invent_staleness(ctx: Ctx) -> None:
    """The vocabulary is an *extra* signal, never a prerequisite.

    If the read fails the answer must be whatever it was before this check
    existed — turning one unreadable endpoint into UNKNOWN for every kind at
    once is fail-closed in the letter and useless in practice.
    """
    _associated("Branch-Std")
    _selects("dns", "snmp")
    respx.get(GROUPS).mock(return_value=httpx.Response(500, text="boom"))

    verdict = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert verdict.state is Owned.UNOWNED


@respx.mock
def test_an_empty_vocabulary_does_not_invent_staleness(ctx: Ctx) -> None:
    """A read that succeeds and returns nothing is not evidence either. It is
    far more likely a shape this parse did not understand — the mock reports
    groups without their template lists, for one."""
    _associated("Branch-Std")
    _selects("dns", "snmp")
    respx.get(GROUPS).mock(return_value=httpx.Response(200, json=[]))

    verdict = ownership.owning_group(ctx, VERIFIED_KIND, NE_PK)
    assert verdict.state is Owned.UNOWNED


@respx.mock
def test_a_partly_stale_mapping_can_still_match(ctx: Ctx) -> None:
    """Only an *entirely* unknown mapping is refused. A kind naming two
    sections where one is real is still usable, and `inbound-shaper` is exactly
    that: `shaper` is real and selected, `inboundShapers` is not a section at
    all."""
    _associated("Branch-Std")
    _selects("shaper")
    _vocabulary("shaper", "qosMaps")

    verdict = ownership.owning_group(ctx, "appliance/inbound-shaper", NE_PK)
    assert verdict.state is Owned.OWNED
