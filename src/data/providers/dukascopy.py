"""
Dukascopy historical data downloader.

Dukascopy tick history is exposed as one compressed `.bi5` file per hour.
This client presents a day-range interface to the rest of the project while
handling hourly downloads, cache reuse, retries, validation, normalization,
and optional M1 aggregation.
"""

from __future__ import annotations

import json
import lzma
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pandas as pd

from config.settings import DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG, DukascopyDownloadConfig
from src.data.historical_pipeline import build_standard_dataset, normalize_symbol, save_processed_dataset

FetchBytes = Callable[[str, int], bytes]

# `download_range` fans out one worker thread per (symbol, day) job, and every
# day for the same symbol appends to the SAME per-symbol campaign_m1_path CSV
# (see `append_csv_dedup` inside `download_day`). Without serializing that
# read-modify-write, two threads racing on the same file silently drop
# whichever thread's day loses the write race -- confirmed empirically during
# review: with workers=4 (the config default) across a 30-day range, only
# ~22/30 days survived in the shared CSV even though every individual day
# reported status="complete". `_campaign_file_locks` gives each resolved
# output path its own lock so concurrent days for one symbol serialize on the
# shared file while different symbols (different paths) keep running in
# parallel; `_campaign_file_locks_guard` only protects the dict lookup itself.
_campaign_file_locks: dict[str, threading.Lock] = {}
_campaign_file_locks_guard = threading.Lock()


