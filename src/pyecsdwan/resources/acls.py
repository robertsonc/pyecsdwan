"""ACLs, IP objects and AppExpress application definitions (#31).

These are the building blocks security and route policies reference, and they
span **two scopes**. Issue #31's original path list was partly wrong; every
path below was re-verified against the two vendored specs
(``specs/appliance-openapi-7.2.0.json``, ``specs/orchestrator-openapi-7.2.0.json``)
and, where noted, probed read-only against a live lab Orchestrator.

Endpoint facts
--------------

* **ACLs are appliance (ECOS) scope, not orchestrator scope.** The
  Orchestrator's own ``GET /acls?nePk=`` is read-only (the vendored SDK's
  ``orch/_acls.py`` exposes nothing else, and the orchestrator spec declares
  only ``get`` on that path) — there is no orchestrator write path for ACLs.
  Writes therefore go through the appliance proxy
  (``ctx.client.appliance_request``), exactly like ``resources/routes.py``
  and ``resources/vrrp.py``:

  - ``GET acls`` — the whole ACL table for one appliance.
  - ``POST acls`` — body ``{"data": {<aclName>: {"entry": {...}}},
    "options": {"merge": bool, "delDependent": bool}}`` (the appliance spec
    states both option flags are mandatory).
  - ``GET dependency/acl/{aclName}`` — what still references an ACL. Used
    here as a *pre-flight* so removing an in-use ACL fails with a named
    error instead of a raw API rejection (``resources/interface_labels.py``'s
    ``_check_removals`` is the precedent).
  - ``DELETE acls/{aclName}`` exists but is deliberately **not** used: the
    ``merge=false`` full-table POST already expresses a removal atomically,
    and a DELETE-then-POST pair could not be rolled back as one operation.

* ``/ipObjects`` alone does not exist. The real orchestrator paths are
  ``/ipObjects/addressGroup`` and ``/ipObjects/serviceGroup``
  (GET/PUT/POST/DELETE).

* ``/applicationDefinition/appExpressGroup/config`` (GET/POST/DELETE) and
  ``/applicationDefinition/appExpressGroup/association`` (GET/POST) are
  correct as the issue wrote them, orchestrator scope.

Live-confirmed vs spec-derived
------------------------------

**Live-confirmed (read-only capture this session, high confidence): the
appliance ACL table only.** Four ACLs on the probed appliance::

    {"Overlay_BulkApps": {
        "entry": {"1000": {"self": 1000, "comment": "", "gms_marked": false,
                           "permit": true, "application": "..."},
                  "1010": {...}},
        "qmap": ..., "rmap": ...},
     "Overlay_CriticalApps": {...}, ...}

ACL name -> ``entry`` -> numeric-priority -> rule. ``qmap``/``rmap`` sit
alongside ``entry`` and are *server-derived* references to the QoS/route maps
that use this ACL (the appliance spec says so in as many words: "'rmap' give
info about the routemap which uses this ACL"), and the spec's own ``ACL``
schema declares ``entry`` as the only writable member — so ``normalize()``
drops them from canonical state and ``apply()`` never posts them.

**Spec-derived, NOT live-confirmed:** everything orchestrator-scope in this
module. ``/ipObjects/addressGroup``, ``/ipObjects/serviceGroup``,
``/applicationDefinition/appExpressGroup/config`` and ``.../association`` were
all reachable but **empty** on the lab (``[]``, ``[]``, ``{}``, ``[]``), so
their shapes come from the vendored 7.2.0 orchestrator spec (cross-checked
against the vendored SDK's ``orch/_ip_objects.py``, which builds the same
bodies). No field is used here that the spec does not declare; unknown fields
found on a live server pass through ``normalize()`` untouched.

Reuse of the security_policy helpers
------------------------------------

``_strip_meta`` is imported from ``resources/security_policy.py`` and used
verbatim — it is a generic recursive ``self``/``gms_marked`` stripper and fits
the ACL nesting exactly (``resources/appliance_zones.py`` set the precedent
for importing it). Its sibling ``_inject_self`` is **not** reused: it is
hard-coded to the security-maps nesting (map -> zonePair -> ``prio`` -> rule)
and would write ``{"self": "entry"}`` into an ACL's ``entry`` container, which
is a different shape one level shallower. ``_inject_acl_self`` below does the
ACL-shaped equivalent (rule-level ``self`` echo only — the captured sample
carries ``self`` on rules and nowhere else).

Ownership
---------

``gms_marked`` is confirmed present per ACL rule and is this resource's
``managed_by()`` precision signal, using the same "gms_marked first,
template-section fallback" order ``resources/routes.py`` uses: a rule the
fabric itself flagged is server-owned regardless of what any template
selects, checked before the coarser template association x selection join.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from typing import Any

import structlog

from pyecsdwan import ownership
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
from pyecsdwan.resources.security_policy import _strip_meta

log = structlog.get_logger("pyecsdwan.resources.acls")

# -- appliance (ECOS) paths ---------------------------------------------------
_ACLS_PATH = "acls"
_ACL_DEPENDENCY_PATH = "dependency/acl"

# -- orchestrator paths -------------------------------------------------------
_ADDRESS_GROUP_PATH = "/ipObjects/addressGroup"
_SERVICE_GROUP_PATH = "/ipObjects/serviceGroup"
_APPEXPRESS_CONFIG_PATH = "/applicationDefinition/appExpressGroup/config"
_APPEXPRESS_ASSOCIATION_PATH = "/applicationDefinition/appExpressGroup/association"

#: Server-derived reference info the appliance returns alongside each ACL's
#: rules; never user intent, never posted back (see module docstring).
_ACL_DERIVED_KEYS = ("qmap", "rmap")

#: Operator directive staged in the desired state — the ECOS ``delDependent``
#: option, this resource's consent-to-cascade flag. Never server state, so
#: diff() strips it from both sides (interface_labels.py's pattern).
_DEL_DEPENDENT_KEY = "delDependent"


class AclInUseError(ValueError):
    """An ACL the write removes is still referenced by another object.

    Subclasses ``ValueError`` so ``ec-cli`` renders it as a plain
    ``error: ...`` line rather than a traceback (same as
    ``interface_labels.LabelInUseError``).
    """


# == appliance-scope ACLs =====================================================


def _priority_sort_key(priority: str) -> tuple[int, int, str]:
    """Numeric-first ordering; ACL priorities are numeric strings on the wire.

    A non-numeric key is sorted into a stable trailing bucket rather than
    crashing normalize() — defensive only, never seen live.
    """
    return (0, int(priority), "") if priority.isdigit() else (1, 0, priority)


def _acl_entry(acl_name: str, raw_entry: Any) -> dict[str, Any]:
    """Canonicalize one ACL's ``entry`` map (priority -> rule)."""
    if not isinstance(raw_entry, Mapping):
        raise ValueError(
            f"acl {acl_name!r}.entry must be a mapping of priority -> rule, "
            f"got {type(raw_entry).__name__}"
        )
    rules: dict[str, Any] = {}
    for priority, rule in raw_entry.items():
        key = str(priority)
        if not isinstance(rule, Mapping):
            raise ValueError(
                f"acl {acl_name!r}.entry.{key} must be a mapping of rule "
                f"fields (permit, application, ...), got {type(rule).__name__}"
            )
        if key.isdigit():
            key = str(int(key))  # canonicalize leading zeros
        if key in rules:
            raise ValueError(
                f"duplicate priority {key!r} in acl {acl_name!r} after key canonicalization"
            )
        # _strip_meta drops the `self` echo and the `gms_marked` bookkeeping
        # flag recursively; managed_by() reads gms_marked off the raw fetch.
        stripped = _strip_meta(dict(rule))
        assert isinstance(stripped, dict)
        rules[key] = stripped
    return {key: rules[key] for key in sorted(rules, key=_priority_sort_key)}


