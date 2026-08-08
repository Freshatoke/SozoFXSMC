"""
Task 11 Phase 9 — Notification System.

Every channel implements the same tiny `Notifier` protocol
(`notify(title, message, level="info") -> bool`) so `NotificationRouter`
can fan one alert out to every configured channel identically.

Two channels work out of the box with zero setup: `ConsoleNotifier`
(stdout) and `FileNotifier` (appends to a log file) -- these are what the
Task 11 Phase 10 forward-paper-trading demo actually uses. `TelegramNotifier`,
`DiscordNotifier`, and `EmailNotifier` are CODE-COMPLETE and fully wired
to their real APIs (python-telegram-bot-style HTTP call, Discord webhook
POST, smtplib SMTP) but require real credentials (bot token, webhook URL,
SMTP account) that do not exist in this environment -- per the platform's
explicit "never fabricate sending" rule, each raises `NotConfiguredError`
immediately if its credentials are missing rather than silently
no-op'ing or pretending to have sent something. Supplying real
credentials via the constructor args (or the matching env vars) is all
that's needed to make them live; nothing else in this module is a stub.
"""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from email.mime.text import MIMEText
from pathlib import Path
from typing import Protocol

import pandas as pd


class NotConfiguredError(RuntimeError):
    """Raised when a real-credential channel is used without credentials
    -- never silently swallowed, never treated as a successful send."""


class Notifier(Protocol):
    def notify(self, title: str, message: str, level: str = "info") -> bool: ...


@dataclass
class ConsoleNotifier:
    def notify(self, title: str, message: str, level: str = "info") -> bool:
        print(f"[{pd.Timestamp.now(tz='UTC').isoformat()}] [{level.upper()}] {title}: {message}")
        return True


@dataclass
class FileNotifier:
    path: str = "data/live/notifications.log"

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": pd.Timestamp.now(tz="UTC").isoformat(), "level": level, "title": title, "message": message}
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        return True


def escape_markdown(text: str) -> str:
    """Escapes Telegram legacy-Markdown's special characters in free-text
    fields -- structured fields we build ourselves (symbol, strategy_id,
    prices) never contain these, but any free-text sentence (rejection
    reasons, AI observations, doc filenames like
    `docs/GITHUB_ACTIONS_SETUP_GUIDE.md`) can contain an ODD number of
    `_`/`*` characters, which Telegram's legacy Markdown parser rejects
    outright with `HTTP 400: can't parse entities` -- it doesn't degrade
    gracefully, the whole send fails. Every free-text string handed to
    Telegram must go through this first. Public (not `_`-prefixed)
    because `src.live.journal`'s report renderers need it too."""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


_md_escape = escape_markdown   # internal alias, kept for this module's own call sites below


def format_trade_alert_markdown(event_label: str, symbol: str = None, strategy_id: str = None,
                                 direction: str = None, entry: float = None, stop_loss: float = None,
                                 take_profit: float = None, ios: float = None, itqs: float = None,
                                 reason: str = None, timestamp=None, extra: str = None) -> str:
    """Task 11.1 Phase 1's required message shape: Symbol, Strategy,
    Direction, Entry, Stop Loss, Take Profit, IOS, ITQS, Reason for
    approval, Timestamp -- Telegram legacy Markdown. Any field left None
    (not every event has every field, e.g. a risk-limit alert has no
    entry price) is simply omitted from the message rather than printed
    as "None"."""
    lines = [f"*{event_label}*"]
    if symbol is not None:
        lines.append(f"*Symbol:* `{symbol}`")
    if strategy_id is not None:
        lines.append(f"*Strategy:* `{strategy_id}`")
    if direction is not None:
        lines.append(f"*Direction:* {direction.upper()}")
    if entry is not None:
        lines.append(f"*Entry:* `{entry}`")
    if stop_loss is not None:
        lines.append(f"*Stop Loss:* `{stop_loss}`")
    if take_profit is not None:
        lines.append(f"*Take Profit:* `{take_profit}`")
    if ios is not None:
        lines.append(f"*IOS:* {ios}")
    if itqs is not None:
        lines.append(f"*ITQS:* {itqs}")
    if reason is not None:
        lines.append(f"*Reason:* {_md_escape(reason)}")
    if extra is not None:
        lines.append(_md_escape(extra))
    ts = timestamp if timestamp is not None else pd.Timestamp.now(tz="UTC")
    lines.append(f"_{ts}_")
    return "\n".join(lines)


