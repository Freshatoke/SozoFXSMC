"""
Task 11 Phase 10 — Forward Paper Trading demo runner.

Runs `LiveOrchestrator` for a BOUNDED number of cycles against the real
Dukascopy near-live feed and prints a summary. This is NOT the "weeks of
unattended operation" the task brief's Phase 12 asks for -- it is the
"framework + a short bounded demo, document the gap honestly" scope the
user explicitly chose for this task. See docs/PRODUCTION_READINESS_REPORT_TASK11.md
for the honest evidence-gap statement this demo's output feeds into.

Usage: python scripts/run_forward_paper_trading.py [--cycles N] [--interval SECONDS] [--symbols EURUSD,GBPUSD]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.live.providers.dukascopy_live import DukascopyLiveProvider, ProviderConnectionError
from src.live.orchestrator import LiveOrchestrator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds between poll cycles.")
    parser.add_argument("--symbols", type=str, default="EURUSD,GBPUSD")
    parser.add_argument("--lookback-hours", type=int, default=6)
    parser.add_argument("--data-dir", type=str, default="data/live/forward_test")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    provider = DukascopyLiveProvider(lookback_hours=args.lookback_hours, timeout=20)
    orch = LiveOrchestrator(symbols=symbols, provider=provider, data_dir=args.data_dir, lots_per_trade=0.1)

    print(f"Starting bounded forward paper trading demo: {args.cycles} cycles, {args.interval}s interval, symbols={symbols}")
    for i in range(args.cycles):
        t0 = time.time()
        try:
            summary = orch.run_cycle()
        except ProviderConnectionError as exc:
            print(f"  cycle {i + 1}: PROVIDER ERROR: {exc}")
            summary = None
        dt = time.time() - t0
        print(f"  cycle {i + 1}/{args.cycles}: {summary} ({dt:.1f}s)")
        if i < args.cycles - 1:
            remaining = max(0.0, args.interval - dt)
            time.sleep(remaining)

    orch.shutdown()

    events = orch.event_logger.read_all()
    print(f"\nDone. {orch.cycles_run} cycles run. {len(events)} events logged to {args.data_dir}/events.jsonl")
    print(f"Dashboard: {args.data_dir}/dashboard.html")
    print(f"Final balance: {orch.broker.balance:.2f}")
    print(f"Open positions: {len(orch.broker.open_positions)}, Closed positions: {len(orch.broker.closed_positions)}")
    print(f"Decision cycles: {orch.decision_engine.cycles_run}, Approved: {len(orch.decision_engine.approved_log)}, Rejected: {len(orch.decision_engine.rejected_log)}")


if __name__ == "__main__":
    main()
