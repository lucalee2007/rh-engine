# Backtest

Daily replay of the real engine over historical bars. See the docstring in
`backtest.py` for exactly what is simulated and what is simplified.

    python3 backtest/backtest.py rh_engine.py backtest/data                 # default 2024-10 to data end
    python3 backtest/backtest.py rh_engine.py backtest/data --start 2025-04-01 --end 2025-12-31
    python3 backtest/backtest.py rh_engine.py backtest/data --rule rs_min=-9  # try a rule change
    python3 backtest/backtest.py old_engine.py backtest/data                # compare versions

`--rule key=value` overrides one RULES entry for that run only (repeatable).
`halt_value` is an absolute dollar level, so compare versions with `--rule halt_value=0`
unless you want to see halts.

## Data (snapshot 2026-10-01)
Built with the Robinhood connector from the Drive watchlist (92 names + IWM):
- `bars.json.gz`: `get_equity_historicals` interval=day, 2024-06-01 to 2026-10-01, converted
  with `rh_engine.py bars`.
- `fin.json`: `get_financials` quarterly, limit 14 (Robinhood has none for 39 of the names).
- `fund.json`: `get_equity_fundamentals` (market cap, shares, sector, industry) on 2026-10-01.
- `universe.json`: watchlist groups and tiers.

## Caveats
The universe is today's watchlist, chosen with hindsight, so absolute returns are inflated
(survivorship bias). Use it to compare rule sets against each other, not to forecast
returns. One decision per day at the close; no earnings history; ~2 years of data.
