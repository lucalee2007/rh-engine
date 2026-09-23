#!/usr/bin/env python3
"""Deterministic decision engine for Luca's Robinhood high-beta agent.

The scheduled Claude run fetches data through the Robinhood connector, writes it
to JSON, and calls this script. The script never talks to a broker. It only turns
a data snapshot into an exact list of actions, and enforces every hard limit.

Two stages:
    python3 rh_engine.py screen  screen.json   -> which tickers to deep-fetch
    python3 rh_engine.py decide  snapshot.json -> actions, skips, log text

Standard library only (Python 3.9+). See README.md for the JSON formats.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Rules (Luca's high-beta ruleset, 23 Sep 2026). Only Luca changes these.
# ---------------------------------------------------------------------------
RULES = {
    # account-level safety
    "halt_value": 1137.00,            # 75% of ~$1,516 start: liquidate + halt
    "bp_kill": 500.00,                # buying power below this: liquidate + halt
    "bp_min_after_buy": 550.00,       # never buy below this leftover buying power
    "daily_loss_no_buys": 0.06,       # down 6% vs start of day: no new buys
    "max_orders_per_day": 8,
    "waterfall_iwm_drop": 0.02,       # IWM down 2%+ and below its open: no buys
    "buy_window_et": ("09:45", "15:45"),
    # universe
    "mcap_min": 200e6,
    "mcap_max": 8e9,
    "mcap_stretch_max": 10e9,         # only if vol ratio still high
    "price_min": 5.00,
    "dollar_vol_min": 5e6,
    "min_history_bars": 60,
    # sizing
    "max_positions": 7,
    "max_name_pct": 0.18,
    "max_sector_pct": 0.45,
    "trade_cap": 200.00,
    "size_default": 100.00,
    "size_mid": 150.00,
    "size_high": 200.00,
    "max_high_conviction_per_day": 2,
    "max_same_industry_buys_per_day": 3,
    "limit_markup_max": 0.005,        # limit <= ask + 0.5%
    # entries
    "rsi_min": 30.0,
    "rsi_max": 75.0,
    "max_day_drop_without_reclaim": 0.12,
    "vol_ratio_min": 1.5,             # 60d realized vol vs IWM ("beta" proxy)
    "quality_min": 2,                 # need at least 2 of 6 criteria
    # exits
    "stop_pct": 0.10,
    "trail_trigger": 0.12,
    "trail_pct": 0.15,
    "take_profit": 0.30,
    "time_stop_days": 25,
    "time_stop_gain": 0.05,
    # screening
    "max_deep_checks": 12,
    "breakout_screen_move": 0.02,
    "pullback_screen_low": -0.04,
    "rejected_recheck_move": 0.03,
}

DRIVER_GROUPS = {"semis", "ai", "fintech", "software", "space", "health", "crypto"}


# ---------------------------------------------------------------------------
# Indicators (pure functions, oldest value first)
# ---------------------------------------------------------------------------
def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes, n=14):
    """Wilder RSI over the whole series."""
    if len(closes) < n + 1:
        return None
    ch = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gain = sum(max(x, 0) for x in ch[:n]) / n
    loss = sum(max(-x, 0) for x in ch[:n]) / n
    for x in ch[n:]:
        gain = (gain * (n - 1) + max(x, 0)) / n
        loss = (loss * (n - 1) + max(-x, 0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def realized_vol(closes, n=60):
    """Stdev of daily log returns over the last n returns."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def heikin_ashi(bars):
    """Return list of (ha_open, ha_close) for bars with o/h/l/c."""
    out = []
    for i, b in enumerate(bars):
        c = (b["o"] + b["h"] + b["l"] + b["c"]) / 4
        o = (b["o"] + b["c"]) / 2 if i == 0 else (out[-1][0] + out[-1][1]) / 2
        out.append((o, c))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pct(a, b):
    return (a / b - 1) if b else 0.0