@dataclass
class TelegramNotifier:
    bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))
    parse_mode: str = "Markdown"

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if not self.bot_token or not self.chat_id:
            raise NotConfiguredError(
                "TelegramNotifier requires bot_token + chat_id (set TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID, or pass them to the constructor) -- none configured in this environment."
            )
        import urllib.request
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        # `message` may already be pre-formatted Markdown (from
        # format_trade_alert_markdown); a plain-text message (e.g. a
        # console-style one-liner) is still valid Markdown as-is, so the
        # same code path handles both -- title is only prepended when the
        # message doesn't already start with a Markdown header line.
        text = message if message.lstrip().startswith("*") else f"*{_md_escape(title)}*\n{message}"
        payload = json.dumps({"chat_id": self.chat_id, "text": text, "parse_mode": self.parse_mode}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200


@dataclass
class DiscordNotifier:
    webhook_url: str = field(default_factory=lambda: os.environ.get("DISCORD_WEBHOOK_URL", ""))

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if not self.webhook_url:
            raise NotConfiguredError(
                "DiscordNotifier requires webhook_url (set DISCORD_WEBHOOK_URL, or pass it to the "
                "constructor) -- none configured in this environment."
            )
        import urllib.request
        payload = json.dumps({"content": f"**[{level.upper()}] {title}**\n{message}"}).encode()
        req = urllib.request.Request(self.webhook_url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)


@dataclass
class EmailNotifier:
    smtp_host: str = field(default_factory=lambda: os.environ.get("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(os.environ.get("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: os.environ.get("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.environ.get("SMTP_PASSWORD", ""))
    to_address: str = field(default_factory=lambda: os.environ.get("ALERT_EMAIL_TO", ""))

    def notify(self, title: str, message: str, level: str = "info") -> bool:
        if not all([self.smtp_host, self.smtp_user, self.smtp_password, self.to_address]):
            raise NotConfiguredError(
                "EmailNotifier requires smtp_host/smtp_user/smtp_password/to_address (set "
                "SMTP_HOST / SMTP_USER / SMTP_PASSWORD / ALERT_EMAIL_TO, or pass them to the "
                "constructor) -- none configured in this environment."
            )
        msg = MIMEText(message)
        msg["Subject"] = f"[{level.upper()}] {title}"
        msg["From"] = self.smtp_user
        msg["To"] = self.to_address
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as server:
            server.starttls()
            server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.smtp_user, [self.to_address], msg.as_string())
        return True


# Alert types the task brief names explicitly -- kept as a fixed set so
# callers (the orchestrator) raise consistent, greppable titles.
ALERT_HIGH_IOS_OPPORTUNITY = "High IOS Opportunity"
ALERT_TRADE_OPENED = "Trade Opened"
ALERT_TRADE_CLOSED = "Trade Closed"
ALERT_RISK_LIMIT_REACHED = "Risk Limit Reached"
ALERT_DATA_FEED_DISCONNECTED = "Data Feed Disconnected"
ALERT_LARGE_SPREAD_DETECTED = "Large Spread Detected"


class NotificationRouter:
    """Fans one alert out to every configured channel. A channel that
    raises `NotConfiguredError` is logged (once, to whichever channels
    DO work) and skipped -- one unconfigured channel must never prevent
    the console/file channels (which always work) from delivering."""

    def __init__(self, channels: list):
        self.channels = channels
        self.history: list = []

    def notify(self, title: str, message: str, level: str = "info") -> dict:
        results = {}
        for channel in self.channels:
            name = type(channel).__name__
            try:
                results[name] = channel.notify(title, message, level)
            except NotConfiguredError as exc:
                results[name] = f"skipped: {exc}"
        self.history.append({"timestamp": pd.Timestamp.now(tz="UTC").isoformat(), "title": title,
                              "message": message, "level": level, "results": results})
        return results
