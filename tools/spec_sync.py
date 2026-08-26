"""Fetch + diff OpenAPI specs against the ``specs/`` baseline (issue #25).

Tier-1 spec-ingestion entry point (docs/plugin-promotion.md): pull the
Orchestrator's published OpenAPI/Swagger document (and the appliance one where
available), compare it endpoint-by-endpoint against the vendored baseline, and
report added / removed / changed endpoints plus component-schema drift.
Later phases hang codegen off this diff; this tool only detects.

Usage:

    python tools/spec_sync.py --diff  [--spec orchestrator|appliance|both]
    python tools/spec_sync.py --update --spec orchestrator --source URL_OR_FILE

Sources are never guessed: give ``--source`` (URL or local file), or set
``ECSDWAN_SPEC_SOURCE_ORCHESTRATOR`` / ``ECSDWAN_SPEC_SOURCE_APPLIANCE``.
Targets without a configured source are skipped, so ``--diff`` works when
only one of the two specs is reachable.

Both sides of a diff are sanitized in memory before comparing, and
``--update`` sanitizes before writing: deployment hostnames in ``servers``,
``basePath``, and ``host`` are replaced with the ``*.example.com``
placeholders the current baselines use, so a real Orchestrator hostname can
never land in the repo (and never shows up as spurious drift).

Exit codes: 0 in sync / updated, 1 drift detected, 2 error or nothing to do.

The tool is standalone on purpose (stdlib only; httpx imported lazily for URL
sources) so file-based diffs run without the project venv.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SPECS_DIR = REPO_ROOT / "specs"

TARGETS = ("orchestrator", "appliance")
ENV_SOURCE = {t: f"ECSDWAN_SPEC_SOURCE_{t.upper()}" for t in TARGETS}
PLACEHOLDER_HOST = {t: f"{t}.example.com" for t in TARGETS}
# Same env conventions as pyecsdwan.config, duplicated as literals so the
# tool keeps working without the package installed.
ENV_API_KEY = "ECSDWAN_API_KEY"
ENV_INSECURE = "ECSDWAN_INSECURE"

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
#: Top-level fields compared as spec metadata (dotted = nested lookup).
METADATA_FIELDS = (
    "openapi", "swagger", "info.title", "info.version", "servers", "basePath", "host",
)
#: Human output caps per list; --json always carries everything.
MAX_LISTED = 50

JsonDict = dict[str, Any]


class SpecSyncError(Exception):
    """Fatal condition reported to the operator; exits with status 2."""


# ---------------------------------------------------------------------------
# Loading


def load_spec(source: str, *, timeout: float, insecure: bool) -> JsonDict:
    """Load an OpenAPI/Swagger JSON document from a URL or a local file."""
    if source.startswith(("http://", "https://")):
        raw = _fetch_url(source, timeout=timeout, insecure=insecure)
    else:
        path = Path(source)
        if not path.is_file():
            raise SpecSyncError(f"spec source not found: {source}")
        raw = path.read_text(encoding="utf-8")
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpecSyncError(f"{source}: not valid JSON ({exc})") from exc
    if not isinstance(spec, dict) or "paths" not in spec:
        raise SpecSyncError(f"{source}: not an OpenAPI/Swagger document (no 'paths' object)")
    return spec


def _fetch_url(url: str, *, timeout: float, insecure: bool) -> str:
    # Lazy import so file-based runs need no third-party packages at all.
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - venv always has httpx
        raise SpecSyncError("fetching a URL requires httpx (pip install httpx)") from exc
    headers = {}
    api_key = os.environ.get(ENV_API_KEY)
    if api_key:
        headers["X-Auth-Token"] = api_key
    try:
        response = httpx.get(
            url,
            headers=headers,
            timeout=timeout,
            verify=not insecure,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SpecSyncError(f"fetch failed: {url}: {exc}") from exc
    return response.text


def find_baseline(specs_dir: Path, target: str) -> Path | None:
    """Locate the vendored baseline for a target, e.g. orchestrator-openapi-*.json."""
    matches = sorted(specs_dir.glob(f"{target}-openapi-*.json"))
    if len(matches) > 1:
        names = ", ".join(m.name for m in matches)
        raise SpecSyncError(f"multiple {target} baselines under {specs_dir}: {names}")
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Sanitization


def sanitize_spec(spec: JsonDict, target: str) -> tuple[JsonDict, list[str]]:
    """Replace deployment hostnames with the baseline placeholder host.

    Covers ``servers[].url`` (reduced to url-only entries: description and
    variables can also name a deployment), the nonstandard full-URL
    ``basePath`` the Orchestrator publishes, and Swagger-2.0 ``host``. URL
    paths are preserved. Returns (sanitized copy, list of fields touched);
    only replaced top-level keys are copied, the rest is shared.
    """
    placeholder = PLACEHOLDER_HOST[target]
    out = dict(spec)
    touched: list[str] = []

    servers = spec.get("servers")
    if isinstance(servers, list):
        new_servers: list[JsonDict] = []
        for i, entry in enumerate(servers):
            url = entry.get("url", "") if isinstance(entry, dict) else ""
            new_url, changed = _sanitize_url(url, placeholder)
            if changed or entry != {"url": new_url}:
                touched.append(f"servers[{i}]")
            if {"url": new_url} not in new_servers:
                new_servers.append({"url": new_url})
        out["servers"] = new_servers

    base_path = spec.get("basePath")
    if isinstance(base_path, str):
        new_base, changed = _sanitize_url(base_path, placeholder)
        if changed:
            touched.append("basePath")
            out["basePath"] = new_base

    host = spec.get("host")
    if isinstance(host, str) and host != placeholder:
        touched.append("host")
        out["host"] = placeholder

    return out, touched


def _sanitize_url(url: str, placeholder: str) -> tuple[str, bool]:
    """Swap the host of an absolute URL for the placeholder, keeping the path.

    Relative URLs (e.g. a plain ``/gms/rest`` basePath) carry no hostname and
    pass through untouched. The scheme is normalized to https, matching the
    baselines. Handles the Orchestrator's quirky empty port (``host:/path``).
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return url, False
    sanitized = f"https://{placeholder}{parts.path}"
    return sanitized, sanitized != url


