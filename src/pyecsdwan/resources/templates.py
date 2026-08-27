"""Template groups and their appliance associations (Phase-1 resources).

Endpoint facts: docs/research/templates-overlays-security.md. Two kinds:

* ``template-group`` — the group's content: which template sections it carries
  and their payloads (``valObject``). REVERSIBLE: content is fully snapshot-
  and-restorable.
* ``template-association`` — the complete set of groups associated to one
  appliance (ref name = appliance hostname). The POST is a *complete
  replacement* (the Orchestrator merges nothing), and it is what triggers the
  actual template push to the appliance — the push itself runs async
  server-side. apply() confirms the push through the action log before
  returning: by key when the response carries one, else via
  ``jobs.wait_for_recent_action`` (``GET /action`` by appliance + time
  window). verify() re-checks the association record, which is the
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

    def apply(self, ctx: Ctx, diff: Diff) -> ApplyResult:
        if diff.empty:
            return ApplyResult.noop()
        ref = diff.ref
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        desired = diff.desired if isinstance(diff.desired, dict) else {"template_groups": []}
        groups = list(desired.get("template_groups", []))
        # Complete replacement; this POST triggers the template push. The real
        # endpoint returns 204 and the per-appliance results land in the action
        # log by guid. When a key IS returned, await it; when the response is
        # keyless, poll the action log by appliance + a window opening just
        # before the POST — either way a failed push fails the commit instead
        # of being reported CONFIRMED.
        since_ms = int(time.time() * 1000)
        resp = ctx.client.post(_ASSOC_PATH, {"templateIds": groups}, params={"nePk": ne_pk})
        key = jobs.extract_action_key(resp)
        outcome: JobOutcome
        if key is not None:
            outcome = jobs.wait_for_action(
                ctx.client, key, ctx.client.settings, f"template push {ref.name}"
            )
        else:
            outcome = jobs.wait_for_recent_action(
                ctx.client, ctx.client.settings, ne_pk, since_ms, f"template push {ref.name}"
            )
        if outcome.state != "SUCCESS":
            return ApplyResult(
                ok=False,
                message=f"template push for {ref.name} {outcome.state}: {outcome.detail}",
                jobs=[outcome],
            )
        return ApplyResult(
            ok=True,
            jobs=[outcome],
            message=(
                f"association for {ref.name} set to {groups or '[]'} "
                f"(template push confirmed)"
            ),
        )

    def rollback(self, ctx: Ctx, ref: Ref, snapshot: RawState) -> ApplyResult:
        ne_pk = ctx.resolver.ne_pk_for(ref.name)
        canonical = self.normalize(snapshot)
        assert isinstance(canonical, dict)
        groups = canonical["template_groups"]
        ctx.client.post(_ASSOC_PATH, {"templateIds": groups}, params={"nePk": ne_pk})
        return ApplyResult(ok=True, message=f"association for {ref.name} restored to {groups}")

    def list_refs(self, ctx: Ctx) -> list[Ref]:
        return [Ref(kind=self.kind, name=n) for n in ctx.resolver.appliance_names()]


register(TemplateGroup())
register(TemplateAssociation())