def minutes_since_open(now_et):
    t = datetime.strptime(now_et, "%Y-%m-%dT%H:%M")
    return max(0, min(390, (t.hour * 60 + t.minute) - (9 * 60 + 30)))


def in_buy_window(now_et):
    hm = now_et[11:16]
    lo, hi = RULES["buy_window_et"]
    return lo <= hm <= hi


def round2(x):
    return math.floor(x * 100 + 0.5) / 100


# ---------------------------------------------------------------------------
# Stage 1: screen
# ---------------------------------------------------------------------------
def screen(data):
    """Pick up to max_deep_checks tickers to fetch full history for.

    data = {
      "tiers": [["MXL", ...], ["ACLS", ...], ...],   # priority order
      "quotes": {"MXL": {"price": .., "prev_close": ..}, ...},
      "held": ["INOD", ...], "banned_today": [...], "bought_today": [...],
      "rejected_today": {"SYM": price_at_rejection}
    }
    """
    held = set(data.get("held", []))
    banned = set(data.get("banned_today", [])) | set(data.get("bought_today", []))
    rejected = data.get("rejected_today", {})
    quotes = data.get("quotes", {})
    breakouts, pullbacks, notes = [], [], []
    for rank, tier in enumerate(data.get("tiers", [])):
        for sym in tier:
            if sym in held or sym in banned:
                continue
            q = quotes.get(sym)
            if not q or not q.get("price") or not q.get("prev_close"):
                notes.append({"symbol": sym, "reason": "no quote"})
                continue
            p = q["price"]
            if p < RULES["price_min"]:
                notes.append({"symbol": sym, "reason": f"price ${p:.2f} under $5"})
                continue
            if p > RULES["trade_cap"]:
                notes.append({"symbol": sym, "reason": f"price ${p:.2f} over the $200 cap"})
                continue
            if sym in rejected and abs(pct(p, rejected[sym])) < RULES["rejected_recheck_move"]:
                continue
            chg = pct(p, q["prev_close"])
            if chg >= RULES["breakout_screen_move"]:
                breakouts.append((rank, -chg, sym))
            elif RULES["pullback_screen_low"] <= chg <= 0:
                pullbacks.append((rank, chg, sym))
    breakouts.sort()
    pullbacks.sort(key=lambda x: (x[0], -x[1]))
    picks = []
    for _, _, sym in breakouts + pullbacks:
        if sym not in picks:
            picks.append(sym)
        if len(picks) >= RULES["max_deep_checks"]:
            break
    return {"version": VERSION, "deep_check": picks, "notes": notes}


