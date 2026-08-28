"""The outcome taxonomy and its exit codes (``grammar.md`` §5).

Derived from gNMI's error taxonomy (D-GNMI-2), extended with the distributed
cases it does not cover because gNMI addresses one target and pyecsdwan fans
out.

**Principle II governs this table.** ``empty`` and ``stale`` exit 0 because
they are *answers*; every other non-``ok`` state is distinguishable and none
may be rendered as success. The distinctions that matter most are the ones a
careless implementation collapses:

* ``empty`` is not ``not_found`` — "the appliance holds no configuration for
  this kind" and "there is no such object" are different facts, and an
  operator acts differently on each.
* ``unsupported`` is not ``error`` — "this API has no endpoint for that" is a
  statement about the product, not about the appliance, and reporting it as a
  failure sends someone to debug a healthy device.
* ``partial`` is not ``ok`` — a fan-out that reached nine of ten targets has
  not answered the question that was asked.

The exit codes are a published contract: scripts branch on them. They live
here, once, and `tests/test_outcomes.py` parses `grammar.md` §5 and asserts
this table matches it — so the spec and the code cannot drift apart silently,
which is the failure mode a hand-copied table has.
"""

from __future__ import annotations

import enum
from typing import Any


class Outcome(enum.Enum):
    """One command's terminal state.

    The value is the JSON ``status`` string; :attr:`exit_code` is the process
    exit status. Both are part of the CLI's contract with scripts.
    """

    OK = "ok"
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    DENIED = "denied"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    PARTIAL = "partial"
    STALE = "stale"
    ERROR = "error"

    @property
    def exit_code(self) -> int:
        return _EXIT_CODES[self]

    @property
    def is_success(self) -> bool:
        """Whether this counts as the command having answered.

        Exit 0 is the definition, not a separate judgement — anything else
        would let the human rendering and the script contract disagree about
        the same result.
        """
        return self.exit_code == 0


#: Outcome -> process exit status (`grammar.md` §5). `timeout` shares 7 with
#: `unreachable` deliberately: from a script's point of view both mean "the
#: target did not answer within the budget", and splitting them would imply a
#: distinction callers cannot act on differently.
_EXIT_CODES: dict[Outcome, int] = {
    Outcome.OK: 0,
    Outcome.EMPTY: 0,
    Outcome.STALE: 0,
    Outcome.ERROR: 1,
    Outcome.INVALID: 2,
    Outcome.NOT_FOUND: 4,
    Outcome.UNSUPPORTED: 5,
    Outcome.DENIED: 6,
    Outcome.UNREACHABLE: 7,
    Outcome.TIMEOUT: 7,
    Outcome.PARTIAL: 8,
}


class CommandOutcome(Exception):
    """A terminal state a command wants to report and stop on.

    An exception because the state is usually decided deep in a fetch or a
    normalize, and threading a result type up through every frame would mean
    every intermediate caller has to remember to propagate it — which is how a
    terminal state ends up silently dropped, the defect #78 was filed for.

    ``detail`` is the operator-facing sentence. It must say what was looked
    for and, where there is one, what to do instead; "unsupported" alone sends
    someone to guess.
    """

    def __init__(self, outcome: Outcome, detail: str, *, remedy: str = "") -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail
        self.remedy = remedy


def classify(exc: BaseException) -> Outcome:
    """Which outcome a raised exception represents (`grammar.md` §5, R7).

    One table, applied at both dispatch boundaries, so the same failure exits
    with the same code whichever surface produced it. Before this every error
    was exit 2 in the scriptable CLI and exit 0 in the shell, which made
    "permission denied", "appliance unreachable" and "you typed it wrong"
    indistinguishable to a script — #78's complaint, at the level of the
    process contract rather than the rendering.

    The distinctions that carry real cost:

    * **denied is not unreachable.** A 403 means the credential lacks the
      right; retrying, or checking the network, wastes the operator's time.
    * **timeout is not unreachable** in the JSON status, even though they
      share an exit code: "it did not answer in time" and "it was not there"
      send an operator to different places, and only one of them is worth
      raising the budget for.
    * **unsupported is not error.** A Tier-1 stub refusing to normalize is
      this tool declining to guess, not the appliance failing.
    """
    import httpx

    from pyecsdwan.client import OrchApiError
    from pyecsdwan.contract import NotCurated
    from pyecsdwan.resolver import ResolveError

    if isinstance(exc, CommandOutcome):
        return exc.outcome
    if isinstance(exc, NotCurated):
        return Outcome.UNSUPPORTED
    if isinstance(exc, ResolveError):
        # The name resolved against nothing on this Orchestrator. The path was
        # well-formed, so this is not `invalid`.
        return Outcome.NOT_FOUND
    if isinstance(exc, OrchApiError):
        return _from_api_error(exc)
    if isinstance(exc, httpx.TimeoutException):
        return Outcome.TIMEOUT
    if isinstance(exc, httpx.HTTPError):
        return Outcome.UNREACHABLE
    if isinstance(exc, ValueError):
        # Usage errors: the parser rejected what was typed.
        return Outcome.INVALID
    return Outcome.ERROR


def _from_api_error(exc: Any) -> Outcome:
    """Map an `OrchApiError` by status, or by what caused it when there is none."""
    import httpx

    status = exc.status_code
    if status is None:
        # No response at all. `raise ... from last_error` preserves which kind
        # of transport failure it was, and that is the only thing separating
        # "did not answer in time" from "was not reachable".
        cause = exc.__cause__
        if isinstance(cause, httpx.TimeoutException):
            return Outcome.TIMEOUT
        return Outcome.UNREACHABLE
    if status in (401, 403):
        return Outcome.DENIED
    if status == 404:
        return Outcome.NOT_FOUND
    if status in (408, 504):
        return Outcome.TIMEOUT
    if status == 501:
        return Outcome.UNSUPPORTED
    # Everything else — 4xx the CLI should not have sent, 5xx the Orchestrator
    # could not answer — is a response this tool did not expect. `error` says
    # that without claiming to know which side is at fault.
    return Outcome.ERROR
