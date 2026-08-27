"""Packaging invariants (issue #65).

The failure this guards against is quiet. `specs.py` deliberately degrades to
an empty endpoint universe when the baselines are absent, so that
`show coverage` still runs from a wheel that lacks them. Combined with
baselines that lived at the repository root and were therefore never packaged,
an installed `ec-cli show coverage` reported "0 of 0 endpoints" — not an
error, just a confident wrong answer.

The wheel is built and installed for real in CI. What is checked here is
everything that can be checked without a build, so the loop is short.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # 3.10 has no tomllib; `tomli` is the backport, a dev-only dependency.
    import tomli as tomllib

from pyecsdwan import specs

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "src" / "pyecsdwan"

#: The vendored baselines are a fixed input; they move only when
#: `tools/spec_sync.py` adopts a new one, which is a deliberate, reviewed act.
#: Pinning the count means "the specs silently stopped shipping" fails here.
EXPECTED_ENDPOINTS = 1833


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


# -- the baselines live inside the package ----------------------------------


def test_specs_resolve_from_inside_the_package(monkeypatch) -> None:
    monkeypatch.delenv(specs.ENV_SPECS_DIR, raising=False)
    specs.clear_caches()
    found = specs.specs_dir()
    assert found is not None, "vendored baselines not found"
    assert found == PACKAGE_ROOT / "_specs"
    # Inside the package is what makes them ship; a sibling of it does not.
    assert found.is_relative_to(PACKAGE_ROOT)


def test_the_endpoint_universe_is_not_empty(monkeypatch) -> None:
    monkeypatch.delenv(specs.ENV_SPECS_DIR, raising=False)
    specs.clear_caches()
    assert len(list(specs.iter_endpoints())) == EXPECTED_ENDPOINTS


def test_the_baselines_are_no_longer_at_the_repository_root() -> None:
    assert not (REPO_ROOT / "specs").exists()


# -- the build declaration matches what is on disk --------------------------


def test_package_data_is_declared_explicitly_not_inherited() -> None:
    """Inclusion must not depend on what happens to be tracked by git.

    setuptools' `include-package-data` default is `true`, under which a data
    file ships because it reached the sdist — so `git add` decides what is in
    the wheel, and an untracked file is omitted with no error anywhere.
    """
    assert _pyproject()["tool"]["setuptools"]["include-package-data"] is False


def test_package_data_declares_every_non_python_file_in_the_package() -> None:
    """Anything on disk and undeclared would work locally and vanish installed.

    That is the worst shape a packaging bug takes: `py.typed` silently dropped
    (the package stops being typed downstream), or a new baseline format that
    `specs_dir()` finds in a checkout and never finds again once installed.
    """
    declared = _pyproject()["tool"]["setuptools"]["package-data"]["pyecsdwan"]
    covered = {
        path.resolve()
        for pattern in declared
        for path in PACKAGE_ROOT.glob(pattern)
        if path.is_file()
    }
    on_disk = {
        path.resolve()
        for path in PACKAGE_ROOT.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }
    assert on_disk, "no package data found at all"
    missing = sorted(str(p.relative_to(PACKAGE_ROOT)) for p in on_disk - covered)
    assert not missing, f"on disk but not declared in package-data: {missing}"


def test_the_typed_marker_is_declared() -> None:
    # Named explicitly: it is one byte of content and all of the package's
    # typing contract with downstream consumers.
    assert (PACKAGE_ROOT / "py.typed").exists()
    declared = _pyproject()["tool"]["setuptools"]["package-data"]["pyecsdwan"]
    assert "py.typed" in declared


def test_the_advertised_entry_points_exist() -> None:
    scripts = _pyproject()["project"]["scripts"]
    assert scripts["ec-cli"] == "pyecsdwan.cli.main:main"
    from pyecsdwan.cli.main import main

    assert callable(main)


def test_only_the_product_package_is_declared() -> None:
    """The vendored SDK and the quarantined MCP server are not components."""
    find = _pyproject()["tool"]["setuptools"]["packages"]["find"]
    assert find["where"] == ["src"]
    assert find["include"] == ["pyecsdwan*"]
    # Both live outside `src/`, so the declaration above cannot reach them.
    assert (REPO_ROOT / "pyedgeconnect").exists()
    assert not (REPO_ROOT / "src" / "pyedgeconnect").exists()
    assert (REPO_ROOT / "contrib" / "mcp_server_legacy").exists()
    assert not (REPO_ROOT / "src" / "mcp_server_legacy").exists()


def test_every_source_subpackage_is_importable_from_the_installed_layout() -> None:
    """Catches a directory that is a package on disk but has no `__init__.py`.

    setuptools' `find` skips those, so they would be missing from the wheel
    while a source checkout imports them fine via namespace packages.
    """
    missing = [
        str(d.relative_to(PACKAGE_ROOT))
        for d in PACKAGE_ROOT.rglob("*")
        if d.is_dir()
        and d.name not in {"__pycache__", "_specs"}
        and any(f.suffix == ".py" for f in d.iterdir())
        and not (d / "__init__.py").exists()
    ]
    assert not missing, f"package dirs without __init__.py: {missing}"


# -- degradation is still available, just no longer the default -------------


def test_an_empty_specs_dir_degrades_rather_than_crashing(monkeypatch, tmp_path) -> None:
    # The degrade path is deliberate — it keeps `show coverage`'s resource
    # table working. What was wrong was reaching it by default.
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path))
    specs.clear_caches()
    try:
        # The directory exists, so it resolves; it just holds no baselines.
        assert specs.specs_dir() == tmp_path
        assert list(specs.iter_endpoints()) == []
    finally:
        specs.clear_caches()


def test_a_specs_dir_that_does_not_exist_resolves_to_none(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(specs.ENV_SPECS_DIR, str(tmp_path / "nope"))
    specs.clear_caches()
    try:
        assert specs.specs_dir() is None
        assert list(specs.iter_endpoints()) == []
    finally:
        specs.clear_caches()


def test_the_cli_says_so_when_the_baselines_are_absent() -> None:
    from pyecsdwan.cli.main import _no_specs_message

    message = _no_specs_message()
    assert specs.ENV_SPECS_DIR in message
    assert "unavailable" in message
