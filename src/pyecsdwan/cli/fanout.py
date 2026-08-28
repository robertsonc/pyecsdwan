"""The fan-out cost gate (``grammar.md`` §6, Decision 7).

**The thing being guarded against is elapsed time, not danger.** A ``fabric``
command makes one call *per appliance*; on a large fabric that is a long wait,
and an operator who did not realise they had asked for it deserves to be told
before it starts rather than after. Nothing here is destructive — every
fan-out command in this CLI is read-only — so this is not a "are you sure"
prompt in the safety sense, and it must not read like one.

Two behaviours, chosen by whether anyone can answer:

* **Interactive** (a TTY to ask on) — prompt, naming the appliance count and
  how long it is expected to take. ``--yes`` skips it.
* **Non-interactive** (piped, scripted, CI) — a prompt cannot be answered and
  would hang the pipeline, which is the failure class #78 exists to remove. So
  it does not prompt: it warns on stderr with the same two figures and
  proceeds.

The count comes from the resolver cache, so the warning itself costs no API
call — a gate that spends a request to tell you a request is expensive would be
its own joke. The duration is count x observed per-call latency, rounded
coarsely and clearly hedged: a wrong-but-honest "around 2 minutes" is more
useful than silence, and precision here would be false.
"""

from __future__ import annotations

import sys

from rich.console import Console
from rich.text import Text

from pyecsdwan.contract import Ctx

#: Used when no call has been made yet, so there is nothing observed to scale
#: from. Deliberately a round number rather than a measured one: it is a
#: placeholder for "we do not know", and the phrasing hedges accordingly.
DEFAULT_LATENCY_MS = 300.0

#: Below this, the wait is not worth interrupting anyone over. A three-appliance
#: lab fabric answering in a second should not make the operator confirm.
QUIET_THRESHOLD_SECONDS = 10.0


class FanoutDeclined(Exception):
    """The operator answered no. Not an error — they were asked and said so."""


def estimate_seconds(ctx: Ctx, appliances: int, calls_each: int = 1) -> float:
    """How long ``appliances x calls_each`` calls are expected to take.

    Uses the latency this client has actually seen (`OrchClient
    .observed_latency_ms`), because a constant is wrong on every fabric and
    wrong by different amounts. Concurrency is *not* divided out: the reports
    bound their pools and the pool size is not visible from here, so this is an
    upper bound. Over-estimating a wait is the safe direction — the operator is
    deciding whether to start it.
    """
    latency = ctx.client.observed_latency_ms or DEFAULT_LATENCY_MS
    return appliances * calls_each * latency / 1000.0


def _half_up(value: float) -> int:
    """Round .5 up, not to even.

    `round()` is banker's rounding, so 2.5 minutes would render as "2" — an
    estimate that under-states the wait the operator is deciding whether to
    start. Over-estimating is the safe direction here.
    """
    return int(value + 0.5)


def describe_duration(seconds: float) -> str:
    """A coarse, hedged rendering. Never a decimal: this is an estimate, and a
    figure like "1.7 minutes" claims a precision the input does not have."""
    if seconds < 10:
        return "a few seconds"
    if seconds < 90:
        return f"around {_half_up(seconds / 10) * 10} seconds"
    minutes = _half_up(seconds / 60) if seconds < 600 else _half_up(seconds / 300) * 5
    return f"around {minutes} minute{'s' if minutes != 1 else ''}"


def _appliance_count(ctx: Ctx) -> int | None:
    """Fabric size from the resolver cache, or None if it cannot be had.

    None means "no basis for a warning", and a caller must then proceed
    silently rather than invent a number: the gate exists to inform the
    operator, and a made-up count misinforms them, which is worse than saying
    nothing. An unreachable Orchestrator is the command's problem to report,
    not this gate's to pre-empt.
    """
    try:
        return len(ctx.resolver.appliance_names())
    except Exception:  # noqa: BLE001 - resolver/cache/API offline: no warning, not a failure
        return None


def confirm(
    ctx: Ctx,
    *,
    console: Console,
    err_console: Console,
    assume_yes: bool = False,
    calls_each: int = 1,
    interactive: bool | None = None,
) -> None:
    """Gate a fabric-wide command. Raises :class:`FanoutDeclined` on "no".

    ``interactive`` defaults to whether stdin is a TTY — the thing that decides
    whether a prompt can be answered at all. It is a parameter so tests can
    exercise both branches without a pty, and because the two branches are the
    whole behaviour: getting the detection right but only testing one of them
    would leave the pipeline-hanging case unexercised, which is the case that
    matters.
    """
    if assume_yes:
        return
    count = _appliance_count(ctx)
    if count is None or count == 0:
        return
    seconds = estimate_seconds(ctx, count, calls_each)
    if seconds < QUIET_THRESHOLD_SECONDS:
        return

    calls = "1 call each" if calls_each == 1 else f"{calls_each} calls each"
    summary = (
        f"This queries {count} appliances ({calls}) and may take "
        f"{describe_duration(seconds)}."
    )
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        # Warn and proceed. A prompt nobody can answer is a hung pipeline, and
        # the operator who scripted this cannot be surprised by it mid-run —
        # they can read the warning in the log afterwards and add --yes to say
        # they meant it. Stderr, so piped output stays machine-parseable.
        err_console.print(Text(f"warning: {summary}", style="yellow"))
        return

    console.print(summary)
    answer = input("Continue? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise FanoutDeclined(summary)