def _lock_for_path(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _campaign_file_locks_guard:
        lock = _campaign_file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _campaign_file_locks[key] = lock
        return lock

PRICE_SCALE = {
    "USDJPY": 1000,
    "XAUUSD": 1000,
}


@dataclass(frozen=True)
class DownloadRequest:
    symbols: tuple[str, ...]
    start: date
    end: date
    data_type: str = "tick"
    build_m1: bool = False
    workers: int = DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.workers
    retries: int = DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.retries
    timeout: int = DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.timeout
    resume: bool = False


@dataclass
class DayResult:
    symbol: str
    day: date
    status: str
    raw_files: list[str] = field(default_factory=list)
    tick_path: str | None = None
    m1_path: str | None = None
    records: int = 0
    validation_status: str = "unknown"
    quality_score: float | None = None
    error: str | None = None


def default_fetch_bytes(url: str, timeout: int) -> bytes:
    request = Request(url, headers={"User-Agent": "forex-smc-quant/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def iter_days(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end must be on or after start")
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def dukascopy_url(base_url: str, symbol: str, day: date, hour: int) -> str:
    # Dukascopy path months are zero-based.
    month = day.month - 1
    return f"{base_url.rstrip('/')}/{normalize_symbol(symbol)}/{day.year}/{month:02d}/{day.day:02d}/{hour:02d}h_ticks.bi5"


def cache_path(cache_dir: Path, symbol: str, day: date, hour: int) -> Path:
    return cache_dir / normalize_symbol(symbol) / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / f"{hour:02d}h_ticks.bi5"


def normalized_tick_path(cache_dir: Path, symbol: str, day: date) -> Path:
    return cache_dir / normalize_symbol(symbol) / "normalized" / f"{normalize_symbol(symbol)}_{day:%Y%m%d}_ticks.csv"


def m1_path(cache_dir: Path, symbol: str, day: date) -> Path:
    return cache_dir / normalize_symbol(symbol) / "m1" / f"{normalize_symbol(symbol)}_{day:%Y%m%d}_M1.csv"


def campaign_m1_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{normalize_symbol(symbol)}_M1.csv"


def metadata_path(cache_dir: Path) -> Path:
    return cache_dir / "metadata_catalogue.json"


def load_catalogue(cache_dir: Path) -> dict:
    path = metadata_path(cache_dir)
    if not path.exists():
        return {"provider": "dukascopy", "downloads": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_catalogue(cache_dir: Path, catalogue: dict) -> None:
    path = metadata_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalogue, indent=2, default=str), encoding="utf-8")


def _catalogue_key(symbol: str, day: date, data_type: str) -> tuple[str, str, str]:
    return normalize_symbol(symbol), day.isoformat(), data_type


def catalogue_has_success(cache_dir: Path, symbol: str, day: date, data_type: str) -> bool:
    """Whether `day` is already resolved and can be skipped on `--resume`.

    Both `validation_status == "valid"` (downloaded and verified) and
    `status == "missing"` (all 24 hourly files 404'd -- Dukascopy simply
    has no data for that day, e.g. a weekend) are stable, permanent facts
    safe to skip forever. `status == "failed"` is deliberately NOT
    skipped: `download_day` only reaches "failed" via a non-404 error
    (network/timeout/corruption) that survived every retry, which is
    exactly the case a future `--resume` run should retry.

    Before this fix, only "valid" was ever skipped, so every `--resume`
    run over a multi-year range re-attempted all ~24 hourly requests for
    every weekend day, every time -- for 10-15 years of EURUSD that is
    several thousand guaranteed-404 requests repeated on every resume.
    """
    catalogue = load_catalogue(cache_dir)
    key = _catalogue_key(symbol, day, data_type)
    for item in catalogue.get("downloads", []):
        if (item.get("symbol"), item.get("date"), item.get("type")) == key:
            return item.get("validation_status") == "valid" or item.get("status") == "missing"
    return False


def update_catalogue(cache_dir: Path, result: DayResult, data_type: str) -> None:
    catalogue = load_catalogue(cache_dir)
    key = _catalogue_key(result.symbol, result.day, data_type)
    downloads = [
        item for item in catalogue.get("downloads", [])
        if (item.get("symbol"), item.get("date"), item.get("type")) != key
    ]
    downloads.append(
        {
            "symbol": normalize_symbol(result.symbol),
            "provider": "dukascopy",
            "date": result.day.isoformat(),
            "date_range": [result.day.isoformat(), result.day.isoformat()],
            "type": data_type,
            "number_of_days": 1,
            "number_of_records": result.records,
            "download_date": datetime.now(timezone.utc).isoformat(),
            "validation_status": result.validation_status,
            "quality_score": result.quality_score,
            "file_paths": {
                "raw": result.raw_files,
                "ticks": result.tick_path,
                "m1": result.m1_path,
            },
            "status": result.status,
            "error": result.error,
        }
    )
    catalogue["downloads"] = sorted(downloads, key=lambda x: (x["symbol"], x["date"], x["type"]))
    save_catalogue(cache_dir, catalogue)


def append_csv_dedup(path: Path, incoming: pd.DataFrame, subset: list[str]) -> pd.DataFrame:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = pd.read_csv(path)
        combined = pd.concat([existing, incoming], ignore_index=True)
    else:
        combined = incoming.copy()
    combined = combined.drop_duplicates(subset=subset, keep="first")
    combined = combined.sort_values(subset, kind="mergesort").reset_index(drop=True)
    combined.to_csv(path, index=False)
    return combined


def file_is_readable(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        with path.open("rb") as handle:
            handle.read(1)
        return True
    except OSError:
        return False


def decompress_bi5(path: Path) -> bytes:
    raw = path.read_bytes()
    return lzma.decompress(raw)


def parse_tick_bi5(path: Path, symbol: str, day: date, hour: int) -> pd.DataFrame:
    payload = decompress_bi5(path)
    if len(payload) % 20 != 0:
        raise ValueError(f"Corrupted Dukascopy payload length: {path}")
    scale = PRICE_SCALE.get(normalize_symbol(symbol), 100000)
    rows = []
    hour_start = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    for offset in range(0, len(payload), 20):
        ms, ask_raw, bid_raw, ask_volume, bid_volume = struct.unpack(">IIIff", payload[offset:offset + 20])
        timestamp = hour_start + timedelta(milliseconds=ms)
        bid = bid_raw / scale
        ask = ask_raw / scale
        rows.append(
            {
                "timestamp": pd.Timestamp(timestamp),
                "symbol": normalize_symbol(symbol),
                "bid": bid,
                "ask": ask,
                "spread": ask - bid,
                "bid_volume": float(bid_volume),
                "ask_volume": float(ask_volume),
                "provider": "dukascopy",
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty and not df["timestamp"].is_monotonic_increasing:
        raise ValueError(f"Invalid timestamp sequence in {path}")
    return df


def validate_tick_file(path: Path, symbol: str, day: date, hour: int) -> bool:
    if not file_is_readable(path):
        return False
    try:
        parse_tick_bi5(path, symbol, day, hour)
        return True
    except (lzma.LZMAError, struct.error, ValueError, OSError):
        return False


def fetch_with_retries(
    url: str,
    path: Path,
    fetcher: FetchBytes,
    timeout: int,
    retries: int,
    pause_seconds: float = 0.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            payload = fetcher(url, timeout)
            if not payload:
                raise ValueError("empty response")
            path.write_bytes(payload)
            return
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            last_error = exc
            if attempt < retries and pause_seconds > 0:
                time.sleep(pause_seconds)
    raise RuntimeError(f"Failed to download {url}: {last_error}")


class DukascopyDownloader:
    def __init__(
        self,
        config: DukascopyDownloadConfig = DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG,
        fetcher: FetchBytes = default_fetch_bytes,
    ) -> None:
        self.config = config
        self.fetcher = fetcher
        self.cache_dir = Path(config.cache_location)

    def download_hour(self, symbol: str, day: date, hour: int, retries: int, timeout: int) -> Path | None:
        path = cache_path(self.cache_dir, symbol, day, hour)
        if validate_tick_file(path, symbol, day, hour):
            return path
        if path.exists():
            path.unlink()
        url = dukascopy_url(self.config.provider_url, symbol, day, hour)
        try:
            fetch_with_retries(
                url,
                path,
                self.fetcher,
                timeout=timeout,
                retries=retries,
                pause_seconds=self.config.request_pause_seconds,
            )
        except RuntimeError as exc:
            # 404 and an empty (0-byte) 200 response both mean "no ticks
            # for this hour" -- not a fetch failure. This is routine for
            # some instruments (e.g. XAUUSD has a genuine ~1-hour daily
            # settlement gap around 21:00 UTC on every trading day), so
            # treating it the same as 404 lets that day still be marked
            # complete from its other 23 hours instead of failing outright.
            if "404" in str(exc) or "empty response" in str(exc):
                return None
            raise
        return path if validate_tick_file(path, symbol, day, hour) else None

    def download_day(self, symbol: str, day: date, retries: int, timeout: int, build_m1: bool) -> DayResult:
        result = DayResult(symbol=normalize_symbol(symbol), day=day, status="pending")
        try:
            raw_files = []
            frames = []
            for hour in range(24):
                hour_path = self.download_hour(symbol, day, hour, retries=retries, timeout=timeout)
                if hour_path is None:
                    continue
                raw_files.append(str(hour_path))
                frames.append(parse_tick_bi5(hour_path, symbol, day, hour))

            if not frames:
                result.status = "missing"
                result.validation_status = "missing"
                result.error = "No hourly files available for this date."
                return result

            ticks = pd.concat(frames, ignore_index=True).sort_values("timestamp", kind="mergesort")
            if ticks["timestamp"].duplicated().any() or not ticks["timestamp"].is_monotonic_increasing:
                raise ValueError("Invalid normalized tick timestamp sequence.")

            tick_path = normalized_tick_path(self.cache_dir, symbol, day)
            tick_path.parent.mkdir(parents=True, exist_ok=True)
            ticks.to_csv(tick_path, index=False)
            result.raw_files = raw_files
            result.tick_path = str(tick_path)
            result.records = len(ticks)

            if build_m1:
                candles = ticks_to_m1(ticks, symbol)
                candle_path = m1_path(self.cache_dir, symbol, day)
                candle_path.parent.mkdir(parents=True, exist_ok=True)
                candles.to_csv(candle_path, index=False)
                campaign_path = campaign_m1_path(self.cache_dir, symbol)
                with _lock_for_path(campaign_path):
                    append_csv_dedup(campaign_path, candles, ["timestamp", "symbol"])
                dataset = build_standard_dataset(
                    candle_path,
                    provider="dukascopy",
                    symbol=symbol,
                    timeframe="M1",
                    source_tz="UTC",
                    expected_interval="1min",
                )
                save_processed_dataset(dataset.data, self.cache_dir / normalize_symbol(symbol) / "processed" / f"{normalize_symbol(symbol)}_{day:%Y%m%d}_M1.parquet")
                result.m1_path = str(candle_path)
                result.quality_score = dataset.report.quality_score

            result.status = "complete"
            result.validation_status = "valid"
            return result
        except Exception as exc:
            result.status = "failed"
            result.validation_status = "invalid"
            result.error = str(exc)
            return result

    def download_range(self, request: DownloadRequest, progress: Callable[[int, int, DayResult], None] | None = None) -> list[DayResult]:
        days = iter_days(request.start, request.end)
        jobs = []
        for symbol in request.symbols:
            for day in days:
                if request.resume and catalogue_has_success(self.cache_dir, symbol, day, request.data_type):
                    continue
                jobs.append((symbol, day))

        total = len(jobs)
        if total == 0:
            return []

        results = []
        done = 0
        build_m1 = request.build_m1 or request.data_type.lower() == "m1"
        with ThreadPoolExecutor(max_workers=max(1, request.workers)) as executor:
            futures = {
                executor.submit(self.download_day, symbol, day, request.retries, request.timeout, build_m1): (symbol, day)
                for symbol, day in jobs
            }
            for future in as_completed(futures):
                result = future.result()
                update_catalogue(self.cache_dir, result, request.data_type)
                done += 1
                results.append(result)
                if progress:
                    progress(done, total, result)
        return sorted(results, key=lambda r: (r.symbol, r.day))


def ticks_to_m1(ticks: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if ticks.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "bid", "ask", "spread"])
    work = ticks.copy()
    work["mid"] = (work["bid"] + work["ask"]) / 2.0
    work = work.set_index("timestamp")
    agg = work.resample("1min", label="left", closed="left").agg(
        open=("mid", "first"),
        high=("mid", "max"),
        low=("mid", "min"),
        close=("mid", "last"),
        volume=("mid", "size"),
        bid=("bid", "last"),
        ask=("ask", "last"),
        spread=("spread", "mean"),
    )
    agg = agg.dropna(subset=["open", "high", "low", "close"]).reset_index()
    agg.insert(1, "symbol", normalize_symbol(symbol))
    return agg
