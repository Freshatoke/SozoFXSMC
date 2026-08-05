"""
Task 11.2 — AI Trading Journal & Daily Intelligence Reports.

"AI" here means an automated ANALYSIS engine over real, recorded
operational data — deterministic rule-based computation and statistical
comparison, not a language model call. Every number in a report traces
back to an activity record written by the live platform (the GitHub
Actions scan-only deployment, or a continuously running `LiveOrchestrator`
with its paper broker); nothing here fabricates, extrapolates, or
"learns" in the machine-learning sense. Per the task's explicit rule,
this module is an analyst, not a trader: it never touches strategy
config, IOS/ITQS formulas, or the decision engine.

Data flow:
    DailyActivityRecorder.record_*()  -- called by the scan script and/or
        the orchestrator throughout the day -- appends one JSON line per
        event to data/live/journal/activity/<YYYY-MM-DD>.jsonl (Nigeria-
        local date, Africa/Lagos = UTC+1, no DST).
    generate_daily_report()  -- reads that day's full activity log once
        (after the trading day ends) and computes every Phase 1 metric.
    save_daily_report() / load_recent_reports()  -- persist/retrieve
        already-computed daily reports (data/live/journal/reports/<date>.json)
        so Phase 3 (historical comparison) and Phase 4 (drift detection)
        have something real to compare against -- and can honestly say
        "insufficient history" when they don't.
    append_learning_log()  -- Phase 6's cumulative journal: one JSONL
        entry (source of truth) plus one Markdown section (human-readable)
        per day, in a file that only ever grows.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

NIGERIA_TZ = ZoneInfo("Africa/Lagos")   # UTC+1 fixed, no DST
MIN_DAYS_FOR_HISTORICAL_COMPARISON = 3
MIN_DAYS_FOR_DRIFT_BASELINE = 7


def nigeria_today(now_utc: Optional[pd.Timestamp] = None) -> str:
    now = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
    return now.tz_convert(NIGERIA_TZ).strftime("%Y-%m-%d")


# ----------------------------------------------------------------------
# Activity recording
# ----------------------------------------------------------------------

class DailyActivityRecorder:
    """Appends one JSON line per event to today's (Nigeria-local)
    activity file. Safe to instantiate fresh in a short-lived process
    (a GitHub Actions run) -- each call opens, appends, and closes the
    file, since a 5-minute scan script has no long-lived file handle to
    keep open across the many separate processes that make up one day."""

    def __init__(self, activity_dir: str | Path = "data/live/journal/activity"):
        self.activity_dir = Path(activity_dir)

    def _path_for(self, date: str) -> Path:
        self.activity_dir.mkdir(parents=True, exist_ok=True)
        return self.activity_dir / f"{date}.jsonl"

    def _write(self, record: dict) -> None:
        date = nigeria_today()
        record.setdefault("ts", pd.Timestamp.now(tz="UTC").isoformat())
        with open(self._path_for(date), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def record_scan(self, symbol: str, candles_processed: int) -> None:
        self._write({"type": "scan", "symbol": symbol, "candles_processed": candles_processed})

    def record_decision(self, decision) -> None:
        opp = decision.opportunity
        self._write({
            "type": "decision", "opportunity_id": opp.opportunity_id, "symbol": opp.symbol,
            "strategy_id": opp.strategy_id, "verdict": decision.verdict, "ios": decision.ios,
            "itqs": opp.itqs, "reasons_against": list(decision.reasons_against or []),
        })

    def record_trade_opened(self, position_id: str, symbol: str, strategy_id: str, direction: str) -> None:
        self._write({"type": "trade_opened", "position_id": position_id, "symbol": symbol,
                      "strategy_id": strategy_id, "direction": direction})

    def record_trade_closed(self, position_id: str, symbol: str, strategy_id: str,
                             realized_pnl: float, reason: str) -> None:
        self._write({"type": "trade_closed", "position_id": position_id, "symbol": symbol,
                      "strategy_id": strategy_id, "realized_pnl": realized_pnl, "reason": reason})

    def record_feed_error(self, symbol: str, detail: str) -> None:
        self._write({"type": "feed_error", "symbol": symbol, "detail": detail})


def load_activity(date: str, activity_dir: str | Path = "data/live/journal/activity") -> list:
    path = Path(activity_dir) / f"{date}.jsonl"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# ----------------------------------------------------------------------
# Phase 1 — Daily Intelligence Report
# ----------------------------------------------------------------------

# Reason-string -> category buckets, matched against the actual literal
# messages `src.decision_engine.portfolio_allocation`/`risk_layer` produce
# (read directly from those modules, not guessed) -- so "most common
# rejection reason" reports a stable category instead of near-unique
# sentences that happen to differ only in an embedded number.
_REJECTION_CATEGORIES = [
    (re.compile(r"max simultaneous trades", re.I), "Portfolio at max simultaneous trades"),
    (re.compile(r"max concurrent trades", re.I), "Strategy at max concurrent trades"),
    (re.compile(r"already at max exposure", re.I), "Currency exposure limit"),
    (re.compile(r"max portfolio risk", re.I), "Portfolio risk % limit"),
    (re.compile(r"shares a currency leg", re.I), "Correlated currency leg"),
    (re.compile(r"correlates .* with already-held", re.I), "Strategy correlation"),
    (re.compile(r"session .* already at max", re.I), "Session exposure limit"),
    (re.compile(r"daily loss limit", re.I), "Daily loss limit"),
    (re.compile(r"weekly loss limit", re.I), "Weekly loss limit"),
    (re.compile(r"monthly loss limit", re.I), "Monthly loss limit"),
    (re.compile(r"portfolio heat", re.I), "Portfolio heat limit"),
]


def categorize_rejection_reason(reason: str) -> str:
    for pattern, category in _REJECTION_CATEGORIES:
        if pattern.search(reason):
            return category
    return "Other"


def generate_daily_report(date: str, activity_dir: str | Path = "data/live/journal/activity") -> dict:
    """Reads ONE day's activity log and computes every Phase 1 metric.
    Every field either reflects real recorded data or explicitly states
    why it can't be computed (e.g. no paper broker running this
    deployment) -- never a silently misleading zero."""
    records = load_activity(date, activity_dir)

    scans = [r for r in records if r["type"] == "scan"]
    decisions = [r for r in records if r["type"] == "decision"]
    opened = [r for r in records if r["type"] == "trade_opened"]
    closed = [r for r in records if r["type"] == "trade_closed"]
    feed_errors = [r for r in records if r["type"] == "feed_error"]

    markets_scanned = sorted({r["symbol"] for r in scans})
    candles_processed = sum(r["candles_processed"] for r in scans)

    approved = [d for d in decisions if d["verdict"] == "EXECUTE"]
    rejected = [d for d in decisions if d["verdict"] in ("IGNORE", "POSTPONE")]

    opened_ids_today = {r["position_id"] for r in opened}
    closed_ids_today = {r["position_id"] for r in closed}
    still_open_today = opened_ids_today - closed_ids_today

    wins = [r for r in closed if r["realized_pnl"] > 0]
    losses = [r for r in closed if r["realized_pnl"] < 0]
    win_rate = round(len(wins) / len(closed), 4) if closed else None
    expectancy = round(sum(r["realized_pnl"] for r in closed) / len(closed), 2) if closed else None
    gross_win = sum(r["realized_pnl"] for r in wins)
    gross_loss = abs(sum(r["realized_pnl"] for r in losses))
    profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 else (None if not closed else float("inf"))

    highest_ios = max((d["ios"] for d in decisions), default=None)
    highest_itqs = max((d["itqs"] for d in decisions if d["itqs"] is not None), default=None)

    strategy_counts = Counter(d["strategy_id"] for d in approved)
    best_strategy = strategy_counts.most_common(1)[0][0] if strategy_counts else None
    best_strategy_basis = "most approved opportunities today" if best_strategy else None
    if closed:
        # If any trades actually closed today, rank by realized PnL instead
        # of opportunity count -- a real outcome beats a proxy when both exist.
        pnl_by_strategy = Counter()
        for r in closed:
            pnl_by_strategy[r["strategy_id"]] += r["realized_pnl"]
        if pnl_by_strategy:
            best_strategy = max(pnl_by_strategy, key=pnl_by_strategy.get)
            best_strategy_basis = "highest realized PnL today"

    symbol_counts = Counter(d["symbol"] for d in approved)
    best_symbol = symbol_counts.most_common(1)[0][0] if symbol_counts else None

    rejection_categories = Counter(
        categorize_rejection_reason(reason)
        for d in rejected for reason in (d["reasons_against"] or ["Other"])
    )
    most_common_rejection_reason = rejection_categories.most_common(1)[0][0] if rejection_categories else None

    has_broker_data = bool(opened or closed)

    return {
        "date": date,
        "generated_at": str(pd.Timestamp.now(tz="UTC")),
        "markets_scanned": markets_scanned,
        "candles_processed": candles_processed,
        "signals_detected": len(decisions),
        "approved_opportunities": len(approved),
        "rejected_opportunities": len(rejected),
        "open_paper_trades": len(still_open_today) if has_broker_data else None,
        "closed_paper_trades": len(closed) if has_broker_data else None,
        "wins": len(wins) if has_broker_data else None,
        "losses": len(losses) if has_broker_data else None,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "highest_ios": round(highest_ios, 2) if highest_ios is not None else None,
        "highest_itqs": round(highest_itqs, 4) if highest_itqs is not None else None,
        "best_strategy": best_strategy,
        "best_strategy_basis": best_strategy_basis,
        "best_symbol": best_symbol,
        "most_common_rejection_reason": most_common_rejection_reason,
        "rejection_reason_breakdown": dict(rejection_categories),
        "feed_errors": len(feed_errors),
        "no_paper_broker_data": not has_broker_data,   # honest flag: True under the scan-only GitHub Actions deployment
        "ios_values": [d["ios"] for d in decisions],       # raw values, kept for Phase 3/4 comparison -- not shown in the Telegram report directly
        "itqs_values": [d["itqs"] for d in decisions if d["itqs"] is not None],
        "strategy_counts": dict(strategy_counts),
    }


# ----------------------------------------------------------------------
# Persisted daily reports -- the record Phase 3/4 compare against
# ----------------------------------------------------------------------

def save_daily_report(report: dict, journal_dir: str | Path = "data/live/journal/reports") -> Path:
    path = Path(journal_dir) / f"{report['date']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return path


def load_recent_reports(before_date: str, days: int, journal_dir: str | Path = "data/live/journal/reports") -> list:
    """Every ALREADY-SAVED report strictly before `before_date`, most
    recent `days` calendar days. Returns fewer than `days` entries if
    fewer exist -- callers must handle that explicitly (this function
    never pads or fabricates a missing day)."""
    journal_dir = Path(journal_dir)
    if not journal_dir.exists():
        return []
    cutoff = pd.Timestamp(before_date) - pd.Timedelta(days=days)
    reports = []
    for path in journal_dir.glob("*.json"):
        d = path.stem
        if d < before_date and pd.Timestamp(d) >= cutoff:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
    return sorted(reports, key=lambda r: r["date"])


# ----------------------------------------------------------------------
# Phase 2 — AI Observations (rule-based, today's data only)
# ----------------------------------------------------------------------

def generate_observations(report: dict) -> list:
    """Every observation is a direct statement of what today's activity
    literally shows -- no inference beyond the recorded numbers, no
    claim about "learning" (that requires historical comparison, which
    is Phase 3/4's job, not this function's)."""
    obs = []
    if not report["markets_scanned"]:
        obs.append("No markets were scanned today -- no activity to observe.")
        return obs

    obs.append(f"Scanned {len(report['markets_scanned'])} market(s) ({', '.join(report['markets_scanned'])}), "
               f"processing {report['candles_processed']} candles.")

    if report["signals_detected"] == 0:
        obs.append("No opportunities were detected today (consistent with S3/S4's known low signal frequency -- "
                    "Task 8/9 research found these strategies fire only a handful of times per symbol per week).")
    else:
        obs.append(f"{report['signals_detected']} opportunity(ies) reached the decision engine: "
                    f"{report['approved_opportunities']} approved, {report['rejected_opportunities']} rejected.")
        if report["best_strategy"]:
            obs.append(f"Strategy {report['best_strategy']} dominated today's approved opportunities "
                       f"({report['best_strategy_basis']}).")
        if report["best_symbol"]:
            obs.append(f"{report['best_symbol']} produced the most approved opportunities today.")
        if report["most_common_rejection_reason"]:
            obs.append(f"The most common rejection reason today was: {report['most_common_rejection_reason']}.")
        if report["highest_ios"] is not None:
            obs.append(f"Highest IOS observed today: {report['highest_ios']}.")

    if report["no_paper_broker_data"]:
        obs.append("No paper broker activity recorded today -- this deployment (GitHub Actions scan-only, "
                    "per docs/GITHUB_ACTIONS_SETUP_GUIDE.md) does not run a paper broker. Win rate/expectancy/"
                    "profit factor are not applicable until a continuously running LiveOrchestrator is deployed.")
    else:
        if report["closed_paper_trades"]:
            obs.append(f"{report['closed_paper_trades']} paper trade(s) closed today: {report['wins']} win(s), "
                       f"{report['losses']} loss(es), win rate {report['win_rate']}.")
        if report["open_paper_trades"]:
            obs.append(f"{report['open_paper_trades']} paper trade(s) remain open at report time.")

    if report["feed_errors"] > 0:
        obs.append(f"{report['feed_errors']} data feed error(s) occurred today.")

    return obs


# ----------------------------------------------------------------------
# Phase 3 — Historical Comparison
# ----------------------------------------------------------------------

def _mean(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def compare_historical(report: dict, journal_dir: str | Path = "data/live/journal/reports") -> dict:
    week = load_recent_reports(report["date"], 7, journal_dir)
    month = load_recent_reports(report["date"], 30, journal_dir)

    def _summarize(reports: list, min_days: int) -> Optional[dict]:
        if len(reports) < min_days:
            return None
        return {
            "days": len(reports),
            "avg_candles_processed": _mean([r["candles_processed"] for r in reports]),
            "avg_signals_detected": _mean([r["signals_detected"] for r in reports]),
            "avg_approved_opportunities": _mean([r["approved_opportunities"] for r in reports]),
            "avg_rejected_opportunities": _mean([r["rejected_opportunities"] for r in reports]),
            "avg_win_rate": _mean([r["win_rate"] for r in reports]),
            "avg_highest_ios": _mean([r["highest_ios"] for r in reports]),
        }

    week_summary = _summarize(week, min_days=1)
    month_summary = _summarize(month, min_days=MIN_DAYS_FOR_HISTORICAL_COMPARISON)

    deviations = []
    if week_summary and week_summary["avg_signals_detected"] is not None:
        today_signals = report["signals_detected"]
        avg = week_summary["avg_signals_detected"]
        if avg > 0 and abs(today_signals - avg) / avg >= 0.5:
            direction = "above" if today_signals > avg else "below"
            deviations.append(f"Today's signal count ({today_signals}) is {direction} the prior-{week_summary['days']}-day "
                              f"average ({avg}) by {abs(today_signals - avg) / avg * 100:.0f}%.")

    return {
        "previous_week": week_summary or f"Insufficient history for a weekly comparison (need >=1 prior day, have {len(week)}).",
        "previous_month": month_summary or f"Insufficient history for a monthly comparison (need >={MIN_DAYS_FOR_HISTORICAL_COMPARISON} prior days, have {len(month)}).",
        "notable_deviations": deviations,
    }


# ----------------------------------------------------------------------
# Phase 4 — Drift Detection
# ----------------------------------------------------------------------

def detect_drift(report: dict, journal_dir: str | Path = "data/live/journal/reports") -> list:
    """Compares today's IOS/ITQS distribution and strategy frequency
    against a rolling baseline of the prior MIN_DAYS_FOR_DRIFT_BASELINE
    days. Returns an EMPTY list with no baseline claim if there isn't
    enough history -- a drift claim without a real baseline would be
    exactly the kind of invented conclusion the task explicitly forbids."""
    baseline_reports = load_recent_reports(report["date"], MIN_DAYS_FOR_DRIFT_BASELINE, journal_dir)
    if len(baseline_reports) < MIN_DAYS_FOR_DRIFT_BASELINE:
        return [{
            "type": "insufficient_baseline",
            "detail": f"Drift detection requires >= {MIN_DAYS_FOR_DRIFT_BASELINE} days of prior history; "
                      f"only {len(baseline_reports)} available. No drift claims made.",
        }]

    anomalies = []
    baseline_ios = [v for r in baseline_reports for v in r.get("ios_values", [])]
    if baseline_ios and report["ios_values"]:
        baseline_mean = sum(baseline_ios) / len(baseline_ios)
        today_mean = sum(report["ios_values"]) / len(report["ios_values"])
        if baseline_mean > 0 and abs(today_mean - baseline_mean) / baseline_mean >= 0.25:
            anomalies.append({"type": "ios_distribution_shift",
                              "detail": f"Today's mean IOS ({today_mean:.1f}) differs from the {len(baseline_reports)}-day "
                                       f"baseline mean ({baseline_mean:.1f}) by {abs(today_mean - baseline_mean) / baseline_mean * 100:.0f}%."})

    baseline_itqs = [v for r in baseline_reports for v in r.get("itqs_values", [])]
    if baseline_itqs and report["itqs_values"]:
        baseline_mean = sum(baseline_itqs) / len(baseline_itqs)
        today_mean = sum(report["itqs_values"]) / len(report["itqs_values"])
        if baseline_mean > 0 and abs(today_mean - baseline_mean) / baseline_mean >= 0.25:
            anomalies.append({"type": "itqs_distribution_shift",
                              "detail": f"Today's mean ITQS ({today_mean:.3f}) differs from the {len(baseline_reports)}-day "
                                       f"baseline mean ({baseline_mean:.3f}) by {abs(today_mean - baseline_mean) / baseline_mean * 100:.0f}%."})

    baseline_strategy_counts = Counter()
    for r in baseline_reports:
        baseline_strategy_counts.update(r.get("strategy_counts", {}))
    baseline_total = sum(baseline_strategy_counts.values())
    today_total = sum(report["strategy_counts"].values())
    if baseline_total > 0 and today_total > 0:
        for strategy_id, today_count in report["strategy_counts"].items():
            today_share = today_count / today_total
            baseline_share = baseline_strategy_counts.get(strategy_id, 0) / baseline_total
            if baseline_share > 0 and abs(today_share - baseline_share) / baseline_share >= 0.5:
                direction = "up" if today_share > baseline_share else "down"
                anomalies.append({"type": "strategy_frequency_shift",
                                  "detail": f"{strategy_id}'s share of today's approved opportunities ({today_share*100:.0f}%) is "
                                           f"{direction} from its {len(baseline_reports)}-day baseline share ({baseline_share*100:.0f}%)."})

    if not anomalies:
        anomalies.append({"type": "none", "detail": f"No distribution/frequency drift detected against the {len(baseline_reports)}-day baseline."})
    return anomalies


# ----------------------------------------------------------------------
# Phase 5 — Recommendations (operational only, never auto-applied)
# ----------------------------------------------------------------------

def generate_recommendations(report: dict, drift: list) -> list:
    recs = []
    if report["signals_detected"] > 0:
        rejection_rate = report["rejected_opportunities"] / report["signals_detected"]
        if rejection_rate >= 0.8:
            recs.append(f"Investigate the high rejection rate today ({rejection_rate*100:.0f}% of {report['signals_detected']} "
                        f"opportunities rejected) -- most common reason: {report['most_common_rejection_reason']}.")
    if report["feed_errors"] > 0:
        recs.append(f"Verify data feed stability -- {report['feed_errors']} feed error(s) recorded today.")
    if not report["no_paper_broker_data"] and report["win_rate"] is not None and report["closed_paper_trades"] >= 5 and report["win_rate"] < 0.3:
        recs.append(f"Review today's strategy performance -- win rate {report['win_rate']} across "
                    f"{report['closed_paper_trades']} closed trades is notably low (sample size still small; "
                    f"treat as a flag to watch, not a conclusion).")
    for anomaly in drift:
        if anomaly["type"] not in ("none", "insufficient_baseline"):
            recs.append(f"Review: {anomaly['detail']}")
    if not recs:
        recs.append("No operational concerns flagged today.")
    return recs


# ----------------------------------------------------------------------
# Rendering + Phase 6 learning log
# ----------------------------------------------------------------------

def render_report_markdown(report: dict, observations: list, historical: dict, drift: list, recommendations: list) -> str:
    lines = [f"*Daily Intelligence Report - {report['date']}*", ""]
    lines.append("*Summary*")
    lines.append(f"Markets scanned: `{', '.join(report['markets_scanned']) or 'none'}`")
    lines.append(f"Candles processed: `{report['candles_processed']}`")
    lines.append(f"Signals detected: `{report['signals_detected']}`")
    lines.append(f"Approved: `{report['approved_opportunities']}` | Rejected: `{report['rejected_opportunities']}`")
    if report["no_paper_broker_data"]:
        lines.append("Paper trades: `not applicable (no paper broker in this deployment)`")
    else:
        lines.append(f"Open trades: `{report['open_paper_trades']}` | Closed: `{report['closed_paper_trades']}`")
        lines.append(f"Wins: `{report['wins']}` | Losses: `{report['losses']}` | Win rate: `{report['win_rate']}`")
        lines.append(f"Expectancy: `{report['expectancy']}` | Profit factor: `{report['profit_factor']}`")
    lines.append(f"Highest IOS: `{report['highest_ios']}` | Highest ITQS: `{report['highest_itqs']}`")
    lines.append(f"Best strategy: `{report['best_strategy']}` | Best symbol: `{report['best_symbol']}`")
    lines.append(f"Most common rejection reason: `{report['most_common_rejection_reason']}`")

    lines += ["", "*Observations*"] + [f"- {o}" for o in observations]

    lines += ["", "*Historical Comparison*"]
    if isinstance(historical["previous_week"], dict):
        w = historical["previous_week"]
        lines.append(f"vs prior {w['days']}-day avg: signals {w['avg_signals_detected']}, approved {w['avg_approved_opportunities']}, IOS {w['avg_highest_ios']}")
    else:
        lines.append(historical["previous_week"])
    if isinstance(historical["previous_month"], dict):
        m = historical["previous_month"]
        lines.append(f"vs prior {m['days']}-day avg: signals {m['avg_signals_detected']}, approved {m['avg_approved_opportunities']}, IOS {m['avg_highest_ios']}")
    else:
        lines.append(historical["previous_month"])
    for dev in historical["notable_deviations"]:
        lines.append(f"- {dev}")

    lines += ["", "*Drift Detection*"] + [f"- {a['detail']}" for a in drift]
    lines += ["", "*Recommendations*"] + [f"- {r}" for r in recommendations]
    lines += ["", f"_Generated {report['generated_at']}_"]
    return "\n".join(lines)


def append_learning_log(report: dict, observations: list, drift: list, recommendations: list,
                         jsonl_path: str | Path = "data/live/journal/learning_log.jsonl",
                         markdown_path: str | Path = "docs/journal/LEARNING_LOG.md") -> None:
    """Phase 6: the cumulative journal. `jsonl_path` is the source of
    truth (one line per day, machine-readable, append-only); `markdown_path`
    is the same information rendered for a human reader -- both files
    only ever grow, entries are never rewritten or removed."""
    entry = {
        "date": report["date"], "observations": observations,
        "anomalies": [a["detail"] for a in drift if a["type"] not in ("none",)],
        "recommendations": recommendations,
    }
    jsonl_path = Path(jsonl_path)
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")

    markdown_path = Path(markdown_path)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    if not markdown_path.exists():
        markdown_path.write_text(
            "# Platform Learning Log\n\n"
            "Cumulative operational history, one section per trading day. Generated automatically by "
            "`src.live.journal` from recorded activity data — never hand-edited, never claims a conclusion "
            "not directly supported by that day's (or a documented historical baseline's) data.\n\n",
            encoding="utf-8",
        )
    section = [f"\n## {report['date']}\n", "**Key observations:**"]
    section += [f"- {o}" for o in observations]
    section.append("\n**Detected anomalies:**")
    anomalies_text = [a["detail"] for a in drift if a["type"] not in ("none",)]
    section += [f"- {a}" for a in anomalies_text] if anomalies_text else ["- None."]
    section.append("\n**Lessons / recommendations:**")
    section += [f"- {r}" for r in recommendations]
    with open(markdown_path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(section) + "\n")
