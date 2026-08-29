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

## The contract any backend must meet

Before proposing a backend, note what `journal.orphaned_txns()` actually asks:
it reads the pid from `watchdog.pid` and probes it for liveness
(`watchdog_alive()`, pid + `/proc/<pid>/stat` start-time token). "Is this
transaction being driven?" is answered by **a running process**, not by a
scheduled intention.

So a backend must:

1. run a process for the whole confirm window, not merely schedule one;
2. have that process write `watchdog.pid` itself — `watch()` already does this
   via `journal.write_watchdog_pid(os.getpid())`, so running
   `python -m pyecsdwan.watchdog <txn-dir> --foreground` satisfies it for free;
3. emit the same journal events (`WATCHDOG_ARMED`, `WATCHDOG_EXIT`,
   `WATCHDOG_REVERT_*`) — again free if you run the same `watch()` loop.

`tests/test_watchdog.py::test_an_armed_txn_with_no_live_process_is_reported_orphaned`
pins point 1.

## Alternative: systemd transient service (opt-in, documented only)

```
systemd-run --user --unit=ec-watchdog-<txn-id> \
    python -m pyecsdwan.watchdog <txn-dir> --foreground
```

A transient *service* starts the watchdog immediately under the user manager:
the process exists for the whole window, writes its own pid file, and logs to
journald. The pid-file and journal-event contract is preserved exactly, so the
orphan scan keeps working. Wiring it is a ~20-line change in `watchdog.arm()`
— replace the `Popen` + double-fork with the `systemd-run` argv and keep the
existing "wait for `watchdog.pid`" loop, which is what turns a failure to start
into a refused commit-confirm.

It is NOT the default because on SSH-only accounts the systemd user manager
stops with the last session unless lingering is enabled:

```
loginctl enable-linger $USER   # requires the admin to allow it
```

Without linger, the service dies with your SSH session — strictly worse than
the double-fork daemon.

### Why not a timer

An earlier version of this document proposed
`systemd-run --user --on-active=<minutes>` — a transient **timer** — while also
promising the pid-file contract could stay identical. Those two cannot both
hold. A timer runs nothing until it fires, so for the entire confirm window
there is no watchdog pid, `watchdog_alive()` is false, and
`orphaned_txns()` reports **every armed transaction as orphaned**: the startup
scan would tell the operator that a healthy commit-confirm needs
`rollback --pending`. Worse, the obvious "fix" — loosening `watchdog_alive()`
so the false alarms stop — would also stop it detecting the real orphans it
exists for.

The timer shape is not merely unnecessary here, it is the wrong primitive: the
watchdog's job is to be *present* for the window so that its absence is
evidence, and a timer is precisely an absence with a promise attached.
