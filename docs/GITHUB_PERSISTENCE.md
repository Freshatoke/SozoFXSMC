# GitHub Persistence (Task 11.3 Phase 9)

How the platform survives GitHub Actions' fundamental constraint: **every workflow run is a fresh, disposable virtual machine.** Nothing written to a runner's local disk exists after that job ends, unless it's explicitly persisted somewhere durable before the job finishes.

## The mechanism: commit state back to the repository

Every workflow that produces state to keep ends with a commit-and-push step:

```yaml
- name: Commit updated operational state
  run: |
    git config user.name "github-actions[bot]"
    git config user.email "github-actions[bot]@users.noreply.github.com"
    git add <paths this workflow owns>
    if ! git diff --cached --quiet; then
      git commit -m "chore: ... [skip ci]"
      git pull --rebase origin "${GITHUB_REF_NAME}"
      git push
    else
      echo "No state change to commit."
    fi
```

- `[skip ci]` in the commit message prevents this commit from re-triggering any push-based workflow (not currently an issue since both workflows trigger on `schedule`/`workflow_dispatch`, not `push`, but it's a defensive habit worth keeping if that ever changes).
- The `if ! git diff --cached --quiet` guard means a no-op scan (nothing new happened) makes zero commits — the repo's history stays meaningful, not spammed with empty commits.
- `git pull --rebase` immediately before `push` handles the case where the OTHER scheduled workflow (or an overlapping run) pushed in the gap between this job's checkout and its own commit — see `docs/OPERATIONAL_STATE_ARCHITECTURE.md` for why this rarely produces a real conflict given the append-only nature of every file involved.

## What's persisted, by workflow

| Workflow | Commits | Cadence |
|---|---|---|
| `telegram-scan.yml` | `data/live/notified_opportunities.json` (Task 11.1's dedupe state), `data/live/journal/activity/<date>.jsonl` (Task 11.3's operational activity) | Every 5 minutes |
| `daily-report.yml` | `data/live/journal/reports/<date>.json`, `data/live/journal/operational/<date>.txt`, `data/live/journal/learning_log.jsonl`, `docs/journal/LEARNING_LOG.md` | Once daily, 21:00 UTC |

## Required repository permissions

Both workflows need `permissions: contents: write` in their YAML (already set) AND the repository itself must allow the default `GITHUB_TOKEN` to write:

**Settings → Actions → General → Workflow permissions → "Read and write permissions"**

Without this repo-level setting, the `git push` step fails with a permissions error even though the workflow YAML requests write access — the YAML `permissions:` block can only narrow what the repo setting already allows, never widen it.

## Surviving a runner restart mid-day

Because every 5-minute run independently commits its own contribution, the platform can lose any individual run (a GitHub Actions outage, a cancelled job, a transient failure) without losing the rest of the day's history — the next successful run simply appends its own activity on top of whatever's already in the repo. `generate_daily_report.py`, run at day's end, reads the FULL accumulated `data/live/journal/activity/<date>.jsonl` from the repo (not from any single run's local state), so a report never depends on any one execution having survived.

## What this does NOT solve

- **Real-time state within a single 5-minute window.** If two scans somehow ran concurrently (shouldn't happen given `concurrency: group: telegram-scan`), there's no locking beyond git's own conflict handling.
- **Sub-second consistency.** This is a batch/commit model, not a live database — the dashboard/report always reflects "as of the last committed scan," never a mid-scan intermediate state.
- **Very high write frequency.** Every 5 minutes is comfortably within what a git-commit-based approach can handle; this would NOT scale to per-second updates (each commit has real overhead: checkout, diff, commit, rebase, push). If Task 11.3's cadence ever needs to tighten significantly, a real database (the VPS-hosted platform's eventual home per `docs/LIVE_DEPLOYMENT_GUIDE_TASK11.md`) is the right next step, not more aggressive git commits.
