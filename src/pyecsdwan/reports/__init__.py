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

from pyecsdwan.reports.applianceconfig import (
    ApplianceConfig,
    BroadcastResult,
    CommandRefused,
    broadcast_running_config,
    fetch_running_config,
    fetch_running_configs,
    validate_command,
)
from pyecsdwan.reports.fanout import (
    DEFAULT_CONCURRENCY,
    Outcome,
    fan_out,
    unreachable,
    values,
)

__all__ = [
    "DEFAULT_CONCURRENCY",
    "ApplianceConfig",
    "BroadcastResult",
    "CommandRefused",
    "Outcome",
    "broadcast_running_config",
    "fan_out",
    "fetch_running_config",
    "fetch_running_configs",
    "unreachable",
    "validate_command",
    "values",
]
