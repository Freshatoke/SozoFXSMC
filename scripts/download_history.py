"""Download Dukascopy historical data into the project data cache."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG, DukascopyDownloadConfig
from src.data.providers.dukascopy import DownloadRequest, DukascopyDownloader


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--symbol")
    group.add_argument("--symbols", nargs="+")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--type", choices=("tick", "m1"), default="tick")
    parser.add_argument("--build-m1", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.workers)
    parser.add_argument("--retries", type=int, default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.retries)
    parser.add_argument("--timeout", type=int, default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.timeout)
    parser.add_argument("--cache-dir", default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.cache_location)
    parser.add_argument("--provider-url", default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.provider_url)
    parser.add_argument("--output-format", default=DEFAULT_DUKASCOPY_DOWNLOAD_CONFIG.output_format)
    return parser.parse_args()


def _symbols(args: argparse.Namespace) -> tuple[str, ...]:
    if args.symbol:
        return (args.symbol,)
    if args.symbols:
        return tuple(args.symbols)
    raise SystemExit("--symbol or --symbols is required unless future resume state is supplied")


def _resume_state_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "download_resume_state.json"


def _save_resume_state(cache_dir: str | Path, args: argparse.Namespace) -> None:
    path = _resume_state_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbols": list(_symbols(args)),
        "start": args.start,
        "end": args.end,
        "type": args.type,
        "build_m1": args.build_m1,
        "provider_url": args.provider_url,
        "output_format": args.output_format,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _apply_resume_state(args: argparse.Namespace) -> argparse.Namespace:
    if not args.resume or (args.start and args.end and (args.symbol or args.symbols)):
        return args
    path = _resume_state_path(args.cache_dir)
    if not path.exists():
        raise SystemExit("--resume has no saved request state; provide --symbol/--symbols, --start, and --end once")
    payload = json.loads(path.read_text(encoding="utf-8"))
    args.symbols = payload["symbols"]
    args.symbol = None
    args.start = payload["start"]
    args.end = payload["end"]
    args.type = payload.get("type", args.type)
    args.build_m1 = payload.get("build_m1", args.build_m1)
    args.provider_url = payload.get("provider_url", args.provider_url)
    args.output_format = payload.get("output_format", args.output_format)
    return args


def _progress(started_at: float):
    def inner(done: int, total: int, result) -> None:
        elapsed = max(0.001, time.time() - started_at)
        speed = done / elapsed
        remaining = max(0, total - done)
        eta = remaining / speed if speed > 0 else 0
        pct = done / total * 100 if total else 100.0
        print(
            f"{result.symbol} {result.day} | {done}/{total} days | "
            f"{pct:6.2f}% | {speed:5.2f} days/s | ETA {eta:6.1f}s | {result.status}",
            flush=True,
        )
    return inner


def main() -> int:
    args = _apply_resume_state(parse_args())
    if not args.resume and (not args.start or not args.end):
        raise SystemExit("--start and --end are required")
    if args.resume and (not args.start or not args.end):
        raise SystemExit("--resume requires a saved or explicit --symbol/--symbols, --start, and --end range")

    config = DukascopyDownloadConfig(
        provider_url=args.provider_url,
        cache_location=args.cache_dir,
        output_format=args.output_format,
        workers=args.workers,
        retries=args.retries,
        timeout=args.timeout,
    )
    request = DownloadRequest(
        symbols=_symbols(args),
        start=parse_date(args.start),
        end=parse_date(args.end),
        data_type=args.type,
        build_m1=args.build_m1,
        workers=args.workers,
        retries=args.retries,
        timeout=args.timeout,
        resume=args.resume,
    )
    _save_resume_state(args.cache_dir, args)

    downloader = DukascopyDownloader(config=config)
    results = downloader.download_range(request, progress=_progress(time.time()))
    completed = sum(1 for r in results if r.status == "complete")
    failed = sum(1 for r in results if r.status == "failed")
    missing = sum(1 for r in results if r.status == "missing")
    print(f"Finished: complete={completed}, missing={missing}, failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
