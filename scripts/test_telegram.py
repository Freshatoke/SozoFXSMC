"""
Task 11.1 Phase 4 — Local Telegram test command.

Sends one test message using whatever TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
are currently in the environment (or passed via --token/--chat-id).
Exits non-zero with a clear message if the channel isn't configured or the
send fails -- never claims success it didn't actually observe.

Usage:
    python scripts/test_telegram.py
    python scripts/test_telegram.py --token 123:ABC --chat-id 456
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live.notifications import TelegramNotifier, NotConfiguredError, format_trade_alert_markdown
import pandas as pd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=None, help="Overrides TELEGRAM_BOT_TOKEN env var.")
    parser.add_argument("--chat-id", default=None, help="Overrides TELEGRAM_CHAT_ID env var.")
    args = parser.parse_args()

    kwargs = {}
    if args.token:
        kwargs["bot_token"] = args.token
    if args.chat_id:
        kwargs["chat_id"] = args.chat_id
    notifier = TelegramNotifier(**kwargs)

    message = format_trade_alert_markdown(
        "Telegram Integration Test", symbol="EURUSD", strategy_id="S3", direction="bullish",
        entry=1.10000, stop_loss=1.09500, take_profit=1.11000, ios=78.5, itqs=0.82,
        reason="This is a test message from scripts/test_telegram.py -- no real trade.",
        timestamp=pd.Timestamp.now(tz="UTC"),
    )

    try:
        ok = notifier.notify("Telegram Integration Test", message)
    except NotConfiguredError as exc:
        print(f"NOT CONFIGURED: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # network/API errors -- surfaced, not swallowed
        print(f"SEND FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

    if ok:
        print("Telegram test message sent successfully.")
    else:
        print("Telegram API did not confirm delivery (unexpected response).", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
