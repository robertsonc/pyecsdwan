"""Distil the vendor's public Postman collections into payload examples (issue #51).

HPE publishes an "EdgeConnect SD-WAN" Postman collection per release (9.3 -
9.6). This tool downloads them, reduces the four documents to one small
artifact -- ``specs/payload-examples-<newest>.json`` -- and reports what
changed against the committed copy. :func:`pyecsdwan.specs.payload_examples`
reads that artifact; nothing else in the tree touches the raw collections,
which are ~5 MB each and are deliberately *not* vendored.

What the collections are actually good for
------------------------------------------
Not endpoint breadth. Measured against the vendored ``specs/*-openapi-7.2.0``
baselines (with every path-parameter dialect normalized), the 9.6 collections
contribute exactly **one** endpoint the baselines lack
(``GET /stats/infrastructure/reportLoaderUrl``) while omitting 58 the baselines
already carry. The baselines are a superset; treat any claim that the
collections "widen coverage" as false.

What they do add is **payload shape**. 204 spec write-operations carry no typed
request body and 802 operations carry no typed 200-response schema; the
collections supply a request example for 159 of the former and a response
example for 151 of the latter, plus a two-level folder taxonomy and per-release
provenance (250 endpoints appear between 9.3 and 9.6, 18 disappear).

**These examples are schema skeletons, not real captures.** The vendor fills
every scalar with ``"string"``, ``0`` or ``true``::

    GET {{applianceBaseUrl}}/lte/config
    {"cell0": {"admin": "string", "apn": "string", "generation": "string"}}

They document field names and nesting and nothing else. Never assert on one as
if it held a real value.

Document format
---------------
The published documents use Postman's internal *v1* model, not the v2.1
collection format: ``data.folders`` is a flat list with parent pointers and
``data.requests`` is a flat list carrying ``rawModeData`` (request example) and
``responses[].text`` (response examples). Scope comes from the base-URL
variable in ``url`` -- ``{{orchestratorBaseUrl}}`` or ``{{applianceBaseUrl}}``
-- there is no other scope marker. Orchestrator requests spell path parameters
``{{nePk}}`` and appliance requests spell them ``:nePk``; both are folded onto
the spec's ``{nePk}`` by :func:`pyecsdwan.specs.endpoint_key`, which this tool
imports rather than reimplements.

Usage::

    python tools/postman_sync.py --diff
    python tools/postman_sync.py --diff --release 9.6 --source ./ec-9.6.json
    python tools/postman_sync.py --update

Sources default to the vendor's public, unauthenticated endpoint
``https://www.postman.com/_api/collection/<uid>?populate=true`` (the
``populate`` query parameter is required -- without it the API returns a 7 KB
metadata stub). Override one with ``--source`` or all of them with
``ECSDWAN_POSTMAN_SOURCE_9_3`` ... ``ECSDWAN_POSTMAN_SOURCE_9_6``; ``--no-fetch``
skips releases that have no local override, so the whole tool runs offline. No
credentials are ever sent: the collections are public, and
``api.getpostman.com`` (which does want an API key) is deliberately not used.

Exit codes: 0 in sync / updated, 1 drift detected, 2 error or nothing to do.

Standalone by design (stdlib only; ``httpx`` imported lazily for URL sources),
except that distilling needs ``pyecsdwan`` importable for the one shared
normalization function.
"""

from __future__ import annotations

import argparse
import datetime as dt
import functools
import json
import os
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPECS_DIR = REPO_ROOT / "specs"

#: Releases in ascending order; ``since`` resolution depends on this order.
RELEASES: tuple[str, ...] = ("9.3", "9.4", "9.5", "9.6")
#: Public collection UIDs, resolved from the vendor links on issue #51.
COLLECTION_UID: dict[str, str] = {
    "9.3": "40501331-a52a6606-f94c-48bb-ba76-871e997a4cce",
    "9.4": "40501331-00d12f94-e656-4be9-9839-842f282786be",
    "9.5": "40501331-bfd23c66-4cc4-43f0-a082-cb844c7095b2",
    "9.6": "32717089-8588a965-ffbc-4fe2-8799-ef4bc0922e9a",
}
PUBLIC_SOURCE: dict[str, str] = {
    release: f"https://www.postman.com/_api/collection/{uid}?populate=true"
    for release, uid in COLLECTION_UID.items()
}
ENV_SOURCE: dict[str, str] = {
    release: f"ECSDWAN_POSTMAN_SOURCE_{release.replace('.', '_')}" for release in RELEASES
}
#: Same env convention as pyecsdwan.config, duplicated as a literal.
ENV_INSECURE = "ECSDWAN_INSECURE"

