"""``show version``: Orchestrator version plus per-appliance partition versions (#57).

Read-only. Nothing here touches the candidate store, the journal, or the
transaction engine — two GET endpoints and a bounded fan-out, no more.

Two API traps this module exists to get right, both verified against
``specs/orchestrator-openapi-7.2.0.json``:

* ``GET /gms/versions`` answers ``{"current": ..., "installed": [...]}`` and
  the spec summarizes it as "Returns available orchestrator versions", which
  reads as if the whole response answered "what version is this?". It does
  not: ``current`` is the *running* Orchestrator and ``installed`` is the
  three versions available to upgrade *to*. Rendering ``installed`` as the
  Orchestrator version would be wrong, so :class:`FabricVersions` keeps them
  in separate fields and the header only ever prints ``current``.
* ``GET /appliancesSoftwareVersions`` requires **both** ``nePk`` and
  ``cached``. ``--no-cache`` therefore has to send ``cached=false``; dropping
  the parameter is a 422, not a default. The values go on the wire as the
  literal strings ``"true"``/``"false"`` so the encoding is this module's
  decision rather than a client library's.

The per-appliance response is one entry per partition, and the three booleans
are independent:

* ``active`` — the partition running right now.
* ``fallback_boot`` — the backup that boots if the active partition fails.
* ``next_boot`` — what boots on the next reload, which **can differ from
  both**. A next boot pointing somewhere other than the active partition means
  a reload silently changes the running version, which is precisely what an
  operator wants to know before a maintenance window, so it is surfaced as a
  first-class flag rather than left for the reader to spot in a column.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import structlog

from pyecsdwan.client import OrchClient
from pyecsdwan.contract import Ctx
from pyecsdwan.reports.fanout import DEFAULT_CONCURRENCY, fan_out

log = structlog.get_logger(__name__)

#: GET, no parameters. `current` is the running Orchestrator.
ORCHESTRATOR_VERSIONS_PATH = "/gms/versions"
#: GET; nePk AND cached are both required by the spec.
APPLIANCE_VERSIONS_PATH = "/appliancesSoftwareVersions"

#: Rendered wherever a version string is missing from a response.
UNKNOWN = "unknown"

_DIGITS = re.compile(r"\d+")


def version_key(version: str) -> tuple[tuple[int, ...], str]:
    """Sort key for an ECOS/Orchestrator version like ``9.4.2.40100``.

    Compares the numeric components numerically (so ``9.10`` sorts above
    ``9.4``, which a plain string compare gets backwards), and falls back to
    the raw string for anything unparseable rather than raising — a report
    must render whatever the fabric reports, including a version format that
    did not exist when this was written.
    """
    return tuple(int(part) for part in _DIGITS.findall(version)), version


# -- data ---------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Partition:
    """One partition's software version, as reported by the appliance."""

    index: int | None
    version: str
    build_time: str
    active: bool
    next_boot: bool
    fallback_boot: bool
    #: The untouched API entry. ``--json`` emits this so consumers get every
    #: field the Orchestrator returned, not just the ones rendered.
    raw: dict[str, Any] = dataclasses.field(default_factory=dict, repr=False)

    @property
    def label(self) -> str:
        """``9.4.2.40100 (p0)`` — version plus which partition carries it."""
        if self.index is None:
            return self.version
        return f"{self.version} (p{self.index})"


def _partition_from_api(entry: dict[str, Any]) -> Partition:
    index = entry.get("partition")
    return Partition(
        index=index if isinstance(index, int) else None,
        version=str(entry.get("build_version") or UNKNOWN),
        build_time=str(entry.get("build_time") or ""),
        active=bool(entry.get("active")),
        next_boot=bool(entry.get("next_boot")),
        fallback_boot=bool(entry.get("fallback_boot")),
        raw=dict(entry),
    )


