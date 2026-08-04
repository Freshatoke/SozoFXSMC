"""Timezone helper utilities. Internal storage/processing is always UTC."""

from __future__ import annotations

import pandas as pd

from config.settings import TIMEZONE_MAP


def to_utc(series: pd.Series, source_tz: str | None) -> pd.Series:
    """Parse a timestamp series and normalize it to tz-aware UTC.

    If `source_tz` is None, the series is assumed to already carry timezone
    info (either tz-aware, or ISO-8601 with offset). If it is naive with no
    `source_tz` given, an explicit ValueError is raised rather than silently
    assuming a timezone (e.g. Nigerian time) — this is a deliberate project
    requirement, not an oversight.
    """
    parsed = pd.to_datetime(series, errors="raise")
    if parsed.dt.tz is None:
        if source_tz is None:
            raise ValueError(
                "Timestamp column is timezone-naive and no source_tz was "
                "provided. Refusing to silently assume a timezone. Pass "
                "source_tz='UTC' (or the true source timezone) explicitly."
            )
        parsed = parsed.dt.tz_localize(source_tz)
    return parsed.dt.tz_convert("UTC")


def add_local_views(df: pd.DataFrame, timestamp_col: str = "timestamp") -> pd.DataFrame:
    """Attach derived local-time columns for reporting only. Never used
    for internal computation, which always stays in UTC."""
    out = df.copy()
    for name, tz in TIMEZONE_MAP.items():
        out[f"{timestamp_col}_{name}"] = out[timestamp_col].dt.tz_convert(tz)
    return out