# ---------------------------------------------------------------------------
# Stage 2: decide
# ---------------------------------------------------------------------------
def analyze_candidate(sym, c, iwm_closes, live_price, minutes):
    """Return (passes, info) for one candidate. info always has 'reasons'."""
    bars = c["bars"]                      # completed daily bars, oldest first
    today = c.get("today", {})
    fund = c.get("fund", {})
    info = {"symbol": sym, "reasons": [], "near_miss": False}
    fails = []

    if len(bars) < RULES["min_history_bars"]:
        info["reasons"].append(f"only {len(bars)} daily bars")
        return False, info

    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    price = live_price
    prev_close = closes[-1]
    closes_live = closes + [price]
    day_chg = pct(price, prev_close)

    # universe
    mcap = fund.get("market_cap") or 0
    vr = None
    sv, iv = realized_vol(closes, 60), realized_vol(iwm_closes, 60)
    if sv and iv:
        vr = sv / iv
    info["vol_ratio"] = round(vr, 2) if vr else None
    dollar_vol = (sum(vols[-20:]) / 20) * sum(closes[-20:]) / 20
    info["dollar_vol_20d"] = round(dollar_vol)
    if price < RULES["price_min"]:
        info["reasons"].append("price under $5")
        return False, info
    if price > RULES["trade_cap"]:
        info["reasons"].append("price over the $200 cap")
        return False, info
    if mcap < RULES["mcap_min"]:
        fails.append(f"market cap ${mcap/1e6:.0f}M under $200M")
    elif mcap > RULES["mcap_max"]:
        stretch_ok = mcap <= RULES["mcap_stretch_max"] and vr and vr >= RULES["vol_ratio_min"]
        if not stretch_ok:
            fails.append(f"market cap ${mcap/1e9:.1f}B over $8B (stretch needs <=$10B and high vol)")
    if dollar_vol < RULES["dollar_vol_min"]:
        fails.append(f"20d dollar volume ${dollar_vol/1e6:.1f}M under $5M")

    # technicals
    s20 = sma(closes_live, 20)
    s20_prev = sma(closes_live[:-5], 20)
    s50 = sma(closes_live, 50)
    r = rsi(closes_live)
    hi10 = max(b["h"] for b in bars[-10:])
    hi20 = max(b["h"] for b in bars[-20:])
    avg_v20 = sum(vols[-20:]) / 20
    frac = max(minutes, 15) / 390
    vol_pace = (today.get("volume", 0) / frac) / avg_v20 if avg_v20 else 0
    info.update({
        "price": price, "day_chg_pct": round(day_chg * 100, 2),
        "sma20": round(s20, 2), "sma50": round(s50, 2), "rsi14": round(r, 1),
        "hi10": hi10, "hi20": hi20, "vol_pace": round(vol_pace, 2),
    })

    green = price > prev_close
    sma20_rising = s20 > s20_prev
    # selling slowed: last 3 red bars have falling volume
    red_bars = [b for b in bars[-6:] if b["c"] < b["o"]][-3:]
    selling_slowed = len(red_bars) >= 2 and all(
        red_bars[i]["v"] < red_bars[i - 1]["v"] for i in range(1, len(red_bars)))
    last3 = bars[-3:]
    freefall = (not green and all(b["c"] < b["o"] for b in last3)
                and last3[0]["v"] < last3[1]["v"] < last3[2]["v"])

    setup = None
    # preferred 1: bounce off a rising 20-day SMA
    touched = any(b["l"] <= s20 * 1.02 for b in bars[-3:]) or today.get("low", price) <= s20 * 1.02
    if sma20_rising and price > s20 and touched and (green or selling_slowed):
        setup = "bounce_20sma"
    # preferred 2: first green day after a 3-10 day pullback on shrinking volume
    if setup is None and green:
        window = bars[-11:]
        hi_idx = max(range(len(window)), key=lambda i: window[i]["h"])
        days_since_hi = len(window) - 1 - hi_idx
        pulled = window[hi_idx + 1:]
        if 3 <= days_since_hi <= 10 and pulled and bars[-1]["c"] < bars[-2]["c"] + 1e-9:
            if sum(b["v"] for b in pulled) / len(pulled) < avg_v20:
                setup = "pullback_first_green"
    # acceptable: breakout through the 10-20 day high on above-average volume
    if setup is None and price > hi10 and vol_pace >= 1.0:
        setup = "breakout_20d" if price > hi20 else "breakout_10d"
    info["setup"] = setup
    info["preferred_setup"] = setup in ("bounce_20sma", "pullback_first_green")

    if setup is None:
        fails.append("no setup (no 20SMA bounce, pullback first-green or breakout)")
    if not (RULES["rsi_min"] <= r <= RULES["rsi_max"]):
        fails.append(f"RSI {r:.1f} outside 30-75")
    if freefall:
        info["reasons"].append("free-fall: 3 red days on rising volume, no bounce")
        return False, info
    if day_chg <= -RULES["max_day_drop_without_reclaim"]:
        reclaimed = price > today.get("open", float("inf")) or (
            today.get("vwap") and price > today["vwap"])
        if not reclaimed:
            info["reasons"].append(f"down {day_chg*100:.1f}% with no VWAP/open reclaim")
            return False, info

    # quality: need 2 of 6
    q = quality(c.get("fin") or [], vr, c.get("group"), fund)
    info["quality"] = q
    if q["count"] < RULES["quality_min"]:
        fails.append(f"quality {q['count']}/6 (needs 2): {', '.join(q['met']) or 'none'}")

    info["earnings_soon"] = bool(c.get("earnings_within_2d"))
    info["conviction"] = (q["count"] + (1 if info["preferred_setup"] else 0)
                          + (1 if vr and vr >= 2.0 else 0))
    info["reasons"].extend(fails)
    # an exception candidate has a real setup and misses exactly one other rule
    if len(fails) == 1 and setup is not None:
        info["near_miss"] = True
    return not fails, info