@dataclasses.dataclass(frozen=True)
class ApplianceVersions:
    """One appliance's partition table, or the reason it has none."""

    ne_pk: str
    hostname: str
    partitions: tuple[Partition, ...] = ()
    #: Non-empty when the appliance could not be read; the row still renders.
    error: str = ""

    @property
    def unreachable(self) -> bool:
        return bool(self.error)

    @property
    def active(self) -> Partition | None:
        """The running partition."""
        return next((p for p in self.partitions if p.active), None)

    @property
    def backup(self) -> Partition | None:
        """The partition that boots if the active one fails.

        Prefers the explicit ``fallback_boot`` flag. When no partition carries
        it — some appliances report the flag only after a failover has been
        armed — the first non-active partition is used, because "the other
        partition" is what an operator means by the backup on a two-partition
        box. Returns None only when there is genuinely nothing else.
        """
        flagged = next((p for p in self.partitions if p.fallback_boot), None)
        if flagged is not None:
            return flagged
        return next((p for p in self.partitions if not p.active), None)

    @property
    def next_boot(self) -> Partition | None:
        """What boots on the next reload, or None if nothing is flagged."""
        return next((p for p in self.partitions if p.next_boot), None)

    @property
    def active_version(self) -> str:
        active = self.active
        return active.version if active is not None else UNKNOWN

    @property
    def next_boot_diverges(self) -> bool:
        """True when a reload would move off the running partition.

        Unknown is not divergence: an appliance that flags no next boot, or
        that has no active partition to compare against, is reported as
        unknown rather than quietly asserted to be consistent.
        """
        active, upcoming = self.active, self.next_boot
        if active is None or upcoming is None:
            return False
        return upcoming is not active or upcoming.version != active.version


@dataclasses.dataclass(frozen=True)
class FabricVersions:
    """The whole report: Orchestrator first, then every appliance."""

    #: ``current`` from /gms/versions — the RUNNING Orchestrator, never
    #: ``installed``.
    orchestrator: str
    #: ``installed`` — versions available to upgrade to. Not the running one.
    orchestrator_available: tuple[str, ...] = ()
    appliances: tuple[ApplianceVersions, ...] = ()
    #: False when ``--no-cache`` forced a live read from each appliance.
    cached: bool = True
    #: Non-empty when /gms/versions itself failed; appliances still render.
    orchestrator_error: str = ""

    @property
    def reachable(self) -> tuple[ApplianceVersions, ...]:
        """Appliances that answered with at least one partition."""
        return tuple(a for a in self.appliances if not a.unreachable and a.partitions)

    @property
    def unreachable(self) -> tuple[ApplianceVersions, ...]:
        return tuple(a for a in self.appliances if a.unreachable)

    @property
    def active_versions(self) -> tuple[str, ...]:
        """Distinct running versions across the fleet, newest first."""
        distinct = {a.active_version for a in self.reachable}
        return tuple(sorted(distinct, key=version_key, reverse=True))

    @property
    def baseline_version(self) -> str:
        """The version the fleet is expected to be on.

        The most common running version, ties broken toward the newest — so a
        two-appliance fabric split across two releases names the newer one as
        the baseline and flags the laggard, rather than picking arbitrarily.
        """
        counts = Counter(a.active_version for a in self.reachable)
        if not counts:
            return UNKNOWN
        top = max(counts.values())
        return max((v for v, n in counts.items() if n == top), key=version_key)

    @property
    def skewed(self) -> bool:
        """True when the fleet is not all on one running version."""
        return len(self.active_versions) > 1

    def is_outlier(self, appliance: ApplianceVersions) -> bool:
        """True when this appliance is off the fleet baseline."""
        if appliance.unreachable or not appliance.partitions:
            return False
        return self.skewed and appliance.active_version != self.baseline_version

    @property
    def divergent_next_boot(self) -> tuple[ApplianceVersions, ...]:
        return tuple(a for a in self.appliances if a.next_boot_diverges)


# -- collection ---------------------------------------------------------------


def orchestrator_version(client: OrchClient) -> tuple[str, tuple[str, ...]]:
    """``(running, available)`` from /gms/versions.

    The first element is ``current``. ``installed`` is returned alongside it
    but is *not* the running version — see the module docstring.
    """
    payload = client.get(ORCHESTRATOR_VERSIONS_PATH)
    if not isinstance(payload, dict):
        return UNKNOWN, ()
    current = str(payload.get("current") or UNKNOWN)
    installed = payload.get("installed")
    available = tuple(str(v) for v in installed) if isinstance(installed, list) else ()
    return current, available


def appliance_partitions(
    client: OrchClient, ne_pk: str, *, cached: bool = True
) -> tuple[Partition, ...]:
    """Per-partition versions for one appliance.

    Both query parameters are required by the spec, so ``cached`` is always
    sent — ``cached=false`` for a live read, never an omitted parameter.
    """
    payload = client.get(
        APPLIANCE_VERSIONS_PATH,
        params={"nePk": ne_pk, "cached": "true" if cached else "false"},
    )
    if not isinstance(payload, list):
        return ()
    return tuple(_partition_from_api(e) for e in payload if isinstance(e, dict))


