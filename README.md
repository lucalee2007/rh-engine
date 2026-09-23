# rh-engine

Deterministic decision engine for Luca's Robinhood high-beta trading agent.
It never talks to a broker: the scheduled Claude run fetches data through the
Robinhood connector, writes JSON, runs this script, and places exactly the
orders it returns. Every hard limit lives in `RULES` at the top of
`rh_engine.py`. Standard library only (Python 3.9+).

## Commands
    python3 rh_engine.py screen screen.json          # which tickers to deep-fetch
    python3 rh_engine.py bars historicals.json DATE  # Robinhood historicals -> bars (before DATE)
    python3 rh_engine.py decide snapshot.json        # actions, skips, near misses, log lines
    python3 -m unittest discover -s tests            # tests

## screen.json
    {"tiers": [["MXL", ...], ...], "quotes": {"MXL": {"price": 83.8, "prev_close": 87.4}},
     "held": ["INOD"], "banned_today": ["PGY"], "bought_today": [], "rejected_today": {"VECO": 45.8}}

## snapshot.json
    {"now_et": "2026-09-24T11:40",
     "account": {"total_value": 1500.2, "buying_power": 1239.8},
     "start_of_day_value": 1498.65, "orders_today": 5, "halted": false,
     "positions": [{"symbol", "qty", "avg_cost", "price", "sector",
                    "stop_order": {"id", "stop_price"} | null, "stop_floor",
                    "high_since_entry", "trading_days_held", "half_taken", "closes": [...]}],
     "bought_today": [...], "banned_today": [...],
     "industry_buys_today": {"Semiconductors": 1}, "high_conviction_buys_today": 0,
     "iwm": {"price", "prev_close", "open", "closes": [80+ daily closes]},
     "quotes": {"SYM": {"price", "ask"}},
     "candidates": {"SYM": {"bars": [...], "today": {"open", "low", "volume", "vwap"},
                            "fund": {"market_cap", "debt_to_equity"}, "fin": [quarterly rows,
                            most recent first], "group": "semis", "sector", "industry",
                            "earnings_within_2d": false}}}

## Output actions
`sell` (cancel `cancel_order_id` first, then market sell `qty`; `then_stop` = new stop
for the remaining shares), `place_stop`, `replace_stop` (cancel, then new GTC stop),
`buy` (limit, GFD; after it fills place a GTC stop `stop_after_fill_pct` below the fill).
`near_misses` are the only names eligible for a logged EXCEPTION.
