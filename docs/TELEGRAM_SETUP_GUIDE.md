# Telegram Setup Guide

How to get a bot token + chat ID, and how the platform uses them. No credentials are ever hardcoded anywhere in this repo — they are read from environment variables (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) everywhere, locally and in CI.

## 1. Create a bot

1. Open Telegram, search for **@BotFather**, start a chat.
2. Send `/newbot`, follow the prompts (choose a name and a unique username ending in `bot`).
3. BotFather replies with a token that looks like `123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` — this is `TELEGRAM_BOT_TOKEN`. Treat it like a password: anyone with it can send messages as your bot.

## 2. Get your chat ID

Simplest option — message your own bot directly:

1. Open a chat with your new bot (search its username, click Start).
2. Send it any message.
3. Visit `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates` in a browser (substitute your real token).
4. Find `"chat":{"id": ...}` in the JSON response — that number (can be negative for groups) is `TELEGRAM_CHAT_ID`.

For a group chat: add the bot to the group, send a message mentioning it, then use the same `getUpdates` call — the group's `id` will be negative.

## 3. Where these values are used

| Location | Purpose |
|---|---|
| Local shell env vars (`export TELEGRAM_BOT_TOKEN=...`) or a local `.env`/`ENV.txt` (gitignored, never committed) | Running the platform or `scripts/test_telegram.py` on your own machine. |
| GitHub Actions secrets (`Settings -> Secrets and variables -> Actions`) | The temporary scheduled scan workflow (`.github/workflows/telegram-scan.yml`) — see `docs/GITHUB_ACTIONS_SETUP_GUIDE.md`. |
| Eventual VPS environment variables | When the platform migrates off GitHub Actions (see that guide's final section) — same two variables, same code, different host. |

`src.live.notifications.TelegramNotifier` reads both from `os.environ` by default (`field(default_factory=lambda: os.environ.get(...))`), or accepts them as constructor args for testing. It is used identically everywhere in the codebase — `LiveOrchestrator`, `scripts/run_forward_paper_trading.py`, and `scripts/telegram_scan_and_notify.py` all construct a plain `TelegramNotifier()` and rely on the environment.

## 4. Message format

Every trade-related alert (`format_trade_alert_markdown` in `src/live/notifications.py`) uses Telegram's legacy Markdown (`parse_mode: "Markdown"`) and includes, when applicable to that event: Symbol, Strategy, Direction, Entry, Stop Loss, Take Profit, IOS, ITQS, Reason, Timestamp. Non-trade alerts (data feed disconnected, risk limit reached, system restarted) are plain text.

## 5. Testing your setup

```bash
export TELEGRAM_BOT_TOKEN=your_token_here
export TELEGRAM_CHAT_ID=your_chat_id_here
python scripts/test_telegram.py
```

Prints `Telegram test message sent successfully.` on success, or a clear `NOT CONFIGURED` / `SEND FAILED` message on `stderr` with a non-zero exit code otherwise — see `docs/TELEGRAM_TROUBLESHOOTING.md` if it fails.

## 6. Events that trigger a Telegram alert

| Event | Where it fires |
|---|---|
| Approved trade opportunity | `LiveOrchestrator.run_cycle()` (full platform) and `scripts/telegram_scan_and_notify.py` (temporary GitHub Actions scan) |
| Paper trade opened | `LiveOrchestrator` only (the scan-only workflow does not run a paper broker — see the GitHub Actions guide) |
| Paper trade closed / Stop Loss hit / Take Profit hit | `LiveOrchestrator` only |
| Risk limit reached | `LiveOrchestrator` only |
| Data feed disconnected | `LiveOrchestrator` and the scan script (via its own provider error handling) |
| System restarted | `LiveOrchestrator.__init__` only |
