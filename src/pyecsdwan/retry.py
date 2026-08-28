"""Which calls may be replayed, and why (#67).

The client used to retry *every* GET on a connection error or a retryable 5xx,
on the usual assumption that GET is idempotent. It is not, on this API. The
vendored OpenAPI baselines describe GET endpoints whose own summaries read
"Clear idle time", "Create blueprint template", "Generate the Sys Dump file on
the appliance", "Logout of current HTTP session" and — the one that would hurt
most — "Delete specific/all segment BGP state". A dropped response to one of
those, replayed, does the thing twice.

The exposure was never in the curated plugins, whose GETs are reviewed at
promotion. It was in `ec-cli api get <any path>`: Tier-0 passthrough reaches
all 1300-odd endpoints, including every one above, and inherited the retry
loop.

So the policy is per call, with the *transport* — not the caller — holding a
veto:

* :attr:`Retry.BOUNDED` — what curated resource reads ask for and keep.
* :attr:`Retry.NEVER` — execute at most once. What Tier-0 raw gets, always.
* :data:`MUTATING_GETS` — endpoints that are NEVER no matter who asks. A
  curated plugin cannot opt into replaying one by accident, and neither can a
  future one written by someone who has not read this file.

**The classification is derived from the specs, not invented.** Every entry
below quotes the vendored baseline's own summary as its evidence, and
`tests/test_retry.py` re-runs the derivation: any GET whose spec summary opens
with an action verb must appear in :data:`MUTATING_GETS` or in
:data:`REVIEWED_SAFE`, with a reason. A future baseline that introduces another
read-shaped action fails the gate instead of quietly joining the retryable
set — which is exactly the drift #67 names ("future vendor releases can
introduce more read-shaped actions").

Ambiguity resolves to NEVER. Where a path names an action and the summary
describes a read, both readings are live in this API, and the costs are not
symmetric: not retrying a read costs one failed call the caller already handles,
while retrying a mutation does it twice.
"""

from __future__ import annotations

import enum

from pyecsdwan import specs


class Retry(str, enum.Enum):
    """How many times a call may be sent."""

    #: At most once. A dropped response is reported, never re-sent.
    NEVER = "never"
    #: Retry connection errors and retryable 5xx, up to ``settings.max_retries``.
    BOUNDED = "bounded"


#: Why an effective policy was chosen, for the debug log and the journal.
#: Fixed strings, never request content — a reason is written to the audit
#: journal, so it must not be able to carry a path parameter or a body.
REASON_MUTATING = "endpoint mutates behind a read-shaped verb"
REASON_WRITE_METHOD = "write method; a lost response to a landed write would double-apply"
REASON_RAW = "tier-0 raw passthrough; endpoint not known read-only"
REASON_CALLER = "caller declared the read safe to replay"
REASON_DEFAULT = "no retry requested"


def _key(scope: str, path: str) -> str:
    return specs.endpoint_key(scope, "GET", path)


#: GET endpoints that mutate, with the vendored spec summary that says so.
#:
#: Keyed by ``specs.endpoint_key()`` so path parameters normalize — a
#: denylist keyed on raw strings would miss ``/bgp/vrfs/3/state`` while
#: matching ``/bgp/vrfs/{vrfId}/state``.
MUTATING_GETS: dict[str, str] = {
    # -- appliance (ECOS) ----------------------------------------------------
    # The worst one: a GET that deletes routing state. `reports/bgpstate.py`
    # already avoids this path for exactly this reason and says so in its
    # docstring; this makes the avoidance structural rather than a comment
    # someone has to have read.
    _key("appliance", "/bgp/vrfs/{vrfId}/state"): "Delete specific/all segment BGP state",
    _key("appliance", "/debugFiles/debugDump/generate"): (
        "Generate the Sys Dump file on the appliance."
    ),
    _key("appliance", "/oro/debug/closeGrpcConnection"): "Close ORO grpc link",
    # -- orchestrator --------------------------------------------------------
    _key("orchestrator", "/appliance/cpustat/historical/cancelfetch"): (
        "Cancel CUP stats fetch request"
    ),
    _key("orchestrator", "/authentication/logout"): "Logout of current HTTP session",
    _key("orchestrator", "/authentication/saml2/logout"): "Logout of Orchestrator via SAML",
    _key("orchestrator", "/authentication/oauth/redirect"): (
        "Login to Orchestrator using Oauth server"
    ),
    _key("orchestrator", "/gms/backup/exportTemplate"): "Create blueprint template",
    _key("orchestrator", "/idle/clear"): "Clear idle time",
    _key("orchestrator", "/ids/updateSignatureFromPortal"): (
        "Update IDPS signature from portal"
    ),
    _key("orchestrator", "/thirdPartyServices/sse/image/refresh"): (
        "Initiate a request to refresh connector images from portal"
    ),
    # The four ipslaSetting GETs all carry the summary "Enable/Disable IPSLA
    # settings for <vendor>". That is plausibly the POST's summary pasted onto
    # the GET — but "plausibly" is not evidence, and the spec is the only
    # source there is for these.
    _key("orchestrator", "/thirdPartyServices/axis/ipslaSetting"): (
        "Enable/Disable IPSLA settings for Axis"
    ),
    _key("orchestrator", "/thirdPartyServices/netskope/ipslaSetting"): (
        "Enable/Disable IPSLA settings for Netskope"
    ),
    _key("orchestrator", "/thirdPartyServices/serviceOrchestration/ipslaSetting"): (
        "Enable/Disable IPSLA settings for Service Provider"
    ),
    _key("orchestrator", "/thirdPartyServices/zscaler/ipslaSetting"): (
        "Enable/Disable IPSLA settings for Zscaler"
    ),
    # Not summary-flagged, but named as an apply channel by
    # docs/research/appliance-jobs.md §Preconfig apply. The spec calls it "Get
    # the apply status", and `jobs.wait_for_preconfig_apply` polls it in a
    # loop — so whichever reading is right, the poller's own loop already
    # supplies the resilience a transport retry would, and removing the retry
    # costs nothing there.
    _key("orchestrator", "/gms/appliance/preconfiguration/apply"): (
        "documented as the preconfig apply channel; the poller loop is the retry"
    ),
    # Path names an action, summary says it returns a handle. Both readings
    # are live in this API.
    _key("orchestrator", "/gms/applianceWizard/apply"): (
        "path names an apply; summary claims a read — unresolved"
    ),
    _key(
        "orchestrator",
        "/thirdPartyServices/awstgnm/globalNetworkToTransitGatewayAssociation/refresh",
    ): "path names a refresh; summary claims a read — unresolved",
}

