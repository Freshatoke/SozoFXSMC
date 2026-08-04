# Required GitHub Secrets

Repository → Settings → Secrets and variables → Actions.

| Secret | Required by | Description | Never do this |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `.github/workflows/telegram-scan.yml` | Bot API token from @BotFather. See `docs/TELEGRAM_SETUP_GUIDE.md` §1. | Hardcode it in a workflow file, script, or commit — it must only ever appear as `${{ secrets.TELEGRAM_BOT_TOKEN }}` in YAML or `os.environ["TELEGRAM_BOT_TOKEN"]` in Python. |
| `TELEGRAM_CHAT_ID` | `.github/workflows/telegram-scan.yml` | Destination chat/group ID. See `docs/TELEGRAM_SETUP_GUIDE.md` §2. | Same as above. |

No other secrets are required for the current (Task 11.1) scope — Discord/Email notifiers exist in `src/live/notifications.py` but are not wired into the GitHub Actions workflow; wiring them would additionally need `DISCORD_WEBHOOK_URL` and/or `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD`/`ALERT_EMAIL_TO` as secrets, following the same pattern.

`GITHUB_TOKEN` (used by the workflow's own "commit updated dedupe state" step) is provided automatically by GitHub Actions for every workflow run — it does not need to be created manually, only the `permissions: contents: write` block in the workflow YAML needs to grant it push access, which `.github/workflows/telegram-scan.yml` already does.

## Verifying secrets are set (without exposing their values)

```bash
gh secret list
```

Lists secret NAMES and last-updated timestamps only — never values. If `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are missing from this list, the workflow's Telegram step will fail with `NotConfiguredError` (see `docs/TELEGRAM_TROUBLESHOOTING.md`).
