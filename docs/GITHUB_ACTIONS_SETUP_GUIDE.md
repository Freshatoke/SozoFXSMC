# GitHub Actions Setup Guide (Temporary Deployment)

**This is a stopgap until a VPS is available.** A GitHub Actions runner is a fresh, disposable VM on every trigger — there is no continuously-running process, no persisted `LiveOrchestrator`, no paper broker. This workflow does the one thing a stateless 5-minute scan can honestly do: fetch recent candles, rebuild market context, run S3/S4, rank via IOS, and Telegram-alert on newly approved opportunities. See `docs/PRODUCTION_READINESS_REPORT_TASK11.md` and this document's final section for what changes when you move to a VPS.

## 1. Prerequisites

- The repository pushed to GitHub with Actions enabled (default for new repos).
- A Telegram bot token + chat ID (see `docs/TELEGRAM_SETUP_GUIDE.md`).

## 2. Configure the required secrets

Repository → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot's token from BotFather |
| `TELEGRAM_CHAT_ID` | Your chat/group ID |

Never commit these values anywhere in the repo — `.gitignore` already excludes `ENV.txt` and `.env` for exactly this reason. The workflow reads them only via `${{ secrets.TELEGRAM_BOT_TOKEN }}` / `${{ secrets.TELEGRAM_CHAT_ID }}`, injected as environment variables at run time (see `docs/REQUIRED_GITHUB_SECRETS.md`).

Via `gh` CLI instead of the web UI, from the repo root (values read from your own local, gitignored env file — never typed into a script):

```bash
gh secret set TELEGRAM_BOT_TOKEN --body "$TELEGRAM_BOT_TOKEN"
gh secret set TELEGRAM_CHAT_ID --body "$TELEGRAM_CHAT_ID"
```

This requires **write** access to the repository from your authenticated `gh` account — see this repo's own setup note if you hit a permissions error (Settings → Collaborators, or push access via an org role).

## 3. What the workflow does

File: `.github/workflows/telegram-scan.yml`

1. Triggers every 5 minutes (`cron: "*/5 * * * *"`) or manually (`workflow_dispatch`).
2. Checks out the repo, sets up Python 3.11, installs `requirements.txt`.
3. Runs `python scripts/telegram_scan_and_notify.py --symbols EURUSD,GBPUSD --lookback-hours 6 --state-file data/live/notified_opportunities.json`.
4. If that script updated the dedupe state file, commits and pushes it back (`git config` a bot identity, `git add`/`commit`/`push`) so the NEXT run knows what's already been alerted on.

`concurrency: group: telegram-scan, cancel-in-progress: false` ensures overlapping runs (e.g. a slow scan plus the next scheduled trigger) queue rather than race on the same state file.

## 4. Testing it

- **Manual trigger**: Actions tab → "Telegram Scan (Temporary)" → "Run workflow". Check the run's logs for `Sent N new alert(s), skipped M already-notified.`
- **Local dry run** (same script, same behavior, easier to debug):
  ```bash
  export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
  python scripts/telegram_scan_and_notify.py --symbols EURUSD --lookback-hours 6 --state-file data/live/notified_opportunities.json
  ```

## 5. Known limitations of this temporary approach

- **No paper trading in the Action.** Only opportunity scanning + alerting. Positions are never opened/closed here (there is no persisted broker to open them in) — this workflow is monitoring, not the forward-paper-trading run itself.
- **No live-account state.** Every run starts a fresh `AccountState`/`LiveDecisionEngine` — portfolio heat, currency exposure, etc. are evaluated per-scan, not carried across runs. A given "approved" opportunity in one scan does not reduce capacity for the next scan the way a continuously running decision engine would.
- **Duplicate-alert prevention is best-effort, not atomic.** The dedupe state file is committed via a normal `git push`; two overlapping runs (mitigated by the `concurrency` group above, but not impossible under GitHub's own scheduling jitter) could theoretically race. Acceptable for a temporary monitoring tool; not acceptable for the eventual production broker-execution path.
- **Cron cadence is approximate.** GitHub's schedule triggers are "best effort" and can be delayed several minutes under load — do not treat `*/5 * * * *` as a real-time guarantee.

## 6. Migrating to a VPS

When a VPS is available:

1. Install the same Python environment (`pip install -r requirements.txt`).
2. Set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` as real environment variables (or a systemd `EnvironmentFile`).
3. Run `scripts/run_forward_paper_trading.py` continuously (under systemd/supervisor, restart-on-failure) instead of `scripts/telegram_scan_and_notify.py` on a cron.
4. **The Telegram notification code (`src/live/notifications.py`, `format_trade_alert_markdown`) is reused unchanged** — `LiveOrchestrator` already wires it up with full paper-broker lifecycle alerts (opened/closed/SL/TP/risk/feed/restart), which the temporary Action never attempted.
5. Delete or disable `.github/workflows/telegram-scan.yml` to avoid duplicate alerts from both the Action and the VPS.
