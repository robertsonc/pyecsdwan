"""GET is not idempotent on this API (#67).

The client retried every GET on the usual assumption. The vendored specs
disagree, in their own words: there are GETs summarised "Clear idle time",
"Create blueprint template", "Generate the Sys Dump file on the appliance",
"Logout of current HTTP session", and — the one that would hurt most —
"Delete specific/all segment BGP state". A dropped response to any of those,
replayed, does it twice.

Four properties, matching #67's acceptance criteria:

1. a raw/unknown GET is never automatically replayed;
2. known curated reads keep bounded retries;
3. a known mutating GET executes at most once, *even when the caller asks for
   retries* — the transport holds the veto, not the caller;
4. the decision is visible in the debug log and the audit journal, and carries
   no secrets.

Plus the one that keeps this working next year: the classification is
re-derived from the vendored specs here, so a future baseline that introduces
another read-shaped action fails rather than quietly joining the retryable set.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import structlog

from pyecsdwan import config, retry, specs
from pyecsdwan.client import OrchApiError, OrchClient

BASE = "https://orch.example.com/gms/rest"


@pytest.fixture
def settings() -> config.Settings:
    return config.Settings(orch_url="https://orch.example.com", api_key="test-key")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda seconds: None)


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Two tests here drive the CLI, which configures structlog for the whole
    process — and that configuration caches bound loggers, so
    `structlog.testing.capture_logs()` in a *later* test silently captures
    nothing. The log assertion below passed alone and failed in the module
    run until this existed."""
    yield
    structlog.reset_defaults()


# -- 1. a raw / unknown GET is never replayed ---------------------------------


@respx.mock
def test_a_bare_request_does_not_retry(settings: config.Settings) -> None:
    """`request()` defaults to NEVER. A caller that has not thought about it
    gets one attempt: a missing retry is an error the caller already handles,
    an unwanted one is a mutation applied twice."""
    route = respx.get(f"{BASE}/appliance").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json=[])]
    )
    with pytest.raises(OrchApiError):
        OrchClient(settings).request("GET", "/appliance")
    assert route.call_count == 1


@respx.mock
def test_a_bare_request_does_not_retry_a_connection_error(
    settings: config.Settings,
) -> None:
    """The 5xx path and the transport-error path are separate branches, and
    only the second one loses the response — which is precisely the case where
    replaying a mutation is undetectable."""
    route = respx.get(f"{BASE}/appliance").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(OrchApiError):
        OrchClient(settings).request("GET", "/appliance")
    assert route.call_count == 1


def test_the_raw_api_command_passes_never(
    state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67's headline: Tier-0 passthrough reaches all 1300-odd endpoints,
    including every mutating GET, and inherited the retry loop.

    **The path here is deliberately an ordinary read that is NOT on the
    denylist.** The first version of this test used `/idle/clear`, and a
    mutation sweep showed it passing with the command asking for BOUNDED —
    because the denylist veto caught it either way. So it proved the veto and
    said nothing about the call site. `/appliance` is a plain read that
    `client.get()` retries happily, so the only thing that can hold this to one
    attempt is `api` passing NEVER.

    That distinction is the whole point of the issue: the denylist can never
    be complete, which is why an operator's arbitrary path is not replayed
    whether or not anyone has classified it.
    """
    from typer.testing import CliRunner

    from pyecsdwan.cli.main import app

    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")

    with respx.mock:
        route = respx.get(f"{BASE}/appliance").mock(
            side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json=[])]
        )
        result = CliRunner().invoke(
            app, ["--orch-url", "https://orch.example.com", "api", "get", "/appliance"]
        )
    assert result.exit_code != 0, result.output
    assert route.call_count == 1


def test_the_raw_api_command_does_not_retry_through_the_proxy_either(
    state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`api --appliance` reaches the ECOS API, which carries the two worst
    read-shaped mutations. Same rule, different call site — and the proxy path
    was a separate branch, so it needed its own proof."""
    from typer.testing import CliRunner

    from pyecsdwan.cli.main import app

    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")

    with respx.mock:
        respx.get(f"{BASE}/appliance").mock(
            return_value=httpx.Response(200, json=[{"nePk": "3.NE", "hostName": "BR1-EC"}])
        )
        route = respx.get(f"{BASE}/appliance/rest").mock(
            side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
        )
        result = CliRunner().invoke(
            app,
            [
                "--orch-url", "https://orch.example.com",
                "api", "get", "bgp/state", "--appliance", "BR1-EC",
            ],
        )
    assert result.exit_code != 0, result.output
    assert route.call_count == 1


# -- 2. curated reads keep bounded retries ------------------------------------


@respx.mock
def test_a_curated_read_still_retries(settings: config.Settings) -> None:
    """`get()` is the curated seam and defaults to BOUNDED. Everything that
    reaches a fabric through a registered plugin comes through here, and those
    GETs are reviewed at promotion."""
    route = respx.get(f"{BASE}/appliance").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=[]),
        ]
    )
    assert OrchClient(settings).get("/appliance") == []
    assert route.call_count == 3