def quality(fin, vol_ratio, group, fund):
    """fin: quarterly rows most-recent-first with revenue, gross_profit, net_income."""
    met = []
    rows = [f for f in fin if f.get("revenue")]
    if len(rows) >= 5:
        r0, r1, r4 = rows[0]["revenue"], rows[1]["revenue"], rows[4]["revenue"]
        yoy0 = pct(r0, r4)
        yoy1 = pct(r1, rows[5]["revenue"]) if len(rows) >= 6 else None
        if yoy0 >= 0.25 or (yoy1 is not None and yoy0 > yoy1 and yoy0 > 0):
            met.append(f"revenue +{yoy0*100:.0f}% YoY" + ("" if yoy0 >= 0.25 else ", accelerating"))
        gm0 = rows[0].get("gross_profit", 0) / r0 if r0 else 0
        gm4 = rows[4].get("gross_profit", 0) / r4 if r4 else 0
        if gm0 >= 0.30 or gm0 > gm4:
            met.append(f"gross margin {gm0*100:.0f}%")
        ni0, ni4 = rows[0].get("net_income", 0), rows[4].get("net_income", 0)
        if ni0 > 0 or (ni0 / r0 > ni4 / r4 if r0 and r4 else False):
            met.append("profitable" if ni0 > 0 else "losses narrowing")
    de = fund.get("debt_to_equity")
    if de is not None and de < 2.0:
        met.append(f"debt/equity {de:.1f}")
    if group in DRIVER_GROUPS:
        met.append(f"growth driver ({group})")
    if vol_ratio and vol_ratio >= RULES["vol_ratio_min"]:
        met.append(f"high beta (vol {vol_ratio:.1f}x IWM)")
    return {"count": len(met), "met": met}


def manage_position(p, now_date, bought_today):
    """Return list of actions for one held position (sells / stop changes)."""
    acts = []
    sym, qty, cost, price = p["symbol"], p["qty"], p["avg_cost"], p["price"]
    frac = abs(qty - round(qty)) > 1e-9
    high = max(p.get("high_since_entry") or price, price)
    existing = (p.get("stop_order") or {}).get("stop_price")
    floor_stop = p.get("stop_floor")      # a stop level that must never move lower
    gain = pct(price, cost)

    desired = cost * (1 - RULES["stop_pct"])
    if pct(high, cost) >= RULES["trail_trigger"]:
        desired = max(desired, high * (1 - RULES["trail_pct"]))
    for lvl in (existing, floor_stop):
        if lvl:
            desired = max(desired, lvl)
    desired = round2(desired)
    same_day = sym in bought_today

    def sell_all(rule, reason):
        acts.append({"action": "sell", "symbol": sym, "qty": qty, "type": "market",
                     "cancel_order_id": (p.get("stop_order") or {}).get("id"),
                     "rule": rule, "reason": reason})

    # stop hit (fractional shares have no broker stop; whole shares' broker stop handles it)
    if price <= desired and (frac or not existing):
        sell_all("stop", f"price ${price:.2f} at/below stop ${desired:.2f}")
        return acts

    if not same_day:
        closes = p.get("closes") or []
        if len(closes) >= 51:
            below = [closes[k] < sum(closes[k - 49:k + 1]) / 50
                     for k in (len(closes) - 1, len(closes) - 2)]
            if all(below):
                sell_all("trend_break", "two closes below the 50-day SMA")
                return acts
        days = p.get("trading_days_held")
        if days is not None and days >= RULES["time_stop_days"] and gain <= RULES["time_stop_gain"]:
            sell_all("time_stop", f"up {gain*100:.1f}% after {days} trading days")
            return acts
        if gain >= RULES["take_profit"] and not p.get("half_taken"):
            half = math.floor(qty / 2) if not frac else round(qty / 2, 6)
            if half > 0:
                acts.append({"action": "sell", "symbol": sym, "qty": half, "type": "market",
                             "cancel_order_id": (p.get("stop_order") or {}).get("id"),
                             "rule": "take_profit",
                             "reason": f"up {gain*100:.1f}% (>= 30%): selling half",
                             "then_stop": None if frac else {"qty": qty - half, "stop_price": desired}})
                return acts

    if not frac:
        if not existing:
            acts.append({"action": "place_stop", "symbol": sym, "qty": qty, "stop_price": desired,
                         "rule": "stop", "reason": "no live stop order"})
        elif desired > existing * 1.005:
            acts.append({"action": "replace_stop", "symbol": sym, "qty": qty,
                         "cancel_order_id": p["stop_order"]["id"], "stop_price": desired,
                         "rule": "trailing", "reason": f"raise stop ${existing:.2f} -> ${desired:.2f}"})
    p["_desired_stop"] = desired
    return acts


