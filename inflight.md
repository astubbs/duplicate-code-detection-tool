# In-flight & deferred work

Tracker for known issues that are understood but deliberately not yet fixed, so
they are not lost. Keep this current when deferring or resolving work.

## Deferred from the base-vs-PR comparison review (PR #2)

A multi-persona code review of the `compare_with_base` feature surfaced two issues
that were consciously deferred because they need more design than a follow-up patch.
The P1/P2 correctness, robustness, and security findings from that review were fixed
in PR #2; these two remain:

### 1. `base_ref` silently defaults to `master` for `issue_comment` triggers / non-`master` repos
- **Where:** `entrypoint.sh`, base-ref resolution (`.pull_request.base.ref // "master"`).
- **Problem:** `issue_comment`-triggered runs (a README-documented pattern) have no
  `.pull_request` key in the event payload, so `jq` returns null and the code falls
  back to the literal branch name `master`. Any repo whose default branch is not
  `master`, or any PR targeting a non-default base, is also affected when
  `INPUT_BASE_REF` is not set explicitly. Two failure modes:
  - No branch named `master` exists -> checkout fails -> silent fallback to absolute
    gating (see item 2).
  - A branch named `master` happens to exist -> checkout succeeds against the WRONG
    baseline -> the delta is computed against unrelated code, producing meaningless
    new/increased verdicts (false pass or false fail) with no error surfaced.
- **Fix direction:** when the event payload has no `.pull_request.base.ref`, resolve
  the PR's real base ref via the GitHub API (`GET /repos/{repo}/pulls/{id}` using the
  already-extracted `pull_request_id` + token), or fail loudly instead of defaulting
  to `master`. Needs a `bats`/shell test harness (currently `entrypoint.sh` has zero
  automated coverage).
- **Severity:** P1. Reachable for downstream consumers; the tool's own sample
  workflow only triggers on `pull_request`, so not hit in its own CI today.

### 2. Silent degrade to absolute gating with no trace in the PR comment
- **Where:** `entrypoint.sh` (base checkout / base-scan failure paths) + `run_action.py`
  (the posted `message` has no channel to say "comparison was requested but skipped").
- **Problem:** when `compare_with_base: true` is requested but the base checkout or
  base scan fails (typo'd ref, transient git/network error, or the PR itself
  restructures the scanned `directories` so the base commit legitimately BAD_INPUTs),
  only a job-log `echo` warning is emitted. The posted PR comment silently reverts to
  absolute `fail_above` gating - reintroducing the exact false-positive class this
  feature removed, but invisibly, since the author sees an ordinary report with no
  hint that comparison did nothing this run.
- **Fix direction:** thread a "comparison degraded" signal from `entrypoint.sh` to
  `run_action.py` (extra flag / env var / sentinel) so the posted comment can prepend
  a visible note, e.g. "Base comparison was requested but could not be performed
  (<reason>); this report reflects ABSOLUTE similarity, not the delta."
- **Severity:** P2.

## Known limitations (documented, as-designed - not bugs to fix here)
- **Rename-laundering:** renaming one file of a pre-existing similar pair makes it
  look like a brand-new pair (path-keyed, no content tracking), so it is judged
  against `fail_above`. Documented in `delta_to_markdown`'s docstring.
- **Salami / cumulative creep:** the per-pair delta gate has no memory across PRs, so
  similarity can ratchet up over many PRs that each stay under `max_increase`.
  Documented in `delta_to_markdown`'s docstring.
