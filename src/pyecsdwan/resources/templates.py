"""Template groups and their appliance associations (Phase-1 resources).

Endpoint facts: docs/research/templates-overlays-security.md. Two kinds:

* ``template-group`` — the group's content: which template sections it carries
  and their payloads (``valObject``). REVERSIBLE: content is fully snapshot-
  and-restorable.
* ``template-association`` — the complete set of groups associated to one
  appliance (ref name = appliance hostname). The POST is a *complete
  replacement* (the Orchestrator merges nothing), and it is what triggers the
  actual template push to the appliance — the push itself runs async
  server-side. apply() *and* rollback() confirm the push through the action
  log before returning: by key when the response carries one, else via
  ``jobs.wait_for_recent_action`` (``GET /action`` by appliance + time
  window), and in both cases only when a record names this appliance's nePk
  (#64 — the action log can confirm the association while saying nothing
  about the push). verify() re-checks the association record, which is the
  orchestrator-side state this resource manages.
"""

from __future__ import annotations

import time
from typing import Any

from pyecsdwan import jobs
from pyecsdwan.client import OrchApiError
from pyecsdwan.contract import (
    ApplyResult,
    CanonicalState,
    Ctx,
    Diff,
    JobOutcome,
    RawState,
    Ref,
    Resource,
    Reversibility,
    Scope,
    Tier,
)
from pyecsdwan.registry import register

_GROUP_PATH = "/template/templateGroups"
_CREATE_PATH = "/template/templateCreate"
_ASSOC_PATH = "/template/applianceAssociation"


class TemplateGroup(Resource):
    kind = "template-group"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    desired_state_doc = (
        "templates: map of template section name -> valObject payload "
        "(section names as in the Orchestrator UI / Default Template Group)"
    )
    #: Create uses templateCreate; update/delete use templateGroups.
    endpoints = (
        "orchestrator GET /template/templateGroups",
        "orchestrator POST /template/templateGroups",
        "orchestrator DELETE /template/templateGroups",
        "orchestrator POST /template/templateCreate",
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        try:
            raw = ctx.client.get(_GROUP_PATH, params={"templateGroup": ref.name})
        except OrchApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return None
        templates: dict[str, Any] = {}
        entries = raw.get("templates")
        if isinstance(entries, dict):
            # already canonical (user intent round-trip)
            for name, val in entries.items():
                templates[str(name)] = val
        elif isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") is not None:
                    templates[str(entry["name"])] = entry.get("valObject")
        # Group name lives in the Ref; server metadata is dropped.
        return {"templates": templates}

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        if diff.desired is None:
            ctx.client.delete(_GROUP_PATH, params={"templateGroup": ref.name})
            ctx.resolver.refresh("template_groups")
            return ApplyResult(ok=True, message=f"template group {ref.name!r} deleted")
        assert isinstance(diff.desired, dict)
        body = self._body(ref.name, diff.desired)
        if diff.current is None:
            # 204 = created empty (no templates key), 200 = created with content.
            ctx.client.post(_CREATE_PATH, body, expected=(200, 204))
            ctx.resolver.refresh("template_groups")
            return ApplyResult(ok=True, message=f"template group {ref.name!r} created")
        ctx.client.post(_GROUP_PATH, body, params={"templateGroup": ref.name})
        return ApplyResult(ok=True, message=f"template group {ref.name!r} updated")

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        if snapshot is None:
            try:
                ctx.client.delete(_GROUP_PATH, params={"templateGroup": ref.name})
            except OrchApiError as exc:
                if exc.status_code != 404:
                    raise
            ctx.resolver.refresh("template_groups")
            return ApplyResult(ok=True, message=f"template group {ref.name!r} removed (compensate)")
        canonical = self.normalize(snapshot)
        assert isinstance(canonical, dict)
        body = self._body(ref.name, canonical)
        if self.fetch(ctx, ref) is None:
            ctx.client.post(_CREATE_PATH, body, expected=(200, 204))
        else:
            ctx.client.post(_GROUP_PATH, body, params={"templateGroup": ref.name})
        ctx.resolver.refresh("template_groups")
        return ApplyResult(ok=True, message=f"template group {ref.name!r} restored")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name=g) for g in ctx.resolver.template_groups()]

    @staticmethod
    def _body(name: str, canonical: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "templates": [
                {"name": tpl, "valObject": val}
                for tpl, val in sorted(canonical.get("templates", {}).items())
            ],
        }