#: The only scope marker the collections carry.
SCOPE_VARIABLE: dict[str, str] = {
    "{{orchestratorBaseUrl}}": "orchestrator",
    "{{applianceBaseUrl}}": "appliance",
}

ARTIFACT_STEM = "payload-examples"
SOURCE_LABEL = (
    "HPE Aruba EdgeConnect SD-WAN public Postman collections "
    "(https://www.postman.com/_api/collection/<uid>?populate=true)"
)
CAVEAT = "shape only — the vendor fills scalars with placeholders, never real values"

#: Guard against a folder cycle in a malformed document.
MAX_FOLDER_DEPTH = 16
#: Human output caps per list; --json always carries everything.
MAX_LISTED = 50

JsonDict = dict[str, Any]


class PostmanSyncError(Exception):
    """Fatal condition reported to the operator; exits with status 2."""


# ---------------------------------------------------------------------------
# Shared normalization


@functools.lru_cache(maxsize=1)
def _endpoint_key_fn() -> Callable[[str, str, str], str]:
    """Borrow ``pyecsdwan.specs.endpoint_key`` -- never reimplement it here.

    The collections, the OpenAPI baselines and a live URL each spell path
    parameters differently; exactly one function in the tree is allowed to know
    how they fold together, and it lives in the package.
    """
    try:
        from pyecsdwan.specs import endpoint_key
    except ImportError as exc:  # pragma: no cover - venv always has the package
        raise PostmanSyncError(
            "distilling needs the pyecsdwan package importable for "
            "specs.endpoint_key() (try: pip install -e .)"
        ) from exc
    return endpoint_key


def endpoint_key(scope: str, method: str, path: str) -> str:
    """``"orchestrator POST /alarm/alarmConfig"`` -- via the package's normalizer."""
    return _endpoint_key_fn()(scope, method, path)


# ---------------------------------------------------------------------------
# Loading


def load_collection(source: str, *, timeout: float, insecure: bool) -> JsonDict:
    """Load one collection and return its ``data`` object.

    Accepts the ``{"model_id": ..., "data": {...}}`` envelope the web API
    returns as well as a bare ``data`` object, so a hand-trimmed fixture can be
    fed straight in.
    """
    if source.startswith(("http://", "https://")):
        raw = _fetch_url(source, timeout=timeout, insecure=insecure)
    else:
        path = Path(source)
        if not path.is_file():
            raise PostmanSyncError(f"collection source not found: {source}")
        raw = path.read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PostmanSyncError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise PostmanSyncError(f"{source}: not a Postman collection document")
    data = document.get("data", document)
    if not isinstance(data, dict) or not isinstance(data.get("requests"), list):
        raise PostmanSyncError(
            f"{source}: not a populated Postman v1 collection (no data.requests list). "
            "Web-API URLs need the ?populate=true query parameter."
        )
    return data


