"""Task 10 Phase 6 — Daily Trading Plan generator: formats a batch of
`Decision` objects (Phase 4) into the human-readable plan format from
the task brief's own worked example."""

from __future__ import annotations

import pandas as pd


def _risk_line(d) -> str:
    return f"Risk: {d.allocated_risk_pct:.2f}%" if d.verdict == "EXECUTE" else ""


def format_daily_plan(decisions: list, plan_date, account_balance: float) -> str:
    executed = [d for d in decisions if d.verdict == "EXECUTE"]
    postponed = [d for d in decisions if d.verdict == "POSTPONE"]
    ignored = [d for d in decisions if d.verdict == "IGNORE"]

    lines = [f"# Institutional Daily Trading Plan — {plan_date}", "", f"Account balance: ${account_balance:,.2f}", ""]

    lines.append("## Today's Best Opportunities")
    lines.append("")
    if not executed:
        lines.append("_No opportunities cleared every institutional check today._")
    for i, d in enumerate(sorted(executed, key=lambda x: -x.ios), start=1):
        o = d.opportunity
        lines.append(f"{i}. **{o.symbol} — {o.strategy_id}**  IOS: {d.ios:.1f} ({d.ios_tier})")
        lines.append("   Reason:")
        for r in d.reasons_for:
            lines.append(f"   • {r}")
        lines.append(f"   Entry: {o.entry:.5f}  Stop: {o.stop:.5f}  Target: {o.target:.5f}  Expected R: {o.expected_r:.2f}")
        lines.append(f"   Risk: {d.allocated_risk_pct:.2f}%")
        lines.append("")

    if postponed:
        lines.append("## Postponed Opportunities (capacity-limited, re-evaluate if a slot opens)")
        lines.append("")
        for d in sorted(postponed, key=lambda x: -x.ios):
            o = d.opportunity
            lines.append(f"**{o.symbol} — {o.strategy_id}**  IOS: {d.ios:.1f}")
            for r in d.reasons_against:
                lines.append(f"Reason: {r}")
            lines.append("")

    lines.append("## Rejected Opportunities")
    lines.append("")
    if not ignored:
        lines.append("_None rejected today._")
    for d in sorted(ignored, key=lambda x: -x.ios):
        o = d.opportunity
        lines.append(f"**{o.symbol} — {o.strategy_id}**  IOS: {d.ios:.1f}")
        lines.append("Reason:")
        for r in d.reasons_against:
            lines.append(f"  {r}")
        lines.append("")

    return "\n".join(lines)


def decisions_to_dataframe(decisions: list) -> pd.DataFrame:
    rows = []
    for d in decisions:
        o = d.opportunity
        rows.append({
            "timestamp": o.timestamp, "symbol": o.symbol, "strategy_id": o.strategy_id, "direction": o.direction,
            "ios": d.ios, "ios_tier": d.ios_tier, "verdict": d.verdict,
            "reasons_for": "; ".join(d.reasons_for), "reasons_against": "; ".join(d.reasons_against),
            "allocated_risk_pct": d.allocated_risk_pct,
            "entry": o.entry, "stop": o.stop, "target": o.target, "expected_r": o.expected_r,
        })
    return pd.DataFrame(rows)
