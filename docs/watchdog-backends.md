# Commit-confirm watchdog backends

The rollback watchdog must outlive the SSH session that armed it.

## Default: detached daemon (double-fork/setsid)

`python -m pyecsdwan.watchdog <txn-dir>` daemonizes itself: fork → setsid →
fork, cwd `/`, umask 077, fds → `<txn-dir>/watchdog.log`. Immune to SIGHUP and
SSH teardown; reparented to init/systemd. Requires nothing from the OS beyond
POSIX — this is why it is the default.

Failure modes and their covers:

| Failure | Cover |
|---|---|
| Watchdog never starts | `arm()` waits for `watchdog.pid`; on timeout the commit auto-reverts immediately |
| Host reboots inside the window | startup orphan scan on every CLI invocation → `rollback --pending` |
| Watchdog killed manually | same orphan scan (pid liveness probe) |
| Revert itself fails | journal state `REVERT_FAILED`, loud in `show journal`; events name exactly what is left un-reverted |

## Alternative: systemd user timer (opt-in, documented only)

A `systemd-run --user --on-active=<minutes>` transient timer invoking
`python -m pyecsdwan.watchdog <txn-dir> --foreground` would survive more kill
scenarios and integrate with journald. It is NOT the default because on
SSH-only accounts the systemd user manager stops with the last session unless
lingering is enabled:

```
loginctl enable-linger $USER   # requires the admin to allow it
```

Without linger, the timer dies with your SSH session — strictly worse than the
double-fork daemon. If your server already runs your user manager persistently,
wiring this backend is a ~20-line change in `watchdog.arm()`; keep the pid-file
and journal-event contract identical so the orphan scan keeps working.