def _acl_record(acl_name: str, raw_acl: Any) -> dict[str, Any]:
    """Canonicalize one ACL: keep ``entry`` (plus unknown fields), drop the
    server-derived ``qmap``/``rmap`` reference info."""
    if not isinstance(raw_acl, Mapping):
        raise ValueError(
            f"acl {acl_name!r} must be a mapping of acl fields (entry, ...), "
            f"got {type(raw_acl).__name__}"
        )
    out: dict[str, Any] = {"entry": _acl_entry(acl_name, raw_acl.get("entry") or {})}
    for key, value in raw_acl.items():
        name = str(key)
        if name == "entry" or name in _ACL_DERIVED_KEYS or name == "self":
            continue
        out[name] = value  # unknown fields pass through (promotion checklist)
    return out


def _inject_acl_self(acls: Mapping[str, Any]) -> dict[str, Any]:
    """Re-add the rule-level ``self`` echo the captured ACL shape carries.

    The ACL-shaped counterpart of ``security_policy._inject_self`` (which is
    hard-coded to the map/zonePair/prio nesting and does not fit here — see
    the module docstring). The captured sample echoes ``self`` on rules only,
    as the rule's own numeric priority, so nothing is injected above that.
    """
    out: dict[str, Any] = {}
    for acl_name, record in acls.items():
        acl = copy.deepcopy(record) if isinstance(record, Mapping) else {}
        entry = acl.get("entry")
        if isinstance(entry, dict):
            for priority, rule in entry.items():
                if isinstance(rule, dict):
                    rule.setdefault(
                        "self", int(priority) if str(priority).isdigit() else priority
                    )
        out[str(acl_name)] = acl
    return out


