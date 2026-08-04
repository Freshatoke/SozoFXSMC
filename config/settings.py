"""
Central configuration for the forex-smc-quant research system.

All timestamps are stored and processed internally in UTC. Session/local-time
labels are derived views computed on demand, never stored as the source of
truth, so no assumption about the user's local timezone (e.g. Nigerian time)
leaks into the core pipeline.
"""

from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------

# Internal canonical timezone for all stored/processed data.
INTERNAL_TZ = "UTC"

# IANA timezones used to derive session/local views on demand.
# DST-sensitive zones (Europe/London, America/New_York) are handled correctly
# by pandas/pytz because we convert from tz-aware UTC timestamps rather than
# applying a fixed offset.
TIMEZONE_MAP = {
    "broker": "UTC",  # override per-broker if the data provider uses a fixed server offset
    "new_york": "America/New_York",
    "london": "Europe/London",
    "nigeria": "Africa/Lagos",  # UTC+1 year-round, no DST
}

# ---------------------------------------------------------------------------
# Trading sessions (expressed in the "london" local time, DST-aware)
# Used later for Asia/London session-based strategies (S5). Not used in Task 1
# beyond exposing the config surface.
# ---------------------------------------------------------------------------

SESSION_WINDOWS_LOCAL = {
    "sydney": {"tz": "Australia/Sydney", "start": "07:00", "end": "16:00"},
    "tokyo": {"tz": "Asia/Tokyo", "start": "09:00", "end": "18:00"},
    "london": {"tz": "Europe/London", "start": "08:00", "end": "16:30"},
    "new_york": {"tz": "America/New_York", "start": "08:00", "end": "17:00"},
}


@dataclass(frozen=True)
class SwingConfig:
    """Parameters controlling deterministic swing-high/low detection.

    A candle at index i is a confirmed swing high if its high is strictly
    greater than the high of `left` candles before it and `right` candles
    after it. The swing cannot be known/confirmed until the `right`-th
    candle after it has CLOSED, which is what prevents look-ahead bias.
    """

    left: int = 2
    right: int = 2
    method: str = "fractal"  # pluggable: "fractal" is the only method implemented in v0.1


@dataclass(frozen=True)
class StructureConfig:
    """Parameters controlling the BOS/CHoCH state machine."""

    swing: SwingConfig = field(default_factory=SwingConfig)
    # Whether the break candle's CLOSE (not wick) is required beyond the level.
    require_close_beyond_level: bool = True


@dataclass(frozen=True)
class ResampleConfig:
    base_timeframe: str = "1min"
    target_timeframes: tuple = ("5min", "15min")


@dataclass(frozen=True)
class DukascopyDownloadConfig:
    provider_url: str = "https://datafeed.dukascopy.com/datafeed"
    cache_location: str = "data/raw/dukascopy"
    output_format: str = "csv"
    workers: int = 4
    retries: int = 3
    timeout: int = 30
    request_pause_seconds: float = 0.0


DEFAULT_SWING_CONFIG = SwingConfig()
DEFAULT_STRUCTURE_CONFIG = StructureConfig()
DEFAULT_RESAMPLE_CONFIG = ResampleConfig()
DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG = DukascopyDownloadConfig()


# ---------------------------------------------------------------------------
# Task 2 -- SMC Feature Engine configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisplacementConfig:
    """Parameters controlling qualifying-displacement detection.

    A candle (or run of same-direction candles) qualifies as displacement
    when its body is unusually large relative to recent volatility AND its
    close sits near the extreme of its range (little opposing wick).
    """

    atr_period: int = 14
    atr_multiplier: float = 1.5          # body_size >= atr_multiplier * ATR
    recent_body_lookback: int = 20
    body_multiple: float = 1.8           # body_size >= body_multiple * avg recent body size
    min_body_ratio: float = 0.6          # body_size / (high-low), close must sit near the impulse extreme
    min_conditions_met: int = 2          # how many of {atr, body_multiple, body_ratio} must hold
    max_group_gap: int = 0               # 0 = only contiguous same-direction candles are grouped


@dataclass(frozen=True)
class OrderBlockConfig:
    displacement: DisplacementConfig = field(default_factory=DisplacementConfig)
    lookback_candles: int = 20            # how far back to search for the opposite-direction OB candle
    archive_after_candles: int = 200      # candles after terminal state before archiving
    invalidation_lookahead: int = 5       # candles after full mitigation to check for opposing structure flip


@dataclass(frozen=True)
class FVGConfig:
    min_gap_size: float = 0.0             # absolute price; 0 disables the filter
    expire_after_candles: int = 500       # candles of no interaction before EXPIRED


