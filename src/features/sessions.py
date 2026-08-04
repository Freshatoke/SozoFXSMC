"""
Trading Session Engine.

Sessions are defined by a LOCAL time-of-day window in a named IANA timezone
(config.settings.SESSION_WINDOWS_LOCAL), e.g. London 08:00-16:30
Europe/London. For each calendar day (in that session's own local
timezone) we compute the session's start/end as tz-aware timestamps,
convert them to UTC, and then slice the (UTC-indexed) candle data to
compute the session's high/low/open/close.

DST correctness: because the window is anchored to the *local* civil time
(e.g. "08:00 London time") and converted to UTC per calendar day, the
resulting UTC start/end automatically shifts by an hour across DST
transitions -- there is no fixed-offset assumption baked in anywhere.

No look-ahead: a session's high/low is only meaningful/usable once its
`end_utc` has passed; callers must filter on that before use, exactly as
with confirmed swings.
"""

from __future__ import annotations

import pandas as pd

from config.settings import SessionConfig, DEFAULT_SESSION_CONFIG


def compute_sessions(
    df: pd.DataFrame,
    config: SessionConfig = DEFAULT_SESSION_CONFIG,
    timestamp_col: str = "timestamp",
) -> pd.DataFrame:
    """Returns one row per (session_name, calendar_date_in_local_tz) with
    the session's OHLC and boundaries."""
    records = []
    ts_utc = df[timestamp_col]

    for session_name, window in config.windows.items():
        tz = window["tz"]
        local_ts = ts_utc.dt.tz_convert(tz)
        local_dates = local_ts.dt.date.unique()

        for d in local_dates:
            start_local = pd.Timestamp(f"{d} {window['start']}", tz=tz)
            end_local = pd.Timestamp(f"{d} {window['end']}", tz=tz)
            start_utc = start_local.tz_convert("UTC")
            end_utc = end_local.tz_convert("UTC")

            mask = (ts_utc >= start_utc) & (ts_utc < end_utc)
            bucket = df.loc[mask]
            if bucket.empty:
                continue

            records.append({
                "session_id": f"{session_name}_{d}",
                "session_name": session_name,
                "local_date": d,
                "timezone": tz,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "open": float(bucket["open"].iloc[0]),
                "high": float(bucket["high"].max()),
                "low": float(bucket["low"].min()),
                "close": float(bucket["close"].iloc[-1]),
                "num_candles": len(bucket),
            })

    columns = [
        "session_id", "session_name", "local_date", "timezone",
        "start_utc", "end_utc", "open", "high", "low", "close", "num_candles",
    ]
    out = pd.DataFrame.from_records(records, columns=columns)
    if not out.empty:
        out = out.sort_values(["session_name", "start_utc"]).reset_index(drop=True)
    return out


def asian_range(sessions: pd.DataFrame, local_date) -> dict | None:
    row = sessions[(sessions.session_name == "tokyo") & (sessions.local_date == local_date)]
    if row.empty:
        return None
    r = row.iloc[0]
    return {"high": r["high"], "low": r["low"], "start_utc": r["start_utc"], "end_utc": r["end_utc"]}
