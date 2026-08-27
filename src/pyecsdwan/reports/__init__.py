"""Read-only operational reports (epic #54).

`show run`, `show version`, `show flows summary`, `show flow <ip>` — commands
that summarize the fabric rather than configure it.

Deliberately **not** under ``resources/``: a resource is a transactional plugin
with a normalize/diff/apply/rollback contract and a reversibility class. These
have none of that. They never touch the candidate config, the journal, or the
transaction engine, and modeling them as resources would imply guarantees they
do not make and cannot honor.

What lives here is the machinery those commands share — chiefly
:func:`~pyecsdwan.reports.fanout.fan_out`, the bounded, failure-isolating
per-appliance fan-out every fleet-wide report needs.
"""

from __future__ import annotations

from pyecsdwan.reports.fanout import (
    DEFAULT_CONCURRENCY,
    Outcome,
    fan_out,
    unreachable,
    values,
)
from pyecsdwan.reports.versions import (
    ApplianceVersions,
    FabricVersions,
    Partition,
)

__all__ = [
    "DEFAULT_CONCURRENCY",
    "ApplianceVersions",
    "FabricVersions",
    "Outcome",
    "Partition",
    "fan_out",
    "unreachable",
    "values",
]
