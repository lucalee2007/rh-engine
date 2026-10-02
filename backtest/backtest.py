#!/usr/bin/env python3
"""Daily replay backtest for rh-engine.

Replays the real engine (`screen` + `decide`) once per trading day over historical daily
bars and simulates fills, Robinhood GTC stop-market stops (including gaps through the
stop), fractional top-ups and halts. It never talks to a broker.

    python3 backtest/backtest.py ENGINE.py DATA_DIR [--start YYYY-MM-DD] [--cash 1500]
                                 [--json OUT.json]

DATA_DIR holds (see backtest/README.md for how to build them from the Robinhood connector):
    bars.json      {"SYM": [{"d","o","h","l","c","v"}, ...]}   daily, oldest first, incl. IWM
    universe.json  {"groups": {"SYM": "semis"}, "tiers": [[...], [...], [...]]}
    fund.json      {"SYM": {"market_cap", "shares_outstanding", "sector", "industry"}}
    fin.json       {"SYM": [{"end", "revenue", "gross_profit", "net_income"}, ...]}  newest first

Simplifications (read these before trusting a number):
  * One decision per day at ~15:30 ET using that day's close as the live price and the
    day's open/low/volume; buys and engine sells fill at the close (buys at the engine's
    limit price, i.e. slightly above it). The live agent runs 7 times a day.
  * Stops are checked against each day's open/low: a gap below the stop fills at the open.
  * Fractional shares have no stop; the engine sells them at its daily decision.
  * Financials are used only ~50 days after each quarter ends (rough reporting lag);
    market cap is today's share count times the historical price.
  * No earnings calendar history: earnings_within_2d is always false.
  * Survivorship bias: the universe is today's watchlist, picked with hindsight.
  * No PDT, commissions or partial fills. Starting cash default $1,500.
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import math
import os
from datetime import date, timedelta

LOOKBACK_BARS = 76          # ~110 calendar days, what the live prompt fetches
IWM_BARS = 100
FIN_LAG_DAYS = 50


def load_engine(path):
    spec = importlib.util.spec_from_file_location("engine_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path) and os.path.exists(path + ".gz"):
        with gzip.open(path + ".gz", "rt") as f:
            return json.load(f)
    with open(path) as f:
        return json.load(f)


class Sim:
    def __init__(self, E, data, cash, start):
        self.E = E
        self.bars = data["bars"]
        self.uni = data["universe"]
        self.fund = data["fund"]
        self.fin = data["fin"]
        self.cash = cash
        self.start_cash = cash
        self.pos = {}            # sym -> dict
        self.trades = []         # closed round trips (per sell)
        self.equity = []         # (date, value)
        self.halted = None
        self.n_orders = 0
        self.dates = [b["d"] for b in self.bars["IWM"]]
        self.idx = {s: {b["d"]: i for i, b in enumerate(bs)} for s, bs in self.bars.items()}
        self.t0 = max(IWM_BARS, next(i for i, d in enumerate(self.dates) if d >= start))

    # -- helpers ---------------------------------------------------------------------
    def bar(self, sym, d):
        i = self.idx[sym].get(d)
        return None if i is None else self.bars[sym][i]

    def hist(self, sym, d, n):
        i = self.idx[sym].get(d)
        if i is None:
            return []
        return self.bars[sym][max(0, i - n):i]          # completed bars before d

    def value(self, d):
        v = self.cash
        for s, p in self.pos.items():
            b = self.bar(s, d)
            v += p["qty"] * (b["c"] if b else p["last"])
        return v

    def fin_rows(self, sym, d):
        rows = self.fin.get(sym) or []
        cut = (date.fromisoformat(d) - timedelta(days=FIN_LAG_DAYS)).isoformat()
        return [{"revenue": r["revenue"], "gross_profit": r["gross_profit"] or 0,
                 "net_income": r["net_income"] or 0} for r in rows if r["end"] <= cut][:6]

    def close_out(self, sym, qty, price, d, rule):
        p = self.pos[sym]
        qty = min(qty, p["qty"])
        self.cash += qty * price
        self.trades.append({"symbol": sym, "entry_date": p["entry_date"], "exit_date": d,
                            "entry": p["avg_cost"], "exit": price, "qty": qty,
                            "pnl": qty * (price - p["avg_cost"]), "rule": rule})
        p["qty"] = round(p["qty"] - qty, 6)
        whole = math.floor(p["qty"] + 1e-9)
        if p["stop"] and p["stop"]["qty"] > whole:
            p["stop"]["qty"] = whole
            if whole == 0:
                p["stop"] = None
        if p["qty"] <= 1e-6:
            del self.pos[sym]

    # -- one day -----------------------------------------------------------------------
    def stops(self, d):
        for sym in list(self.pos):
            p = self.pos[sym]
            b = self.bar(sym, d)
            if not b:
                continue
            st = p["stop"]
            if st and st["qty"] > 0 and p["entry_date"] < d:
                if b["o"] <= st["price"]:
                    self.close_out(sym, st["qty"], b["o"], d, "broker_stop_gap")
                elif b["l"] <= st["price"]:
                    self.close_out(sym, st["qty"], st["price"], d, "broker_stop")
                if sym in self.pos:
                    self.pos[sym]["stop"] = None

    def snapshot(self, d, i, prev_value):
        E = self.E
        iwm_b = self.bars["IWM"][i]
        iwm_hist = [b["c"] for b in self.bars["IWM"][max(0, i - IWM_BARS):i]]
        positions = []
        held = list(self.pos)
        for sym, p in self.pos.items():
            b = self.bar(sym, d)
            if not b:
                continue
            p["high"] = max(p["high"], b["h"])
            p["last"] = b["c"]
            hb = self.hist(sym, d, LOOKBACK_BARS)
            days_held = self.idx["IWM"][d] - self.idx["IWM"][p["entry_date"]]
            positions.append({
                "symbol": sym, "qty": p["qty"], "avg_cost": p["avg_cost"], "price": b["c"],
                "sector": (self.fund.get(sym) or {}).get("sector"),
                "stop_order": ({"id": sym, "stop_price": p["stop"]["price"]}
                               if p["stop"] else None),
                "stop_floor": None, "high_since_entry": p["high"],
                "trading_days_held": days_held, "half_taken": p["half_taken"],
                "closes": [x["c"] for x in hb], "bars": hb})
        quotes = {}
        for sym in self.uni["groups"]:
            b = self.bar(sym, d)
            j = self.idx[sym].get(d)
            if b and j:
                quotes[sym] = {"price": b["c"], "prev_close": self.bars[sym][j - 1]["c"]}
        scr = E.screen({"tiers": self.uni["tiers"], "quotes": quotes, "held": held,
                        "banned_today": [], "bought_today": [], "rejected_today": {}})
        cands = {}
        for sym in scr["deep_check"]:
            b = self.bar(sym, d)
            f = self.fund.get(sym) or {}
            last_close = self.bars[sym][-1]["c"]
            mcap = (f.get("market_cap") or 0) * b["c"] / last_close if last_close else 0
            cands[sym] = {
                "bars": self.hist(sym, d, LOOKBACK_BARS),
                "today": {"open": b["o"], "low": b["l"], "high": b["h"], "volume": b["v"]},
                "fund": {"market_cap": mcap, "sector": f.get("sector"),
                         "industry": f.get("industry")},
                "fin": self.fin_rows(sym, d), "group": self.uni["groups"].get(sym),
                "sector": f.get("sector"), "industry": f.get("industry"),
                "earnings_within_2d": False}
        quotes_c = {s: {"price": self.bar(s, d)["c"], "ask": self.bar(s, d)["c"]}
                    for s in list(cands) + held}
        return {
            "now_et": d + "T15:30",
            "account": {"total_value": self.value(d), "buying_power": self.cash},
            "start_of_day_value": prev_value, "orders_today": 0, "halted": False,
            "positions": positions, "bought_today": [], "banned_today": [],
            "industry_buys_today": {}, "high_conviction_buys_today": 0,
            "iwm": {"price": iwm_b["c"], "prev_close": self.bars["IWM"][i - 1]["c"],
                    "open": iwm_b["o"], "closes": iwm_hist},
            "quotes": quotes_c, "candidates": cands}

    def execute(self, d, out):
        for a in out["actions"]:
            sym = a["symbol"]
            b = self.bar(sym, d)
            if a["action"] == "sell" and sym in self.pos:
                self.n_orders += 1
                if a.get("rule") == "take_profit":
                    self.pos[sym]["half_taken"] = True
                self.close_out(sym, a["qty"], b["c"], d, a.get("rule"))
                if sym in self.pos:
                    ts = a.get("then_stop")
                    if ts:
                        self.pos[sym]["stop"] = {"qty": ts["qty"], "price": ts["stop_price"]}
                    elif a.get("cancel_order_id"):
                        self.pos[sym]["stop"] = None
            elif a["action"] in ("place_stop", "replace_stop") and sym in self.pos:
                self.n_orders += 1
                self.pos[sym]["stop"] = {"qty": a["qty"], "price": a["stop_price"]}
            elif a["action"] == "buy" and sym not in self.pos:
                qty, frac = a["qty"], a.get("frac_qty") or 0
                fill = a["limit_price"]
                cost = qty * fill + frac * b["c"]
                if cost > self.cash + 1e-9:
                    continue
                self.n_orders += 2 + (1 if frac else 0)
                self.cash -= cost
                total = qty + frac
                stop_px = math.floor(fill * (1 - a["stop_after_fill_pct"]) * 100) / 100
                self.pos[sym] = {"qty": round(total, 6), "avg_cost": cost / total,
                                 "entry_date": d, "high": b["c"], "last": b["c"],
                                 "half_taken": False,
                                 "stop": {"qty": qty, "price": stop_px} if qty else None}

    def run(self):
        prev_value = self.value(self.dates[self.t0 - 1])
        for i in range(self.t0, len(self.dates)):
            d = self.dates[i]
            if self.halted:
                self.equity.append((d, self.cash))
                continue
            self.stops(d)
            snap = self.snapshot(d, i, prev_value)
            out = self.E.decide(snap)
            self.execute(d, out)
            if out.get("halt"):
                for sym in list(self.pos):
                    self.close_out(sym, self.pos[sym]["qty"], self.bar(sym, d)["c"], d, "halt")
                self.halted = d
            prev_value = self.value(d)
            self.equity.append((d, prev_value))
        return self.report()

    def report(self):
        vals = [v for _, v in self.equity]
        peak, mdd = vals[0], 0.0
        for v in vals:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        d0, d1 = self.equity[0][0], self.equity[-1][0]
        years = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
        iwm0 = self.bar("IWM", d0)["c"]
        iwm1 = self.bar("IWM", d1)["c"]
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        gross_w = sum(t["pnl"] for t in wins)
        gross_l = -sum(t["pnl"] for t in losses)
        rets = [vals[k] / vals[k - 1] - 1 for k in range(1, len(vals))]
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0
        by_rule = {}
        for t in self.trades:
            r = by_rule.setdefault(t["rule"] or "?", {"n": 0, "pnl": 0.0})
            r["n"] += 1
            r["pnl"] += t["pnl"]
        return {
            "engine": self.E.VERSION, "start": d0, "end": d1,
            "start_value": round(self.start_cash, 2), "end_value": round(vals[-1], 2),
            "total_return_pct": round((vals[-1] / self.start_cash - 1) * 100, 1),
            "cagr_pct": round(((vals[-1] / self.start_cash) ** (1 / years) - 1) * 100, 1),
            "max_drawdown_pct": round(mdd * 100, 1),
            "sharpe": round(mu / sd * math.sqrt(252), 2) if sd else None,
            "iwm_return_pct": round((iwm1 / iwm0 - 1) * 100, 1),
            "exits": len(self.trades), "win_rate_pct": round(len(wins) / len(self.trades) * 100, 1)
            if self.trades else None,
            "avg_win": round(gross_w / len(wins), 2) if wins else 0,
            "avg_loss": round(-gross_l / len(losses), 2) if losses else 0,
            "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
            "orders": self.n_orders, "halted_on": self.halted, "by_exit_rule": by_rule,
            "equity_curve": self.equity,
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine")
    ap.add_argument("data_dir")
    ap.add_argument("--start", default="2024-10-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--cash", type=float, default=1500.0)
    ap.add_argument("--json")
    ap.add_argument("--rule", action="append", default=[],
                    help="override a RULES value for this run, e.g. --rule halt_value=0")
    a = ap.parse_args()
    data = {k: load(a.data_dir, k + ".json") for k in ("bars", "universe", "fund", "fin")}
    if a.end:
        data["bars"] = {s: [b for b in bs if b["d"] <= a.end] for s, bs in data["bars"].items()}
    E = load_engine(a.engine)
    for kv in a.rule:
        k, v = kv.split("=", 1)
        if k not in E.RULES:
            raise SystemExit(f"unknown rule {k}")
        E.RULES[k] = type(E.RULES[k])(float(v)) if not isinstance(E.RULES[k], tuple) else v
    rep = Sim(E, data, a.cash, a.start).run()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(rep, f)
    rep.pop("equity_curve")
    print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