@respx.mock
def test_a_curated_read_can_opt_out(settings: config.Settings) -> None:
    route = respx.get(f"{BASE}/appliance").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json=[])]
    )
    with pytest.raises(OrchApiError):
        OrchClient(settings).get("/appliance", retry_policy=retry.Retry.NEVER)
    assert route.call_count == 1


@respx.mock
def test_a_write_never_retries_whatever_it_asks_for(settings: config.Settings) -> None:
    route = respx.post(f"{BASE}/gms/interfaceLabels").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
    )
    with pytest.raises(OrchApiError):
        OrchClient(settings).request(
            "POST", "/gms/interfaceLabels", json_body={}, retry_policy=retry.Retry.BOUNDED
        )
    assert route.call_count == 1


# -- 3. a mutating GET executes at most once, caller's wishes notwithstanding --


@respx.mock
def test_a_mutating_get_is_never_replayed_even_when_asked(
    settings: config.Settings,
) -> None:
    """The veto. A curated plugin cannot opt into replaying a mutation by
    accident, and neither can one written years from now by someone who has
    not read `retry.py`."""
    route = respx.get(f"{BASE}/idle/clear").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
    )
    with pytest.raises(OrchApiError):
        OrchClient(settings).get("/idle/clear", retry_policy=retry.Retry.BOUNDED)
    assert route.call_count == 1


@respx.mock
def test_the_proxy_classifies_on_the_ecos_path_not_the_transport_path(
    settings: config.Settings,
) -> None:
    """Every proxied call wears `/appliance/rest`. Classifying on that would
    make the whole appliance API one undifferentiated endpoint — and the two
    worst read-shaped mutations in the specs are appliance-scope and reachable
    only through the proxy."""
    route = respx.get(f"{BASE}/appliance/rest").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
    )
    with pytest.raises(OrchApiError):
        OrchClient(settings).appliance_request("GET", "3.NE", "bgp/vrfs/3/state")
    assert route.call_count == 1


@respx.mock
def test_an_ordinary_proxied_read_still_retries(settings: config.Settings) -> None:
    """The other side of the gate, so the veto above is not simply refusing
    every proxied read."""
    route = respx.get(f"{BASE}/appliance/rest").mock(
        side_effect=[httpx.Response(503, text="busy"), httpx.Response(200, json={})]
    )
    assert OrchClient(settings).appliance_request("GET", "3.NE", "bgp/state") == {}
    assert route.call_count == 2


def test_a_parameterized_mutating_path_is_matched_with_its_value() -> None:
    """The denylist is keyed on spec templates (`/bgp/vrfs/{vrfId}/state`) but
    a real call carries a value (`/bgp/vrfs/3/state`). `normalize_path`
    collapses `{vrfId}` and has nothing to collapse in `3`, so a plain dict
    lookup misses every parameterized entry — including the delete-BGP-state
    one, which is the single most dangerous row. The first smoke test of the
    module caught exactly that, so it is asserted here."""
    assert retry.is_mutating_get("appliance", "GET", "/bgp/vrfs/3/state")
    assert retry.is_mutating_get("appliance", "GET", "bgp/vrfs/999/state")
    # ...and it does not over-match: a different depth is a different endpoint.
    assert not retry.is_mutating_get("appliance", "GET", "/bgp/vrfs/3/state/extra")
    assert not retry.is_mutating_get("appliance", "GET", "/bgp/state")


def test_the_scope_is_part_of_the_identity() -> None:
    """`/multicast/enable` exists in both specs. A denylist that ignored scope
    would deny one because of the other."""
    assert retry.is_mutating_get("orchestrator", "GET", "/idle/clear")
    assert not retry.is_mutating_get("appliance", "GET", "/idle/clear")


# -- 4. the decision is visible, and carries nothing secret -------------------


@respx.mock
def test_a_vetoed_retry_is_logged_with_the_specs_own_words(
    settings: config.Settings,
) -> None:
    """The veto is the interesting event, so it is not silent: an operator
    asking "why did that not retry?" gets the spec's own sentence back."""
    from structlog.testing import capture_logs

    respx.get(f"{BASE}/idle/clear").mock(return_value=httpx.Response(200, json={}))
    with capture_logs() as logs:
        OrchClient(settings).get("/idle/clear", retry_policy=retry.Retry.BOUNDED)
    vetoes = [entry for entry in logs if entry.get("event") == "retry_vetoed"]
    assert vetoes, logs
    assert "Clear idle time" in vetoes[0]["reason"]
    assert vetoes[0]["path"] == "/idle/clear"


@respx.mock
def test_an_ordinary_read_logs_no_veto(settings: config.Settings) -> None:
    """Guards the guard: a log line emitted on every call would carry no
    information and would train a reader to skip it."""
    from structlog.testing import capture_logs

    respx.get(f"{BASE}/appliance").mock(return_value=httpx.Response(200, json=[]))
    with capture_logs() as logs:
        OrchClient(settings).get("/appliance")
    assert not [entry for entry in logs if entry.get("event") == "retry_vetoed"]


