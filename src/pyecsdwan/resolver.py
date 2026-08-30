"""Name <-> ID resolver with a disk-backed cache.

Everything user-facing speaks names (appliance hostname, overlay name,
template group name); everything the API speaks is IDs (nePk, overlayId).
``normalize()`` implementations resolve through this cache so canonical
states are stable across renumbering.

Cache: ``~/.pyecsdwan/cache/<host>.json`` with a TTL; ``refresh()`` forces a
reload (wired to ``ec-cli cache refresh`` and shell completion misses).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.client import OrchClient


class ResolveError(Exception):
    pass


class ProjectedAway(KeyError):
    """A field this project deliberately does not cache was read (#9).

    Loud on purpose. The alternative — returning ``None`` for a dropped key —
    is a silent wrong answer of exactly the kind this repository keeps finding:
    an appliance's site would render blank rather than raising, and nothing
    would say why.
    """


#: The only ``GET /appliance`` fields written to the on-disk cache.
#:
#: The inventory response is far wider than this on a real Orchestrator —
#: serial numbers, addresses, software and license detail — and all of it used
#: to land verbatim in ``~/.pyecsdwan/cache/<host>.json``, a plaintext file
#: that outlives the process. None of it was read. Epic #9's definition of
#: done asks that nothing be written unredacted; the cheapest way to not leak a
#: field is to not store it.
#:
#: Each entry names what consumes it, because the set is only safe while it is
#: complete — a field dropped from here that something still reads becomes a
#: :class:`ProjectedAway` at runtime, not a blank cell.
APPLIANCE_FIELDS: frozenset[str] = frozenset(
    {
        # Resolver.ne_pk_for / appliance_name_for / appliance_names, and every
        # report that labels a row.
        "hostName",
        "nePk",
        # The `nePk or id` fallback every consumer writes. Not exercised by the
        # bundled mock, which always sends nePk — an empirical sweep of the
        # suite therefore missed it, and dropping it would have broken exactly
        # the fabrics that need it.
        "id",
        # `show appliances`, the shell's appliance table, fabric's by-site and
        # by-model breakdowns.
        "site",
        "model",
        # fabric.py's reachability and topology breakdowns.
        "state",
        "networkRole",
    }
)

#: Sections cached whole, and why. `overlays` is not an index of names: the
#: endpoint returns each overlay's whole *configuration*, which
#: `resources/interface_labels.py` walks in full to find labels in use. There
#: is no consumed-field subset to project it to.
UNPROJECTED_SECTIONS: frozenset[str] = frozenset({"overlays", "template_groups"})


def project_appliance(record: dict[str, Any]) -> dict[str, Any]:
    """Keep only :data:`APPLIANCE_FIELDS`, in the order the server sent them."""
    return {k: v for k, v in record.items() if k in APPLIANCE_FIELDS}


class ApplianceRecord(dict[str, Any]):
    """One cached appliance, which knows what it is *not* carrying.

    A plain dict cannot tell "the Orchestrator did not send this" from "we
    chose not to keep it", and the two need different answers: the first is
    data, the second is a bug in :data:`APPLIANCE_FIELDS`.
    """

    def _check(self, key: Any) -> None:
        if key not in APPLIANCE_FIELDS:
            raise ProjectedAway(
                f"{key!r} is not cached: the resolver keeps only "
                f"{sorted(APPLIANCE_FIELDS)} of the appliance inventory (#9). "
                f"Read it from a live GET /appliance, or add it to "
                f"pyecsdwan.resolver.APPLIANCE_FIELDS if it belongs in the cache"
            )

    def get(self, key: Any, default: Any = None) -> Any:
        self._check(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self._check(key)
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        self._check(key)
        return super().__contains__(key)


def _suggest(name: str, known: list[str]) -> str:
    import difflib

    close = difflib.get_close_matches(name, known, n=3)
    return f" (did you mean: {', '.join(close)}?)" if close else ""


class Resolver:
    def __init__(self, client: OrchClient, ttl: float = 300.0, cache_dir: Path | None = None):
        self.client = client
        self.ttl = ttl
        cache_dir = cache_dir if cache_dir is not None else config.cache_root()
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Keyed by the canonical origin, not the display host: two tenants on
        # one hostname must not share a resolved nePk cache (#63).
        self._origin = client.settings.origin
        self._cache_path = cache_dir / f"{config.origin_slug(self._origin)}.json"
        self._data: dict[str, Any] = {}
        self._load()

    # -- appliances ----------------------------------------------------------

    def appliances(self) -> list[dict[str, Any]]:
        """Cached inventory, projected to :data:`APPLIANCE_FIELDS`.

        Wrapped in :class:`ApplianceRecord` on the way out rather than only
        projected on the way in, so reading a dropped field raises here instead
        of quietly answering ``None`` three layers away.
        """
        value = self._section("appliances", self._fetch_appliances)
        if not isinstance(value, list):
            return []
        return [ApplianceRecord(a) for a in value if isinstance(a, dict)]

    def ne_pk_for(self, name: str) -> str:
        """Appliance hostname -> nePk; falls back to accepting a raw nePk."""
        if re.match(r"^\d{1,10}\.\w{1,10}$", name):
            return name
        matches = [a for a in self.appliances() if a.get("hostName") == name]
        if not matches:
            self.refresh("appliances")
            matches = [a for a in self.appliances() if a.get("hostName") == name]
        if not matches:
            known = [str(a.get("hostName")) for a in self.appliances() if a.get("hostName")]
            raise ResolveError(f"unknown appliance {name!r}{_suggest(name, known)}")
        if len(matches) > 1:
            pks = ", ".join(str(a.get("nePk") or a.get("id")) for a in matches)
            raise ResolveError(f"appliance name {name!r} is ambiguous (nePks: {pks})")
        ne_pk = matches[0].get("nePk") or matches[0].get("id")
        if not ne_pk:
            raise ResolveError(f"appliance {name!r} has no nePk in inventory response")
        return str(ne_pk)

    def appliance_name_for(self, ne_pk: str) -> str:
        for a in self.appliances():
            if str(a.get("nePk") or a.get("id")) == ne_pk:
                return str(a.get("hostName") or ne_pk)
        return ne_pk

    def appliance_names(self) -> list[str]:
        return sorted(str(a["hostName"]) for a in self.appliances() if a.get("hostName"))

    # -- overlays ------------------------------------------------------------

    def overlays(self) -> list[dict[str, Any]]:
        value = self._section("overlays", lambda: self.client.get("/gms/overlays/config") or [])
        return value if isinstance(value, list) else []

    def overlay_id_for(self, name: str) -> str:
        for ov in self.overlays():
            if ov.get("name") == name:
                return str(ov.get("id"))
        self.refresh("overlays")
        for ov in self.overlays():
            if ov.get("name") == name:
                return str(ov.get("id"))
        known = [str(o.get("name")) for o in self.overlays() if o.get("name")]
        raise ResolveError(f"unknown overlay {name!r}{_suggest(name, known)}")

    def overlay_name_for(self, overlay_id: str) -> str:
        for ov in self.overlays():
            if str(ov.get("id")) == str(overlay_id):
                return str(ov.get("name") or overlay_id)
        return str(overlay_id)

    # -- template groups -----------------------------------------------------

    def template_groups(self) -> list[str]:
        value = self._section("template_groups", self._fetch_template_groups)
        return value if isinstance(value, list) else []

    def template_group_exists(self, name: str) -> bool:
        if name in self.template_groups():
            return True
        self.refresh("template_groups")  # refresh-on-miss: a just-created group
        return name in self.template_groups()

    def _fetch_template_groups(self) -> list[str]:
        raw = self.client.get("/template/templateGroups")
        if isinstance(raw, list):
            return sorted(
                str(g.get("name")) for g in raw if isinstance(g, dict) and g.get("name")
            )
        if isinstance(raw, dict):
            # Only treat dict values that look like group objects as groups —
            # never the field names of a single group object (which would
            # fabricate "name"/"templates" as phantom group names).
            names = [
                str(v.get("name") or k)
                for k, v in raw.items()
                if isinstance(v, dict) and ("name" in v or "templates" in v)
            ]
            return sorted(names)
        return []

    # -- cache plumbing ------------------------------------------------------

    def refresh(self, section: str | None = None) -> None:
        if section is None:
            self._data = {}
        else:
            self._data.pop(section, None)
        self._save()

    def _fetch_appliances(self) -> list[dict[str, Any]]:
        """Projected before it is stored, so an unwanted field is never written
        to disk *or* held in memory — one rule, not two."""
        raw = self.client.get("/appliance")
        if not isinstance(raw, list):
            return []
        return [project_appliance(a) for a in raw if isinstance(a, dict)]

    def cached(self, name: str, fetch: Any) -> Any:
        """Public entry to the same TTL cache the resolved sections use.

        Exists so callers outside this module — `ownership`, which reads the
        Orchestrator's template vocabulary once per run rather than once per
        planned item — get the origin keying and the staleness bound for free
        instead of growing a second cache with neither (#63).
        """
        return self._section(name, fetch)

    def _section(self, name: str, fetch: Any) -> Any:
        entry = self._data.get(name)
        if entry and (time.time() - entry.get("ts", 0)) < self.ttl:
            return entry["value"]
        value = fetch()
        self._data[name] = {"ts": time.time(), "value": value}
        self._save()
        return value

    #: Key under which the cache records which Orchestrator it came from.
    #: Leading underscore so it cannot collide with a section name.
    ORIGIN_KEY = "_origin"

    def _load(self) -> None:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}
            return
        if not isinstance(data, dict) or data.get(self.ORIGIN_KEY) != self._origin:
            # The file name carries a digest of the origin, so this only fires
            # for a cache moved, restored from a backup, or written by a build
            # before #63. Discarded rather than refused: a cache is derivable,
            # and one refetch is a far smaller cost than resolving a name to
            # another fabric's nePk and then writing to it.
            self._data = {}
            return
        # Stripped after the check, so nothing downstream can ever mistake the
        # marker for a cache section.
        self._data = {k: v for k, v in data.items() if k != self.ORIGIN_KEY}

    def _save(self) -> None:
        try:
            self._cache_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._cache_path.parent), prefix=self._cache_path.name + ".", suffix=".tmp"
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({**self._data, self.ORIGIN_KEY: self._origin}, fh)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self._cache_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError:
            pass