def _fetch_url(url: str, *, timeout: float, insecure: bool) -> str:
    # Lazy import so file-based runs need no third-party packages at all.
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - venv always has httpx
        raise PostmanSyncError("fetching a URL requires httpx (pip install httpx)") from exc
    # No credentials: the collections are public and Postman is not our vendor.
    try:
        response = httpx.get(url, timeout=timeout, verify=not insecure, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PostmanSyncError(f"fetch failed: {url}: {exc}") from exc
    return response.text


# ---------------------------------------------------------------------------
# Distillation


def folder_paths(folders: Iterable[Any]) -> dict[str, list[str]]:
    """Map folder id -> root-first name path.

    ``data.folders`` is a flat list where each entry names its parent in
    ``folder``; the two roots are ``Appliance Level`` and ``Orchestrator
    Level``. Depth is bounded so a self-referential parent pointer cannot hang
    the tool.
    """
    by_id: dict[str, JsonDict] = {}
    for entry in folders:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            by_id[entry["id"]] = entry
    resolved: dict[str, list[str]] = {}
    for folder_id in by_id:
        names: list[str] = []
        current: str | None = folder_id
        seen: set[str] = set()
        while current is not None and current in by_id and len(names) < MAX_FOLDER_DEPTH:
            if current in seen:
                break
            seen.add(current)
            entry = by_id[current]
            names.append(str(entry.get("name", "")))
            parent = entry.get("folder")
            current = parent if isinstance(parent, str) else None
        resolved[folder_id] = list(reversed(names))
    return resolved


def split_url(url: str) -> tuple[str, str] | None:
    """``"{{orchestratorBaseUrl}}/alarm"`` -> ``("orchestrator", "/alarm")``.

    Returns ``None`` for a URL carrying neither base-URL variable: scope is not
    guessed, and an unscoped request is dropped rather than filed wrongly.
    """
    for variable, scope in SCOPE_VARIABLE.items():
        if url.startswith(variable):
            return scope, url[len(variable) :]
    return None


def parse_example(text: Any) -> tuple[Any, str | None]:
    """``(parsed, None)`` for JSON, ``(None, raw)`` for anything else.

    A handful of request bodies are the bare word ``string`` and a few response
    bodies are prose. Those are kept verbatim under a distinct field rather
    than dropped -- "this endpoint takes a scalar" is itself information.
    """
    if not isinstance(text, str) or not text.strip():
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, text


def pick_response(responses: Any) -> tuple[Any, str | None, int | None]:
    """Choose one saved response: first with a body, 2xx preferred.

    Most entries are named ``"No response was specified"`` yet still carry a
    usable ``text``; the name is worthless, the body is not.
    """
    if not isinstance(responses, list):
        return None, None, None
    fallback: tuple[Any, str | None, int | None] | None = None
    for entry in responses:
        if not isinstance(entry, dict):
            continue
        parsed, raw = parse_example(entry.get("text"))
        if parsed is None and raw is None:
            continue
        code = entry.get("responseCode")
        status = code.get("code") if isinstance(code, dict) else None
        status = status if isinstance(status, int) else None
        candidate = (parsed, raw, status)
        if status is not None and 200 <= status < 300:
            return candidate
        if fallback is None:
            fallback = candidate
    return fallback if fallback is not None else (None, None, None)


def distil_release(data: JsonDict) -> dict[str, JsonDict]:
    """Reduce one collection's ``data`` object to ``endpoint_key`` -> entry."""
    paths = folder_paths(data.get("folders") or [])
    entries: dict[str, JsonDict] = {}
    requests = data.get("requests")
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        url = request.get("url")
        method = request.get("method")
        if not isinstance(url, str) or not isinstance(method, str):
            continue
        split = split_url(url)
        if split is None:
            continue
        scope, path = split
        entry: JsonDict = {"name": str(request.get("name", "")), "path": path}
        folder = request.get("folder")
        if isinstance(folder, str) and folder in paths:
            entry["folder"] = paths[folder]
        req_parsed, req_raw = parse_example(request.get("rawModeData"))
        if req_parsed is not None:
            entry["request"] = req_parsed
        elif req_raw is not None:
            entry["request_raw"] = req_raw
        res_parsed, res_raw, status = pick_response(request.get("responses"))
        if res_parsed is not None:
            entry["response"] = res_parsed
        elif res_raw is not None:
            entry["response_raw"] = res_raw
        if status is not None and status != 200 and (res_parsed is not None or res_raw is not None):
            entry["response_status"] = status
        # Later requests in a release never collide today; first wins, stably.
        entries.setdefault(endpoint_key(scope, method, path), entry)
    return entries


def distil(
    releases: Sequence[tuple[str, JsonDict]], *, retrieved: str | None = None
) -> JsonDict:
    """Fold per-release collections into the committed artifact.

    *releases* is ascending by version. The newest release that carries an
    endpoint supplies its example -- shapes drift and the current one is the
    useful one -- while ``since`` records the oldest release that carried it.
    An endpoint that vanished before the newest sourced release keeps its last
    known shape and is marked ``removed_after`` so a reader is never misled
    into thinking it is current.
    """
    per_release = [(release, distil_release(data)) for release, data in releases]
    endpoints: JsonDict = {}
    for release, entries in per_release:
        for key, entry in entries.items():
            existing = endpoints.get(key)
            if existing is None:
                endpoints[key] = {**entry, "since": release}
            else:
                # Newer release wins the payload; `since` stays at the oldest.
                endpoints[key] = {**entry, "since": existing["since"]}
    if per_release:
        newest = per_release[-1][0]
        last_seen = {
            key: release for release, entries in per_release for key in entries
        }
        for key, release in last_seen.items():
            if release != newest:
                endpoints[key]["removed_after"] = release
    return {
        "meta": {
            "source": SOURCE_LABEL,
            "retrieved": retrieved or dt.date.today().isoformat(),
            "releases": [release for release, _ in releases],
            "caveat": CAVEAT,
        },
        "endpoints": endpoints,
    }


# ---------------------------------------------------------------------------
# Diffing


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


@dataclass
class ExampleDiff:
    """Endpoint-level delta between the committed artifact and a fresh distillation."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    #: (field, committed value, distilled value)
    metadata: list[tuple[str, Any, Any]] = field(default_factory=list)

    @property
    def drift(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.metadata)

    def as_json(self) -> JsonDict:
        return {
            "drift": self.drift,
            "endpoints": {"added": self.added, "removed": self.removed, "changed": self.changed},
            "metadata": [
                {"field": name, "committed": old, "distilled": new}
                for name, old, new in self.metadata
            ],
        }


#: ``retrieved`` is a provenance stamp, not content: it would otherwise make
#: every --diff run report drift on a collection that never changed.
DRIFT_META_FIELDS = ("source", "releases", "caveat")


def diff_artifacts(committed: JsonDict, distilled: JsonDict) -> ExampleDiff:
    """Compare two artifacts by endpoint entry and by content metadata."""
    diff = ExampleDiff()
    old = committed.get("endpoints")
    new = distilled.get("endpoints")
    old_eps: JsonDict = old if isinstance(old, dict) else {}
    new_eps: JsonDict = new if isinstance(new, dict) else {}
    diff.added = sorted(set(new_eps) - set(old_eps))
    diff.removed = sorted(set(old_eps) - set(new_eps))
    diff.changed = sorted(
        key
        for key in set(old_eps) & set(new_eps)
        if _canonical(old_eps[key]) != _canonical(new_eps[key])
    )
    old_meta = committed.get("meta")
    new_meta = distilled.get("meta")
    old_meta = old_meta if isinstance(old_meta, dict) else {}
    new_meta = new_meta if isinstance(new_meta, dict) else {}
    for name in DRIFT_META_FIELDS:
        if old_meta.get(name) != new_meta.get(name):
            diff.metadata.append((name, old_meta.get(name), new_meta.get(name)))
    return diff


# ---------------------------------------------------------------------------
# Artifact I/O


def find_artifact(specs_dir: Path) -> Path | None:
    """Locate the committed artifact, e.g. ``specs/payload-examples-9.6.json``."""
    matches = sorted(specs_dir.glob(f"{ARTIFACT_STEM}-*.json"))
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise PostmanSyncError(f"multiple payload-example artifacts under {specs_dir}: {names}")
    return matches[0] if matches else None


def write_artifact(artifact: JsonDict, specs_dir: Path) -> tuple[Path, Path | None]:
    """Write the artifact, named after the newest release it covers.

    Stored the way ``specs/*.json`` already are: compact, single line, sorted
    keys. Sorting is what keeps a regeneration diff reviewable. Returns
    (written path, superseded path or None).
    """
    meta = artifact.get("meta")
    releases = meta.get("releases") if isinstance(meta, dict) else None
    newest = str(releases[-1]) if isinstance(releases, list) and releases else "unknown"
    previous = find_artifact(specs_dir)
    dest = specs_dir / f"{ARTIFACT_STEM}-{newest}.json"
    specs_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(_canonical(artifact), encoding="utf-8")
    superseded = None
    if previous is not None and previous != dest:
        previous.unlink()
        superseded = previous
    return dest, superseded


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="postman_sync.py",
        description=(
            "Distil the vendor's public Postman collections into "
            "specs/payload-examples-*.json, or diff them against the committed copy."
        ),
        epilog=(
            "sources default to the vendor's public collection URLs; override with "
            "--source (single --release) or ECSDWAN_POSTMAN_SOURCE_9_3 ... _9_6.\n"
            "exit codes: 0 in sync / updated, 1 drift detected, 2 error or nothing to do."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--diff", action="store_true", help="report drift against the artifact")
    mode.add_argument("--update", action="store_true", help="rewrite the artifact")
    parser.add_argument(
        "--release",
        choices=(*RELEASES, "all"),
        default="all",
        help="which release collections to distil (default: all)",
    )
    parser.add_argument(
        "--source",
        metavar="URL_OR_FILE",
        help="collection source for a single --release (overrides the env var)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=DEFAULT_SPECS_DIR,
        metavar="DIR",
        help="artifact directory (default: specs/)",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="skip releases with no --source or env override instead of downloading",
    )
    parser.add_argument(
        "--retrieved",
        metavar="YYYY-MM-DD",
        help="provenance stamp for meta.retrieved (default: today)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout, seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get(ENV_INSECURE, "") == "1",
        help=f"skip TLS verification (or {ENV_INSECURE}=1)",
    )
    return parser


def resolve_source(release: str, override: str | None, *, no_fetch: bool) -> str | None:
    """``--source`` > env var > the public URL, unless ``--no-fetch`` forbids it."""
    if override:
        return override
    from_env = os.environ.get(ENV_SOURCE[release])
    if from_env:
        return from_env
    return None if no_fetch else PUBLIC_SOURCE[release]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    releases = list(RELEASES) if args.release == "all" else [args.release]
    if args.source and len(releases) != 1:
        parser.error("--source needs a single release: pass --release 9.6 (etc.)")

    report: JsonDict = {"mode": "update" if args.update else "diff", "releases": {}, "skipped": []}
    lines: list[str] = []
    loaded: list[tuple[str, JsonDict]] = []
    try:
        for release in releases:
            source = resolve_source(release, args.source, no_fetch=args.no_fetch)
            if not source:
                report["skipped"].append(release)
                lines.append(
                    f"{release}: skipped (no source; pass --source or set "
                    f"{ENV_SOURCE[release]})"
                )
                continue
            data = load_collection(source, timeout=args.timeout, insecure=args.insecure)
            loaded.append((release, data))
            requests = data.get("requests")
            n_requests = len(requests) if isinstance(requests, list) else 0
            report["releases"][release] = {"source": source, "requests": n_requests}
            lines.append(f"{release}: loaded {n_requests} requests from {source}")

        if not loaded:
            print("\n".join(lines), file=sys.stderr)
            print("postman_sync: nothing to do (no collection source)", file=sys.stderr)
            return 2

        artifact = distil(loaded, retrieved=args.retrieved)
        endpoints = artifact["endpoints"]
        report["endpoint_count"] = len(endpoints)
        lines.append(f"distilled {len(endpoints)} endpoints from {len(loaded)} release(s)")

        drift = False
        if args.update:
            dest, superseded = write_artifact(artifact, args.specs_dir)
            report["artifact"] = str(dest)
            report["superseded"] = str(superseded) if superseded else None
            lines.append(f"wrote {dest} ({len(endpoints)} endpoints)")
            if superseded:
                lines.append(f"removed superseded artifact {superseded}")
        else:
            artifact_path = find_artifact(args.specs_dir)
            if artifact_path is None:
                raise PostmanSyncError(
                    f"no payload-example artifact under {args.specs_dir} (run --update first)"
                )
            committed = json.loads(artifact_path.read_text(encoding="utf-8"))
            diff = diff_artifacts(committed, artifact)
            drift = diff.drift
            report["artifact"] = str(artifact_path)
            report.update(diff.as_json())
            lines.extend(_render_diff(artifact_path, committed, diff))
    except PostmanSyncError as exc:
        print(f"postman_sync: error: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(lines))
        if args.diff:
            print("drift detected" if drift else "payload examples in sync")
    return 1 if (args.diff and drift) else 0


def _render_diff(artifact_path: Path, committed: JsonDict, diff: ExampleDiff) -> list[str]:
    entries = committed.get("endpoints")
    n_committed = len(entries) if isinstance(entries, dict) else 0
    lines = [f"artifact: {artifact_path} ({n_committed} endpoints)"]
    for name, old, new in diff.metadata:
        lines.append(f"  ~ meta.{name}: {json.dumps(old)} -> {json.dumps(new)}")
    for label, marker, keys in (
        ("added endpoints", "+", diff.added),
        ("removed endpoints", "-", diff.removed),
        ("changed endpoints", "~", diff.changed),
    ):
        lines.extend(_render_list(label, marker, keys))
    return lines


def _render_list(label: str, marker: str, entries: list[str]) -> list[str]:
    lines = [f"  {label} ({len(entries)}):"] if entries else []
    lines.extend(f"    {marker} {entry}" for entry in _capped(entries))
    return lines


def _capped(entries: list[str]) -> list[str]:
    if len(entries) <= MAX_LISTED:
        return entries
    hidden = len(entries) - MAX_LISTED
    return [*entries[:MAX_LISTED], f"... and {hidden} more (use --json for the full list)"]


if __name__ == "__main__":
    sys.exit(main())