# ---------------------------------------------------------------------------
# Diffing


def endpoint_index(spec: JsonDict) -> dict[str, str]:
    """Map ``"METHOD /path"`` to a fingerprint of the effective operation.

    The fingerprint hashes the operation object together with path-level
    ``parameters`` (shared across methods), so a change to either marks the
    endpoint changed. ``$ref``s are hashed literally, not resolved — drift
    inside referenced component schemas is reported separately.
    """
    index: dict[str, str] = {}
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return index
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters")
        for method in HTTP_METHODS:
            if method not in item:
                continue
            payload: JsonDict = {"operation": item[method]}
            if shared is not None:
                payload["pathParameters"] = shared
            index[f"{method.upper()} {path}"] = _canonical_hash(payload)
    return index


def schema_index(spec: JsonDict) -> dict[str, str]:
    """Map component-schema name to content fingerprint (OpenAPI 3 or Swagger 2)."""
    components = spec.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        schemas = spec.get("definitions")
    if not isinstance(schemas, dict):
        return {}
    return {str(name): _canonical_hash(schema) for name, schema in schemas.items()}


def _canonical_hash(obj: Any) -> str:
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class SpecDiff:
    """Endpoint/schema/metadata delta between a baseline and a fetched spec."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    schemas_added: list[str] = field(default_factory=list)
    schemas_removed: list[str] = field(default_factory=list)
    schemas_changed: list[str] = field(default_factory=list)
    #: (field, baseline value, fetched value)
    metadata: list[tuple[str, Any, Any]] = field(default_factory=list)

    @property
    def drift(self) -> bool:
        return bool(
            self.added
            or self.removed
            or self.changed
            or self.schemas_added
            or self.schemas_removed
            or self.schemas_changed
            or self.metadata
        )

    def as_json(self) -> JsonDict:
        return {
            "drift": self.drift,
            "endpoints": {"added": self.added, "removed": self.removed, "changed": self.changed},
            "schemas": {
                "added": self.schemas_added,
                "removed": self.schemas_removed,
                "changed": self.schemas_changed,
            },
            "metadata": [
                {"field": name, "baseline": old, "fetched": new}
                for name, old, new in self.metadata
            ],
        }


def diff_specs(baseline: JsonDict, fetched: JsonDict) -> SpecDiff:
    """Compare two (already sanitized) specs by endpoint, schema, and metadata."""
    diff = SpecDiff()

    base_eps = endpoint_index(baseline)
    new_eps = endpoint_index(fetched)
    diff.added = sorted(set(new_eps) - set(base_eps), key=_endpoint_key)
    diff.removed = sorted(set(base_eps) - set(new_eps), key=_endpoint_key)
    diff.changed = sorted(
        (ep for ep in set(base_eps) & set(new_eps) if base_eps[ep] != new_eps[ep]),
        key=_endpoint_key,
    )

    base_schemas = schema_index(baseline)
    new_schemas = schema_index(fetched)
    diff.schemas_added = sorted(set(new_schemas) - set(base_schemas))
    diff.schemas_removed = sorted(set(base_schemas) - set(new_schemas))
    diff.schemas_changed = sorted(
        name
        for name in set(base_schemas) & set(new_schemas)
        if base_schemas[name] != new_schemas[name]
    )

    for fields in METADATA_FIELDS:
        old = _dotted(baseline, fields)
        new = _dotted(fetched, fields)
        if old != new:
            diff.metadata.append((fields, old, new))

    return diff


def _endpoint_key(endpoint: str) -> tuple[str, str]:
    method, _, path = endpoint.partition(" ")
    return (path, method)


def _dotted(spec: JsonDict, dotted: str) -> Any:
    node: Any = spec
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


# ---------------------------------------------------------------------------
# Update


def write_baseline(
    fetched: JsonDict, target: str, specs_dir: Path
) -> tuple[Path, list[str], Path | None]:
    """Sanitize and write the fetched spec as the new vendored baseline.

    Written byte-compatibly with the existing baselines: single-line compact
    JSON, ASCII-escaped, no trailing newline. The file is named after
    ``info.version``; a previous baseline with a different version is removed
    so ``specs/`` keeps exactly one baseline per target. Returns
    (written path, sanitized fields, removed superseded path or None).
    """
    sanitized, touched = sanitize_spec(fetched, target)
    info = fetched.get("info")
    version = str(info.get("version", "") if isinstance(info, dict) else "") or "unknown"
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", version)
    previous = find_baseline(specs_dir, target)
    dest = specs_dir / f"{target}-openapi-{slug}.json"
    specs_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(sanitized, separators=(",", ":")), encoding="utf-8")
    superseded = None
    if previous is not None and previous != dest:
        previous.unlink()
        superseded = previous
    return dest, touched, superseded


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spec_sync.py",
        description="Fetch the published OpenAPI spec and diff or refresh the specs/ baseline.",
        epilog=(
            "sources: --source, or ECSDWAN_SPEC_SOURCE_ORCHESTRATOR / "
            "ECSDWAN_SPEC_SOURCE_APPLIANCE (URL or file); unconfigured targets are skipped.\n"
            "exit codes: 0 in sync / updated, 1 drift detected, 2 error or nothing to do."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--diff", action="store_true", help="report drift against the baseline")
    mode.add_argument("--update", action="store_true", help="refresh the baseline (sanitized)")
    parser.add_argument(
        "--spec",
        choices=(*TARGETS, "both"),
        default="both",
        help="which spec to sync (default: both)",
    )
    parser.add_argument(
        "--source",
        metavar="URL_OR_FILE",
        help="spec source for a single --spec target (overrides the env var)",
    )
    parser.add_argument(
        "--specs-dir",
        type=Path,
        default=DEFAULT_SPECS_DIR,
        metavar="DIR",
        help="baseline directory (default: specs/)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable output"
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout, seconds")
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=os.environ.get(ENV_INSECURE, "") == "1",
        help=f"skip TLS verification (or {ENV_INSECURE}=1)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    targets = list(TARGETS) if args.spec == "both" else [args.spec]
    if args.source and len(targets) != 1:
        parser.error("--source needs a single target: pass --spec orchestrator|appliance")

    report: JsonDict = {
        "mode": "update" if args.update else "diff",
        "targets": {},
        "skipped": [],
    }
    lines: list[str] = []
    drift = False
    try:
        for target in targets:
            source = args.source or os.environ.get(ENV_SOURCE[target])
            if not source:
                report["skipped"].append(target)
                lines.append(
                    f"{target}: skipped (no source; pass --source or set {ENV_SOURCE[target]})"
                )
                continue
            fetched = load_spec(source, timeout=args.timeout, insecure=args.insecure)
            if args.update:
                dest, touched, superseded = write_baseline(fetched, target, args.specs_dir)
                report["targets"][target] = {
                    "source": source,
                    "written": str(dest),
                    "sanitized": touched,
                    "superseded": str(superseded) if superseded else None,
                }
                lines.extend(_render_update(target, source, dest, touched, superseded))
            else:
                baseline_path = find_baseline(args.specs_dir, target)
                if baseline_path is None:
                    raise SpecSyncError(
                        f"no {target} baseline under {args.specs_dir} (run --update first)"
                    )
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
                diff = diff_specs(
                    sanitize_spec(baseline, target)[0], sanitize_spec(fetched, target)[0]
                )
                drift = drift or diff.drift
                target_report = diff.as_json()
                target_report["source"] = source
                target_report["baseline"] = str(baseline_path)
                report["targets"][target] = target_report
                lines.extend(_render_diff(target, source, baseline_path, baseline, fetched, diff))
    except SpecSyncError as exc:
        print(f"spec_sync: error: {exc}", file=sys.stderr)
        return 2

    if not report["targets"]:
        print("\n".join(lines), file=sys.stderr)
        print("spec_sync: nothing to do (no spec source configured)", file=sys.stderr)
        return 2

    if args.as_json:
        report["drift"] = drift
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(lines))
        if args.diff:
            print("drift detected" if drift else "baselines in sync")
    return 1 if (args.diff and drift) else 0


def _render_update(
    target: str, source: str, dest: Path, touched: list[str], superseded: Path | None
) -> list[str]:
    sanitized = f"sanitized: {', '.join(touched)}" if touched else "nothing to sanitize"
    lines = [f"{target}: wrote {dest} from {source} ({sanitized})"]
    if superseded:
        lines.append(f"{target}: removed superseded baseline {superseded}")
    return lines


def _render_diff(
    target: str,
    source: str,
    baseline_path: Path,
    baseline: JsonDict,
    fetched: JsonDict,
    diff: SpecDiff,
) -> list[str]:
    lines = [
        f"== {target} ==",
        f"baseline: {baseline_path} ({_describe(baseline)})",
        f"fetched:  {source} ({_describe(fetched)})",
    ]
    for name, old, new in diff.metadata:
        lines.append(f"  ~ {name}: {json.dumps(old)} -> {json.dumps(new)}")
    for label, marker, entries in (
        ("added endpoints", "+", diff.added),
        ("removed endpoints", "-", diff.removed),
        ("changed endpoints", "~", diff.changed),
        ("added schemas", "+", diff.schemas_added),
        ("removed schemas", "-", diff.schemas_removed),
        ("changed schemas", "~", diff.schemas_changed),
    ):
        lines.extend(_render_list(label, marker, entries))
    verdict = "drift" if diff.drift else "in sync"
    lines.append(f"  {target}: {verdict}")
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


def _describe(spec: JsonDict) -> str:
    info = spec.get("info")
    version = info.get("version", "?") if isinstance(info, dict) else "?"
    paths = spec.get("paths")
    n_paths = len(paths) if isinstance(paths, dict) else 0
    return f"version {version}, {n_paths} paths, {len(endpoint_index(spec))} endpoints"


if __name__ == "__main__":
    sys.exit(main())
