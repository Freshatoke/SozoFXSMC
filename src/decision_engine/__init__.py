"""
Task 10 — Institutional Trading Decision Engine (ITDE).

Sits ABOVE the Strategy Engine (Task 3), the Backtesting Engine
(Task 4), and the Research Laboratory / Institutional Research
(Tasks 5, 8, 9). It does not generate signals, detect market
structure, or simulate trades -- it only asks, of every signal every
enabled strategy already produced: "is this one of the best trades
available in the market right now, and does taking it fit within
institutional risk limits?"

Modules:
    opportunity.py           -- Opportunity dataclass + unified queue builder (Phase 1)
    ios.py                   -- Institutional Opportunity Score (Phase 2)
    portfolio_allocation.py  -- position/exposure/correlation limits (Phase 3)
    trade_selection.py       -- execute/ignore/postpone decisions with reasons (Phase 4)
    risk_layer.py            -- institutional risk gate (Phase 5)
    daily_plan.py            -- human-readable trading plan generator (Phase 6/7)
    paper_trading.py         -- historical-data-as-live-data simulation (Phase 8)
"""
