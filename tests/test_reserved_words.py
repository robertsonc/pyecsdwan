"""The grammar's reserved words and the runtime's are one list (#71 R12).

`grammar.md` §2 names the tokens `show configuration <token>` needs for itself,
and `contract.RESERVED_CLI_WORDS` is what actually refuses a kind alias. Two
copies of one rule drift — that is #68's whole finding, where the roadmap
claimed a tier the code did not implement and no test read either — so the
spec's list is parsed and compared rather than trusted.

The direction that matters is the spec growing and the code not following: a
word reserved on paper but not in `RESERVED_CLI_WORDS` reserves nothing, and
the collision it was meant to prevent lands on an operator instead of on this
test. Decision 9 reserved `orchestrator`/`orchestrators` (#121) ahead of the
feature that uses them, which is exactly the case where the two can separate:
nothing in the running code needs them yet.
"""

from __future__ import annotations

import re
from pathlib import Path

from pyecsdwan.contract import RESERVED_CLI_WORDS

SPEC = Path(__file__).resolve().parents[1] / "specs/001-cli-command-taxonomy/grammar.md"


def _spec_words() -> set[str]:
    """The words in §2's reserved-words fenced block."""
    section = SPEC.read_text(encoding="utf-8").split("### Reserved words", 1)
    assert len(section) == 2, "grammar.md has no '### Reserved words' section"
    body = section[1].split("\n### ", 1)[0]
    fenced = body.split("```")
    assert len(fenced) >= 3, "the reserved-words section has no fenced block"
    # Split on the separator *and* the newline: the block runs to two lines,
    # and joining them would make "configuration orchestrator" one token.
    return {w.strip() for w in re.split(r"[·\n]", fenced[1]) if w.strip()}


def test_the_spec_block_actually_parsed() -> None:
    """Guards the guard: an empty parse would make the comparison vacuous."""
    words = _spec_words()
    assert len(words) >= 5, words
    assert "configuration" in words


def test_the_spec_and_the_runtime_reserve_the_same_words() -> None:
    assert _spec_words() == set(RESERVED_CLI_WORDS)


def test_decision_9_reserved_the_selector_noun() -> None:
    """Named rather than left to the set comparison: these two are reserved
    for a feature that does not exist yet (#121), so nothing else in the tree
    would notice if they quietly went missing."""
    assert {"orchestrator", "orchestrators"} <= set(RESERVED_CLI_WORDS)


def test_the_selector_noun_is_not_the_scope_noun() -> None:
    """Decision 9's reason for existing. `fabric` is a scope noun meaning
    'every appliance, fanned out', so reusing it for target selection would
    put two senses of one word in adjacent positions of one command."""
    assert "fabric" in RESERVED_CLI_WORDS
    assert "orchestrator" != "fabric"
    scope_section = SPEC.read_text(encoding="utf-8").split("## 3. Scope nouns", 1)[1]
    scope_section = scope_section.split("\n## ", 1)[0]
    assert "not a scope" in scope_section.lower(), (
        "grammar.md §3 must say the selector is not a scope noun; that "
        "distinction is the whole of Decision 9"
    )
