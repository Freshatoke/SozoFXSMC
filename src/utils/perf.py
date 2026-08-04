"""
Task 7.4 -- shared performance measurement utilities.

`ResourceMonitor` is the single place that measures wall-clock time, CPU
usage, and peak RSS memory for a block of work. It is used by:
    - the baseline/re-profiling scripts (Objective 1/2)
    - the scaling benchmark (Objective 4)
    - progress instrumentation (Objective 5)
so all three report numbers that are directly comparable (same
measurement method, not three different ad hoc timers).

`ProgressReporter` (Objective 5) builds on `ResourceMonitor` to print live
stage/%/ETA/memory/throughput lines for long-running commands -- see its
own docstring for the checkpoint-based design.

Peak RSS is sampled by a lightweight background thread (psutil does not
expose a cross-platform "peak RSS so far" counter directly), polling at
`sample_interval_sec`. This adds negligible overhead (a few memory reads
per second) relative to the work being measured.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import psutil


@dataclass
class ResourceUsage:
    wall_seconds: float = 0.0
    cpu_seconds: float = 0.0
    cpu_percent_avg: float = 0.0
    peak_rss_mb: float = 0.0
    start_rss_mb: float = 0.0

    def to_dict(self) -> dict:
        return {
            "wall_seconds": round(self.wall_seconds, 3),
            "cpu_seconds": round(self.cpu_seconds, 3),
            "cpu_percent_avg": round(self.cpu_percent_avg, 1),
            "peak_rss_mb": round(self.peak_rss_mb, 1),
            "start_rss_mb": round(self.start_rss_mb, 1),
        }


class ResourceMonitor:
    """Context manager: `with ResourceMonitor() as mon: ...` then read
    `mon.usage` afterward. Also usable manually via `start()`/`stop()`
    for cases (e.g. progress instrumentation) that need a running total
    while work is still in flight."""

    def __init__(self, sample_interval_sec: float = 0.2):
        self.sample_interval_sec = sample_interval_sec
        self._process = psutil.Process()
        self._peak_rss = 0
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._start_wall = 0.0
        self._start_cpu = None
        self.usage = ResourceUsage()

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                rss = self._process.memory_info().rss
                self._peak_rss = max(self._peak_rss, rss)
            except psutil.Error:
                pass
            self._stop_event.wait(self.sample_interval_sec)

    def start(self) -> "ResourceMonitor":
        self._start_wall = time.perf_counter()
        self._start_cpu = self._process.cpu_times()
        self.usage.start_rss_mb = self._process.memory_info().rss / (1024 * 1024)
        self._peak_rss = self._process.memory_info().rss
        self._process.cpu_percent(interval=None)  # prime the internal counter
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        return self

    def current_peak_rss_mb(self) -> float:
        return self._peak_rss / (1024 * 1024)

    def current_elapsed_sec(self) -> float:
        return time.perf_counter() - self._start_wall

    def stop(self) -> ResourceUsage:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.sample_interval_sec * 2)
        wall = time.perf_counter() - self._start_wall
        end_cpu = self._process.cpu_times()
        cpu_seconds = (end_cpu.user - self._start_cpu.user) + (end_cpu.system - self._start_cpu.system)
        cpu_percent = self._process.cpu_percent(interval=None)
        self.usage = ResourceUsage(
            wall_seconds=wall,
            cpu_seconds=cpu_seconds,
            cpu_percent_avg=cpu_percent,
            peak_rss_mb=self._peak_rss / (1024 * 1024),
            start_rss_mb=self.usage.start_rss_mb,
        )
        return self.usage

    def __enter__(self) -> "ResourceMonitor":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


def candles_per_second(num_candles: int, wall_seconds: float) -> float:
    if wall_seconds <= 0:
        return 0.0
    return round(num_candles / wall_seconds, 1)


def _format_duration(seconds: float) -> str:
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf guard
        return "unknown"
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class ProgressReporter:
    """Task 7.4 Objective 5 -- live progress instrumentation for
    long-running commands (multi-year validation campaigns).

    Work is reported as a sequence of (stage_name, unit_count) checkpoints
    -- e.g. one checkpoint per strategy module, per dataset stage, or per
    N trades -- rather than requiring per-candle hooks inside the trading
    logic itself (which this task explicitly must not modify). Each
    checkpoint call prints stage, overall % complete, units processed,
    elapsed time, an ETA extrapolated from the observed average
    throughput so far, peak RSS memory (via the shared ResourceMonitor),
    and current throughput (units/sec).

    `total_units` is the total amount of "work" (typically candles, or
    candles-equivalent) the whole run represents; callers advance it via
    `checkpoint(stage, units_done_delta)` as each piece of work finishes.
    """

    def __init__(self, total_units: int, label: str = "run", min_print_interval_sec: float = 2.0):
        self.total_units = max(total_units, 0)
        self.label = label
        self.units_done = 0
        self.monitor = ResourceMonitor()
        self.min_print_interval_sec = min_print_interval_sec
        self._t0 = 0.0
        self._stage = ""
        self._last_print_t = None

    def start(self) -> "ProgressReporter":
        self._t0 = time.perf_counter()
        self.monitor.start()
        return self

    def checkpoint(self, stage: str, units_delta: int = 0) -> None:
        """Advances progress; the printed line itself is throttled to at
        most once per `min_print_interval_sec` (except stage transitions
        and the final 100% checkpoint, which always print) so that
        high-frequency callers -- e.g. one checkpoint per trade during a
        multi-thousand-trade backtest -- don't flood the console. The
        underlying units_done total is always updated regardless of
        whether this particular call prints."""
        stage_changed = stage != self._stage
        self._stage = stage
        self.units_done = min(self.units_done + units_delta, self.total_units) if self.total_units else self.units_done + units_delta
        is_complete = bool(self.total_units) and self.units_done >= self.total_units
        now = time.perf_counter()
        should_print = (
            stage_changed
            or is_complete
            or self._last_print_t is None
            or (now - self._last_print_t) >= self.min_print_interval_sec
        )
        if not should_print:
            return
        self._last_print_t = now

        elapsed = now - self._t0
        pct = (100.0 * self.units_done / self.total_units) if self.total_units else 0.0
        throughput = self.units_done / elapsed if elapsed > 0 else 0.0
        remaining = max(self.total_units - self.units_done, 0)
        eta_seconds = (remaining / throughput) if throughput > 0 else float("inf")
        print(
            f"[{self.label}] stage={stage} {pct:5.1f}% "
            f"({self.units_done:,}/{self.total_units:,} candles) "
            f"elapsed={_format_duration(elapsed)} eta={_format_duration(eta_seconds)} "
            f"peak_rss={self.monitor.current_peak_rss_mb():.1f}MB "
            f"throughput={throughput:.1f} candles/s",
            flush=True,
        )

    def stop(self) -> None:
        self.monitor.stop()

    def __enter__(self) -> "ProgressReporter":
        return self.start()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
