"""One product, described the same way everywhere (#68).

This repository began as a fork of the vendored `pyedgeconnect` SDK and
inherited its packaging: a flake8 config with `max-line-length = 79` (this
project uses ruff at 100), a `requirements.txt` pinning `requests~=2.25.1`
(pyproject pins `~=2.32`), a Read the Docs config building
`project = "pyedgeconnect"`, and a CONTRIBUTING.md telling contributors to use
black and PyCharm.

None of it was wired to anything — ruff and mypy exclude `docs/`, CI never read
`requirements.txt` — which is exactly why it survived: inert files are invisible
to every check. What they were not invisible to is a person opening the
repository and reading, in `docs/source/index.rst`, that this "is a python
wrapper for leveraging the API for Aruba Orchestrator".

The other half of this file matters more. The upstream SDK, its `examples/`,
and the vendored OpenAPI baselines are **kept on purpose** — several research
notes cite them as primary-source evidence for endpoint behaviour. A cleanup
that swept them out with the packaging would break those citations, and the
first draft of this one nearly did. So the citations are checked too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

#: Upstream packaging, with why each one is wrong for this project. Removed in
#: #68; asserted absent so a merge or a well-meaning restore has to argue with
#: a named reason rather than a silent diff.
WITHDRAWN: dict[str, str] = {
    ".flake8": "flake8 config at line-length 79; this project lints with ruff at 100",
    "requirements.txt": "pinned requests~=2.25.1, contradicting pyproject's ~=2.32",
    ".readthedocs.yaml": "builds docs/source, which is the upstream SDK's Sphinx project",
    "docs/source": 'Sphinx docs for project = "pyedgeconnect", (c) 2022 HPE',
    "docs/Makefile": "Sphinx wrapper for the removed docs/source",
    "docs/make.bat": "Sphinx wrapper for the removed docs/source",
}

#: Kept on purpose: the endpoint reference the plugins are built from. Each is
#: cited by a research note, so deleting it would orphan the evidence.
PRIMARY_SOURCES: dict[str, str] = {
    "pyedgeconnect": "the vendored upstream SDK — field names come from its docstrings",
    "examples/upload_security_policy/upload_security_policy.py": (
        "cited by docs/research/appliance-jobs.md as the save-after-proxy-write evidence"
    ),
    "src/pyecsdwan/_specs": "the OpenAPI baselines show coverage and retry.py read",
    "docs/pyedgeconnect-README.md": "linked from README.md as the SDK reference",
}


@pytest.mark.parametrize("path,why", sorted(WITHDRAWN.items()), ids=lambda v: str(v)[:40])
def test_upstream_packaging_stays_withdrawn(path: str, why: str) -> None:
    assert not (REPO / path).exists(), f"{path} is back: {why}"


@pytest.mark.parametrize("path,why", sorted(PRIMARY_SOURCES.items()), ids=lambda v: str(v)[:40])
def test_the_cited_primary_sources_stay(path: str, why: str) -> None:
    """The guard on the guard above.

    "Clean up the upstream stuff" reads like it covers `examples/` and the
    vendored SDK too. It does not: they are the evidence base. This test is
    what stops the next cleanup — human or otherwise — from taking them.
    """
    assert (REPO / path).exists(), f"{path} is gone, but {why}"


def test_the_research_citations_resolve() -> None:
    """Not just that the files exist, but that what cites them still points at
    something real — the citation is the reason to keep them."""
    cited = REPO / "examples" / "upload_security_policy" / "upload_security_policy.py"
    assert cited.exists()
    for note in ("appliance-jobs.md", "appliance-config.md", "templates-overlays-security.md"):
        text = (REPO / "docs" / "research" / note).read_text(encoding="utf-8")
        assert "examples/upload_security_policy" in text, note


# -- the product is described the same way everywhere ------------------------


#: The upstream one-liner, and the phrasing it came from. This is the sentence
#: `docs/source/index.rst` opened with, and (at the time of writing) still the
#: GitHub repository description — which no tooling in this session can set,
#: so it is listed in the #68 hand-off rather than asserted here.
_WRAPPER = re.compile(r"python wrapper", re.I)


def test_the_readme_does_not_call_this_a_python_wrapper() -> None:
    """It is a transactional CLI over the Orchestrator API. "Wrapper" is what
    the thing it vendors is, and describing both the same way is how a reader
    ends up thinking this project is that project."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    opening = readme.split("## ", 1)[0]
    assert not _WRAPPER.search(opening), opening
    assert "transactional CLI" in opening


def test_the_roadmap_agrees_with_the_readme() -> None:
    roadmap = (REPO / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    assert not _WRAPPER.search(roadmap.split("## ", 1)[0])


def test_the_wrapper_phrasing_is_still_findable_where_it_belongs() -> None:
    """Guards the guard: the regex has to match something, or the two
    assertions above pass because it matches nothing anywhere. The vendored
    SDK really is a wrapper, and says so."""
    sdk_readme = (REPO / "docs" / "pyedgeconnect-README.md").read_text(encoding="utf-8")
    assert _WRAPPER.search(sdk_readme)


# -- CONTRIBUTING describes this project's workflow --------------------------


def test_contributing_describes_the_real_toolchain() -> None:
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for tool in ("ruff", "mypy", "pytest", "make check"):
        assert tool in text, tool


def test_contributing_no_longer_prescribes_the_upstream_toolchain() -> None:
    """`black`, `flake8` and PyCharm were the inherited instructions. They may
    be *mentioned* — the file explains what it replaced — but not prescribed,
    so the check is that the gate section names the right tools and the old
    ones never appear as an instruction."""
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "The maintainers use black" not in text
    assert "PEP-8 check must be successful" not in text
    # And it says outright which tooling is this project's.
    assert "make check" in text and "ruff format" in text


def test_contributing_points_at_the_two_axes() -> None:
    """Tier and evidence are independent and a contributor conflating them is
    the failure #66 and #68 both exist to prevent."""
    text = (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "docs/plugin-promotion.md" in text
    assert "docs/live-validation.md" in text
    assert "mock-verified" in text
