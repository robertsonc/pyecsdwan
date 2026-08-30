"""The one secret detector every rendering surface shares (#106).

What is worth testing is not that string replacement works — it is the two
claims the module makes:

* **one detector, biased toward hiding.** A field name that plausibly names a
  credential is redacted wherever it appears, however it is spelled
  (`md5Password`, `md5_password`, `MD5-PASSWORD`), and the cost of a false
  positive is a hidden value, never a leaked one;
* **redaction is not deletion.** The marker keeps a change hint, so two
  redacted exports can still answer "did it change?" — and an empty secret
  keeps no hint at all, because a digest of `""` is confirmable by anyone.
"""

from __future__ import annotations

import pytest

from pyecsdwan import redaction

# -- the detector -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "password",              # BGP neighbor
        "md5Password",           # OSPF interface
        "authKey",               # OSPF interface
        "community",             # SNMP v2c
        "communityString",
        "privPassword",          # SNMP v3
        "apiKey",
        "api-key",
        "shared_secret",
        "PASSPHRASE",
        "integrationToken",
        "credentials",
        "preSharedKey",
        "psk",
    ],
)
def test_credential_shaped_names_are_detected(name: str) -> None:
    assert redaction.looks_secret(name)


@pytest.mark.parametrize(
    "name",
    [
        "nePk",            # appliance primary key — an identifier, not a credential
        "ref_key",         # candidate item addressing
        "hostname",
        "remote_as",
        "keyring_error",   # metadata *about* credentials, holds none
        "publicKey",       # would hide the non-secret half of a keypair? No:
                           # it contains no token — and if it ever did, the
                           # failure mode is hiding, which is the safe one.
        "next_hop_self",
    ],
)
def test_identifier_shaped_names_are_not(name: str) -> None:
    assert not redaction.looks_secret(name)


# -- the tree walk ------------------------------------------------------------

BGP_NEIGHBOR = {
    "neighbors": [
        {"ip": "10.0.0.1", "remote_as": 65001, "password": "s3ntinel-bgp"},
        {"ip": "10.0.0.2", "remote_as": 65002, "password": ""},
    ],
    "asn": 65000,
}


def test_redact_tree_masks_secrets_and_keeps_everything_else() -> None:
    out = redaction.redact_tree(BGP_NEIGHBOR)
    assert out["asn"] == 65000
    assert out["neighbors"][0]["ip"] == "10.0.0.1"
    assert out["neighbors"][0]["password"].startswith(redaction.REDACTED_PREFIX)
    assert "s3ntinel-bgp" not in str(out)


def test_redact_tree_does_not_mutate_its_input() -> None:
    """The same object is also what commit writes to the fabric; a redactor
    that edited it in place would corrupt the write it was protecting."""
    redaction.redact_tree(BGP_NEIGHBOR)
    assert BGP_NEIGHBOR["neighbors"][0]["password"] == "s3ntinel-bgp"


def test_a_whole_subtree_under_a_secret_name_goes() -> None:
    """A dict under `credentials` is one value: its inner names are not what
    marked it, and `{"user": ..., "pass": ...}` would leak the half whose
    name happens not to match."""
    out = redaction.redact_tree({"credentials": {"user": "admin", "word": "hunter2"}})
    assert isinstance(out["credentials"], str)
    assert "hunter2" not in str(out)


def test_the_marker_is_a_change_hint_not_a_deletion() -> None:
    """Two different secrets get two different markers, the same secret gets
    the same one — so a diff of two redacted trees still says *whether* the
    credential moved without saying what it is."""
    one = redaction.redact_tree({"password": "alpha"})["password"]
    same = redaction.redact_tree({"password": "alpha"})["password"]
    other = redaction.redact_tree({"password": "beta"})["password"]
    assert one == same
    assert one != other


def test_an_empty_secret_keeps_no_digest() -> None:
    """`password: ""` means "none set" — worth showing as distinct state, and
    a digest of the empty string is a rainbow-table entry of one."""
    marker = redaction.redact_tree({"password": ""})["password"]
    assert marker == f"{redaction.REDACTED_PREFIX}:empty>"


def test_finds_secrets_is_the_detectors_tree_form() -> None:
    assert redaction.finds_secrets(BGP_NEIGHBOR)
    assert redaction.finds_secrets([{"snmp": {"community": "x"}}])
    assert not redaction.finds_secrets({"asn": 65000, "neighbors": [{"ip": "a"}]})
    assert not redaction.finds_secrets("password")  # a *value* is not a field name


# -- parameters and query strings ---------------------------------------------


def test_secret_params_keep_their_name_and_lose_their_value() -> None:
    out = redaction.redact_params({"apiKey": "s3ntinel-key", "limit": "10"})
    assert out["limit"] == "10"
    assert out["apiKey"].startswith(redaction.REDACTED_PREFIX)
    assert "s3ntinel-key" not in str(out)


def test_query_string_values_are_masked_in_place() -> None:
    masked = redaction.redact_query("/snmp/config?community=s3cret&rows=5")
    assert masked.startswith("/snmp/config?")
    assert "rows=5" in masked
    assert "s3cret" not in masked
    assert redaction.REDACTED_PREFIX in masked


def test_a_path_without_a_query_comes_back_byte_identical() -> None:
    """The path is what the audit trail exists to record; touching it would
    make the record disagree with what was sent."""
    assert redaction.redact_query("/appliance/rest/BR1/bgp") == "/appliance/rest/BR1/bgp"
