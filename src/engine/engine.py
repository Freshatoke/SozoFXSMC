"""
IncrementalEngine -- the orchestrator that wires the event bus, the
per-feature incremental trackers (src.engine.incremental), and the Active
Object Registry (src.engine.registry) together.

Per-candle pipeline (fixed order, matches the task brief exactly):

    New Candle
      -> Update Sessions
      -> Update Swings
      -> Update BOS / CHoCH
      -> Update Order Blocks (via completed Displacement runs)
      -> Update FVG
      -> Update Liquidity
      -> Update Reference Levels
      -> Update Engulfing
      -> Refresh Active Registry
      -> Produce New Confluence Snapshot (ConfluenceUpdated event)

Each step only touches the state relevant to it (see incremental.py
docstrings for the per-tracker complexity) -- there is no re-scan of
history anywhere in this pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import (
    SwingConfig, StructureConfig, DisplacementConfig, OrderBlockConfig,
    FVGConfig, LiquidityConfig, SessionConfig, EngulfingConfig,
)
from src.engine.event_bus import EventBus, EventType
from src.engine.registry import ActiveObjectRegistry, _json_default, _json_object_hook
from src.engine.incremental import (
    Candle, IncrementalSwingTracker, IncrementalStructureTracker,
    IncrementalDisplacementTracker, IncrementalOrderBlockTracker,
    IncrementalFVGTracker, IncrementalLiquidityTracker, IncrementalSessionTracker,
    IncrementalReferenceLevelTracker, IncrementalEngulfingTracker,
)


class IncrementalEngine:
    def __init__(
        self,
        symbol: str,
        timeframe: str,
        interval: pd.Timedelta,
        swing_config: SwingConfig = SwingConfig(),
        structure_config: StructureConfig = StructureConfig(),
        displacement_config: DisplacementConfig = DisplacementConfig(),
        ob_config: OrderBlockConfig = OrderBlockConfig(),
        fvg_config: FVGConfig = FVGConfig(),
        liquidity_config: LiquidityConfig = LiquidityConfig(),
        session_config: SessionConfig = SessionConfig(),
        engulfing_config: EngulfingConfig = EngulfingConfig(),
        event_bus: Optional[EventBus] = None,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.interval = interval
        self.event_bus = event_bus or EventBus()

        self.swing_config = swing_config
        self.structure_config = structure_config
        self.displacement_config = displacement_config
        self.ob_config = ob_config
        self.fvg_config = fvg_config
        self.liquidity_config = liquidity_config
        self.session_config = session_config
        self.engulfing_config = engulfing_config

        self.swings = IncrementalSwingTracker(swing_config, interval, timeframe, self.event_bus)
        self.structure = IncrementalStructureTracker(structure_config, symbol, timeframe, self.event_bus)
        self.displacement = IncrementalDisplacementTracker(displacement_config)
        self.order_blocks = IncrementalOrderBlockTracker(ob_config, symbol, timeframe, self.event_bus)
        self.fvgs = IncrementalFVGTracker(fvg_config, symbol, timeframe, interval, self.event_bus)
        self.liquidity = IncrementalLiquidityTracker(liquidity_config, symbol, timeframe, self.event_bus)
        self.sessions = IncrementalSessionTracker(session_config, self.event_bus)
        self.reference_levels = IncrementalReferenceLevelTracker(self.event_bus)
        self.engulfing = IncrementalEngulfingTracker(engulfing_config)

        self.registry = ActiveObjectRegistry(symbol, timeframe)
        self.candles_processed = 0

    def process_candle(self, candle: Candle) -> "ConfluenceSnapshot":  # noqa: F821
        # 1. Sessions
        self.sessions.update(candle)

        # 2. Swings (may confirm 0, 1 or 2 swings on this candle)
        new_swings = self.swings.update(candle)
        for swing in new_swings:
            self.structure.ingest_swing(swing)
            self.liquidity.ingest_swing(swing)

        # 3. BOS / CHoCH
        structure_events_this_candle = self.structure.update(candle)

        # 4. Order Blocks -- driven by displacement runs. An Order Block is
        # created/updated as soon as a qualifying run is IN PROGRESS
        # (active_run), not only once it completes -- see
        # IncrementalOrderBlockTracker's docstring for why a streaming
        # engine cannot wait for a "closing" candle the way the batch
        # reference implementation can.
        active_run, completed_run = self.displacement.update(candle)
        displacement_signal = active_run or completed_run
        if active_run is not None:
            self.order_blocks.on_displacement_active(active_run, atr=self.displacement._atr())
        self.order_blocks.update(candle, structure_events_this_candle)

        # 5. FVG
        self.fvgs.update(candle)

        # 6. Liquidity (sweep checks against already-registered levels)
        self.liquidity.update(candle)

        # 7. Reference levels
        self.reference_levels.update(candle)

        # Engulfing (feeds the confluence snapshot; not in the brief's
        # named pipeline steps but tracked the same lightweight way)
        engulfing_signal = self.engulfing.update(candle)

        # 8. Refresh Active Registry
        self.registry.refresh(
            self.swings, self.structure, self.order_blocks, self.fvgs,
            self.liquidity, self.sessions, self.reference_levels,
        )

        # Weekend gaps are rare (at most ~1/week) so this scan never grows
        # meaningfully; the mutable gap dict is copied before it reaches
        # the snapshot, same immutability reasoning as the OB/FVG/liquidity
        # accessors above -- otherwise a later gap_filled_pct update would
        # silently rewrite an already-returned snapshot's weekend_gap field.
        open_gap_live = next((g for g in self.reference_levels.weekend_gaps if g["state"] != "FILLED"), None)
        open_gap = dict(open_gap_live) if open_gap_live is not None else None

        # 9. Confluence snapshot
        snapshot = self.registry.build_confluence_snapshot(
            candle.timestamp, engulfing_signal, displacement_signal, open_gap,
        )
        self.event_bus.publish(EventType.CONFLUENCE_UPDATED, candle.timestamp, snapshot.to_dict())

        self.candles_processed += 1
        return snapshot

    def process_dataframe(self, df, timestamp_col: str = "timestamp") -> list:
        """Candle `index` continues from `self.candles_processed`, the
        engine's running global position in the stream -- NOT reset to 0
        for each call. This is what makes a save()/load() followed by
        feeding further chunks equivalent to having processed the whole
        stream in one call (verified in tests/test_incremental_engine.py's
        restart/recovery test); resetting to 0 here would silently corrupt
        every index-based comparison (OB/FVG/liquidity creation_index
        checks) after a resume."""
        snapshots = []
        base_index = self.candles_processed
        for offset, row in enumerate(df.itertuples(index=False)):
            candle = Candle(
                index=base_index + offset, timestamp=getattr(row, timestamp_col),
                open=row.open, high=row.high, low=row.low, close=row.close,
            )
            snapshots.append(self.process_candle(candle))
        return snapshots

    # ------------------------------------------------------------------
    # Persistence -- full engine state, not just the registry summary
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "timeframe": self.timeframe,
            "interval_seconds": self.interval.total_seconds(),
            "candles_processed": self.candles_processed,
            "swings": self.swings.state_dict(),
            "structure": self.structure.state_dict(),
            "displacement": self.displacement.state_dict(),
            "order_blocks": self.order_blocks.state_dict(),
            "fvgs": self.fvgs.state_dict(),
            "liquidity": self.liquidity.state_dict(),
            "sessions": self.sessions.state_dict(),
            "reference_levels": self.reference_levels.state_dict(),
            "engulfing": self.engulfing.state_dict(),
        }

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), default=_json_default, indent=2))

    @classmethod
    def load(cls, path: str | Path, event_bus: Optional[EventBus] = None) -> "IncrementalEngine":
        p = Path(path)
        state = json.loads(p.read_text(), object_hook=_json_object_hook)

        engine = cls(
            symbol=state["symbol"], timeframe=state["timeframe"],
            interval=pd.Timedelta(seconds=state["interval_seconds"]), event_bus=event_bus,
        )
        engine.candles_processed = state["candles_processed"]
        engine.swings.restore(state["swings"])
        engine.structure.restore(state["structure"])
        engine.displacement.restore(state["displacement"])
        engine.order_blocks.restore(state["order_blocks"])
        engine.fvgs.restore(state["fvgs"])
        engine.liquidity.restore(state["liquidity"])
        engine.sessions.restore(state["sessions"])
        engine.reference_levels.restore(state["reference_levels"])
        engine.engulfing.restore(state["engulfing"])
        engine.registry.refresh(
            engine.swings, engine.structure, engine.order_blocks, engine.fvgs,
            engine.liquidity, engine.sessions, engine.reference_levels,
        )
        return engine
