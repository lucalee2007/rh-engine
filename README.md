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
`buy` (limit, GFD; after it fills place a GTC stop `stop_after_fill_pct` below the fill,
for the whole-share `qty` only). If `frac_qty` > 0, after the whole shares also buy that
fractional quantity as a MARKET order, regular hours, GFD, only if the live ask is still
within 0.5% of `limit_price` (Robinhood takes fractional orders only as market orders in
regular hours, and won't hold any stop on a fraction). The fraction has no broker stop; the
scans manage it.
Sizing: whole shares toward the target, rounded up one share when that still fits the $250
cap, 18% per name, the buying-power floor and the sector cap; otherwise topped up with a
fraction. Stops (`place_stop`, `replace_stop`, `then_stop`) always cover whole shares only.
`near_misses` are the only names eligible for a logged EXCEPTION.

## v1.3.0 quant rules
- Quality needs 2 of 6 criteria AND at least one business criterion (revenue growth,
  gross margin, or profits / narrowing losses). Growth-driver group, high beta and low
  debt alone no longer pass.
- Volume pace uses a typical intraday (U-shaped) volume curve, so 9:45 readings aren't
  inflated ~2.5x.
- Initial stop = 2.5 x ATR(14) below entry, clamped to 7%-15% (`stop_after_fill_pct` on
  each buy). Pass each held position's daily `bars` in the snapshot so the engine keeps
  using its ATR stop; without bars it keeps the live broker stop as the base.
- Relative strength: skip names lagging IWM over 63 trading days; rank buys by
  conviction, then 63-day outperformance (no longer by volatility). +1 conviction when
  beating IWM by 10%+.

## v1.4.0 price band and sizing
- New buys only between $5 and $50 a share (`price_max`). Held names above $50 are still
  managed normally (stops, trails, exits).
- Preferred band $10-$25: ranked ahead of other names with the same conviction, in both
  `screen` and `decide`.
- Up to 10 positions; buys sized $200 / $250 / $300 by conviction, $300 cap, 20% per name.
