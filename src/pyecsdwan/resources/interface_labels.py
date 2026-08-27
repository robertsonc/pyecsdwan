"""Interface labels — the Phase-0 trivial resource proving the full loop.

Orchestrator-scoped singleton: ``GET /gms/interfaceLabels`` returns the whole
label table, ``POST /gms/interfaceLabels`` replaces it completely. That makes
snapshot/restore exact — a textbook REVERSIBLE resource.

Shape (see docs/research and pyedgeconnect orch/_interface_labels.py)::

    {"wan": {"<label_id>": {"name": str, "active": bool, "topology": int}},
     "lan": {...}}

``topology`` 0 = full mesh, 2 = hub & spoke.

Constraints the server enforces. Both are checked *client-side first* (issue
#39), so the operator gets a named pre-flight error instead of a raw API 4xx:

* **label ids unique across wan+lan** — ``normalize()`` rejects a table that
  reuses an id on both sides, naming the id. It runs on user intent and on
  server state, so the error surfaces at plan time, before any write.
* **labels in use by an overlay cannot be removed** — an overlay references
  WAN label ids from ``wanPorts`` (primary/secondary/backup/crossConnect),
  ``internetPolicy.localBreakout`` (per-overlay and per-hub), and the
  ``label_<id>`` form of ``overlayFallbackOption``. ``canonicalize_desired()``
  (plan time) and ``apply()`` (right before the POST, catching an overlay
  that started using the label between plan and commit) both refuse a write
  that drops such a label, naming the label(s) and the overlay(s) using them.

``deleteDependencies`` (``POST /gms/interfaceLabels?deleteDependencies=``) is
the operator's consent to cascade: per the OpenAPI param doc it deletes the
removed labels *from the port profiles and templates using them*, and it is
the documented escape hatch for the overlay constraint. It used to be sent
unconditionally as ``true`` on every forward write, so a pure add or rename
silently carried a destructive cascade. It is now driven by an explicit
operator directive staged in the desired state::

    ec-cli set interface-labels global deleteDependencies true

The directive is *intent, not state*: the Orchestrator never returns it, so
``diff()`` strips it from both sides before comparing (otherwise it would show
as permanent phantom drift and fail post-apply verify). ``apply()`` still reads
it off ``diff.desired``. The rollback path keeps ``deleteDependencies=true``
unconditionally — a restore must land regardless of what the reverted change
attached to the labels it added.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register
from pyecsdwan.resolver import Resolver

_PATH = "/gms/interfaceLabels"
_SIDES = ("wan", "lan")

#: Operator directive staged in the desired state (see module docstring).
#: Never part of canonical state — diff() strips it from both sides.
_CASCADE_KEY = "deleteDependencies"

#: What deleteDependencies=true cascades into (per the OpenAPI param doc).
_CASCADE_TARGETS = "port profiles and templates"

#: Where an overlay config references WAN interface-label ids.
_WAN_PORT_KEYS = ("primary", "secondary", "backup", "crossConnect")
_LOCAL_BREAKOUT_KEYS = ("primary", "backup")
_FALLBACK_LABEL_PREFIX = "label_"


class LabelInUseError(ValueError):
    """A label the write removes is still referenced by an overlay.

    Subclasses ``ValueError`` so ``ec-cli`` renders it as a plain
    ``error: ...`` line (see cli/main.py's top-level handler) rather than a
    traceback.
    """


def _id_set(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value}
    return set()


def _internet_policies(overlay: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """The overlay's own internet policy plus every per-hub override."""
    policy = overlay.get("internetPolicy")
    if isinstance(policy, Mapping):
        yield policy
    hubs = overlay.get("hubInternetPolicies")
    if isinstance(hubs, Mapping):
        for hub in hubs.values():
            if isinstance(hub, Mapping):
                inner = hub.get("internetPolicy")
                if isinstance(inner, Mapping):
                    yield inner


def labels_referenced_by(overlay: Mapping[str, Any]) -> set[str]:
    """Every interface-label id one overlay config references."""
    used: set[str] = set()
    wan_ports = overlay.get("wanPorts")
    if isinstance(wan_ports, Mapping):
        for key in _WAN_PORT_KEYS:
            used |= _id_set(wan_ports.get(key))
    for policy in _internet_policies(overlay):
        breakout = policy.get("localBreakout")
        if isinstance(breakout, Mapping):
            for key in _LOCAL_BREAKOUT_KEYS:
                used |= _id_set(breakout.get(key))
    fallback = overlay.get("overlayFallbackOption")
    if isinstance(fallback, str) and fallback.startswith(_FALLBACK_LABEL_PREFIX):
        used.add(fallback[len(_FALLBACK_LABEL_PREFIX) :])
    return used


def overlay_label_usage(ctx: Ctx) -> dict[str, set[str]]:
    """Read-only view: label id -> names of the overlays referencing it.

    Reuses the resolver's overlay section (``GET /gms/overlays/config``, the
    same call ``resources/overlays.py`` reads through) rather than adding a
    new API surface. The cache is refreshed first: a pre-flight safety check
    must never clear a removal on the strength of a stale overlay list.
    """
    # Ctx declares a non-optional resolver, but unit contexts construct one
    # with None; widen the type so the guard is honest at runtime.
    resolver: Resolver | None = ctx.resolver
    if resolver is None:
        return {}
    try:
        resolver.refresh("overlays")
        overlays: list[Any] = list(resolver.overlays())
    except OrchApiError as exc:
        # A build without the overlays endpoint has no overlay to be in use by.
        if exc.status_code != 404:
            raise
        return {}
    usage: dict[str, set[str]] = {}
    for overlay in overlays:
        if not isinstance(overlay, Mapping):
            continue
        name = str(overlay.get("name") or overlay.get("id") or "?")
        for label_id in labels_referenced_by(overlay):
            usage.setdefault(label_id, set()).add(name)
    return usage


def _sort_ids(ids: set[str]) -> list[str]:
    """Numeric-first ordering; label ids are numeric strings on the wire."""
    return sorted(ids, key=lambda i: (0, int(i), "") if i.isdigit() else (1, 0, i))


def _label_ids(state: Any) -> set[str]:
    if not isinstance(state, Mapping):
        return set()
    return {
        str(label_id)
        for side in _SIDES
        for label_id in (state.get(side) or {})
    }


def _without_directive(state: CanonicalState) -> CanonicalState:
    if isinstance(state, dict) and _CASCADE_KEY in state:
        return {k: v for k, v in state.items() if k != _CASCADE_KEY}
    return state


class InterfaceLabels(Resource):
    kind = "interface-labels"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: A singleton table: there is no "the labels don't exist" state, so
    #: whole-resource delete is refused (delete individual labels instead).
    deletable = False
    desired_state_doc = (
        "wan/lan maps of label-id -> {name: str, active: bool, topology: int}; "
        "topology 0 = full mesh, 2 = hub & spoke. Label ids must be unique "
        "across wan+lan. Removing a label an overlay still references is "
        "refused unless deleteDependencies: true is staged, which also "
        "detaches removed labels from " + _CASCADE_TARGETS + "."
    )
    endpoints = (
        "orchestrator GET /gms/interfaceLabels",
        "orchestrator POST /gms/interfaceLabels",
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.get(_PATH)
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return {"wan": {}, "lan": {}}
        out: dict[str, Any] = {}
        #: label id -> the side that claimed it, so the uniqueness constraint
        #: is enforced across wan+lan (and against key-canonicalization
        #: collisions such as {1: ...} vs {"1": ...} on one side).
        claimed: dict[str, str] = {}
        for side in _SIDES:
            labels: dict[str, Any] = {}
            for label_id, label in (raw.get(side) or {}).items():
                key = str(label_id)
                if key in claimed:
                    if claimed[key] == side:
                        raise ValueError(
                            f"duplicate interface-label id {key!r} under {side} "
                            f"after key canonicalization"
                        )
                    raise ValueError(
                        f"interface-label id {key!r} appears under both "
                        f"{claimed[key]} and {side}; label ids must be unique "
                        f"across wan+lan (the Orchestrator rejects the table) "
                        f"— renumber one of the two entries"
                    )
                if not isinstance(label, dict):
                    raise ValueError(
                        f"{side}.{key} must be a mapping of label fields "
                        f"(name/active/topology), got {type(label).__name__}"
                    )
                entry = dict(label)
                # Fill the server-injected defaults on BOTH sides so a partial
                # `set` (e.g. name only) and the server's full record converge
                # — otherwise post-apply verify sees phantom drift and reverts.
                entry.setdefault("active", False)
                entry.setdefault("topology", 0)
                try:
                    entry["topology"] = int(entry["topology"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{side}.{key}.topology must be an integer "
                        f"(0=full mesh, 2=hub&spoke), got {entry['topology']!r}"
                    ) from exc
                entry["active"] = bool(entry["active"])
                claimed[key] = side
                labels[key] = entry
            out[side] = labels
        return out

    # -- desired-state shaping -------------------------------------------------

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Normalize intent, carry the cascade directive, pre-flight removals.

        ``normalize()`` only ever reads the wan/lan sides, so the directive is
        dropped there and re-attached here — it must reach ``apply()`` through
        ``diff.desired`` without ever being compared as state.
        """
        cascade = bool(desired.get(_CASCADE_KEY, False))
        out = self.normalize(dict(desired))
        assert isinstance(out, dict)
        # Plan-time pre-flight: the operator sees a named error from `compare`
        # / `commit` before a transaction is ever opened. apply() re-checks.
        self._check_removals(ctx, self.normalize(self.fetch(ctx, ref)), out, cascade)
        if cascade:
            out[_CASCADE_KEY] = True
        return out

    def diff(self, ref: Ref, current: CanonicalState, desired: CanonicalState) -> Diff:
        """Structural diff with the cascade directive excluded from comparison.

        The directive is operator intent, never server state: leaving it in
        would diff as a permanent add and make post-apply ``verify()`` report
        drift. ``Diff.desired`` still carries it for ``apply()``.
        """
        from pyecsdwan.diffing import structural_diff

        return Diff(
            ref=ref,
            entries=structural_diff(_without_directive(current), _without_directive(desired)),
            desired=desired,
            current=current,
        )

    # -- constraints -------------------------------------------------------------

    def _check_removals(
        self,
        ctx: Ctx,
        current: CanonicalState,
        desired: CanonicalState,
        cascade: bool,
    ) -> None:
        """Refuse a write that drops a label an overlay still references."""
        removed = _label_ids(current) - _label_ids(desired)
        if not removed or cascade:
            return
        usage = overlay_label_usage(ctx)
        offenders = {label_id: usage[label_id] for label_id in removed if label_id in usage}
        if not offenders:
            return
        detail = "; ".join(
            f"{label_id} (overlay {', '.join(sorted(offenders[label_id]))})"
            for label_id in _sort_ids(set(offenders))
        )
        raise LabelInUseError(
            f"cannot remove interface label(s) still in use by an overlay: {detail}. "
            f"Remove the reference from the overlay first, or stage the cascade "
            f"with `set {self.kind} global {_CASCADE_KEY} true` — which also "
            f"detaches the removed labels from {_CASCADE_TARGETS}."
        )

    # -- write side ---------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        desired = diff.desired if isinstance(diff.desired, dict) else {"wan": {}, "lan": {}}
        current = diff.current if isinstance(diff.current, dict) else {"wan": {}, "lan": {}}
        cascade = bool(desired.get(_CASCADE_KEY, False))
        # Authoritative re-check immediately before the write: an overlay may
        # have started referencing the label between plan and commit.
        self._check_removals(ctx, current, desired, cascade)
        removed = _label_ids(current) - _label_ids(desired)
        payload = {"wan": desired.get("wan", {}), "lan": desired.get("lan", {})}
        # Full-replace POST. deleteDependencies is the operator's explicit
        # consent to cascade (see module docstring) — it is NOT derived from
        # the presence of removals: deriving it would make every removal
        # cascade silently, which is exactly the unconditional-true behavior
        # this replaces, and would leave the server's in-use constraint
        # permanently bypassed.
        ctx.client.post(
            _PATH, payload, params={"deleteDependencies": "true" if cascade else "false"}
        )
        message = "interface labels replaced"
        if removed:
            message += f" (-{len(removed)} label(s): {', '.join(_sort_ids(removed))})"
        if cascade:
            message += (
                f"; deleteDependencies=true — removed labels detached "
                f"from {_CASCADE_TARGETS}"
            )
        else:
            message += "; deleteDependencies=false"
        return ApplyResult(ok=True, message=message)

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent (a degenerate GET at commit time)
            # must never be replayed as "POST an empty label table" — that
            # would wipe every label. Refuse loudly instead.
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty interface-label table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        # deleteDependencies=true: labels added by the change being reverted
        # may already be attached to a port profile, template or overlay; the
        # restore must remove them regardless of what picked them up.
        ctx.client.post(
            _PATH,
            {"wan": restored.get("wan", {}), "lan": restored.get("lan", {})},
            params={"deleteDependencies": "true"},
        )
        return ApplyResult(ok=True, message="interface labels restored from snapshot")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(InterfaceLabels())
