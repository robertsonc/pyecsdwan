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
