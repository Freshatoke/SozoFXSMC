"""
Task 12.2 — Live/Batch Context Parity Harness.

RESEARCH-ONLY, read-only with respect to production. Does not modify
S3, S4, ITQS, IOS, the Decision Engine, the Paper Broker, or the live
scanner. It only constructs a batch `MarketContext` and a live
`LiveMarketContext` from real historical data and RUNS THE EXISTING,
UNMODIFIED `generate_signals()` functions from `src.strategies.s3_liquidity_sweep`
and `src.strategies.s4_pdh_pdl_sweep` against each, then compares outputs.

PARITY CONTRACT (Task 12.2 Phase 2)
------------------------------------
For a given evaluation timestamp T:

  BATCH-AS-OF-T = what `s3/s4.generate_signals()` returns when run
      against a `MarketContext` built from the FULL available history up
      to and including T, restricted to signals whose own `timestamp`
      field equals T. This is already point-in-time-safe by construction
      (`MarketContext`'s "asof" helpers only ever read data with
      `creation_timestamp`/`confirmed_timestamp`/`break_candle_timestamp`
      <= the queried timestamp) -- no special reconstruction is needed;
      it is what the existing, already-no-lookahead-tested batch engine
      produces when queried at T, which is the reference/ground truth.

  LIVE-AS-OF-T(lookback) = what `s3/s4.generate_signals()` returns when
      run against a FRESH `LiveMarketContext` that has ingested ONLY the
      M1 candles in the half-open window (T - lookback, T], restricted
      to signals whose own `timestamp` field equals T. This simulates
      exactly what a stateless GitHub-Actions-style scan cycle, running
      at time T with a `--lookback-hours` window of `lookback`, would
      have produced -- the live scanner IS rebuilt from scratch every
      cycle (see `scripts/telegram_scan_and_notify.py`), so this is not
      a simplification, it is a faithful simulation of the real
      deployment's actual behavior.

The comparison is BATCH-AS-OF-T vs. LIVE-AS-OF-T(lookback) -- never
"full-history batch" vs. "live's current running state" (which would
conflate two different questions and is explicitly forbidden by this
task's Phase 2).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.strategies.context import MarketContext
from src.strategies import s3_liquidity_sweep, s4_pdh_pdl_sweep
from src.live.context_stream import LiveMarketContext


def build_batch_ground_truth(symbol: str, m1: pd.DataFrame, s3_config=None, s4_config=None) -> list:
    """Runs S3+S4, UNMODIFIED, against a full-history batch MarketContext.
    Returns a flat list of dicts, one per signal, each carrying every
    field Phase 7 needs to compare (strategy, direction, timestamp,
    entry, stop, target, referenced OB/liquidity/CHoCH ids)."""
    context = MarketContext(symbol=symbol, m1=m1)
    signals = []
    if s3_config is None or s3_config.enabled:
        signals += s3_liquidity_sweep.generate_signals(context, s3_config) if s3_config else s3_liquidity_sweep.generate_signals(context)
    if s4_config is None or s4_config.enabled:
        signals += s4_pdh_pdl_sweep.generate_signals(context, s4_config) if s4_config else s4_pdh_pdl_sweep.generate_signals(context)
    return [_signal_to_row(s) for s in signals]


def _signal_to_row(s) -> dict:
    snap = s.confluence_snapshot or {}
    low, high = s.entry_zone
    return {
        "strategy_id": s.strategy_id, "symbol": s.symbol, "direction": s.direction,
        "timestamp": s.timestamp, "entry_low": low, "entry_high": high,
        "stop_loss_reference": s.stop_loss_reference, "target_reference": s.target_reference,
        "order_block_id": snap.get("order_block_id"), "liquidity_id": snap.get("liquidity_id"),
        "choch_timestamp": snap.get("choch_timestamp"), "swept_timestamp": snap.get("swept_timestamp"),
        "confidence_score": s.confidence_score,
    }


def build_live_context_at(symbol: str, m1_full: pd.DataFrame, t: pd.Timestamp, lookback: pd.Timedelta) -> LiveMarketContext:
    """Builds a FRESH LiveMarketContext and ingests only the M1 candles
    in (t - lookback, t] -- faithfully simulating one stateless live scan
    cycle occurring immediately after candle t closes, with a
    `--lookback-hours`-equivalent window of `lookback`."""
    window = m1_full[(m1_full["timestamp"] > t - lookback) & (m1_full["timestamp"] <= t)]
    ctx = LiveMarketContext(symbol=symbol)
    for row in window.itertuples(index=False):
        ctx.ingest_m1_candle(row.timestamp, row.open, row.high, row.low, row.close)
    return ctx


def live_signals_at(symbol: str, m1_full: pd.DataFrame, t: pd.Timestamp, lookback: pd.Timedelta,
                     s3_config=None, s4_config=None) -> list:
    """LIVE-AS-OF-T(lookback): all S3/S4 signals a live scan cycle at t
    (with this lookback) would produce, filtered to those actually
    timestamped exactly t (the "would this scan cycle itself have
    surfaced this opportunity" question -- a live scan at a LATER time
    t' > t would find the same opportunity if it's still within ITS OWN
    lookback window, which is a separate, correctly-scoped question this
    harness does not conflate with "did THIS cycle see it")."""
    ctx = build_live_context_at(symbol, m1_full, t, lookback)
    signals = []
    if s3_config is None or s3_config.enabled:
        signals += s3_liquidity_sweep.generate_signals(ctx, s3_config) if s3_config else s3_liquidity_sweep.generate_signals(ctx)
    if s4_config is None or s4_config.enabled:
        signals += s4_pdh_pdl_sweep.generate_signals(ctx, s4_config) if s4_config else s4_pdh_pdl_sweep.generate_signals(ctx)
    return [_signal_to_row(s) for s in signals]


@dataclass
class ParityResult:
    lookback_label: str
    lookback: pd.Timedelta
    n_batch_total: int
    n_evaluated: int          # number of batch ground-truth signals actually tested (may be a bounded sample)
    n_matched: int
    n_missed: int
    n_live_extra: int          # live signals at evaluated T's not present in batch ground truth (false positives)
    matched_ids: list = field(default_factory=list)
    missed_ids: list = field(default_factory=list)
    runtime_seconds: float = 0.0

    @property
    def recall(self) -> Optional[float]:
        return round(self.n_matched / self.n_evaluated, 4) if self.n_evaluated else None

    @property
    def precision(self) -> Optional[float]:
        denom = self.n_matched + self.n_live_extra
        return round(self.n_matched / denom, 4) if denom else None


def _row_id(row: dict) -> tuple:
    return (row["strategy_id"], row["symbol"], row["direction"], row["timestamp"])


def run_parity_check(symbol: str, m1_full: pd.DataFrame, batch_ground_truth: list, lookback_label: str,
                      lookback: pd.Timedelta, s3_config=None, s4_config=None,
                      sample: Optional[int] = None, seed: int = 42) -> ParityResult:
    """Evaluates LIVE-AS-OF-T(lookback) at every (or a bounded random
    sample of) batch ground-truth signal timestamps, and classifies each
    as matched/missed. Also records any live signal at an evaluated T
    NOT present in batch ground truth (a false positive -- Phase 7
    category C)."""
    import random
    rows = list(batch_ground_truth)
    if sample is not None and len(rows) > sample:
        rows = random.Random(seed).sample(rows, sample)

    t0 = time.time()
    matched, missed, live_extra = [], [], 0
    for row in rows:
        t = row["timestamp"]
        live_rows = live_signals_at(symbol, m1_full, t, lookback, s3_config, s4_config)
        live_ids_at_t = {_row_id(r) for r in live_rows if r["timestamp"] == t}
        target_id = _row_id(row)
        if target_id in live_ids_at_t:
            matched.append(target_id)
        else:
            missed.append(target_id)
        # Any live signal at this T not matching ANY batch ground-truth row at this T is an extra/false positive.
        batch_ids_at_t = {_row_id(r) for r in batch_ground_truth if r["timestamp"] == t}
        live_extra += len(live_ids_at_t - batch_ids_at_t)

    runtime = time.time() - t0
    return ParityResult(
        lookback_label=lookback_label, lookback=lookback, n_batch_total=len(batch_ground_truth),
        n_evaluated=len(rows), n_matched=len(matched), n_missed=len(missed), n_live_extra=live_extra,
        matched_ids=matched, missed_ids=missed, runtime_seconds=round(runtime, 2),
    )


def verify_point_in_time_correctness(rows: list) -> list:
    """Phase 5: for every returned signal row, verify every referenced
    timestamp is <= the signal's own timestamp. Returns a list of
    violation descriptions (empty = all clean)."""
    violations = []
    for row in rows:
        t = row["timestamp"]
        for field_name in ("choch_timestamp", "swept_timestamp"):
            ref_ts = row.get(field_name)
            if ref_ts is not None and pd.Timestamp(ref_ts) > t:
                violations.append(f"{row['strategy_id']} signal at {t}: {field_name}={ref_ts} is AFTER the signal's own timestamp")
    return violations