def test_the_journal_records_the_policy_and_the_reason(
    state_home: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Was this call sent once?" must be answerable from the audit trail
    without re-deriving the classification."""
    from typer.testing import CliRunner

    from pyecsdwan.cli.main import app

    monkeypatch.setenv("ECSDWAN_API_KEY", "test-key")
    with respx.mock:
        respx.get(f"{BASE}/idle/clear").mock(return_value=httpx.Response(200, json={}))
        result = CliRunner().invoke(
            app, ["--orch-url", "https://orch.example.com", "api", "get", "/idle/clear"]
        )
    assert result.exit_code == 0, result.output

    events = [
        json.loads(line)
        for path in Path(state_home).rglob("events.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    raw = [e for e in events if e.get("event") == "RAW_API"]
    assert raw, events
    assert raw[0]["retry_policy"] == retry.Retry.NEVER.value
    assert "Clear idle time" in raw[0]["retry_reason"]


def test_no_reason_string_can_carry_a_secret() -> None:
    """Reasons are written to the audit journal, so they must be fixed strings
    plus spec text — never a path parameter, a body, or a header."""
    reasons = [
        retry.REASON_MUTATING,
        retry.REASON_WRITE_METHOD,
        retry.REASON_RAW,
        retry.REASON_CALLER,
        retry.REASON_DEFAULT,
    ]
    for reason in reasons:
        assert reason == reason.strip() and reason
    _, why = retry.effective_policy("GET", "/appliance?apiKey=hunter2", retry.Retry.NEVER)
    assert "hunter2" not in why


# -- the classification is derived, and stays derived -------------------------


#: The spec's *own* description of a mutation: a summary whose first word is an
#: action verb. Deliberately lives here rather than in `retry.py` — this test
#: is the derivation, and `retry.py` holds the reviewed result. A regex shared
#: with the module under test could not catch the module being wrong.
_ACTION_VERB = re.compile(
    r"^(create|update|delete|remove|reset|clear|close|reboot|restart|start|stop|cancel"
    r"|set|save|push|install|upgrade|activate|deactivate|enable|disable|logout|login"
    r"|revoke|purge|flush|terminate|trigger|run|execute|send|assign|unassign|refresh"
    r"|rotate|sync|import|generate|initiate|apply|add|modify|change|configure|force"
    r"|rebuild|regenerate|reload|renew|restore|schedule|toggle|upload|download|export)\b",
    re.I,
)


def _action_summary_gets() -> dict[str, str]:
    found = {}
    for key, endpoint in specs.endpoint_index().items():
        if endpoint.method.upper() != "GET":
            continue
        summary = (endpoint.summary or "").strip()
        if _ACTION_VERB.match(summary):
            found[key] = summary
    return found


def test_the_derivation_finds_something() -> None:
    """Guards the guard. A regex that matched nothing, or a spec index that
    came back empty, would make every assertion below vacuously true — which
    is exactly how this class of test quietly stops working."""
    found = _action_summary_gets()
    assert len(found) >= 15, found
    assert any("Delete specific/all segment BGP state" in s for s in found.values())


def test_every_action_shaped_get_is_classified() -> None:
    """The drift gate #67 asks for: "future vendor releases can introduce more
    read-shaped actions".

    A new baseline lands its new endpoint in neither map and fails here, so
    someone has to classify it — with a written reason — rather than it
    silently joining the retryable set.
    """
    unclassified = {
        key: summary
        for key, summary in _action_summary_gets().items()
        if key not in retry.MUTATING_GETS and key not in retry.REVIEWED_SAFE
    }
    assert not unclassified, (
        "these GETs are described by the spec as actions but are classified in neither "
        "retry.MUTATING_GETS nor retry.REVIEWED_SAFE. Add each with the reason: "
        + json.dumps(unclassified, indent=2)
    )


def test_every_denied_endpoint_exists_in_the_specs() -> None:
    """The other direction: a denylist entry for a path the specs do not carry
    is a typo that protects nothing, and it would keep the table looking
    healthy."""
    universe = specs.endpoint_index()
    if not universe:  # pragma: no cover - a wheel with no baselines
        pytest.skip("no vendored specs")
    stray = sorted(
        key
        for key in (*retry.MUTATING_GETS, *retry.REVIEWED_SAFE)
        if key not in universe
    )
    assert not stray, stray


def test_every_entry_carries_evidence() -> None:
    """A classification without a reason is a preference. Each entry's value is
    the vendored summary or the sentence that resolved the ambiguity."""
    for table in (retry.MUTATING_GETS, retry.REVIEWED_SAFE):
        for key, why in table.items():
            assert why.strip(), key
            assert len(why) > 10, (key, why)


def test_the_two_tables_do_not_overlap() -> None:
    both = set(retry.MUTATING_GETS) & set(retry.REVIEWED_SAFE)
    assert not both, both
