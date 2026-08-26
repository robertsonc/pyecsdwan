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
import re
import time
from pathlib import Path
from typing import Any

from pyecsdwan import config
from pyecsdwan.client import OrchClient


class ResolveError(Exception):
    pass


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
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", client.settings.host)
        self._cache_path = cache_dir / f"{safe}.json"
        self._data: dict[str, Any] = {}
        self._load()

    # -- appliances ----------------------------------------------------------

    def appliances(self) -> list[dict[str, Any]]:
        value = self._section("appliances", self._fetch_appliances)
        return value if isinstance(value, list) else []

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
        def fetch() -> list[str]:
            raw = self.client.get("/template/templateGroups")
            names: list[str] = []
            if isinstance(raw, dict):
                names = sorted(raw)
            elif isinstance(raw, list):
                names = sorted(
                    str(g.get("name")) for g in raw if isinstance(g, dict) and g.get("name")
                )
            return names

        value = self._section("template_groups", fetch)
        return value if isinstance(value, list) else []

    # -- cache plumbing ------------------------------------------------------

    def refresh(self, section: str | None = None) -> None:
        if section is None:
            self._data = {}
        else:
            self._data.pop(section, None)
        self._save()

    def _fetch_appliances(self) -> list[dict[str, Any]]:
        raw = self.client.get("/appliance")
        return raw if isinstance(raw, list) else []

    def _section(self, name: str, fetch: Any) -> Any:
        entry = self._data.get(name)
        if entry and (time.time() - entry.get("ts", 0)) < self.ttl:
            return entry["value"]
        value = fetch()
        self._data[name] = {"ts": time.time(), "value": value}
        self._save()
        return value

    def _load(self) -> None:
        try:
            self._data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        try:
            tmp = self._cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data), encoding="utf-8")
            tmp.replace(self._cache_path)
        except OSError:
            pass
