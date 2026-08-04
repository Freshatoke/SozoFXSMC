import lzma
import struct
from datetime import date

import pandas as pd

from config.settings import DukascopyDownloadConfig
from src.data.providers import dukascopy
from src.data.providers.dukascopy import (
    DownloadRequest,
    DukascopyDownloader,
    cache_path,
    campaign_m1_path,
    load_catalogue,
    parse_tick_bi5,
    ticks_to_m1,
    update_catalogue,
    DayResult,
)


def _bi5(records):
    payload = b"".join(struct.pack(">IIIff", *record) for record in records)
    return lzma.compress(payload)


def _config(tmp_path):
    return DukascopyDownloadConfig(
        provider_url="https://example.test/datafeed",
        cache_location=str(tmp_path),
        workers=1,
        retries=2,
        timeout=1,
    )


def test_download_success_and_metadata_update(tmp_path):
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return _bi5([(0, 110010, 110000, 1.0, 2.0)])

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    result = downloader.download_day("EURUSD", date(2024, 1, 2), retries=1, timeout=1, build_m1=False)
    update_catalogue(tmp_path, result, "tick")

    catalogue = load_catalogue(tmp_path)
    assert result.status == "complete"
    assert result.records == 24
    assert len(calls) == 24
    assert catalogue["downloads"][0]["validation_status"] == "valid"


def test_resume_skips_catalogued_success(tmp_path):
    result = DayResult(symbol="EURUSD", day=date(2024, 1, 2), status="complete", validation_status="valid")
    update_catalogue(tmp_path, result, "tick")

    def fetcher(url, timeout):
        raise AssertionError("resume should not fetch already valid days")

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    results = downloader.download_range(
        DownloadRequest(
            symbols=("EURUSD",),
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
            resume=True,
        )
    )

    assert results == []


def test_resume_skips_catalogued_missing_days_but_retries_failed_days(tmp_path):
    """Regression test: a day where every hourly file 404's (status=
    "missing", e.g. a weekend) is a stable, permanent fact and must be
    skipped on resume just like a validated success -- previously only
    validation_status=="valid" was ever skipped, so every --resume run
    re-fetched all 24 hours of every weekend day, forever. A day marked
    "failed" (a real error survived retries) must still be retried, so
    it is NOT treated as already resolved."""
    missing_day = date(2024, 1, 6)  # a Saturday
    failed_day = date(2024, 1, 8)
    update_catalogue(tmp_path, DayResult(symbol="EURUSD", day=missing_day, status="missing", validation_status="missing"), "tick")
    update_catalogue(tmp_path, DayResult(symbol="EURUSD", day=failed_day, status="failed", validation_status="invalid"), "tick")

    from src.data.providers.dukascopy import catalogue_has_success

    assert catalogue_has_success(tmp_path, "EURUSD", missing_day, "tick") is True
    assert catalogue_has_success(tmp_path, "EURUSD", failed_day, "tick") is False


def test_retry_logic(tmp_path):
    attempts = {"count": 0}

    def fetcher(url, timeout):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise TimeoutError("temporary")
        return _bi5([(0, 110010, 110000, 1.0, 2.0)])

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    path = downloader.download_hour("EURUSD", date(2024, 1, 2), 0, retries=2, timeout=1)

    assert path is not None
    assert attempts["count"] == 2


def test_corrupted_cache_is_repaired(tmp_path):
    path = cache_path(tmp_path, "EURUSD", date(2024, 1, 2), 0)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad")

    def fetcher(url, timeout):
        return _bi5([(0, 110010, 110000, 1.0, 2.0)])

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    repaired = downloader.download_hour("EURUSD", date(2024, 1, 2), 0, retries=1, timeout=1)
    parsed = parse_tick_bi5(repaired, "EURUSD", date(2024, 1, 2), 0)

    assert len(parsed) == 1


def test_duplicate_download_is_avoided_when_cache_valid(tmp_path):
    path = cache_path(tmp_path, "EURUSD", date(2024, 1, 2), 0)
    path.parent.mkdir(parents=True)
    path.write_bytes(_bi5([(0, 110010, 110000, 1.0, 2.0)]))

    def fetcher(url, timeout):
        raise AssertionError("valid cache should be reused")

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    cached = downloader.download_hour("EURUSD", date(2024, 1, 2), 0, retries=1, timeout=1)

    assert cached == path


def test_concurrent_days_do_not_lose_data_in_shared_campaign_csv(tmp_path):
    """Regression test: with workers > 1, multiple days for the SAME
    symbol used to race on the shared per-symbol campaign_m1_path CSV
    (each day's `append_csv_dedup` call did a read-modify-write on that
    one file from a different thread), and whichever thread wrote last
    silently discarded the other threads' days. Confirmed empirically
    during review: 30 days all reported status="complete" individually,
    but only ~22/30 survived in the shared CSV with workers=4/8. Fixed by
    serializing writes to a given output path with a per-path lock (see
    `_lock_for_path` in src/data/providers/dukascopy.py)."""
    def fetcher(url, timeout):
        return _bi5([(0, 110010, 110000, 1.0, 2.0)])

    config = DukascopyDownloadConfig(cache_location=str(tmp_path), workers=8, retries=1, timeout=1)
    downloader = DukascopyDownloader(config=config, fetcher=fetcher)
    request = DownloadRequest(
        symbols=("EURUSD",), start=date(2024, 1, 1), end=date(2024, 1, 30),
        build_m1=True, workers=8, retries=1, timeout=1,
    )

    results = downloader.download_range(request)
    completed = [r for r in results if r.status == "complete"]
    assert len(completed) == 30

    campaign_df = pd.read_csv(campaign_m1_path(tmp_path, "EURUSD"))
    unique_days = campaign_df["timestamp"].str[:10].nunique()
    assert unique_days == 30, f"expected all 30 days in the shared campaign CSV, found {unique_days}"


def test_tick_aggregation_to_m1():
    ticks = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01T00:00:00Z", "2024-01-01T00:00:30Z", "2024-01-01T00:01:00Z"]
            ),
            "bid": [1.1000, 1.1002, 1.1004],
            "ask": [1.1001, 1.1003, 1.1005],
            "spread": [0.0001, 0.0001, 0.0001],
        }
    )

    m1 = ticks_to_m1(ticks, "EURUSD")

    assert len(m1) == 2
    assert m1.loc[0, "open"] == 1.10005
    assert m1.loc[0, "close"] == 1.10025
    assert m1.loc[0, "volume"] == 2


def test_validation_integration_for_build_m1(tmp_path, monkeypatch):
    saved = {"called": False}

    def fetcher(url, timeout):
        return _bi5([(0, 110010, 110000, 1.0, 2.0), (30_000, 110020, 110010, 1.0, 2.0)])

    def fake_save(df, path):
        saved["called"] = True

    monkeypatch.setattr(dukascopy, "save_processed_dataset", fake_save)

    downloader = DukascopyDownloader(config=_config(tmp_path), fetcher=fetcher)
    result = downloader.download_day("EURUSD", date(2024, 1, 2), retries=1, timeout=1, build_m1=True)

    assert result.status == "complete"
    assert result.m1_path is not None
    assert campaign_m1_path(tmp_path, "EURUSD").exists()
    assert saved["called"]
    assert result.quality_score is not None
