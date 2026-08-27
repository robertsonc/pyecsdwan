# CLI design corpus

Issue #73, blocking the taxonomy decision in #71 and the epic in #70.

"Junos-flavored" was a loose description rather than a documented set of
choices. That is a risk in a specific way: copying familiar tokens without
comparing their semantics makes a CLI *look* conventional while behaving
unexpectedly — which is worse than looking unfamiliar, because it defeats the
operator's correct instincts.

This corpus records what each source actually does, what transfers, what does
not, and the resulting decision.

## Contents

| File | Purpose |
|---|---|
| `precedent-matrix.md` | Nine dimensions compared across the corpus, each resolved into a decision. **Start here.** |
| `sources/junos.md` | Mode-carried intent; format as a modifier. |
| `sources/cisco-nso.md` | Closest structural analogue: model-driven multi-device controller with transactional commit. |
| `sources/openconfig-gnmi.md` | The CONFIG/STATE/OPERATIONAL split, and the error taxonomy. |
| `sources/cisco-ios.md` | BGP operational vocabulary operators already have. |
| `sources/nvue.md` | applied/startup/operational vocabulary — and the one rejected technique. |
| `sources/kubectl.md` | Verb separation, discoverability, format orthogonality. |
| `sources/edgeconnect.md` | The constraints every borrowed pattern must survive. |

## Rules this corpus follows

1. **Primary sources only for normative claims.** Where a source could not be
   read, that is recorded rather than filled in from general knowledge — see
   the source-quality note in `sources/cisco-ios.md`, where #73's suggested
   PDF was unreadable and a general-knowledge answer was discarded.
2. **Summarize and link; do not copy.** Vendor documentation is quoted only
   where a short exact phrase carries the meaning, and always attributed.
3. **Every borrowed pattern says why it fits; every rejected one says why not.**
   The rejections are the more useful half — `sources/nvue.md` and
   `sources/kubectl.md` carry the substantive ones.
4. **EdgeConnect reality wins.** `sources/edgeconnect.md` is the constraint
   list. A pattern that reads well but that the API cannot honestly answer is
   rejected, however good its pedigree.
5. **Access dates recorded**, because vendor documentation moves.

## What this does not do

It does not choose the grammar. It produces the decisions (`D-*`) the grammar
is built from; `specs/001-cli-command-taxonomy/spec.md` does the choosing and
cites them.