def decide(s):
    now = s["now_et"]
    acct = s["account"]
    tv, bp = acct["total_value"], acct["buying_power"]
    sod = s.get("start_of_day_value") or tv
    orders_used = s.get("orders_today", 0)
    positions = s.get("positions", [])
    bought_today = set(s.get("bought_today", []))
    out = {"version": VERSION, "actions": [], "skipped": [], "near_misses": [],
           "flags": [], "halt": False, "buys_allowed": True, "log": []}
    log = out["log"]
    log.append(f"engine {VERSION} | {now} ET | value ${tv:,.2f} | buying power ${bp:,.2f} | "
               f"start-of-day ${sod:,.2f} ({pct(tv, sod)*100:+.2f}%) | orders today {orders_used}/"
               f"{RULES['max_orders_per_day']}")

    if s.get("halted"):
        out.update(halt=True, buys_allowed=False)
        out["flags"].append("HALTED doc exists: trading stopped until Luca resumes")
        return out

    # 1. account-level safety
    if tv <= RULES["halt_value"] or bp < RULES["bp_kill"]:
        why = (f"value ${tv:,.2f} <= ${RULES['halt_value']:,.0f}" if tv <= RULES["halt_value"]
               else f"buying power ${bp:,.2f} < ${RULES['bp_kill']:,.0f}")
        out.update(halt=True, buys_allowed=False)
        for p in positions:
            out["actions"].append({"action": "sell", "symbol": p["symbol"], "qty": p["qty"],
                                   "type": "market",
                                   "cancel_order_id": (p.get("stop_order") or {}).get("id"),
                                   "rule": "halt", "reason": why})
        out["flags"].append(f"HALT: {why}. Create HALTED doc and notify Luca.")
        log.append(f"HALT triggered: {why}")
        return out

    no_buy = []
    if pct(tv, sod) <= -RULES["daily_loss_no_buys"]:
        no_buy.append(f"down {pct(tv, sod)*100:.1f}% today (6% limit)")
    iwm = s.get("iwm", {})
    iwm_chg = pct(iwm.get("price", 0), iwm.get("prev_close", 0)) if iwm.get("prev_close") else 0
    if iwm_chg <= -RULES["waterfall_iwm_drop"] and iwm.get("price", 0) < iwm.get("open", 0):
        no_buy.append(f"IWM waterfall ({iwm_chg*100:.1f}% and below its open)")
    if not in_buy_window(now):
        no_buy.append("outside the 9:45-15:45 ET buy window")
    log.append(f"IWM {iwm_chg*100:+.2f}% on the day")

    # 2. positions
    budget = RULES["max_orders_per_day"] - orders_used
    for p in positions:
        for a in manage_position(p, now[:10], bought_today):
            # cancels don't count as orders; a half-sale also needs a new stop for the rest
            cost = 2 if a.get("then_stop") else 1
            if budget < cost:
                if a["action"] == "sell" and a["rule"] == "stop":
                    # a stop-loss exit is protective; it goes through even at the order cap
                    out["flags"].append(f"order limit exceeded to exit {a['symbol']} at its stop")
                else:
                    out["flags"].append(
                        f"order limit reached: could not {a['action']} {a['symbol']}")
                    continue
            out["actions"].append(a)
            budget -= cost

    # 3. buys
    if budget < 2:
        no_buy.append("not enough orders left today for a buy + its stop")
    open_positions = len([p for p in positions]) - len(
        [a for a in out["actions"] if a["action"] == "sell" and a["rule"] != "take_profit"])
    if open_positions >= RULES["max_positions"]:
        no_buy.append(f"{open_positions} positions (max {RULES['max_positions']})")
    if no_buy:
        out["buys_allowed"] = False
        log.append("no new buys: " + "; ".join(no_buy))
        return _finish(out, positions)

    minutes = minutes_since_open(now)
    iwm_closes = iwm.get("closes", [])
    analyzed = []
    for sym, c in s.get("candidates", {}).items():
        live = (s.get("quotes", {}).get(sym) or {}).get("price") or c.get("price")
        if sym in set(s.get("banned_today", [])) | bought_today or any(
                p["symbol"] == sym for p in positions):
            out["skipped"].append({"symbol": sym, "reason": "held, bought or banned today"})
            continue
        ok, info = analyze_candidate(sym, c, iwm_closes, live, minutes)
        info["sector"] = c.get("sector") or (c.get("fund") or {}).get("sector")
        info["industry"] = c.get("industry") or (c.get("fund") or {}).get("industry")
        info["ask"] = (s.get("quotes", {}).get(sym) or {}).get("ask") or live
        if ok:
            analyzed.append(info)
        else:
            out["skipped"].append({"symbol": sym, "reason": "; ".join(info["reasons"]),
                                   "detail": _brief(info)})
            if info.get("near_miss"):
                out["near_misses"].append({"symbol": sym, "missed": info["reasons"],
                                           "detail": _brief(info)})

    analyzed.sort(key=lambda x: (-x["conviction"], -(x.get("vol_ratio") or 0)))
    sector_val = {}
    for p in positions:
        sector_val[p.get("sector")] = sector_val.get(p.get("sector"), 0) + p["qty"] * p["price"]
    industry_buys = dict(s.get("industry_buys_today", {}))
    high_conv_used = s.get("high_conviction_buys_today", 0)
    bp_left = bp
    n_pos = open_positions
    for info in analyzed:
        sym = info["symbol"]
        if budget < 2 or n_pos >= RULES["max_positions"]:
            out["skipped"].append({"symbol": sym, "reason": "no room left (orders or positions)"})
            continue
        size = RULES["size_default"]
        top = (not info["earnings_soon"] and high_conv_used < RULES["max_high_conviction_per_day"]
               and info["conviction"] >= 4)
        if top:
            size = RULES["size_high"] if info["conviction"] >= 5 else RULES["size_mid"]
        ask = info["ask"]
        limit = round2(min(ask * (1 + RULES["limit_markup_max"]), ask + max(0.01, ask * 0.002)))
        qty = math.floor(size / limit)
        if qty == 0 and limit <= RULES["trade_cap"] and top:
            qty = 1
        notional = qty * limit
        reasons = []
        if qty == 0:
            reasons.append(f"${limit:.2f}/share: 0 whole shares at ${size:.0f}")
        if notional > RULES["trade_cap"] + 1e-9:
            reasons.append("over the $200 cap")
        if notional > RULES["max_name_pct"] * tv:
            reasons.append("over 18% of account")
        sec = info.get("sector")
        if (sector_val.get(sec, 0) + notional) > RULES["max_sector_pct"] * tv:
            reasons.append(f"sector {sec} would exceed 45%")
        ind = info.get("industry")
        if industry_buys.get(ind, 0) >= RULES["max_same_industry_buys_per_day"]:
            reasons.append(f"already 3 buys in {ind} today")
        if bp_left - notional < RULES["bp_min_after_buy"]:
            reasons.append(f"would leave buying power ${bp_left - notional:,.2f} (< $550)")
        if reasons:
            out["skipped"].append({"symbol": sym, "reason": "; ".join(reasons),
                                   "detail": _brief(info)})
            continue
        out["actions"].append({
            "action": "buy", "symbol": sym, "qty": qty, "limit_price": limit,
            "notional": round2(notional), "time_in_force": "gfd",
            "stop_after_fill_pct": RULES["stop_pct"], "rule": info["setup"],
            "conviction": info["conviction"],
            "reason": (f"{info['setup']}: ${info['price']:.2f}, SMA20 ${info['sma20']:.2f}, "
                       f"SMA50 ${info['sma50']:.2f}, RSI {info['rsi14']}, vol pace "
                       f"{info['vol_pace']}x, vol {info.get('vol_ratio')}x IWM; quality "
                       f"{info['quality']['count']}/6 ({', '.join(info['quality']['met'])})"
                       + ("; earnings within 2 days, sized at $100" if info["earnings_soon"] else "")),
        })
        budget -= 2
        n_pos += 1
        bp_left -= notional
        sector_val[sec] = sector_val.get(sec, 0) + notional
        industry_buys[ind] = industry_buys.get(ind, 0) + 1
        if top:
            high_conv_used += 1
    return _finish(out, positions)


