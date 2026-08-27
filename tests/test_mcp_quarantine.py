"""The quarantined legacy MCP server's trust boundary (issue #62).

`policy.py` carries no `mcp` or `pyedgeconnect` import precisely so these can
run in the normal suite. The old server sat outside ruff, mypy and the tests
entirely, which is how it kept `verify_ssl=False` and ~250 reflectively
exposed write operations for as long as it did.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_server_legacy import policy
from mcp_server_legacy.policy import Disabled, OpClass

ON = {policy.ENABLE_ENV: "1"}
ON_WRITES = {policy.ENABLE_ENV: "1", policy.ALLOW_WRITES_ENV: "1"}


# -- disabled by default -----------------------------------------------------


def test_disabled_with_no_environment() -> None:
    assert policy.enabled({}) is False
    with pytest.raises(Disabled) as excinfo:
        policy.check_enabled({})
    message = str(excinfo.value)
    assert policy.ENABLE_ENV in message
    assert "ec-cli" in message  # points at the surface that is transactional


def test_enabling_is_explicit_and_not_accidental() -> None:
    assert policy.enabled(ON) is True
    for value in ("", "0", "false", "no", "off", "maybe"):
        assert policy.enabled({policy.ENABLE_ENV: value}) is False, value


def test_writes_need_their_own_opt_in() -> None:
    assert policy.writes_allowed({}) is False
    assert policy.writes_allowed(ON) is False
    assert policy.writes_allowed(ON_WRITES) is True


def test_allow_writes_alone_does_not_enable_anything() -> None:
    # A stray ALLOW_WRITES in a shell profile must not be half a decision.
    assert policy.writes_allowed({policy.ALLOW_WRITES_ENV: "1"}) is False


# -- TLS ---------------------------------------------------------------------


def test_tls_verification_defaults_on() -> None:
    assert policy.verify_tls({}) is True
    assert policy.verify_tls(ON) is True


def test_insecure_transport_is_a_separate_explicit_opt_in() -> None:
    assert policy.verify_tls({policy.INSECURE_ENV: "1"}) is False
    # Enabling the server does not imply disabling TLS.
    assert policy.verify_tls(ON_WRITES) is True


# -- direct-to-appliance access ---------------------------------------------


def test_appliance_tools_are_unavailable_by_construction() -> None:
    # #10 defers direct appliance access until an RBAC broker exists. This is
    # not a setting, so no environment can turn it on.
    assert policy.appliance_tools_enabled() is False


def test_no_environment_variable_re_enables_appliance_tools() -> None:
    import inspect

    source = inspect.getsource(policy.appliance_tools_enabled)
    assert "os.environ" not in source and "env" not in source.split('"""')[-1]


def test_the_legacy_appliance_tools_are_gone_from_the_server() -> None:
    server = (Path(__file__).parent.parent / "contrib/mcp_server_legacy/server.py").read_text()
    assert "ec_connect" not in server
    assert "EdgeConnect" not in server


# -- classification ----------------------------------------------------------


def test_a_get_only_read_is_a_read() -> None:
    assert policy.classify("get_appliances", frozenset({"get"})) is OpClass.READ


def test_a_read_shaped_name_that_posts_is_not_a_read() -> None:
    # 53 get_* methods in the vendored SDK issue a POST, and this repository
    # has already found endpoints that mutate behind a read-shaped verb
    # (GET /oro/debug/closeGrpcConnection — see issue #67).
    assert policy.classify("get_aggregate_stats_flows", frozenset({"post"})) is OpClass.WRITE
    assert policy.classify("get_thing", frozenset({"get", "post"})) is OpClass.WRITE


def test_unknown_verbs_fail_closed_to_write() -> None:
    # None means "we could not read the method". That is not "probably fine".
    assert policy.classify("get_appliances", None) is OpClass.WRITE
    assert policy.classify("get_appliances", frozenset()) is OpClass.WRITE


def test_destructive_names_are_destructive_whatever_the_verb() -> None:
    for name in (
        "delete_appliance",
        "remove_template_group",
        "reset_appliance",
        "reboot_appliance",
        "purge_logs",
        "revoke_license",
    ):
        assert policy.classify(name, frozenset({"get"})) is OpClass.DESTRUCTIVE, name


def test_a_delete_verb_is_destructive_whatever_the_name() -> None:
    assert policy.classify("update_thing", frozenset({"delete"})) is OpClass.DESTRUCTIVE


