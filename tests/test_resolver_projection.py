"""The resolver cache stores only what it consumes (epic #9 hardening).

`GET /appliance` on a real Orchestrator is far wider than the seven fields
anything here reads — serial numbers, addresses, software and license detail —
and all of it used to land verbatim in `~/.pyecsdwan/cache/<host>.json`, a
plaintext file that outlives the process. Nothing read it. The cheapest way to
not leak a field is to not store it.

The risk in a projection is the opposite one: dropping a field something still
needs, and getting a blank cell instead of an error. So the projection is
loud — reading a dropped key raises `ProjectedAway` naming the constant to add
it to — and the tests below check the drop *and* the alarm.

One field is here only because the empirical sweep missed it. Instrumenting
every appliance record and running the whole suite reported six fields read;
`id` was not among them, because every consumer writes `record.get("nePk") or
record.get("id")` and the bundled mock always sends `nePk`, so the `or` never
evaluated its right-hand side. Dropping `id` would have broken exactly the
fabrics that need it and no test at all.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pyecsdwan import config, resolver
from pyecsdwan.client import OrchClient
from pyecsdwan.resolver import (
    APPLIANCE_FIELDS,
    ApplianceRecord,
    ProjectedAway,
    Resolver,
)

pytest.importorskip("pyecsdwan.mock.server")

from pyecsdwan.mock.server import MockState, run_in_thread

#: What a real inventory row carries beyond what we keep. Names taken from the
#: `/appliance` shape the vendored SDK documents; the point is not the exact
#: list but that a wide row narrows.
NOISY_ROW: dict[str, Any] = {
    "nePk": "9.NE",
    "id": "9.NE",
    "hostName": "BR9-EC",
    "site": "Branch 9",
    "model": "EC-XS",
    "state": 1,
    "networkRole": 0,
    "serialNumber": "CN12345678",
    "IP": "10.9.0.1",
    "softwareVersion": "9.3.4.0_98765",
    "applianceId": 9,
    "uptime": 123456,
    "licenseType": "EC-V",
    "contact": "noc@example.com",
}


@pytest.fixture(scope="module")
def mock_server() -> Iterator[tuple[str, MockState]]:
    base_url, state, shutdown = run_in_thread()
    yield base_url, state
    shutdown()


@pytest.fixture
def resolved(
    state_home: Any, mock_server: tuple[str, MockState], tmp_path: Path
) -> Iterator[tuple[Resolver, Path]]:
    base_url, state = mock_server
    state.reset()
    state.appliances.append(dict(NOISY_ROW))
    cache_dir = tmp_path / "cache"
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    res = Resolver(OrchClient(settings), cache_dir=cache_dir)
    yield res, cache_dir
    state.reset()


# -- what reaches the disk ---------------------------------------------------


def test_the_cache_file_holds_only_the_projected_fields(
    resolved: tuple[Resolver, Path],
) -> None:
    """Asserted against the bytes on disk, not against the return value: the
    file is the thing that outlives the process and the thing an attacker
    reads."""
    res, cache_dir = resolved
    res.appliances()

    written = json.loads(next(cache_dir.glob("*.json")).read_text(encoding="utf-8"))
    rows = written["appliances"]["value"]
    assert rows, "expected the inventory to have been cached"
    for row in rows:
        assert set(row) <= APPLIANCE_FIELDS, sorted(set(row) - APPLIANCE_FIELDS)

    # And the noisy row really was noisy — otherwise the assertion above holds
    # because there was nothing to drop.
    dropped = set(NOISY_ROW) - APPLIANCE_FIELDS
    assert dropped, "fixture no longer carries anything worth dropping"
    serialized = json.dumps(written)
    for field in sorted(dropped):
        assert field not in serialized, f"{field} reached the cache file"
    assert "CN12345678" not in serialized  # the value, not just the key


def test_the_cache_is_not_world_readable(resolved: tuple[Resolver, Path]) -> None:
    """It still holds hostnames, sites and models — an inventory of the
    network. 0600 on the file, 0700 on the directory."""
    res, cache_dir = resolved
    res.appliances()
    path = next(cache_dir.glob("*.json"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cache_dir).st_mode) == 0o700


def test_the_projection_survives_a_reload(resolved: tuple[Resolver, Path]) -> None:
    """A second process reads the file, not the API. If the projection lived
    only on the outbound path, the first process would see full records and
    every later one would see narrow ones — the worst kind of bug to reproduce.
    """
    res, cache_dir = resolved
    res.appliances()

    settings = config.Settings(orch_url=res.client.settings.orch_url, api_key="test-key")
    second = Resolver(OrchClient(settings), cache_dir=cache_dir)
    rows = second.appliances()
    assert rows
    for row in rows:
        assert set(row) <= APPLIANCE_FIELDS


# -- the alarm ---------------------------------------------------------------


def test_reading_a_dropped_field_raises_rather_than_answering_none() -> None:
    """The whole reason a projection is safe to make. `None` here would render
    as an empty cell in `show appliances` and nothing would say why."""
    record = ApplianceRecord({"hostName": "BR9-EC", "nePk": "9.NE"})
    with pytest.raises(ProjectedAway) as caught:
        record.get("serialNumber")
    message = str(caught.value)
    assert "serialNumber" in message
    # Actionable: it names where to change the decision.
    assert "APPLIANCE_FIELDS" in message

    # Every access path, not just `.get` — `in` returning False would be the
    # same silent wrong answer wearing a different shape.
    with pytest.raises(ProjectedAway):
        record["serialNumber"]
    with pytest.raises(ProjectedAway):
        "serialNumber" in record  # noqa: B015 - the access is the assertion


def test_the_resolver_hands_out_records_that_carry_the_alarm(
    resolved: tuple[Resolver, Path],
) -> None:
    """The projection and the alarm are two separate things, and the mutation
    sweep found nothing testing the second: returning plain dicts from
    `appliances()` left every other test green while restoring the silent
    `None` the alarm exists to remove."""
    res, _cache_dir = resolved
    rows = res.appliances()
    assert rows
    assert all(isinstance(row, ApplianceRecord) for row in rows)
    with pytest.raises(ProjectedAway):
        rows[0].get("serialNumber")


def test_a_projected_but_absent_field_is_just_absent() -> None:
    """The distinction the type exists for: "the Orchestrator did not send
    this" is data and must not raise. Only "we chose not to keep it" does."""
    record = ApplianceRecord({"hostName": "BR9-EC", "nePk": "9.NE"})
    assert record.get("site") is None
    assert record.get("model", "unknown") == "unknown"
    assert "site" not in record


