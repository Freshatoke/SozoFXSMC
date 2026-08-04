"""
Task 11 — Live Market Infrastructure & Forward Paper Trading.

Transforms the platform from an offline research engine into a
continuously-running institutional PAPER-trading system. No live money,
no automatic broker execution -- only live market monitoring and
simulated execution (the task's explicit scope boundary).

Package layout:
    live/providers/        Phase 2  -- live data provider architecture
                                       (mirrors src.data.historical_pipeline's
                                       DataAdapter protocol + registry pattern)
    live/context_stream.py Phase 3  -- MarketContext-compatible adapter over
                                       src.engine.IncrementalEngine
    live/strategy_runner.py Phase 4 -- per-candle S3/S4 signal generation + ITQS/IOS
    live/decision_stream.py Phase 5 -- continuous (stateful) decision engine
    live/broker.py          Phase 6 -- simulated paper-trading broker
    live/event_log.py       Phase 7 -- structured audit trail
    live/dashboard.py       Phase 8 -- monitoring dashboard generator
    live/notifications.py   Phase 9 -- alert abstraction (console/file + Telegram/Discord/Email adapters)
    live/orchestrator.py    Phase 10 -- ties every phase together into one continuous loop
    live/analytics.py       Phase 11 -- daily/weekly/monthly report generation
"""
