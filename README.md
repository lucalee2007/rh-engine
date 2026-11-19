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
                    "high_since_entry", "trading_days_held", "half_taken", "closes": [...],
                    "earnings_before_next_open": false}],
     "bought_today": [...], "banned_today": [...],
     "recent_exits": {"PAYS": {"date": "2026-10-28", "pnl": -33.44}},
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
Sizing: whole shares toward the target, rounded up one share when that still fits the trade
cap, 20% per name, the buying-power floor and the sector cap; otherwise topped up with a
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

## v1.5.0 larger balance (~$2,240 after the Oct 2026 deposit)
- Halt level rebased to $1,680 (75% of the new balance, same rule as before; was $1,137).
- Up to 12 positions; buys sized $225 / $275 / $325 by conviction, $325 cap, 20% per name.
- Buying-power floors stay fixed ($300 after a buy, $250 kill), so more of the cash is
  deployable. The engine still only buys when a name passes every rule; it never forces buys.
- Backtest at $2,240 (halt off): v1.4.0 +125%, Sharpe 1.47, max DD -28.7%; v1.5.0 +109%,
  Sharpe 1.32, max DD -30.7%; last 12 months v1.4.0 +56% / DD -28.4% vs v1.5.0 +85% / DD -18.4%.
  Larger sizes ($250-$450) backtested worse (Sharpe ~0.9-1.1, DD 35-38%).

## v1.5.1 high_since_entry in the log
- Each `position` log line now ends with `high_since_entry $X` (the higher of the snapshot
  value and today's price). Scans copy the engine log into the Drive scan log, so the next
  scan can floor high_since_entry at the last logged value. No rule or sizing changes.

## v1.6.0 trade review (live record 22 Sep - 17 Nov 2026)
35 closed trades, -$100.51. Breakouts made +$128 (6 trades, 3 wins); dip buys lost $228
(bounce 17 trades / 3 wins, pullback 12 / 5), almost all bought while IWM was below its
50-day SMA. Re-buys within days of an exit went 0 for 6 (-$112). The $300+ "high
conviction" buys went 1 for 9 (-$149) while quality-2 buys made +$68.
- **Market filter:** bounce_20sma and pullback_first_green only when IWM's last close is
  above its 50-day SMA. Breakouts are allowed in any market (`dip_setups_need_iwm_uptrend`).
- **Breakouts first:** a move through the 10/20-day high on volume is labeled a breakout
  even if the stock touched its 20-day SMA in the last 3 days.
- **Re-entry cool-off:** no buy within 10 trading days of a losing exit in that name
  (`reentry_cooldown_days`; after a winning exit, `reentry_cooldown_after_win_days` = 0).
  Needs `recent_exits` in the snapshot; an exit with unknown P&L counts as a loss.
- **Shrinking revenue fails:** revenue down 10%+ year on year rejects the buy.
- **Flat sizing:** every buy targets $250 (`size_default` = `size_mid` = `size_high`), and
  bounce/pullback no longer get +1 conviction (`preferred_setup_bonus` = 0).
- **Pre-earnings exit:** a held position with `earnings_before_next_open` = true (reports
  after today's close or before the next open) is sold at that scan, unless it was bought
  today. A missing field is flagged every scan.
- Not adopted, because the record contradicts them: failing names with no financials (they
  include the three biggest winners) and blocking buys below the 50-day SMA (net +$14 with
  ASAN, PL and CELH among them).
- Backtest, average of six start dates (2024-10 to 2026-01, data to 2026-10-01, halt off):
  v1.5.1 +82% / Sharpe 1.48 / worst drawdown -37%; v1.6.0 +135% / 2.23 / -23%. v1.6.0 was
  ahead from every start date. The universe has hindsight bias and no earnings history, so
  compare versions, don't forecast. Replayed on the 35 live trades, the rules keep 7
  (+$115 at the sizes used): they block 20 losers (-$359) and 8 winners (+$143, including
  RPD and ASAN).