def _brief(info):
    keys = ("price", "day_chg_pct", "sma20", "sma50", "rsi14", "vol_pace", "vol_ratio", "setup")
    return {k: info.get(k) for k in keys if k in info}


def _finish(out, positions):
    for p in positions:
        out["log"].append(
            f"position {p['symbol']}: {p['qty']} @ ${p['avg_cost']:.2f}, now ${p['price']:.2f} "
            f"({pct(p['price'], p['avg_cost'])*100:+.1f}%), stop ${p.get('_desired_stop', 0):.2f}")
    for a in out["actions"]:
        out["log"].append(f"ACTION {a['action'].upper()} {a['symbol']} "
                          f"{a.get('qty')} | {a['rule']} | {a['reason']}")
    for sk in out["skipped"]:
        out["log"].append(f"SKIPPED {sk['symbol']} | {sk['reason']}")
    return out


def convert_historicals(raw, before_date=None):
    """Turn a get_equity_historicals response into {symbol: [bars]} (completed days only).

    Drops interpolated bars and any bar dated on/after before_date (YYYY-MM-DD),
    so today's partial bar never leaks into the completed-bar history.
    """
    results = raw.get("data", raw).get("results", [])
    out = {}
    for r in results:
        bars = []
        for b in r.get("bars", []):
            if b.get("interpolated"):
                continue
            if before_date and b["begins_at"][:10] >= before_date:
                continue
            bars.append({"d": b["begins_at"][:10], "o": float(b["open_price"]),
                         "h": float(b["high_price"]), "l": float(b["low_price"]),
                         "c": float(b["close_price"]), "v": float(b["volume"])})
        out[r["symbol"]] = bars
    return out


def main(argv):
    if len(argv) >= 3 and argv[1] == "bars":
        # python3 rh_engine.py bars historicals.json [YYYY-MM-DD]
        with open(argv[2]) as f:
            raw = json.load(f)
        print(json.dumps(convert_historicals(raw, argv[3] if len(argv) > 3 else None)))
        return 0
    if len(argv) != 3 or argv[1] not in ("screen", "decide"):
        print(__doc__)
        return 2
    with open(argv[2]) as f:
        data = json.load(f)
    result = screen(data) if argv[1] == "screen" else decide(data)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