def collect(
    ctx: Ctx,
    *,
    cached: bool = True,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float | None = None,
) -> FabricVersions:
    """Build the whole report: Orchestrator version, then every appliance.

    Partial failure degrades rather than raising — a version report is most
    needed exactly when something is down. An appliance that cannot be read
    becomes a row carrying the reason, and a failed ``/gms/versions`` leaves
    the header unknown rather than taking the appliance table down with it.

    The one failure that *does* propagate is the inventory fetch
    (``resolver.appliances()``): with no list of appliances there is no report
    to degrade, and the CLI's existing error path already says so clearly.
    """
    orchestrator: str = UNKNOWN
    available: tuple[str, ...] = ()
    orch_error = ""
    try:
        orchestrator, available = orchestrator_version(ctx.client)
    except Exception as exc:  # noqa: BLE001 - the appliance table still renders
        orch_error = f"{type(exc).__name__}: {exc}"
        log.debug("orchestrator_version_failed", error=orch_error)

    inventory = ctx.resolver.appliances()
    targets = _targets(inventory)
    outcomes = fan_out(
        targets,
        lambda target: appliance_partitions(ctx.client, target[0], cached=cached),
        concurrency=concurrency,
        timeout=timeout,
    )
    appliances = tuple(
        ApplianceVersions(
            ne_pk=outcome.item[0],
            hostname=outcome.item[1],
            partitions=outcome.value or (),
            error=outcome.error,
        )
        for outcome in outcomes
    )
    report = FabricVersions(
        orchestrator=orchestrator,
        orchestrator_available=available,
        appliances=appliances,
        cached=cached,
        orchestrator_error=orch_error,
    )
    log.debug(
        "version_report",
        orchestrator=orchestrator,
        appliances=len(appliances),
        unreachable=len(report.unreachable),
        skewed=report.skewed,
        cached=cached,
    )
    return report


def _targets(inventory: Sequence[dict[str, Any]]) -> list[tuple[str, str]]:
    """``(nePk, hostname)`` per appliance, in inventory order.

    Order is preserved rather than sorted so the table matches ``show
    appliances``; ``fan_out`` returns results in this order whatever the
    completion order, so the rendered table is stable between runs.
    """
    targets: list[tuple[str, str]] = []
    for entry in inventory:
        ne_pk = entry.get("nePk") or entry.get("id")
        if not ne_pk:
            continue
        targets.append((str(ne_pk), str(entry.get("hostName") or ne_pk)))
    return targets


# -- machine-readable output ---------------------------------------------------


def _partition_payload(partition: Partition) -> dict[str, Any]:
    """One partition as JSON: everything the API sent, normalized on top.

    Starting from the untouched entry means a field this report does not model
    still reaches a ``--json`` consumer — the client deliberately passes
    unknown fields through, and re-modeling them here would throw them away.
    The six known keys are then overlaid so their presence and type are
    guaranteed even when the appliance omitted one.
    """
    return {
        **partition.raw,
        "partition": partition.index,
        "build_version": partition.version,
        "build_time": partition.build_time,
        "active": partition.active,
        "next_boot": partition.next_boot,
        "fallback_boot": partition.fallback_boot,
    }


def to_payload(report: FabricVersions) -> dict[str, Any]:
    """``--json`` body: the full per-partition data, not a rendered summary.

    Every partition is emitted in full — see :func:`_partition_payload` —
    alongside the derived flags (fleet skew, next-boot divergence) that are the
    whole point of the command.
    """
    return {
        "orchestrator": {
            "current": report.orchestrator,
            "available": list(report.orchestrator_available),
            "error": report.orchestrator_error,
        },
        "cached": report.cached,
        "fleet": {
            "skewed": report.skewed,
            "baseline_version": report.baseline_version,
            "active_versions": list(report.active_versions),
            "unreachable": [a.ne_pk for a in report.unreachable],
        },
        "appliances": [
            {
                "nePk": appliance.ne_pk,
                "hostname": appliance.hostname,
                "unreachable": appliance.unreachable,
                "error": appliance.error,
                "active_version": appliance.active_version if appliance.partitions else None,
                "backup_version": (
                    appliance.backup.version if appliance.backup is not None else None
                ),
                "next_boot_version": (
                    appliance.next_boot.version if appliance.next_boot is not None else None
                ),
                "next_boot_diverges": appliance.next_boot_diverges,
                "version_skew": report.is_outlier(appliance),
                "partitions": [_partition_payload(p) for p in appliance.partitions],
            }
            for appliance in report.appliances
        ],
    }