@dataclass(frozen=True)
class LiquidityConfig:
    equal_level_tolerance: float = 0.0005  # relative tolerance (fraction of price) for "equal" highs/lows
    swing_left: int = 2
    swing_right: int = 2
    min_touches_for_strength: int = 2
    archive_after_candles: int = 200       # candles after a sweep before archiving the level


@dataclass(frozen=True)
class SessionConfig:
    windows: dict = field(default_factory=lambda: SESSION_WINDOWS_LOCAL)


@dataclass(frozen=True)
class ReferenceLevelConfig:
    week_start_day: int = 6   # pandas dayofweek convention adapted: Sunday session open, see reference_levels.py


@dataclass(frozen=True)
class EngulfingConfig:
    strong_body_ratio: float = 1.5     # engulfing body >= 1.5x engulfed body -> STRONG
    normal_body_ratio: float = 1.0     # engulfing body >= 1.0x engulfed body -> NORMAL, else WEAK


DEFAULT_DISPLACEMENT_CONFIG = DisplacementConfig()
DEFAULT_ORDER_BLOCK_CONFIG = OrderBlockConfig()
DEFAULT_FVG_CONFIG = FVGConfig()
DEFAULT_LIQUIDITY_CONFIG = LiquidityConfig()
DEFAULT_SESSION_CONFIG = SessionConfig()
DEFAULT_REFERENCE_LEVEL_CONFIG = ReferenceLevelConfig()
DEFAULT_ENGULFING_CONFIG = EngulfingConfig()


# ---------------------------------------------------------------------------
# Task 3 -- Strategy Engine configuration
#
# Every strategy is independently enable/disable-able and every entry
# condition toggle lives here -- no strategy hardcodes a threshold that a
# researcher would want to sweep without touching code.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class S1Config:
    enabled: bool = True
    min_gap_size: float = 0.0005            # absolute price; gaps smaller than this are ignored
    require_engulfing: bool = False         # optional confirmation per the spec
    require_fresh_ob: bool = True
    choch_timeframe: str = "M5"             # first CHoCH confirmation timeframe
    entry_choch_timeframe: str = "M1"       # second/entry CHoCH confirmation timeframe
    ob_timeframe: str = "M15"
    target_style: str = "gap_fill"          # S1's target is always the Friday close by definition
    stop_reference: str = "ob_opposite_boundary"
    confidence_threshold: float = 0.0       # signals below this score are suppressed, not just low-ranked
    session_filter: Optional[tuple] = None  # e.g. ("london", "new_york") to restrict entries to a session


@dataclass(frozen=True)
class S2Config:
    enabled: bool = True
    bos_timeframe: str = "M15"
    choch_timeframe: str = "M5"
    entry_choch_timeframe: str = "M1"
    ob_timeframe: str = "M15"
    require_fresh_ob: bool = True
    require_fvg: bool = False
    target_style: str = "measured_move"     # "measured_move" or "liquidity"
    stop_reference: str = "ob_opposite_boundary"
    confidence_threshold: float = 0.0
    session_filter: Optional[tuple] = None


@dataclass(frozen=True)
class S3Config:
    enabled: bool = True
    choch_timeframe: str = "M5"
    ob_timeframe: str = "M15"
    require_fresh_ob: bool = True
    require_fvg: bool = False
    require_displacement: bool = True
    target_style: str = "liquidity"
    stop_reference: str = "sweep_extreme"
    confidence_threshold: float = 0.0
    session_filter: Optional[tuple] = None


@dataclass(frozen=True)
class S4Config:
    enabled: bool = True
    choch_timeframe: str = "M5"
    ob_timeframe: str = "M15"
    require_fresh_ob: bool = True
    require_fvg: bool = False
    target_style: str = "liquidity"
    stop_reference: str = "sweep_extreme"
    confidence_threshold: float = 0.0
    session_filter: Optional[tuple] = None


@dataclass(frozen=True)
class S5Config:
    enabled: bool = True
    choch_timeframe: str = "M5"
    ob_timeframe: str = "M15"
    require_fresh_ob: bool = True
    require_fvg: bool = False
    target_style: str = "liquidity"
    stop_reference: str = "sweep_extreme"
    confidence_threshold: float = 0.0
    session_filter: Optional[tuple] = ("london",)


DEFAULT_S1_CONFIG = S1Config()
DEFAULT_S2_CONFIG = S2Config()
DEFAULT_S3_CONFIG = S3Config()
DEFAULT_S4_CONFIG = S4Config()
DEFAULT_S5_CONFIG = S5Config()


