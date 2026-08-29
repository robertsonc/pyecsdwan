"""The journal as an exportable audit trail (epic #9).

The transaction journal already records everything an auditor wants: who
changed what, when, under which ownership decision, whether it was confirmed
or reverted, and what the server said. It just had no way out of
``~/.pyecsdwan/journal/`` except reading the files.

This is that way out, and the whole of it is one decision:

**Snapshot bodies are redacted by default.** ``journal.py`` opens its event log
0600 because "snapshots can embed sensitive server config" — a SNAPSHOT event
carries an entire appliance object, which is the point (it is what rollback
restores from) and is also the one thing in the journal that is not metadata.
Exporting is *distribution*: to a file, a pipe, a SIEM. A default that shipped
whole config bodies to a log aggregator would undo the 0600 without anyone
deciding to.

Redaction is not deletion. Each redacted snapshot keeps a SHA-256 of its
canonical JSON and its size, so an auditor can prove two exports describe the
same state — or that a restore matched what was captured — without ever seeing
the config. That is the pattern ``ec-cli api`` already uses for request bodies
(``body_sha256``), applied to the other place a body appears.

``--include-snapshots`` opts in. It is spelled as an opt-in rather than
``--redact`` as an opt-out because the safe default should be the one you get
by not thinking about it.

Every record is self-contained. An NDJSON line lands in a SIEM with no
surrounding context, so ``txn_id``, ``orch_host`` and ``orch_origin`` are
stamped onto each one
rather than left implied by the file it came from.

Apart from that stamping and the redaction, a record is the journal's own event
verbatim — including ``ref`` in its stored, percent-encoded key form rather
than the user-facing noun the CLI would print. An audit trail that prettified
what it reproduced would no longer be evidence of what was recorded, and the
decoded noun is recoverable from the key while the reverse is not.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import Any

from pyecsdwan.journal import TxnJournal

#: Event kind -> the fields carrying a whole server object rather than
#: metadata. Only SNAPSHOT has one today; the mapping exists so a new
#: body-bearing event is a one-line change here rather than a silent leak.
BODY_FIELDS: dict[str, tuple[str, ...]] = {"SNAPSHOT": ("raw",)}

#: What replaces a redacted body. `sha256` is over the canonical JSON, so it is
#: stable across re-exports and comparable between two journals.
REDACTED_MARKER = "redacted"


def digest(value: Any) -> str:
    """SHA-256 of a value's canonical JSON form."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def redact(event: dict[str, Any]) -> dict[str, Any]:
    """Replace any body field with a digest and a size.

    Returns a new dict; the caller's event is never mutated, because the same
    event object is also what a human-readable render may print.
    """
    fields = BODY_FIELDS.get(str(event.get("event", "")), ())
    if not fields:
        return dict(event)
    out = dict(event)
    for field in fields:
        if field not in out:
            continue
        body = out[field]
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        out[field] = {
            REDACTED_MARKER: True,
            "sha256": digest(body),
            "bytes": len(canonical.encode("utf-8")),
        }
    return out


@dataclasses.dataclass(frozen=True)
class Summary:
    """One transaction, without its events — what `show journal --json` emits."""

    txn_id: str
    orch_host: str
    #: Canonical identity of the target (#63). Empty on journals written
    #: before origins were recorded, where `orch_host` is all there is.
    orch_origin: str
    created_at: str
    state: str
    confirm_deadline: str | None
    items: tuple[str, ...]

    @staticmethod
    def of(journal: TxnJournal) -> Summary:
        meta = journal.meta
        return Summary(
            txn_id=meta.txn_id,
            orch_host=meta.orch_host,
            orch_origin=meta.orch_origin,
            created_at=meta.created_at,
            state=meta.state,
            confirm_deadline=meta.confirm_deadline,
            items=tuple(meta.items),
        )

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self) | {"items": list(self.items)}


def summaries(txns: Iterable[TxnJournal]) -> list[dict[str, Any]]:
    return [Summary.of(t).as_json() for t in txns]


def events(
    txns: Iterable[TxnJournal], *, include_snapshots: bool = False
) -> Iterator[dict[str, Any]]:
    """Every event of every transaction, oldest transaction first.

    Each record carries the transaction it belongs to and the Orchestrator it
    was against, so one line is meaningful on its own.
    """
    for journal in txns:
        meta = journal.meta
        for event in journal.events():
            record = event if include_snapshots else redact(event)
            # Stamped, not merged under a key, so a SIEM's flat field mapping
            # sees them. `txn_id`/`orch_host`/`orch_origin` are not event field
            # names, so there is nothing to collide with.
            yield {
                "txn_id": meta.txn_id,
                "orch_host": meta.orch_host,
                "orch_origin": meta.orch_origin,
                **record,
            }


def to_ndjson(records: Iterable[dict[str, Any]]) -> Iterator[str]:
    """One JSON object per line — what log shippers consume.

    Not a JSON array: an array has to be complete before it parses, so a run
    interrupted halfway produces a file nothing can read. NDJSON degrades to
    "the lines that made it".
    """
    for record in records:
        yield json.dumps(record, sort_keys=True, default=str)
