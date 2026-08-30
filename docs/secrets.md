# Secrets in persisted and exported state (#106)

The Orchestrator API hands this tool material worth stealing: BGP neighbor
`password`s, OSPF `authKey`/`md5Password`, SNMP `community` strings, and the
token fields of integration endpoints. Three of this tool's own features then
want to *keep* that material — the candidate store stages it, the journal
snapshots it for rollback, and the audit export distributes records of it.
File mode `0600` was the only protection, and mode is access control, not
redaction, encryption, or safe export.

Four layers now stand between a secret and a place it does not belong. Each
exists because the one before it has a hole, and the ordering is the design.

## The layers

**1. Detection** (`pyecsdwan/redaction.py`). One name-based detector shared by
every surface: a field whose name contains a credential-shaped token
(`password`, `community`, `authKey`, `token`, …, spelled any way) marks its
value secret. Name-based means fallible — a secret under a name the list does
not recognise is not detected — which is why detection only ever *adds*
protection on top of the layers below, and why the bias is toward false
positives: the cost of over-matching is a hidden value, never a leaked one.

**2. Separation** (`pyecsdwan/journal.py`). Snapshot bodies no longer live in
`events.jsonl`. The event log — the thing `show journal --events` exports —
carries the digest and size of each body; the body itself lives in the
transaction's `private.jsonl`, which nothing exports. This layer does not
depend on detection: *every* body is rollback-private, including ones whose
secrets hide under unrecognised names. Rollback cross-checks the body against
the digest the event log recorded and refuses on any mismatch, so the split
cannot silently restore a body the journal cannot vouch for.

**3. Encryption** (`pyecsdwan/vault.py`). Material the detector marks secret
is sealed with AES-256-GCM under an envelope key before it is persisted:
whole snapshot bodies in `private.jsonl`, and individual secret values inside
candidate `intent` (the rest of the candidate file stays readable JSON, which
keeps it debuggable). Sealing is fail-closed — see below.

**4. Redaction at every exit** (`redaction.py` again, applied at each
surface). Rendered diffs (`compare`, `plan`, `drift`), `show configuration
candidate`, the audit export in both modes, journaled Tier-0 `api`
parameters, and `OrchApiError` text all mask secret-named values. A masked
value keeps the field name and a truncated digest — a change hint — so "did
the password change?" stays answerable while the password does not travel.

## The envelope key

Sources, in precedence order:

1. `ECSDWAN_ENVELOPE_KEY` — base64 of 32 random bytes, for headless boxes
   with no keyring backend. When set, it is the *only* key consulted; whoever
   sets it owns its lifecycle.
2. The OS keyring: service `pyecsdwan`, username `envelope-key`. Created
   automatically the first time secret-bearing state needs sealing.

Generate an environment key with:

```
python -c "import base64,secrets;print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

### When no key can be had

Writes fail closed. Staging a candidate item with a detected secret, or
snapshotting a secret-bearing body at commit time, raises before anything
touches disk — and the snapshot happens before the commit's first fabric
write, so the refusal also comes before any change lands. The error names
both remedies. A box that stages nothing the detector recognises never needs
a key at all.

Reads fail loudly, and recoverably. Sealed state with no key (or the wrong
key) raises `VaultOpenError`; it never reads as "no value here", because a
revert would interpret an absent snapshot as "the resource did not exist" and
delete it. The blobs are never modified by a failed open, so restoring the
key restores everything.

## Rotation

```
ec-cli rotate-key
```

Retires the keyring key and re-seals every sealed blob in the state directory
under a fresh one. Crash-safe by ordering: the outgoing key moves to the
`envelope-key-previous` slot before the new key replaces it, and is deleted
only after every blob is rewritten — at every intermediate point both keys
are consulted and everything opens, so an interrupted rotation is simply
re-run. Run it when no commit is in flight; a commit mid-rotation is writing
snapshots the re-seal walk could miss.

Under `ECSDWAN_ENVELOPE_KEY` the command refuses: rotating the keyring entry
underneath an environment-keyed deployment would strand every blob, and the
operator who exported a key is the only party who can rotate it.

API-key rotation is separate and unchanged: the Orchestrator credential lives
under service `pyecsdwan`, username = the canonical origin, and rotates with
the `keyring` CLI (see the README).

## Backup and retention

Once anything is sealed, **the state directory alone is not a backup**. A
restore onto a machine without the envelope key yields candidate stores and
journals that refuse to open until the key is restored — by design, and
loudly. Back up the keyring entry (or the exported variable) with the state,
and store them apart from each other.

Retention is the journal's existing pruning: `private.jsonl` lives and dies
with its transaction directory, so the confirmed-history and audit-record
quotas that bound the journal bound the rollback-private material with it.
Nothing retains a secret longer than the transaction that needed it for
rollback.

## What this does not do

- Detect secrets under names the token list does not match. Those values
  still get the private/audit split and `0600`, but land unencrypted in
  `private.jsonl` and unmasked in rendered diffs. Extending
  `redaction.SECRET_NAME_TOKENS` is a one-line change and the tests sweep
  the state directory for seeded sentinels, so a new field name is cheap to
  cover once known.
- Protect the YAML a user keeps in git (`apply --from` directories). That is
  the user's repository; this tool's promise covers its own state directory
  and its own output.
- Make the truncated digest in a redaction marker safe against dictionary
  confirmation of a low-entropy secret. It is a change hint; treat any
  surface that shows one with the care the field name alone would warrant.
