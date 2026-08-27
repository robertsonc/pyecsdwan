"""Bounded per-appliance fan-out for read-only reports (epic #54).

Every operational `show` command that reports across the fleet has the same
three obligations, and getting any of them wrong is worse than not having the
command:

* **Bounded.** The Orchestrator is a low-QPS control plane
  (``docs/research/expert-repo.md``). A report that opens one connection per
  appliance across a large fabric is a self-inflicted outage, so concurrency is
  capped and the cap is deliberately small.
* **Resilient.** One unreachable appliance degrades to a row marked
  ``unreachable``. It never fails the report — a version report that dies
  because a single branch box is down is useless precisely when it is needed.
* **Ordered.** Results come back in the order the callers passed their items,
  regardless of which finished first, so a rendered table is stable between
  runs.

Threads rather than asyncio: ``OrchClient`` wraps a synchronous
``httpx.Client``, which is safe to share across threads, and the rest of the
codebase is synchronous. An async rewrite would touch every resource plugin to
buy nothing a thread pool does not already give a fleet-sized fan-out.

**``timeout`` is an overall deadline, and it abandons rather than cancels.**
Python cannot interrupt a thread blocked in a socket read. Two consequences,
both deliberate:

* The pool is shut down with ``wait=False``, *not* through the context-manager
  form. ``ThreadPoolExecutor.__exit__`` joins every worker, so the ``with``
  form would block on exactly the straggler the deadline exists to escape —
  the timeout would be decorative.
* An abandoned worker keeps running until httpx's own ``connect_timeout`` /
  ``read_timeout`` (``Settings``) fires, and ``concurrent.futures`` joins its
  threads at interpreter exit. Those settings are therefore the real ceiling;
  this deadline only stops the *report* waiting. Anything past it is reported
  as ``unreachable`` with the reason spelled out, so an operator is not misled
  into reading it as "the appliance refused the connection".
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import time
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

import structlog

log = structlog.get_logger(__name__)

#: The Orchestrator is a control plane, not a data plane. Four in flight is
#: enough to make a fleet-wide report feel immediate without becoming load.
DEFAULT_CONCURRENCY = 4

T = TypeVar("T")
R = TypeVar("R")


@dataclasses.dataclass(frozen=True)
class Outcome(Generic[T, R]):
    """One item's result, whether it succeeded or not.

    Never raises on the caller's behalf: a report renders a failed row rather
    than losing the whole table, so failure is data here, not control flow.
    """

    item: T
    value: R | None = None
    #: Human-readable reason, empty when the call succeeded.
    error: str = ""
    #: True when *call* returned. Distinguishes "returned None" from "never
    #: ran" — some reads legitimately answer None, and a report must not drop
    #: that row as if the appliance were unreachable.
    done: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def unreachable(self) -> bool:
        """True when this item has no result to render."""
        return bool(self.error)


def fan_out(
    items: Sequence[T],
    call: Callable[[T], R],
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> list[Outcome[T, R]]:
    """Run *call* over *items* with a concurrency cap, isolating failures.

    Returns one :class:`Outcome` per item, in the order given. An exception
    from *call* — or a wait that exceeds *timeout* — becomes an ``unreachable``
    outcome carrying the reason, never a raised exception.

    ``timeout``, when given, is a deadline for the fan-out as a whole, not a
    per-call budget: a report that must render in 30s must render in 30s
    whether one appliance is slow or ten are.

    ``concurrency`` is clamped to at least 1 and never exceeds ``len(items)``,
    so a single-appliance report does not spin up a pool it cannot use.
    """
    if not items:
        return []
    workers = max(1, min(concurrency, len(items)))
    deadline = None if timeout is None else time.monotonic() + timeout
    outcomes: list[Outcome[T, R]] = []

    # Not the `with` form: its __exit__ joins every worker, which would block
    # on the very straggler the deadline exists to escape.
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="ecsdwan-report"
    )
    try:
        futures = [pool.submit(call, item) for item in items]
        for item, future in zip(items, futures, strict=True):
            outcomes.append(_collect(item, future, deadline, timeout))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return outcomes


def _collect(
    item: T,
    future: concurrent.futures.Future[R],
    deadline: float | None,
    timeout: float | None,
) -> Outcome[T, R]:
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        return Outcome(item=item, value=future.result(timeout=remaining), done=True)
    except concurrent.futures.TimeoutError:
        future.cancel()
        log.debug("report_fanout_timeout", item=str(item), timeout=timeout)
        return Outcome(
            item=item,
            error=f"no response within the {timeout}s report deadline",
        )
    except Exception as exc:  # noqa: BLE001 - one bad appliance never fails the report
        log.debug("report_fanout_error", item=str(item), error=str(exc))
        return Outcome(item=item, error=f"{type(exc).__name__}: {exc}")


def unreachable(outcomes: Sequence[Outcome[T, R]]) -> list[Outcome[T, R]]:
    """The subset that produced no result — for a report's footer."""
    return [o for o in outcomes if o.unreachable]


def values(outcomes: Sequence[Outcome[T, R]]) -> list[tuple[T, R | None]]:
    """(item, value) for the outcomes that succeeded, order preserved.

    Keyed on ``done``, not on the value being non-None: an endpoint that
    legitimately answers ``null`` still produced a result.
    """
    return [(o.item, o.value) for o in outcomes if o.done]
