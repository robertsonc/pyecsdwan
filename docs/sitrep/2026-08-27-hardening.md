# Sitrep — 2026-08-27 — Epic #9, first tranche (#62, #63, #65)

Follows `docs/sitrep/2026-08-27-epic54.md`.

## TL;DR

- Took the **audit batch #62–#69** rather than the roadmap's own "Next" list.
  That list leads with live verification, which is impossible here — session
  access has been read-only across four epics now.
- Shipped the two **P0s** (#62 MCP trust boundary, #63 concurrency) and the
  **P1 CI/packaging** issue (#65) that makes the other two enforceable.
- **1239 → 1313 tests.** `make check` green at every commit; `make smoke`
  (new) green against a real wheel install.
- Three separate cases this session where the obvious implementation passed
  its own test. Each is recorded below, because each was found by trying to
  break the test rather than by reading the code.

## The lock alone does not fix the lost update

The obvious reading of #63 is "add a lock around the candidate write." That is
half of it, and the wrong half on its own.

Two shells both load the candidate at T0. Serializing their *saves* means they
no longer interleave — and the second one still publishes a view of the world
from before the first one's change. The staged work of the first operator
disappears with no error and nothing in the journal.

So the mutation had to become a locked **read-modify-write**: re-read from disk
*inside* the lock, then mutate, then save. I verified this rather than assuming
it — with the re-read removed and the lock left in place, both lost-update
tests still fail:

```
FAILED test_two_stores_do_not_lose_each_others_staged_changes
FAILED test_concurrent_processes_do_not_lose_staged_changes
```

That distinction is now what those two tests are pinned to.

## flock deadlocks a process against itself

`flock` was the right primitive — the kernel drops the lock when the holding
file descriptor closes, so a SIGKILLed holder leaves nothing stale, which is a
stronger guarantee than any pid-file scheme can offer by detection.

But `flock` attaches to the **open file description**, not the process. Two
`HostLock` objects for the same host in one process hold two different
descriptions, and the second blocks on the first — forever. `commit` nests
inside commit-scoped work, so this was a deadlock waiting for a caller to find.

Re-entrancy is therefore keyed on the lock *path*, process-wide, not on the
instance. With per-instance re-entrancy the regression test reports the
process blocking on itself, by pid:

```
LockBusy: commit lock for orch.example.com is held by pid 4914 (pytest ...)
```

**A second finding fell out of the same property.** My first four
transaction-level lock tests took the lock in-process and asserted the commit
was refused. All four passed for the wrong reason at first — no, they *failed*,
"DID NOT RAISE", because the in-process holder nested instead of blocking.
Exclusion is a cross-process property and cannot be tested from one process.
There is now a `lock_holder` fixture in `conftest.py` that spawns a real
second process, and the four tests use it.

## The drift path had no test at all

`commit()` already re-fetched at commit time, noticed that the diff had moved
since compare, appended a message, **and carried on** — silently folding
another operator's concurrent change into this operator's changeset. Grepping
the suite for that behavior found nothing: it was entirely untested.

It now aborts before the first write, journals `DRIFT_ABORT`, and names what
moved. `--rebase` opts in.

**And `--rebase` had to be more than a re-diff.** For a merge-mode (`set`)
item the desired state was materialized over the *pre-drift* world, so
re-applying it would delete whatever the concurrent writer had added — the
same lost update, reached by a different route and with the operator's
consent nominally obtained. `PlanItem` now carries its originating candidate
item so a rebase re-merges intent over current state. The test asserts `mtu`
survives.

## The MCP server was never a front end over this product

The line that settles #62 is its own import:

```python
from pyedgeconnect import EdgeConnect, Orchestrator
```

It wraps the **vendored reference SDK**, not `pyecsdwan`. It shares nothing
with this product but the repository — which is why it had none of the safety
model. "Rebuild it as a curated front end over `Resource`/`txn`" is therefore
not a port; it is new code against a different library. That reframing is
recorded in `docs/futures/README.md`, and the rebuild-vs-archive decision is
left to the owner. The quarantine stands either way.

Scale of what was exposed: **641 public methods** on `Orchestrator` alone,
~250 of which write or destroy, all reflectively registered as MCP tools, with
`verify_ssl=False` and credentials accepted as tool arguments. The shipped
`claude_desktop_config.json` was a template handing out a plaintext password
with TLS off.

**Classification could not be a prefix match.** 53 `get_*` methods in the
vendored SDK issue a POST, and this repo already found `GET
/oro/debug/closeGrpcConnection` mutating behind a read-shaped verb (#67, and
the #56 work before it). A method counts as a read only if its name looks like
one **and** its body issues nothing but GETs; unreadable verbs classify as a
write.

`policy.py` imports neither `mcp` nor `pyedgeconnect`, so the security-relevant
half of the component runs under the ordinary `make check` — the old server sat
outside ruff, mypy, packaging and the test suite entirely, which is how all of
the above survived.

## A packaging guard that guarded nothing

I declared `[tool.setuptools.package-data]` for the relocated API baselines,
built a wheel, confirmed the specs were in it, and wrote a test asserting the
declaration covered every spec on disk.

Then I removed the declaration. **The specs still shipped, and `make smoke`
still passed.**

setuptools' `include-package-data` defaults to `true`, under which the files
shipped because they were tracked by git and reached the sdist. So `git add`
was deciding what landed in the wheel, my declaration was decorative, and my
test was asserting a no-op.

`include-package-data` is now explicitly `false`, and the test covers **every**
non-Python file in the package rather than only the specs. That caught
something the narrower version never would have: `py.typed` was shipping by
the same accident, and it carries the package's entire typing contract with
downstream consumers.

Verified by removal this time — dropping `py.typed` from the declaration fails
two tests.

## What #65 actually fixes

The baselines lived at the repository root, outside the package, so no wheel
ever contained them. `specs.py` degrades to an empty endpoint universe when
they are absent — deliberately, so `show coverage`'s resource table still
works — so an installed `ec-cli show coverage` reported **"0 of 0 endpoints."**
Not an error. A confident wrong answer, in the command the roadmap names as
the source of truth for coverage.

They now live in `src/pyecsdwan/_specs/`, which is where `specs_dir()` already
looked first. A clean wheel install reports `119 of 1833`.

CI asserts **1833**, not "non-zero" — the failure being guarded against
produces a number, and `> 0` would pass for a wheel carrying one spec file.

## What is NOT verified

- **The CI workflow has never run.** It is valid YAML, and every one of its
  smoke assertions was executed locally against a real clean-environment wheel
  install, but GitHub Actions has not executed the file itself. First push
  will tell.
- The `O_EXCL` lock fallback is exercised by tests that force `HAVE_FLOCK`
  false; it has not run on a platform that genuinely lacks `flock`.
- Unchanged from prior epics: every write path from epics #3 and #4 is
  spec-confirmed and has never executed against real gear. `ipEitherFlag`
  (#59) remains the sharpest single open question.

## Noted, not fixed

`CONTRIBUTING.md` describes `black`, `flake8` and PyCharm. It was inherited
from the vendored upstream project and describes neither this repository's
tooling nor its workflow. That is #68's subject, so it stays there rather than
widening #65. Recorded in `docs/futures/README.md`.

## Final state

`make check`: ruff clean, mypy `--strict` clean (94 source files — `contrib/`
is inside both now), **1313 passed, 7 skipped**. `make smoke` green against a
real wheel install.

## Next

1. **#64** (P1) — async jobs and `saveChanges` fail-closed. Same epic, and the
   remaining P1 with real safety weight: "Completed + Invalid configuration"
   currently reads as success.
2. **#69** (P1) — shared write-target collisions. Its stated dependency, the
   #63 serialization work, is now in.
3. **#66** (P1) — the evidence ladder. It is the honest answer to the
   live-verification debt that four sitreps have now deferred: it does not
   need gear, it needs "shipped" to stop meaning five different things.
4. **#67**, **#68** (P2) — retry policy per endpoint; documentation and
   metadata reconciliation.
5. Still open from #62: rebuild the MCP surface over `Resource`/`txn`, or
   archive it as a separate project.
