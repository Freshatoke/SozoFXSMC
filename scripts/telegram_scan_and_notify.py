"""
Task 11.1 Phase 3 — GitHub Actions scan-and-notify script.

Runs ONE stateless scan: fetch recent live candles -> update market
context -> run S3/S4 (unmodified) -> rank via the decision engine's IOS
(unmodified) -> send a Telegram alert for every NEWLY approved
opportunity. No paper broker here on purpose -- the temporary GitHub
Actions runner is a fresh VM every 5 minutes with no persisted process,
so simulating trade lifecycle (open/SL/TP/close) inside it would just be
state that gets silently discarded every run. Monitoring/alerting is
what a stateless scheduled scan can honestly do; full paper trading
belongs on the eventual always-on VPS via `src.live.orchestrator.LiveOrchestrator`
/ `scripts/run_forward_paper_trading.py`, unchanged.

Deduplication: a GitHub Actions runner has no memory between runs, so
the SAME underlying signal can be re-detected on the next scan if it's
still inside the (bounded) lookback window -- `LiveStrategyRunner`'s own
in-memory signal_id dedup only protects within one process's few-second
lifetime, which is not enough here. `--state-file` (default
data/live/notified_opportunities.json, committed back to the repo by
the workflow after each run) is the actual duplicate-prevention
mechanism: every opportunity_id ever alerted on is recorded there and
checked before sending.

Trading logic, IOS, and ITQS are used exactly as Task 11 built them --
nothing in `src.strategies`, `src.decision_engine.ios`, or
`src.research.itqs` is touched by this script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from src.live.providers.dukascopy_live import DukascopyLiveProvider, ProviderConnectionError
from src.live.context_stream import LiveMarketContext
from src.live.strategy_runner import LiveStrategyRunner
from src.live.decision_stream import LiveDecisionEngine
from src.decision_engine.risk_layer import AccountState
from src.live.notifications import TelegramNotifier, NotConfiguredError, format_trade_alert_markdown
from src.live.journal import DailyActivityRecorder

ALERT_APPROVED_OPPORTUNITY = "Approved Trade Opportunity"
STATE_RETENTION_DAYS = 14   # dedupe entries older than this are pruned (state file must not grow forever)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def prune_state(state: dict, now: pd.Timestamp) -> dict:
    cutoff = now - pd.Timedelta(days=STATE_RETENTION_DAYS)
    return {oid: ts for oid, ts in state.items() if pd.Timestamp(ts) >= cutoff}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="EURUSD,GBPUSD")
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--state-file", default="data/live/notified_opportunities.json")
    parser.add_argument("--activity-dir", default="data/live/journal/activity",
                         help="Task 11.2: where per-scan activity is recorded for the daily intelligence report.")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    state_path = Path(args.state_file)
    now = pd.Timestamp.now(tz="UTC")
    state = prune_state(load_state(state_path), now)

    notifier = TelegramNotifier()
    provider = DukascopyLiveProvider(lookback_hours=args.lookback_hours, timeout=20)
    recorder = DailyActivityRecorder(activity_dir=args.activity_dir)

    account = AccountState()
    decision_engine = LiveDecisionEngine(account=account)

    all_new_opportunities = []
    for symbol in symbols:
        ctx = LiveMarketContext(symbol=symbol)
        runner = LiveStrategyRunner(context=ctx)
        try:
            provider.connect()
            batch = provider.poll(symbol, "M1", since=None)
        except ProviderConnectionError as exc:
            print(f"[{symbol}] provider error: {exc}", file=sys.stderr)
            recorder.record_feed_error(symbol, str(exc))
            continue

        for row in batch.candles.itertuples(index=False):
            ctx.ingest_m1_candle(row.timestamp, row.open, row.high, row.low, row.close)
            all_new_opportunities.extend(runner.on_candle_closed())

        recorder.record_scan(symbol, len(batch.candles))
        print(f"[{symbol}] candles={len(batch.candles)} opportunities={len(all_new_opportunities)}")

    decisions = decision_engine.on_new_opportunities(all_new_opportunities)
    for d in decisions:
        recorder.record_decision(d)
    approved = [d for d in decisions if d.verdict == "EXECUTE"]
    print(f"Decisions: {len(decisions)} total, {len(approved)} approved.")

    sent, skipped = 0, 0
    for d in approved:
        opp = d.opportunity
        if opp.opportunity_id in state:
            skipped += 1
            continue

        message = format_trade_alert_markdown(
            ALERT_APPROVED_OPPORTUNITY, symbol=opp.symbol, strategy_id=opp.strategy_id, direction=opp.direction,
            entry=opp.entry, stop_loss=opp.stop, take_profit=opp.target, ios=d.ios, itqs=opp.itqs,
            reason="; ".join(d.reasons_for) or "IOS ranking + allocation checks passed", timestamp=opp.timestamp,
        )
        try:
            notifier.notify(ALERT_APPROVED_OPPORTUNITY, message)
            sent += 1
        except NotConfiguredError as exc:
            print(f"Telegram not configured: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"Telegram send failed for {opp.opportunity_id}: {exc}", file=sys.stderr)
            continue  # do NOT mark as notified if the send failed -- retry next scan

        state[opp.opportunity_id] = str(opp.timestamp)

    save_state(state_path, state)
    print(f"Sent {sent} new alert(s), skipped {skipped} already-notified. State file: {state_path} ({len(state)} entries).")


if __name__ == "__main__":
    main()