def test_the_projection_actually_drops_something() -> None:
    """Guards the guards. If `APPLIANCE_FIELDS` ever grew to cover everything,
    every assertion above would pass while the cache leaked exactly as before.
    """
    assert resolver.project_appliance(dict(NOISY_ROW)) != NOISY_ROW
    assert set(resolver.project_appliance(dict(NOISY_ROW))) < set(NOISY_ROW)


# -- the fields that must stay -----------------------------------------------


def test_the_id_fallback_still_resolves(
    state_home: Any, mock_server: tuple[str, MockState], tmp_path: Path
) -> None:
    """`id` is in the projection only because consumers write `nePk or id`, a
    branch the bundled mock never takes. This is the test the empirical sweep
    could not be."""
    base_url, state = mock_server
    state.reset()
    state.appliances.append({"id": "9.NE", "hostName": "BR9-EC"})  # no nePk
    try:
        settings = config.Settings(orch_url=base_url, api_key="test-key")
        res = Resolver(OrchClient(settings), cache_dir=tmp_path / "cache")
        assert res.ne_pk_for("BR9-EC") == "9.NE"
        assert res.appliance_name_for("9.NE") == "BR9-EC"
    finally:
        state.reset()


def test_overlays_are_deliberately_not_projected(
    resolved: tuple[Resolver, Path],
) -> None:
    """`overlays()` is not an index of names: the endpoint returns each
    overlay's whole *configuration*, and `resources/interface_labels.py` walks
    it in full to find which labels are in use. There is no consumed-field
    subset, so projecting it would break a real feature — recorded here so the
    exemption is a decision rather than an oversight."""
    assert "overlays" in resolver.UNPROJECTED_SECTIONS
    res, _cache_dir = resolved
    overlays = res.overlays()
    assert overlays
    # Plain dicts, and wider than {id, name}.
    assert not isinstance(overlays[0], ApplianceRecord)
    assert set(overlays[0]) - {"id", "name"}


# -- which fabric the cache came from (#63) ----------------------------------


def test_the_cache_records_the_origin_it_was_built_against(
    resolved: tuple[Resolver, Path],
) -> None:
    res, cache_dir = resolved
    res.appliances()
    written = json.loads(next(cache_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert written[Resolver.ORIGIN_KEY] == res.client.settings.origin


def test_a_cache_from_another_origin_is_discarded_not_used(
    state_home: Any, mock_server: tuple[str, MockState], tmp_path: Path
) -> None:
    """The file name carries a digest of the origin, so this only fires for a
    cache moved, restored from a backup, or written before #63. It still has
    to fire: a stale name -> nePk mapping is what the next write is aimed at,
    so using another fabric's cache means writing to another fabric's
    appliance."""
    base_url, state = mock_server
    state.reset()
    state.appliances.append(dict(NOISY_ROW))
    cache_dir = tmp_path / "cache"
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    res = Resolver(OrchClient(settings), cache_dir=cache_dir)
    res.appliances()
    cache_file = next(cache_dir.glob("*.json"))

    poisoned = json.loads(cache_file.read_text(encoding="utf-8"))
    poisoned[Resolver.ORIGIN_KEY] = "https://some-other-orchestrator.example.com"
    poisoned["appliances"]["value"] = [
        {"nePk": "666.NE", "id": "666.NE", "hostName": "BR9-EC"}
    ]
    cache_file.write_text(json.dumps(poisoned), encoding="utf-8")

    fresh = Resolver(OrchClient(settings), cache_dir=cache_dir)
    # Discarded and refetched, so the name resolves against *this* fabric.
    assert fresh.ne_pk_for("BR9-EC") == "9.NE"


def test_a_cache_written_before_origins_were_recorded_is_discarded(
    state_home: Any, mock_server: tuple[str, MockState], tmp_path: Path
) -> None:
    """No marker at all is a mismatch, not a pass: it is exactly the case
    where nothing on disk says which fabric the mapping came from."""
    base_url, state = mock_server
    state.reset()
    state.appliances.append(dict(NOISY_ROW))
    cache_dir = tmp_path / "cache"
    settings = config.Settings(orch_url=base_url, api_key="test-key")
    res = Resolver(OrchClient(settings), cache_dir=cache_dir)
    res.appliances()
    cache_file = next(cache_dir.glob("*.json"))

    legacy = json.loads(cache_file.read_text(encoding="utf-8"))
    del legacy[Resolver.ORIGIN_KEY]
    legacy["appliances"]["value"] = [
        {"nePk": "666.NE", "id": "666.NE", "hostName": "BR9-EC"}
    ]
    cache_file.write_text(json.dumps(legacy), encoding="utf-8")

    assert Resolver(OrchClient(settings), cache_dir=cache_dir).ne_pk_for("BR9-EC") == "9.NE"
