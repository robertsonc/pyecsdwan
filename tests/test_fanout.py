"""Bounded per-appliance fan-out (epic #54 foundation).

The three properties every operational report depends on: it is bounded, one
bad appliance never kills the table, and the deadline actually bounds the
report rather than being decorative.
"""

from __future__ import annotations

import threading
import time

import pytest

from pyecsdwan.reports import fanout


def test_results_come_back_in_input_order_not_completion_order() -> None:
    """A rendered table must be stable between runs, so ordering follows the
    caller's list, not whichever appliance answered first."""

    def slow_for_the_first(item: int) -> int:
        time.sleep(0.05 if item == 0 else 0.0)
        return item * 10

    outcomes = fanout.fan_out([0, 1, 2, 3], slow_for_the_first, concurrency=4)
    assert [o.item for o in outcomes] == [0, 1, 2, 3]
    assert [o.value for o in outcomes] == [0, 10, 20, 30]
    assert all(o.ok for o in outcomes)


def test_one_failing_item_never_fails_the_report() -> None:
    """A version report that dies because one branch box is down is useless
    precisely when it is needed."""

    def fails_on_two(item: int) -> str:
        if item == 2:
            raise ConnectionError("connection refused")
        return f"v{item}"

    outcomes = fanout.fan_out([1, 2, 3], fails_on_two)
    assert [o.ok for o in outcomes] == [True, False, True]
    bad = outcomes[1]
    assert bad.unreachable
    assert "ConnectionError" in bad.error
    assert "connection refused" in bad.error
    assert [o.item for o in fanout.unreachable(outcomes)] == [2]
    assert fanout.values(outcomes) == [(1, "v1"), (3, "v3")]


def test_concurrency_is_capped() -> None:
    """The Orchestrator is a low-QPS control plane; a report must not open one
    connection per appliance across a large fabric."""
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def observe(item: int) -> int:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.02)
        with lock:
            in_flight -= 1
        return item

    fanout.fan_out(list(range(12)), observe, concurrency=3)
    assert peak <= 3


def test_deadline_bounds_the_whole_report_not_each_call() -> None:
    """The bug this test exists for: the obvious `with ThreadPoolExecutor(...)`
    form joins every worker on exit, so a hung appliance stalls the report even
    though every individual wait timed out. The deadline has to actually
    return."""
    release = threading.Event()

    def hangs_until_released(item: int) -> int:
        if item == 1:
            release.wait(30)
        return item

    started = time.monotonic()
    try:
        outcomes = fanout.fan_out([0, 1, 2], hangs_until_released, concurrency=3, timeout=0.2)
        elapsed = time.monotonic() - started
        # Generous bound: the point is that it returns in well under the 30s
        # the straggler would otherwise impose.
        assert elapsed < 5.0, f"fan_out waited {elapsed:.1f}s on a hung item"
        assert outcomes[1].unreachable
        assert "deadline" in outcomes[1].error
        # The others still produced their rows.
        assert outcomes[0].ok and outcomes[2].ok
    finally:
        release.set()


def test_a_call_returning_none_is_a_result_not_an_unreachable_row() -> None:
    """Some reads legitimately answer null. Keying "succeeded" off a non-None
    value would silently drop those rows as if the appliance were down."""
    outcomes = fanout.fan_out(["a"], lambda _item: None)
    assert outcomes[0].ok
    assert outcomes[0].done
    assert not outcomes[0].unreachable
    assert fanout.values(outcomes) == [("a", None)]


def test_empty_input_does_no_work() -> None:
    calls: list[object] = []
    assert fanout.fan_out([], lambda item: calls.append(item)) == []
    assert calls == []


@pytest.mark.parametrize("requested", [0, -5, 1, 99])
def test_worker_count_is_clamped_to_something_usable(requested: int) -> None:
    """A single-appliance report should not ask for a 99-thread pool, and a
    concurrency of 0 must not deadlock."""
    outcomes = fanout.fan_out([1, 2], lambda item: item, concurrency=requested)
    assert [o.value for o in outcomes] == [1, 2]