# ---------------------------------------------------------------------------
# Task 4 -- Backtesting & Execution Simulator configuration
#
# Every method below is picked by name from a small registry (see
# src/backtest/entry.py, stop_loss.py, take_profit.py) so a new method can
# be added later without changing any existing one.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryConfig:
    method: str = "market"
    # "market" | "ob_touch" | "ob_midpoint" | "ob_proximal_edge" |
    # "ob_distal_edge" | "confirmation_close"
    max_wait_candles: int = 60           # give up (EXPIRED) if entry never triggers within this many M1 candles
    entry_buffer_pips: float = 0.0       # extra buffer added/subtracted at the entry price, in pips


@dataclass(frozen=True)
class StopLossConfig:
    method: str = "ob_extreme"
    # "m5_structural" | "ob_extreme" | "fixed_pips" | "atr_multiple" | "percentage"
    fixed_pips: float = 20.0
    atr_period: int = 14
    atr_multiplier: float = 1.5
    percentage: float = 0.005            # 0.5% of entry price
    buffer_pips: float = 2.0             # extra room beyond the structural/OB level


@dataclass(frozen=True)
class TakeProfitConfig:
    method: str = "fixed_rr"
    # "fixed_rr" | "previous_high_low" | "liquidity_level" | "gap_fill_25" |
    # "gap_fill_50" | "gap_fill_75" | "gap_fill_100" | "next_bos_target"
    risk_reward: float = 2.0
    partial_exits: tuple = ()            # e.g. ((1.0, 0.5), (2.0, 0.5)) = (R-multiple, fraction of position) pairs, applied in order


@dataclass(frozen=True)
class ExecutionConfig:
    pip_size: float = 0.0001
    spread_pips: float = 1.0             # constant spread; entry buys at ask, exits sell at bid (and vice versa for shorts)
    commission_per_lot: float = 7.0      # round-turn commission per standard lot, in account currency
    slippage_pips: float = 0.5           # applied unfavourably at entry AND at stop-out (never at take-profit)
    latency_candles: int = 0             # candles of delay between signal/trigger and the fill being sampled
    contract_size: float = 100_000.0     # units per standard lot
    leverage: float = 30.0               # Task 11 Phase 6 (Paper Broker): margin = notional / leverage, a typical retail-account default
    swap_long_per_lot_per_day: float = -2.5   # Task 11 Phase 6: account-currency cost/credit per standard lot per day held, charged once per rollover (negative = cost)
    swap_short_per_lot_per_day: float = 0.5


@dataclass(frozen=True)
class RiskConfig:
    sizing_method: str = "fixed_percentage_risk"
    # "fixed_lot" | "fixed_percentage_risk" | "fixed_monetary_risk" | "volatility_adjusted"
    fixed_lot_size: float = 0.10
    risk_per_trade_pct: float = 0.01     # 1% of account balance
    fixed_monetary_risk: float = 100.0
    atr_period: int = 14                 # used only by volatility_adjusted sizing
    starting_balance: float = 10_000.0
    max_daily_loss_pct: float = 0.03
    max_weekly_loss_pct: float = 0.06
    max_consecutive_losses: int = 5
    consecutive_loss_cooldown_days: float = 1.0   # circuit-breaker pause after max_consecutive_losses is hit, NOT a permanent lockout -- see RiskTracker.can_open
    max_simultaneous_positions: int = 3
    max_portfolio_exposure_pct: float = 0.10   # sum of open positions' risk as a fraction of balance
    min_lot_size: float = 0.01
    max_lot_size: float = 10.0


@dataclass(frozen=True)
class ManagementConfig:
    breakeven_trigger_r: Optional[float] = 1.0     # move stop to entry once price reaches this R-multiple; None disables
    breakeven_buffer_pips: float = 0.0
    trailing_method: Optional[str] = None          # None | "atr" | "structure" | "fixed_pips"
    trailing_atr_multiplier: float = 1.5
    trailing_fixed_pips: float = 15.0
    max_trade_duration_candles: Optional[int] = 1440   # None disables; default = 1 day of M1 candles
    session_close_exit: Optional[str] = None       # e.g. "new_york" -> force-close at that session's end_utc
    daily_trade_limit: Optional[int] = None        # None disables; caps new entries per calendar day


DEFAULT_ENTRY_CONFIG = EntryConfig()
DEFAULT_STOP_LOSS_CONFIG = StopLossConfig()
DEFAULT_TAKE_PROFIT_CONFIG = TakeProfitConfig()
DEFAULT_EXECUTION_CONFIG = ExecutionConfig()
DEFAULT_RISK_CONFIG = RiskConfig()
DEFAULT_MANAGEMENT_CONFIG = ManagementConfig()