def test_writes_are_writes() -> None:
    for name in ("update_x", "set_x", "add_x", "create_x", "post_x", "modify_x"):
        assert policy.classify(name, frozenset({"post"})) is OpClass.WRITE, name


# -- exposure ----------------------------------------------------------------


def test_nothing_is_exposed_while_disabled() -> None:
    for op in OpClass:
        assert policy.is_exposed(op, {}) is False, op


def test_enabled_exposes_reads_only() -> None:
    assert policy.is_exposed(OpClass.READ, ON) is True
    assert policy.is_exposed(OpClass.WRITE, ON) is False
    assert policy.is_exposed(OpClass.DESTRUCTIVE, ON) is False


def test_allowing_writes_exposes_writes_and_destructive() -> None:
    assert policy.is_exposed(OpClass.WRITE, ON_WRITES) is True
    assert policy.is_exposed(OpClass.DESTRUCTIVE, ON_WRITES) is True


# -- credentials -------------------------------------------------------------


def test_credentials_come_from_the_environment_not_arguments() -> None:
    url, key = policy.orchestrator_credentials(
        {policy.URL_ENV: "https://orch.example.com", policy.API_KEY_ENV: "secret-key"}
    )
    assert url == "https://orch.example.com"
    assert key == "secret-key"


def test_missing_url_refuses_rather_than_prompting_for_one() -> None:
    with pytest.raises(Disabled, match=policy.URL_ENV):
        policy.orchestrator_credentials({})


def test_no_tool_accepts_a_credential_argument() -> None:
    """The single most load-bearing property here.

    A credential in a tool argument is visible to the model, lands in the
    transcript, and is echoed by anything logging tool calls.
    """
    server = (Path(__file__).parent.parent / "contrib/mcp_server_legacy/server.py").read_text()
    for banned in ("api_key: str =", "password: str =", "user: str =", "mfacode: str ="):
        assert banned not in server, banned


def test_redact_removes_a_known_secret_from_a_response() -> None:
    assert "hunter2hunter2" not in policy.redact("key=hunter2hunter2 ok", "hunter2hunter2")
    assert policy.redact("nothing here", "hunter2hunter2") == "nothing here"


def test_redact_ignores_values_too_short_to_be_secrets() -> None:
    # Blanking a 3-character value would corrupt ordinary output.
    assert policy.redact("id=abc", "abc") == "id=abc"


def test_redact_handles_a_missing_key() -> None:
    assert policy.redact("body", None) == "body"


# -- the Tier-0 label --------------------------------------------------------


def test_write_tools_carry_a_tier_zero_warning() -> None:
    notice = policy.tier0_notice(OpClass.WRITE)
    assert "Tier 0" in notice
    assert "rollback" in notice and "ec-cli" in notice
    assert policy.tier0_notice(OpClass.DESTRUCTIVE) != ""


def test_read_tools_carry_no_warning() -> None:
    assert policy.tier0_notice(OpClass.READ) == ""


# -- packaging ---------------------------------------------------------------


def test_the_legacy_server_is_not_packaged() -> None:
    """It must not ship in the wheel — see #65's packaging tests too."""
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert 'include = ["pyecsdwan*"]' in pyproject
    assert not (Path(__file__).parent.parent / "src/pyecsdwan/mcp_server").exists()


def test_it_no_longer_sits_in_the_product_tree() -> None:
    root = Path(__file__).parent.parent
    assert not (root / "mcp_server").exists()
    assert (root / "contrib/mcp_server_legacy/policy.py").exists()


# -- the guard fires before anything network-capable is imported -------------


def test_importing_the_server_refuses_before_importing_mcp() -> None:
    """The refusal must precede the `mcp` / `pyedgeconnect` imports.

    Neither package is a product dependency, so if the guard ever moves below
    them a disabled server would fail with ImportError instead of an
    explanation — and, worse, an operator who *did* install the extra would
    get a running server before the check ran at all.
    """
    import os
    import subprocess
    import sys

    contrib = str(Path(__file__).parent.parent / "contrib")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ECSDWAN_MCP_LEGACY")}
    result = subprocess.run(
        [sys.executable, "-c", "import mcp_server_legacy.server"],
        capture_output=True,
        text=True,
        cwd=contrib,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    # The failure must be the policy refusing, not a missing dependency.
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert policy.ENABLE_ENV in result.stderr