def _has_gms_marked(raw: RawState) -> bool:
    if not isinstance(raw, dict):
        return False
    for record in raw.values():
        if not isinstance(record, Mapping):
            continue
        entry = record.get("entry")
        if not isinstance(entry, Mapping):
            continue
        for rule in entry.values():
            if isinstance(rule, Mapping) and bool(rule.get("gms_marked")):
                return True
    return False


def _dependency_users(raw: Any) -> list[str]:
    """Flatten a ``GET dependency/acl/{name}`` response into referencing names.

    The appliance spec declares no response schema for this endpoint ("No
    response"), and the lab appliance had no dependent objects to capture, so
    this parser is deliberately shape-tolerant and **fails closed**: anything
    non-empty it cannot flatten still counts as a reference, so a pre-flight
    never green-lights a removal it did not understand.
    """
    users: list[str] = []
    if raw is None:
        return users
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            label = str(key)
            if isinstance(value, Mapping):
                users.extend(f"{label} {name}" for name in sorted(str(n) for n in value))
            elif isinstance(value, (list, tuple)):
                users.extend(f"{label} {name}" for name in sorted(str(n) for n in value))
            elif isinstance(value, bool):
                if value:
                    users.append(label)
            elif value not in (None, "", 0):
                users.append(f"{label} {value}")
        return sorted(users)
    if isinstance(raw, (list, tuple)):
        return sorted(str(item) for item in raw if item not in (None, ""))
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    return [str(raw)]


def _acl_names(state: CanonicalState) -> set[str]:
    if not isinstance(state, Mapping):
        return set()
    table = state.get("acl")
    return set(table) if isinstance(table, Mapping) else set()


def _without_directive(state: CanonicalState) -> CanonicalState:
    if isinstance(state, dict) and _DEL_DEPENDENT_KEY in state:
        return {k: v for k, v in state.items() if k != _DEL_DEPENDENT_KEY}
    return state