#: GETs whose summary opens with an action verb but which are reads, with the
#: reason each was cleared. Exists so the drift gate in `tests/test_retry.py`
#: is a *review* record rather than a list of exceptions: a new spec entry
#: lands in neither map and fails, and whoever classifies it writes down why.
REVIEWED_SAFE: dict[str, str] = {
    _key("appliance", "/configdb/download/{dbName}"): (
        "download: transfers a backup file, changes nothing on the appliance"
    ),
    _key("appliance", "/debugFiles/downloadFile"): (
        "download: transfers an already-generated file (the *generate* GET is denied above)"
    ),
}


def _segments(key: str) -> tuple[str, ...]:
    return tuple(key.split(" ", 2)[2].strip("/").split("/"))


def _matches(template: tuple[str, ...], actual: tuple[str, ...]) -> bool:
    """Segment-wise match where a spec parameter matches any one segment.

    Needed because the denylist is keyed on spec templates
    (``/bgp/vrfs/{vrfId}/state``) while a real call carries a value
    (``/bgp/vrfs/3/state``). ``specs.normalize_path`` collapses ``{vrfId}`` to
    ``{}`` but has nothing to collapse in ``3``, so a plain dict lookup silently
    misses every parameterized entry — including the delete-BGP-state one,
    which is the single most dangerous row in the table. The first smoke test
    of this module caught exactly that.
    """
    if len(template) != len(actual):
        return False
    return all(
        want == "{}" or want == have
        for want, have in zip(template, actual, strict=True)
    )


def is_mutating_get(scope: str, method: str, path: str) -> str:
    """The spec summary proving this GET mutates, or "" if it does not.

    ``scope`` is "orchestrator" or "appliance" — the same two the vendored
    baselines use. Returns the evidence rather than a bool so the caller can
    journal *why* a call was held to one attempt.
    """
    if method.upper() != "GET":
        return ""
    key = specs.endpoint_key(scope, "GET", path)
    hit = MUTATING_GETS.get(key)
    if hit is not None:
        return hit
    actual = _segments(key)
    prefix = f"{scope} GET "
    for candidate, evidence in MUTATING_GETS.items():
        if candidate.startswith(prefix) and _matches(_segments(candidate), actual):
            return evidence
    return ""


def effective_policy(
    method: str, path: str, requested: Retry, *, scope: str = "orchestrator"
) -> tuple[Retry, str]:
    """The policy that will actually be used, and the reason, for the journal.

    The veto order is the point: a mutating GET is NEVER even when the caller
    asked for BOUNDED, because the caller may be a plugin written years after
    this file and against a newer spec.
    """
    method = method.upper()
    if method != "GET":
        return Retry.NEVER, REASON_WRITE_METHOD
    evidence = is_mutating_get(scope, method, path)
    if evidence:
        return Retry.NEVER, f"{REASON_MUTATING}: {evidence}"
    if requested is Retry.BOUNDED:
        return Retry.BOUNDED, REASON_CALLER
    return Retry.NEVER, REASON_DEFAULT
