"""
Task 11 Phase 10 (framework) — Live Orchestrator.

Wires every prior phase into one runnable loop:

    FeedManager (P2) -> LiveMarketContext (P3) -> LiveStrategyRunner (P4)
      -> LiveDecisionEngine (P5) -> PaperBroker (P6)
    every event from every stage -> EventLogger (P7)
    every cycle -> dashboard snapshot written (P8)
    key events -> NotificationRouter (P9)

`run_cycle()` is one discrete unit of work (poll every symbol once, feed
every new candle through context/strategy/decision/broker, log
everything, refresh the dashboard) -- calling it repeatedly (in a loop,
or on a schedule) IS the forward paper trading run (Phase 10's actual
execution, driven by `scripts/run_forward_paper_trading.py`, not by this
module, which only provides the mechanism).

An EXECUTEd Decision's `Opportunity.opportunity_id` is used directly as
the resulting `PaperPosition.position_id` -- this is what lets
`PaperBroker`'s `paper_trade_closed` event map straight back to
`LiveDecisionEngine.close_position(opportunity_id, ...)` with no separate
id-translation table.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from src.live.providers.feed_manager import FeedManager
from src.live.context_stream import LiveMarketContext
from src.live.strategy_runner import LiveStrategyRunner
from src.live.decision_stream import LiveDecisionEngine
from src.live.broker import PaperBroker
from src.live.event_log import EventLogger
from src.live.dashboard import build_snapshot, write_dashboard
from src.live.notifications import (
    NotificationRouter, ConsoleNotifier, FileNotifier, TelegramNotifier, DiscordNotifier, EmailNotifier,
    format_trade_alert_markdown,
    ALERT_HIGH_IOS_OPPORTUNITY, ALERT_TRADE_OPENED, ALERT_TRADE_CLOSED, ALERT_RISK_LIMIT_REACHED,
    ALERT_DATA_FEED_DISCONNECTED, ALERT_LARGE_SPREAD_DETECTED,
)

ALERT_TRADE_APPROVED = "Trade Approved"
ALERT_STOP_LOSS_HIT = "Stop Loss Hit"
ALERT_TAKE_PROFIT_HIT = "Take Profit Hit"
ALERT_SYSTEM_RESTARTED = "System Restarted"
from src.decision_engine.risk_layer import AccountState, RiskLimits
from config.settings import ExecutionConfig, ManagementConfig

HIGH_IOS_ALERT_THRESHOLD = 75.0   # tier "A" per portfolio_allocation.ios_tier
LARGE_SPREAD_ALERT_PIPS = 3.0     # 3x the platform's default 1.0-pip assumption


class LiveOrchestrator:
    def __init__(self, symbols: list, provider, data_dir: str = "data/live",
                 execution_config: ExecutionConfig = ExecutionConfig(),
                 management_config: ManagementConfig = ManagementConfig(),
                 starting_balance: float = 10_000.0, risk_limits: RiskLimits = RiskLimits(),
                 lots_per_trade: float = 0.1):
        self.symbols = symbols
        self.lots_per_trade = lots_per_trade
        self.data_dir = Path(data_dir)
        self.started_at = pd.Timestamp.now(tz="UTC")

        self.event_logger = EventLogger(self.data_dir / "events.jsonl")
        self.feed_manager = FeedManager(provider, symbols, timeframe="M1", on_event=self.event_logger.handler("feed_manager"))
        self.contexts = {s: LiveMarketContext(symbol=s) for s in symbols}
        self.runners = {s: LiveStrategyRunner(context=self.contexts[s]) for s in symbols}
        for s, ctx in self.contexts.items():
            self.event_logger.bind_event_bus(f"context:{s}", ctx.event_bus)

        self.decision_engine = LiveDecisionEngine(
            account=AccountState(starting_balance=starting_balance, balance=starting_balance),
            risk_limits=risk_limits, on_event=self.event_logger.handler("decision_engine"),
        )
        self.broker = PaperBroker(execution_config, management_config, starting_balance,
                                   on_event=self._on_broker_event)

        self.notifier = NotificationRouter([
            ConsoleNotifier(), FileNotifier(str(self.data_dir / "notifications.log")),
            TelegramNotifier(), DiscordNotifier(), EmailNotifier(),
        ])
        self.cycles_run = 0
        # Opportunity_id -> Opportunity, for enriching broker events (which
        # only carry symbol/price/lots) with strategy_id/IOS/ITQS/stop/target
        # when building the Task 11.1 Telegram message shape. Bounded --
        # entries are dropped once the position closes (see _on_broker_event).
        self._opportunities_by_id: dict = {}

        self.notifier.notify(ALERT_SYSTEM_RESTARTED, f"Live orchestrator started for symbols {symbols}")

    # ------------------------------------------------------------------
    def _on_broker_event(self, event_type: str, detail: dict) -> None:
        self.event_logger.log("broker", event_type, detail.get("timestamp"), detail)
        opp = self._opportunities_by_id.get(detail.get("position_id"))

        if event_type == "paper_trade_closed":
            closed_pos = next((p for p in self.broker.closed_positions if p.position_id == detail["position_id"]), None)
            total_pnl = closed_pos.realized_pnl if closed_pos is not None else sum(
                pe["pnl"] for pe in detail.get("partial_exits", [])
            )
            self.decision_engine.close_position(detail["position_id"], total_pnl)
            reason = detail.get("reason", "")
            label = ALERT_STOP_LOSS_HIT if reason == "StopLoss" else (ALERT_TAKE_PROFIT_HIT if reason.startswith("TakeProfit") else ALERT_TRADE_CLOSED)
            self.notifier.notify(label, format_trade_alert_markdown(
                label, symbol=detail.get("symbol", opp.symbol if opp else None),
                strategy_id=opp.strategy_id if opp else None, direction=opp.direction if opp else None,
                entry=opp.entry if opp else None, stop_loss=opp.stop if opp else None, take_profit=opp.target if opp else None,
                ios=getattr(opp, "_ios", None) if opp else None, itqs=opp.itqs if opp else None,
                reason=f"{reason}, pnl={total_pnl:.2f}", timestamp=detail.get("timestamp"),
            ))
            self._opportunities_by_id.pop(detail.get("position_id"), None)
        elif event_type == "paper_trade_partial_exit":
            reason = detail.get("reason", "")
            if reason.startswith("TakeProfit"):
                self.notifier.notify(ALERT_TAKE_PROFIT_HIT, format_trade_alert_markdown(
                    ALERT_TAKE_PROFIT_HIT, symbol=detail.get("symbol", opp.symbol if opp else None),
                    strategy_id=opp.strategy_id if opp else None, direction=opp.direction if opp else None,
                    entry=opp.entry if opp else None, stop_loss=opp.stop if opp else None, take_profit=opp.target if opp else None,
                    ios=getattr(opp, "_ios", None) if opp else None, itqs=opp.itqs if opp else None,
                    reason=f"Partial exit {detail.get('lots')} lots @ {detail.get('exit_price')}", timestamp=detail.get("timestamp"),
                ))
        elif event_type == "paper_trade_opened":
            self.notifier.notify(ALERT_TRADE_OPENED, format_trade_alert_markdown(
                ALERT_TRADE_OPENED, symbol=detail["symbol"], strategy_id=opp.strategy_id if opp else None,
                direction=detail["direction"], entry=detail["entry_price"], stop_loss=detail.get("stop_loss"),
                take_profit=opp.target if opp else None, ios=getattr(opp, "_ios", None) if opp else None,
                itqs=opp.itqs if opp else None, reason="Paper trade opened", timestamp=detail.get("timestamp"),
            ))

    def _check_data_feed_health(self) -> None:
        hb = self.feed_manager.provider.heartbeat()
        if not hb.connected or hb.consecutive_failures > 0:
            self.notifier.notify(ALERT_DATA_FEED_DISCONNECTED,
                                  f"{hb.provider_name}: connected={hb.connected}, consecutive_failures={hb.consecutive_failures}, last_error={hb.last_error}",
                                  level="warning")

    def _check_risk_limits(self) -> None:
        risk_pct = self.decision_engine.portfolio_heat_pct
        if risk_pct >= self.decision_engine.allocation_limits.max_portfolio_risk_pct:
            self.notifier.notify(ALERT_RISK_LIMIT_REACHED, f"Portfolio heat at {risk_pct}%", level="warning")

    # ------------------------------------------------------------------
    def run_cycle(self) -> dict:
        """One full cycle: poll every symbol, feed every new candle
        through context -> strategy -> decision -> broker, log, notify,
        refresh the dashboard. Returns a small summary dict for the
        caller's own logging/printing."""
        poll_results = self.feed_manager.poll_all()
        self._check_data_feed_health()

        all_new_opportunities = []
        candles_processed = 0
        for symbol, batch in poll_results.items():
            if batch is None or batch.candles.empty:
                continue
            ctx = self.contexts[symbol]
            for row in batch.candles.itertuples(index=False):
                ctx.ingest_m1_candle(row.timestamp, row.open, row.high, row.low, row.close)
                self.broker.on_candle(symbol, row.timestamp, row.open, row.high, row.low, row.close, m1_so_far=ctx.m1)
                candles_processed += 1
                opps = self.runners[symbol].on_candle_closed()
                for opp in opps:
                    if opp.itqs >= HIGH_IOS_ALERT_THRESHOLD:
                        self.notifier.notify(ALERT_HIGH_IOS_OPPORTUNITY, format_trade_alert_markdown(
                            ALERT_HIGH_IOS_OPPORTUNITY, symbol=opp.symbol, strategy_id=opp.strategy_id,
                            direction=opp.direction, entry=opp.entry, stop_loss=opp.stop, take_profit=opp.target,
                            itqs=opp.itqs, reason="High ITQS opportunity detected, pending decision engine ranking",
                            timestamp=opp.timestamp,
                        ))
                all_new_opportunities.extend(opps)

        decisions = self.decision_engine.on_new_opportunities(all_new_opportunities)
        for d in decisions:
            if d.verdict != "EXECUTE":
                continue
            opp = d.opportunity
            opp._ios = d.ios  # decision_stream/trade_selection already set this on EXECUTE; kept explicit here for notification use
            self._opportunities_by_id[opp.opportunity_id] = opp
            self.notifier.notify(ALERT_TRADE_APPROVED, format_trade_alert_markdown(
                ALERT_TRADE_APPROVED, symbol=opp.symbol, strategy_id=opp.strategy_id, direction=opp.direction,
                entry=opp.entry, stop_loss=opp.stop, take_profit=opp.target, ios=d.ios, itqs=opp.itqs,
                reason="; ".join(d.reasons_for) or "IOS ranking + allocation checks passed", timestamp=opp.timestamp,
            ))
            self.broker.open_market_position(
                opp.symbol, opp.direction, self.lots_per_trade, opp.entry, opp.stop,
                take_profit_levels=[(opp.target, 1.0)], timestamp=opp.timestamp,
                strategy_id=opp.strategy_id, position_id=opp.opportunity_id,
            )

        self._check_risk_limits()

        snapshot = build_snapshot(
            feed_manager=self.feed_manager, contexts=self.contexts, decision_engine=self.decision_engine,
            broker=self.broker, event_logger=self.event_logger, started_at=self.started_at,
        )
        write_dashboard(self.data_dir / "dashboard.html", snapshot)

        self.cycles_run += 1
        return {
            "cycle": self.cycles_run, "candles_processed": candles_processed,
            "opportunities": len(all_new_opportunities), "decisions": len(decisions),
            "open_positions": len(self.broker.open_positions), "balance": self.broker.balance,
        }

    def shutdown(self) -> None:
        self.event_logger.close()