class Acls(Resource):
    kind = "appliance/acl"
    scope = Scope.APPLIANCE
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: An ACL rule's address/service matchers name the orchestrator-scope
    #: ip-object groups (the spec calls them "ACL address group"/"ACL service
    #: group" — they exist to be referenced from here), so within one
    #: changeset the groups must be created first and, reversed, ACL removals
    #: apply before the groups they point at are removed.
    dependencies = ("ip-address-group", "ip-service-group")
    #: The ACL table always exists (possibly empty) on any appliance — there
    #: is no "absent" state for the table itself, so whole-resource delete is
    #: refused; delete individual `acl.<name>` entries instead.
    deletable = False
    desired_state_doc = (
        "acl: map of ACL name -> {entry: {priority: {permit, application, "
        "comment, ...}}}. Full-table replace against ECOS 'acls' with "
        "options.merge=false, persisted via save-changes. Server-derived "
        "qmap/rmap are read-only and never posted. Removing an ACL that "
        "GET dependency/acl/<name> reports as in use is refused unless "
        "delDependent: true is staged."
    )

    @staticmethod
    def _ne_pk(ctx: Ctx, ref: Ref) -> str:
        if ref.appliance is None:
            raise ValueError(f"{ref.kind} is appliance-scoped; ref.appliance is required")
        return ctx.resolver.ne_pk_for(ref.appliance)

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        raw = ctx.client.appliance_request("GET", self._ne_pk(ctx, ref), _ACLS_PATH)
        if raw is None or isinstance(raw, dict):
            return raw
        raise ValueError(f"unexpected acls response shape from {ref}: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return {"acl": {}}
        # The appliance returns the table unwrapped ({aclName: {...}}), while
        # canonical state / user intent round-trips back through here wrapped
        # as {"acl": {...}}. Same unwrap convention as appliance_zones.py.
        table: Any = raw.get("acl", raw) if "acl" in raw else raw
        if not isinstance(table, Mapping):
            raise ValueError(
                f"acls must be a mapping of ACL name -> {{entry: ...}}, "
                f"got {type(table).__name__}"
            )
        acls = {
            str(name): _acl_record(str(name), record)
            for name, record in table.items()
            if str(name) != _DEL_DEPENDENT_KEY
        }
        return {"acl": {name: acls[name] for name in sorted(acls)}}

    def managed_by(self, ctx: Ctx, ref: Ref) -> str | None:
        ne_pk = self._ne_pk(ctx, ref)
        # Per-rule precision first: a rule the fabric itself flagged
        # gms_marked is server-owned regardless of what any template selects
        # (routes.py's pattern).
        if _has_gms_marked(self.fetch(ctx, ref)):
            return "gms (gms_marked ACL rule present on this appliance)"
        return ownership.owning_group(ctx, self.kind, ne_pk)

    # -- desired-state shaping -------------------------------------------------

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Normalize intent, carry the cascade directive, pre-flight removals.

        ``normalize()`` only ever reads the ACL table, so the directive is
        dropped there and re-attached here — it must reach ``apply()`` through
        ``diff.desired`` without ever being compared as state.
        """
        cascade = bool(desired.get(_DEL_DEPENDENT_KEY, False))
        out = self.normalize(dict(desired))
        assert isinstance(out, dict)
        # Plan-time pre-flight: the operator sees the named error from
        # `compare`/`commit` before a transaction is ever opened; apply()
        # re-checks immediately before the write.
        self._check_removals(ctx, ref, self.normalize(self.fetch(ctx, ref)), out, cascade)
        if cascade:
            out[_DEL_DEPENDENT_KEY] = True
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

    # -- constraints -----------------------------------------------------------

    def acl_dependencies(self, ctx: Ctx, ref: Ref, acl_name: str) -> list[str]:
        """What still references ``acl_name`` on this appliance (read-only)."""
        ne_pk = self._ne_pk(ctx, ref)
        try:
            raw = ctx.client.appliance_request(
                "GET", ne_pk, f"{_ACL_DEPENDENCY_PATH}/{acl_name}"
            )
        except OrchApiError as exc:
            # No dependency record is "nothing references it", not an error.
            if exc.status_code in (204, 404):
                return []
            raise
        return _dependency_users(raw)

    def _check_removals(
        self,
        ctx: Ctx,
        ref: Ref,
        current: CanonicalState,
        desired: CanonicalState,
        cascade: bool,
    ) -> None:
        """Refuse a write that drops an ACL something still references."""
        removed = sorted(_acl_names(current) - _acl_names(desired))
        if not removed or cascade:
            return
        offenders: dict[str, list[str]] = {}
        for acl_name in removed:
            users = self.acl_dependencies(ctx, ref, acl_name)
            if users:
                offenders[acl_name] = users
        if not offenders:
            return
        detail = "; ".join(
            f"{acl_name} (used by {', '.join(offenders[acl_name])})"
            for acl_name in sorted(offenders)
        )
        raise AclInUseError(
            f"cannot remove ACL(s) still in use on {ref.appliance}: {detail}. "
            f"Remove the reference first, or stage the cascade with "
            f"`set {self.kind} {ref.name} {_DEL_DEPENDENT_KEY} true "
            f"--appliance {ref.appliance}` — which lets the appliance delete "
            f"the dependent objects along with the ACL."
        )

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            # deletable=False makes build_plan refuse this first; guard anyway
            # so a direct apply() can never POST an empty ACL table.
            return ApplyResult(
                ok=False,
                message=(
                    f"{ref.kind} is a singleton table; refusing to POST an empty "
                    f"ACL table (delete individual acl.<name> entries instead)"
                ),
            )
        assert isinstance(diff.desired, dict)
        current = diff.current if isinstance(diff.current, dict) else {"acl": {}}
        cascade = bool(diff.desired.get(_DEL_DEPENDENT_KEY, False))
        # Authoritative re-check right before the write: something may have
        # started referencing the ACL between plan and commit.
        self._check_removals(ctx, ref, current, diff.desired, cascade)
        return self._replace(ctx, ref, diff.desired, cascade, "apply")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            # A snapshot recorded as absent (a degenerate GET at commit time)
            # must never be replayed as "POST an empty ACL table" — that would
            # wipe every ACL. Refuse loudly instead (interface_labels.py's
            # reasoning).
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty ACL table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        # delDependent=true regardless: an ACL added by the change being
        # reverted may already have been picked up by a route/QoS map, and the
        # restore must remove it anyway.
        return self._replace(ctx, ref, restored, True, "rollback")

    def _replace(
        self,
        ctx: Ctx,
        ref: Ref,
        desired: Mapping[str, Any],
        cascade: bool,
        verb: str,
    ) -> ApplyResult:
        ne_pk = self._ne_pk(ctx, ref)
        table = desired.get("acl") or {}
        payload = _inject_acl_self(table if isinstance(table, Mapping) else {})
        ctx.client.appliance_request(
            "POST",
            ne_pk,
            _ACLS_PATH,
            json_body={
                "data": payload,
                # merge=false: this body is the complete ACL table for the
                # appliance, so a removal is expressed by absence and the
                # snapshot/restore round-trip is exact. delDependent is the
                # operator's explicit consent to cascade — never derived from
                # the presence of removals, which would make every removal
                # cascade silently.
                "options": {"merge": False, "delDependent": cascade},
            },
        )
        rules = sum(
            len(acl.get("entry", {})) for acl in payload.values() if isinstance(acl, Mapping)
        )
        log.debug(
            "acls_replace", ref=str(ref), verb=verb, acls=len(payload), rules=rules,
            del_dependent=cascade,
        )
        outcome = ctx.save_changes([ne_pk], f"acls {verb}: {ref}")
        message = (
            f"acls {verb} on {ne_pk}: {len(payload)} ACL(s), {rules} rule(s); "
            f"delDependent={'true' if cascade else 'false'}"
        )
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                jobs=[outcome],
                message=(
                    f"{message} — not persisted, save-changes "
                    f"{outcome.state}: {outcome.detail}"
                ),
            )
        return ApplyResult(ok=True, jobs=[outcome], message=f"{message}, persisted")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [
            Ref(kind=self.kind, name="global", appliance=name)
            for name in ctx.resolver.appliance_names()
        ]


# == orchestrator-scope ip objects ============================================
#
# Everything below is SPEC-DERIVED (the lab returned empty collections) — see
# the module docstring.


def _rule_sort_key(rule: Mapping[str, Any]) -> str:
    """Stable, content-derived ordering for a group's rule list.

    Canonical lists must be stably sorted: ``diffing.structural_diff``
    compares lists positionally. Neither the spec nor the vendored SDK gives
    rule order any meaning for these set-union style objects, so sorting by
    canonical content is safe and makes both sides of a diff agree regardless
    of the order the server hands them back in.
    """
    return json.dumps(rule, sort_keys=True, default=str)


def _canonical_group_rule(
    where: str,
    rule: Any,
    list_fields: Sequence[str],
    required: Sequence[str],
) -> dict[str, Any]:
    if not isinstance(rule, Mapping):
        raise ValueError(f"{where} must be a mapping of rule fields, got {type(rule).__name__}")
    out: dict[str, Any] = {str(k): v for k, v in rule.items()}
    for field in list_fields:
        values = out.get(field) or []
        if not isinstance(values, (list, tuple)):
            raise ValueError(
                f"{where}.{field} must be a list of strings, got {type(values).__name__}"
            )
        # Filled on both sides so a partial `set` and the server's full record
        # converge instead of drifting forever.
        out[field] = sorted(str(v) for v in values)
    out["comment"] = str(out.get("comment") or "")
    for field in required:
        value = out.get(field)
        if value is None or str(value) == "":
            raise ValueError(f"{where} is missing the required field {field!r}")
        out[field] = str(value)
    return out


def _canonical_group(
    raw: Mapping[str, Any],
    type_code: str,
    list_fields: Sequence[str],
    required: Sequence[str],
) -> dict[str, Any]:
    name = raw.get("name")
    if name is None or str(name) == "":
        raise ValueError("ip-object group is missing the required field 'name'")
    rules_raw = raw.get("rules")
    if rules_raw is None:
        rules_raw = []
    if not isinstance(rules_raw, (list, tuple)):
        raise ValueError(
            f"group {name!r}.rules must be a list of rule objects, "
            f"got {type(rules_raw).__name__}"
        )
    rules = [
        _canonical_group_rule(f"group {name!r}.rules[{i}]", rule, list_fields, required)
        for i, rule in enumerate(rules_raw)
    ]
    rules.sort(key=_rule_sort_key)
    out: dict[str, Any] = {"name": str(name), "type": type_code, "rules": rules}
    for key, value in raw.items():
        field = str(key)
        if field not in ("name", "type", "rules"):
            out[field] = value  # unknown fields pass through
    return out


class _IpObjectGroup(Resource):
    """Shared implementation for the two ``/ipObjects/*`` group kinds.

    Both are named, orchestrator-scope objects with identical lifecycle
    (POST create / PUT replace / DELETE by ``name`` query param) and differ
    only in path, ``type`` discriminator and rule fields.
    """

    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    _path: str = ""
    _type_code: str = ""
    _list_fields: tuple[str, ...] = ()
    _required: tuple[str, ...] = ()

    # -- read side ------------------------------------------------------------

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            raw = ctx.client.get(self._path, params={"name": ref.name})
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return None
            raise
        return self._select(raw, ref.name)

    @staticmethod
    def _select(raw: Any, name: str) -> dict[str, Any] | None:
        """Pick one group out of whatever the name-filtered GET returned.

        The spec types the address-group GET as a single object and the
        service-group GET as an array, and the lab returned an empty array for
        both — so accept either and fail neither.
        """
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, Mapping) and str(item.get("name")) == name:
                    return dict(item)
            return None
        if isinstance(raw, Mapping) and raw:
            return dict(raw)
        return None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict) or not raw:
            return None
        return _canonical_group(raw, self._type_code, self._list_fields, self._required)

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """User intent may omit the redundant ``name``/``type`` discriminators;
        the ref supplies the name and the spec fixes the type."""
        body = dict(desired)
        body.setdefault("name", ref.name)
        body["type"] = self._type_code
        return self.normalize(body)

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            ctx.client.delete(self._path, params={"name": ref.name})
            return ApplyResult(ok=True, message=f"{self.kind} {ref.name!r} deleted")
        assert isinstance(diff.desired, dict)
        body = self._body(ref, diff.desired)
        if diff.current is None:
            # POST is documented as create-or-replace; PUT as replace-only.
            ctx.client.post(self._path, body)
            return ApplyResult(ok=True, message=f"{self.kind} {ref.name!r} created")
        ctx.client.put(self._path, body)
        return ApplyResult(ok=True, message=f"{self.kind} {ref.name!r} replaced")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        if restored is None:
            try:
                ctx.client.delete(self._path, params={"name": ref.name})
            except OrchApiError as exc:
                if exc.status_code == 404:
                    return ApplyResult(ok=True, changed=False, message="already absent")
                raise
            return ApplyResult(ok=True, message=f"{self.kind} {ref.name!r} removed (compensate)")
        assert isinstance(restored, dict)
        body = self._body(ref, restored)
        if self.fetch(ctx, ref) is None:
            ctx.client.post(self._path, body)
        else:
            ctx.client.put(self._path, body)
        return ApplyResult(ok=True, message=f"{self.kind} {ref.name!r} restored")

    def _body(self, ref: Ref, desired: Mapping[str, Any]) -> dict[str, Any]:
        body = dict(desired)
        body["name"] = ref.name
        body["type"] = self._type_code
        body.setdefault("rules", [])
        return body

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        raw = ctx.client.get(self._path)
        items: list[Any]
        if isinstance(raw, list):
            items = list(raw)
        elif isinstance(raw, Mapping) and raw:
            items = [raw]
        else:
            items = []
        return [
            Ref(kind=self.kind, name=str(item["name"]))
            for item in items
            if isinstance(item, Mapping) and item.get("name")
        ]


class IpAddressGroup(_IpObjectGroup):
    kind = "ip-address-group"
    _path = _ADDRESS_GROUP_PATH
    _type_code = "AG"
    _list_fields = ("includedIPs", "excludedIPs", "includedGroups")
    _required = ()
    desired_state_doc = (
        "rules: [{includedIPs: [cidr], excludedIPs: [cidr], includedGroups: "
        "[groupName], comment: str}] — name and type ('AG') come from the ref "
        "and the spec. Every group needs at least one include list or include "
        "group or the Orchestrator rejects it with HTTP 400."
    )


class IpServiceGroup(_IpObjectGroup):
    kind = "ip-service-group"
    _path = _SERVICE_GROUP_PATH
    _type_code = "SG"
    _list_fields = (
        "includedPorts",
        "excludedPorts",
        "includedGroups",
        "excludedGroups",
        "icmpTypes",
        "icmpCodes",
    )
    #: The spec marks `protocol` required on every service-group rule; a
    #: missing one is refused at plan time rather than as a raw API 400.
    _required = ("protocol",)
    desired_state_doc = (
        "rules: [{protocol: TCP|UDP|ICMP, includedPorts: [port|range], "
        "excludedPorts: [...], includedGroups: [...], excludedGroups: [...], "
        "icmpTypes: [...], icmpCodes: [...], comment: str}] — name and type "
        "('SG') come from the ref and the spec. protocol is required on every "
        "rule; the port/group fields are ignored by the server when it is ICMP."
    )


# == orchestrator-scope AppExpress ============================================


#: Declared by the 7.2.0 spec's AppExpressGroupConfig.targetQoE enum.
_TARGET_QOE_VALUES = ("UNACCEPTABLE", "POOR", "MINOR", "FAIR", "EXCELLENT")
#: Spec-declared defaults, filled on both sides of the diff so a partial `set`
#: converges with the server's full record.
_APPEXPRESS_DEFAULT_QOE = "EXCELLENT"
_APPEXPRESS_LIST_FIELDS = ("eligibleTransportPaths", "dnsServers", "appExpressApps")


class AppExpressGroup(Resource):
    kind = "app-express-group"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    desired_state_doc = (
        "AppExpress group config: {targetQoE: UNACCEPTABLE|POOR|MINOR|FAIR|"
        "EXCELLENT, overlayId: int, eligibleTransportPaths: [str], "
        "sourceLoopbacks: [{loopbackName, segmentName}], useSystemDnsServer: "
        "bool, dnsServers: [str], appExpressApps: [str], pingInterval / "
        "pingQoEUpdateInterval / userQoEUpdateInterval: number}. GET returns "
        "every group keyed by lower-cased name; POST edits one group."
    )

    # -- read side ------------------------------------------------------------

    def _all(self, ctx: Ctx) -> dict[str, Any]:
        try:
            raw = ctx.client.get(_APPEXPRESS_CONFIG_PATH)
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return {}
            raise
        return dict(raw) if isinstance(raw, dict) else {}

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        groups = self._all(ctx)
        # The spec keys the map by the lower-cased group name; accept the
        # as-written key too, in case a live server preserves case.
        record = groups.get(ref.name)
        if record is None:
            record = groups.get(ref.name.lower())
        if not isinstance(record, dict):
            return None
        out = dict(record)
        out.setdefault("name", ref.name)
        return out

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict) or not raw:
            return None
        name = raw.get("name")
        if name is None or str(name) == "":
            raise ValueError("app-express-group is missing the required field 'name'")
        out: dict[str, Any] = {str(k): v for k, v in raw.items()}
        out["name"] = str(name)
        qoe = str(out.get("targetQoE") or _APPEXPRESS_DEFAULT_QOE).upper()
        if qoe not in _TARGET_QOE_VALUES:
            raise ValueError(
                f"app-express-group {name!r}: targetQoE must be one of "
                f"{', '.join(_TARGET_QOE_VALUES)} (per the 7.2.0 spec), got {qoe!r}"
            )
        out["targetQoE"] = qoe
        out["useSystemDnsServer"] = bool(out.get("useSystemDnsServer", False))
        for field in _APPEXPRESS_LIST_FIELDS:
            values = out.get(field) or []
            if not isinstance(values, (list, tuple)):
                raise ValueError(
                    f"app-express-group {name!r}.{field} must be a list, "
                    f"got {type(values).__name__}"
                )
            out[field] = sorted(str(v) for v in values)
        loopbacks = out.get("sourceLoopbacks") or []
        if not isinstance(loopbacks, (list, tuple)):
            raise ValueError(
                f"app-express-group {name!r}.sourceLoopbacks must be a list of "
                f"{{loopbackName, segmentName}}, got {type(loopbacks).__name__}"
            )
        canonical_loopbacks: list[dict[str, Any]] = []
        for i, loopback in enumerate(loopbacks):
            if not isinstance(loopback, Mapping):
                raise ValueError(
                    f"app-express-group {name!r}.sourceLoopbacks[{i}] must be a "
                    f"mapping, got {type(loopback).__name__}"
                )
            canonical_loopbacks.append({str(k): v for k, v in loopback.items()})
        canonical_loopbacks.sort(
            key=lambda lb: (str(lb.get("loopbackName", "")), str(lb.get("segmentName", "")))
        )
        out["sourceLoopbacks"] = canonical_loopbacks
        return out

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        body = dict(desired)
        body.setdefault("name", ref.name)
        return self.normalize(body)

    # -- write side -------------------------------------------------------------

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            ctx.client.delete(_APPEXPRESS_CONFIG_PATH, params={"groupName": ref.name})
            return ApplyResult(ok=True, message=f"app-express group {ref.name!r} deleted")
        assert isinstance(diff.desired, dict)
        body = dict(diff.desired)
        body["name"] = ref.name
        # POST edits a single group (the spec is explicit about that), so this
        # never disturbs the other groups in the table.
        ctx.client.post(_APPEXPRESS_CONFIG_PATH, body)
        verb = "created" if diff.current is None else "updated"
        return ApplyResult(ok=True, message=f"app-express group {ref.name!r} {verb}")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        restored = self.normalize(snapshot)
        if restored is None:
            try:
                ctx.client.delete(_APPEXPRESS_CONFIG_PATH, params={"groupName": ref.name})
            except OrchApiError as exc:
                if exc.status_code == 404:
                    return ApplyResult(ok=True, changed=False, message="already absent")
                raise
            return ApplyResult(
                ok=True, message=f"app-express group {ref.name!r} removed (compensate)"
            )
        assert isinstance(restored, dict)
        body = dict(restored)
        body["name"] = ref.name
        ctx.client.post(_APPEXPRESS_CONFIG_PATH, body)
        return ApplyResult(ok=True, message=f"app-express group {ref.name!r} restored")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        refs: list[Ref] = []
        for key, record in self._all(ctx).items():
            name = record.get("name") if isinstance(record, Mapping) else None
            refs.append(Ref(kind=self.kind, name=str(name or key)))
        return refs


class AppExpressAssociation(Resource):
    """Which appliance runs which AppExpress group.

    A singleton table, not a per-group object: the spec warns that the POST
    "is not used for editing a single AppExpress group association — find all
    existing associations first and use this API", i.e. the body is the whole
    array. That makes snapshot/restore exact (REVERSIBLE) and whole-resource
    delete meaningless.
    """

    kind = "app-express-association"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    #: An association names a group, so within one changeset the group must be
    #: created first (and, reversed, associations are removed first).
    dependencies = ("app-express-group",)
    #: The association table always exists (possibly empty) — no "absent"
    #: state, so whole-resource delete is refused.
    deletable = False
    desired_state_doc = (
        "associations: [{appliance: <hostname> | nePk: <pk>, "
        "appExpressGroupName: <group>}] — the complete table; canonical state "
        "speaks nePks (stable across renames), hostnames are resolved for you."
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            raw = ctx.client.get(_APPEXPRESS_ASSOCIATION_PATH)
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return []
            raise
        if raw is None:
            return []
        if isinstance(raw, (list, dict)):
            return raw
        raise ValueError(f"unexpected appExpress association response shape: {raw!r}")

    def normalize(self, raw: RawState) -> CanonicalState:
        if isinstance(raw, dict):
            # Canonical state round-trips back through here wrapped.
            entries: Any = raw.get("associations", [])
        elif isinstance(raw, list):
            entries = raw
        else:
            entries = []
        if not isinstance(entries, (list, tuple)):
            raise ValueError(
                f"associations must be a list of {{nePk, appExpressGroupName}}, "
                f"got {type(entries).__name__}"
            )
        out: list[dict[str, Any]] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"associations[{i}] must be a mapping, got {type(entry).__name__}"
                )
            record = {str(k): v for k, v in entry.items()}
            ne_pk = record.get("nePk")
            group = record.get("appExpressGroupName")
            if ne_pk is None or str(ne_pk) == "":
                raise ValueError(f"associations[{i}] is missing the required field 'nePk'")
            if group is None or str(group) == "":
                raise ValueError(
                    f"associations[{i}] is missing the required field 'appExpressGroupName'"
                )
            record["nePk"] = str(ne_pk)
            record["appExpressGroupName"] = str(group)
            out.append(record)
        out.sort(key=lambda r: (r["nePk"], r["appExpressGroupName"]))
        return {"associations": out}

    def canonicalize_desired(
        self, ctx: Ctx, ref: Ref, desired: Mapping[str, Any]
    ) -> CanonicalState:
        """Intent may name appliances by hostname; canonical state uses nePks
        (bio-association's convention), resolved through the resolver."""
        entries = desired.get("associations", desired)
        if isinstance(entries, Mapping):
            entries = entries.get("associations", [])
        if not isinstance(entries, (list, tuple)):
            raise ValueError(
                f"associations must be a list of {{appliance|nePk, "
                f"appExpressGroupName}}, got {type(entries).__name__}"
            )
        resolved: list[dict[str, Any]] = []
        for i, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise ValueError(
                    f"associations[{i}] must be a mapping, got {type(entry).__name__}"
                )
            record = {str(k): v for k, v in entry.items()}
            appliance = record.pop("appliance", None)
            if appliance is not None and not record.get("nePk"):
                record["nePk"] = ctx.resolver.ne_pk_for(str(appliance))
            resolved.append(record)
        return self.normalize({"associations": resolved})

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        if diff.desired is None:
            return ApplyResult(
                ok=False,
                message=(
                    f"{self.kind} is a singleton table; refusing to POST an empty "
                    f"association table (delete individual entries instead)"
                ),
            )
        assert isinstance(diff.desired, dict)
        return self._replace(ctx, diff.desired.get("associations") or [], "replaced")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            return ApplyResult(
                ok=False,
                message="no usable snapshot; refusing to POST an empty association table",
            )
        restored = self.normalize(snapshot)
        assert isinstance(restored, dict)
        return self._replace(ctx, restored.get("associations") or [], "restored")

    def _replace(self, ctx: Ctx, associations: list[Any], verb: str) -> ApplyResult:
        ctx.client.post(_APPEXPRESS_ASSOCIATION_PATH, associations)
        return ApplyResult(
            ok=True,
            message=f"app-express associations {verb} ({len(associations)} entry/entries)",
        )

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name="global")]


register(Acls())
register(IpAddressGroup())
register(IpServiceGroup())
register(AppExpressGroup())
register(AppExpressAssociation())