class TemplateAssociation(Resource):
    kind = "template-association"
    scope = Scope.ORCHESTRATOR
    reversibility = Reversibility.REVERSIBLE
    tier = Tier.CURATED
    dependencies = ("template-group",)
    desired_state_doc = "template_groups: complete list of group names for the appliance"
    endpoints = (
        "orchestrator GET /template/applianceAssociation",
        "orchestrator POST /template/applianceAssociation",
    )

    def fetch(self, ctx: Ctx, ref: Ref) -> RawState:
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        try:
            raw = ctx.client.get(_ASSOC_PATH, params={"nePk": ne_pk})
        except OrchApiError as exc:
            if exc.status_code in (204, 404):
                return None
            raise
        return raw if isinstance(raw, dict) else None

    def normalize(self, raw: RawState) -> CanonicalState:
        if not isinstance(raw, dict):
            return {"template_groups": []}
        groups = raw.get("templateIds", raw.get("template_groups", []))
        if not isinstance(groups, list):
            groups = []
        return {"template_groups": sorted(str(g) for g in groups)}

    def write_target(self, ctx: Ctx, ref: Ref) -> str | None:
        """One appliance's complete template-group association (#69): the
        POST replaces the whole list and triggers the push. Instance-scoped
        by nePk; the ref's *name* is the appliance for this kind."""
        return f"appliance {ctx.resolver.ne_pk_for(ref.name)} template-association"

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        desired = diff.desired if isinstance(diff.desired, dict) else {"template_groups": []}
        groups = list(desired.get("template_groups", []))
        # Complete replacement; this POST triggers the template push. The real
        # endpoint returns 204 and the per-appliance results land in the action
        # log by guid — so a failed push fails the commit instead of being
        # reported CONFIRMED.
        outcome = self._push(ctx, ref, ne_pk, groups)
        return self._confirmed(ref, ne_pk, outcome, f"set to {groups or '[]'}")

    def _push(self, ctx: Ctx, ref: Ref, ne_pk: str, groups: list[str]) -> JobOutcome:
        """POST the association and await the push it triggers.

        Shared by apply() and rollback(): a revert that does not confirm its
        own push is the same unverified claim as an apply that does not, and
        the revert is the one running after something has already gone wrong.
        """
        since_ms = int(time.time() * 1000)
        # Snapshot the action log *before* writing, so the keyless waiter can
        # tell a new guid from one that was already there — including this
        # appliance's own previous push, which a revert following an apply
        # would otherwise find sitting in its window (#64).
        before = jobs.action_log_guids(ctx.client, ne_pk, since_ms)
        resp = ctx.client.post(_ASSOC_PATH, {"templateIds": groups}, params={"nePk": ne_pk})
        key = jobs.extract_action_key(resp)
        if key is not None:
            return jobs.wait_for_action(
                ctx.client, key, ctx.client.settings, f"template push {ref.name}"
            )
        return jobs.wait_for_recent_action(
            ctx.client,
            ctx.client.settings,
            ne_pk,
            since_ms,
            f"template push {ref.name}",
            ignore_guids=before,
        )

    def _confirmed(
        self, ref: Ref, ne_pk: str, outcome: JobOutcome, what: str
    ) -> ApplyResult:
        """Turn a push outcome into a result, requiring appliance-side evidence.

        A SUCCESS outcome is not on its own enough (#64): the action log can
        answer about the *control-plane association* — the Orchestrator
        recorded which groups this appliance should carry — while saying
        nothing about whether the appliance received the push. The evidence
        that it did is a record naming this ``nePk``; ``_terminal_outcome``
        collects exactly those into ``per_appliance``, so an empty entry there
        means the confirmation covers the wrong thing.
        """
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=f"template push for {ref.name} {outcome.state}: {outcome.detail}",
                jobs=[outcome],
            )
        if ne_pk not in outcome.per_appliance:
            return ApplyResult(
                ok=False,
                message=(
                    f"template push for {ref.name} reported success but no action-log "
                    f"record names {ne_pk}, so only the association was confirmed and "
                    f"not the push to the appliance"
                ),
                jobs=[outcome],
            )
        return ApplyResult(
            ok=True,
            jobs=[outcome],
            message=f"association for {ref.name} {what} (template push confirmed)",
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        canonical = self.normalize(snapshot)
        assert isinstance(canonical, dict)
        groups = canonical["template_groups"]
        outcome = self._push(ctx, ref, ne_pk, groups)
        return self._confirmed(ref, ne_pk, outcome, f"restored to {groups}")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name=n) for n in ctx.resolver.appliance_names()]


register(TemplateGroup())
register(TemplateAssociation())
