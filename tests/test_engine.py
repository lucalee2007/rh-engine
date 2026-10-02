import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rh_engine as E  # noqa: E402


def bars_from(closes, vol=1_000_000, spread=0.01):
    out = []
    prev = closes[0]
    for c in closes:
        o = prev
        out.append({"o": o, "h": max(o, c) * (1 + spread), "l": min(o, c) * (1 - spread),
                    "c": c, "v": vol})
        prev = c
    return out


def iwm_closes(n=80):
    # gentle, low-vol series
    return [200 * (1 + 0.004 * math.sin(i / 2)) for i in range(n)]


def uptrend_closes(n=80, start=20.0, drift=0.004, wiggle=0.03):
    return [start * (1 + drift) ** i * (1 + wiggle * math.sin(i)) for i in range(n)]


GOOD_FIN = [  # most recent first: +40% YoY, 60% GM, losses narrowing
    {"revenue": 140, "gross_profit": 84, "net_income": -5},
    {"revenue": 130, "gross_profit": 77, "net_income": -8},
    {"revenue": 120, "gross_profit": 70, "net_income": -10},
    {"revenue": 110, "gross_profit": 64, "net_income": -12},
    {"revenue": 100, "gross_profit": 58, "net_income": -15},
    {"revenue": 95, "gross_profit": 55, "net_income": -16},
]


def base_snapshot(**kw):
    s = {
        "now_et": "2026-09-24T11:40",
        "account": {"total_value": 1500.0, "buying_power": 1240.0},
        "start_of_day_value": 1500.0,
        "orders_today": 0,
        "positions": [],
        "iwm": {"price": 200.0, "prev_close": 200.0, "open": 200.0, "closes": iwm_closes()},
        "quotes": {},
        "candidates": {},
    }
    s.update(kw)
    return s


def breakout_candidate():
    closes = uptrend_closes()
    bars = bars_from(closes, vol=1_000_000)
    hi20 = max(b["h"] for b in bars[-20:])
    price = round(hi20 * 1.01, 2)
    return price, {
        "bars": bars,
        "today": {"open": closes[-1], "low": closes[-1], "volume": 900_000},
        "fund": {"market_cap": 2e9, "sector": "Electronic Technology",
                 "industry": "Semiconductors"},
        "fin": GOOD_FIN, "group": "semis", "earnings_within_2d": False,
    }


class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(E.sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(E.sma([1], 2))

    def test_rsi_bounds(self):
        self.assertEqual(E.rsi(list(range(1, 30))), 100.0)
        r = E.rsi([10 + math.sin(i) for i in range(40)])
        self.assertTrue(0 < r < 100)

    def test_realized_vol_ratio(self):
        hi = E.realized_vol(uptrend_closes(wiggle=0.05))
        lo = E.realized_vol(iwm_closes())
        self.assertGreater(hi / lo, 1.5)


class TestSafety(unittest.TestCase):
    def test_halt_liquidates(self):
        s = base_snapshot(account={"total_value": 1130.0, "buying_power": 900.0},
                          positions=[{"symbol": "QUBT", "qty": 16, "avg_cost": 9.33,
                                      "price": 9.0, "stop_order": {"id": "x", "stop_price": 8.58}}])
        out = E.decide(s)
        self.assertTrue(out["halt"])
        self.assertEqual(out["actions"][0]["action"], "sell")
        self.assertEqual(out["actions"][0]["cancel_order_id"], "x")

    def test_bp_kill(self):
        out = E.decide(base_snapshot(account={"total_value": 1500.0, "buying_power": 200.0}))
        self.assertTrue(out["halt"])

    def test_daily_loss_blocks_buys(self):
        price, c = breakout_candidate()
        s = base_snapshot(account={"total_value": 1400.0, "buying_power": 1200.0},
                          start_of_day_value=1500.0, candidates={"ABC": c},
                          quotes={"ABC": {"price": price, "ask": price}})
        out = E.decide(s)
        self.assertFalse(out["buys_allowed"])
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))

    def test_waterfall_blocks_buys(self):
        price, c = breakout_candidate()
        s = base_snapshot(iwm={"price": 195.0, "prev_close": 200.0, "open": 198.0,
                               "closes": iwm_closes()},
                          candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        self.assertFalse(E.decide(s)["buys_allowed"])

    def test_buy_window(self):
        price, c = breakout_candidate()
        s = base_snapshot(now_et="2026-09-24T15:50", candidates={"ABC": c},
                          quotes={"ABC": {"price": price, "ask": price}})
        self.assertFalse(E.decide(s)["buys_allowed"])

    def test_order_budget(self):
        price, c = breakout_candidate()
        s = base_snapshot(orders_today=11, candidates={"ABC": c},
                          quotes={"ABC": {"price": price, "ask": price}})
        self.assertFalse(E.decide(s)["buys_allowed"])

    def test_halted_doc(self):
        out = E.decide(base_snapshot(halted=True))
        self.assertTrue(out["halt"])
        self.assertEqual(out["actions"], [])


class TestBuys(unittest.TestCase):
    def test_breakout_buy_respects_caps(self):
        price, c = breakout_candidate()
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        out = E.decide(s)
        buys = [a for a in out["actions"] if a["action"] == "buy"]
        self.assertEqual(len(buys), 1, out["skipped"])
        b = buys[0]
        self.assertLessEqual(b["notional"], 250.0)
        self.assertLessEqual(b["limit_price"], price * 1.005 + 1e-9)
        self.assertEqual(b["qty"], math.floor(b["notional"] / b["limit_price"] + 1e-9))

    def test_bp_floor_blocks(self):
        price, c = breakout_candidate()
        s = base_snapshot(account={"total_value": 1500.0, "buying_power": 400.0},
                          candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        out = E.decide(s)
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))

    def test_under_5_rejected(self):
        price, c = breakout_candidate()
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": 4.5, "ask": 4.5}})
        out = E.decide(s)
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))

    def test_big_cap_rejected(self):
        price, c = breakout_candidate()
        c["fund"]["market_cap"] = 15e9
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        self.assertFalse(any(a["action"] == "buy" for a in E.decide(s)["actions"]))

    def test_weak_fundamentals_and_low_vol_rejected(self):
        closes = [20 * 1.003 ** i * (1 + 0.004 * math.sin(i)) for i in range(80)]
        bars = bars_from(closes, spread=0.002)
        price = round(max(b["h"] for b in bars[-20:]) * 1.01, 2)
        c = {"bars": bars, "today": {"volume": 900_000},
             "fund": {"market_cap": 2e9}, "fin": [], "group": "other"}
        s = base_snapshot(candidates={"XYZ": c}, quotes={"XYZ": {"price": price, "ask": price}})
        out = E.decide(s)
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))
        self.assertIn("quality", out["skipped"][0]["reason"])

    def test_held_not_rebought(self):
        price, c = breakout_candidate()
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}},
                          positions=[{"symbol": "ABC", "qty": 5, "avg_cost": price,
                                      "price": price,
                                      "stop_order": {"id": "s", "stop_price": price * 0.9}}])
        self.assertFalse(any(a["action"] == "buy" for a in E.decide(s)["actions"]))


class TestPositions(unittest.TestCase):
    def test_missing_stop_placed_and_never_lowered(self):
        p = {"symbol": "QUBT", "qty": 16, "avg_cost": 9.33, "price": 9.30, "stop_order": None,
             "stop_floor": 8.58}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["action"], "place_stop")
        self.assertEqual(acts[0]["stop_price"], 8.58)   # floor beats 10% ($8.40)

    def test_fractional_stop_sells(self):
        p = {"symbol": "INOD", "qty": 1.544845, "avg_cost": 64.73, "price": 65.50,
             "stop_floor": 65.94, "high_since_entry": 74.93}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["action"], "sell")
        self.assertEqual(acts[0]["rule"], "stop")

    def test_trailing_raises_stop(self):
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 10.0, "price": 12.5,
             "high_since_entry": 13.0, "stop_order": {"id": "s1", "stop_price": 9.0}}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["action"], "replace_stop")
        self.assertAlmostEqual(acts[0]["stop_price"], 11.05)

    def test_take_profit_half(self):
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 10.0, "price": 15.2,
             "high_since_entry": 15.3, "stop_order": {"id": "s1", "stop_price": 11.3}}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["rule"], "take_profit")
        self.assertEqual(acts[0]["qty"], 5)
        self.assertEqual(acts[0]["then_stop"]["qty"], 5)

    def test_time_stop(self):
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 10.0, "price": 10.3,
             "trading_days_held": 26, "stop_order": {"id": "s1", "stop_price": 9.0}}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["rule"], "time_stop")

    def test_trend_break(self):
        closes = [20.0] * 49 + [19.0, 18.0, 17.0]
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 18.0, "price": 17.5, "closes": closes,
             "stop_order": {"id": "s1", "stop_price": 16.2}}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertEqual(acts[0]["rule"], "trend_break")

    def test_pdt_blocks_same_day_non_stop_exit(self):
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 10.0, "price": 10.3,
             "trading_days_held": 26, "stop_order": {"id": "s1", "stop_price": 9.0}}
        acts = E.manage_position(p, "2026-09-24", {"ABC"})
        self.assertFalse(any(a["action"] == "sell" for a in acts))


class TestAggressiveRules(unittest.TestCase):
    def test_no_take_profit_at_32pct(self):
        p = {"symbol": "ABC", "qty": 10, "avg_cost": 10.0, "price": 13.2,
             "high_since_entry": 13.3, "stop_order": {"id": "s1", "stop_price": 11.3}}
        acts = E.manage_position(p, "2026-09-24", set())
        self.assertFalse(any(a.get("rule") == "take_profit" for a in acts))

    def test_bp_450_no_longer_halts(self):
        out = E.decide(base_snapshot(account={"total_value": 1500.0, "buying_power": 450.0}))
        self.assertFalse(out["halt"])

    def test_orders_7_still_allows_buys(self):
        price, c = breakout_candidate()
        s = base_snapshot(orders_today=7, candidates={"ABC": c},
                          quotes={"ABC": {"price": price, "ask": price}})
        self.assertTrue(E.decide(s)["buys_allowed"])


class TestScreen(unittest.TestCase):
    def test_screen_orders_and_filters(self):
        data = {"tiers": [["A", "B", "C"], ["D", "E"]],
                "quotes": {"A": {"price": 10.3, "prev_close": 10.0},   # +3% breakout
                           "B": {"price": 9.8, "prev_close": 10.0},    # -2% pullback
                           "C": {"price": 4.0, "prev_close": 4.1},     # under $5
                           "D": {"price": 300, "prev_close": 290},     # over $250
                           "E": {"price": 11.0, "prev_close": 10.0}},  # +10% tier 2
                "held": [], "banned_today": []}
        out = E.screen(data)
        self.assertEqual(out["deep_check"], ["A", "E", "B"])


class TestHybridSizing(unittest.TestCase):
    def test_round_up_when_it_fits(self):
        # KLIC-like: $150 target at $90.75 -> 2 whole shares ($181.50 <= $250)
        q, f, _ = E.size_order(150, 90.75, 250, 10)
        self.assertEqual((q, f), (2, 0.0))

    def test_fraction_when_round_up_breaks_cap(self):
        # $250 target at $200: 2 shares = $400 > cap -> 1 whole + 0.25 fractional
        q, f, _ = E.size_order(250, 200.0, 250, 10)
        self.assertEqual(q, 1)
        self.assertAlmostEqual(f, 0.25)

    def test_no_fraction_without_order_budget(self):
        q, f, _ = E.size_order(250, 200.0, 250, 2)
        self.assertEqual((q, f), (1, 0.0))

    def test_tiny_fraction_skipped(self):
        # $150 at $148: round-up ($296) breaks cap, fraction worth $2 < $5 minimum
        q, f, _ = E.size_order(150, 148.0, 250, 10)
        self.assertEqual((q, f), (1, 0.0))

    def test_never_fraction_only(self):
        # $150 target at $260: no whole share fits the $250 cap -> no buy at all
        q, f, _ = E.size_order(150, 260.0, 250, 10)
        self.assertEqual((q, f), (0, 0.0))

    def test_round_up_respects_name_cap(self):
        # room limited to $170 -> stays at 1 whole + fraction
        q, f, _ = E.size_order(150, 90.0, 170, 10)
        self.assertEqual(q, 1)
        self.assertLessEqual((q + f) * 90.0, 170)

    def test_buy_action_total_within_caps(self):
        price, c = breakout_candidate()
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        b = [a for a in E.decide(s)["actions"] if a["action"] == "buy"][0]
        self.assertGreaterEqual(b["qty"], 1)
        self.assertLessEqual(b["notional"], 250.0 + 1e-9)

    def test_mixed_position_stop_covers_whole_only(self):
        p = {"symbol": "KLIC", "qty": 1.65, "avg_cost": 90.75, "price": 92.0, "stop_order": None}
        acts = E.manage_position(p, "2026-10-02", set())
        self.assertEqual(acts[0]["action"], "place_stop")
        self.assertEqual(acts[0]["qty"], 1)

    def test_mixed_position_stop_hit_sells_fraction(self):
        p = {"symbol": "KLIC", "qty": 1.65, "avg_cost": 90.75, "price": 81.0,
             "stop_order": {"id": "s1", "stop_price": 81.67}}
        acts = E.manage_position(p, "2026-10-02", set())
        self.assertEqual(acts[0]["action"], "sell")
        self.assertAlmostEqual(acts[0]["qty"], 0.65)
        self.assertIsNone(acts[0]["cancel_order_id"])

    def test_mixed_take_profit_sells_fraction_keeps_stopped_whole(self):
        p = {"symbol": "ABC", "qty": 1.4, "avg_cost": 10.0, "price": 15.2, "high_since_entry": 15.3,
             "stop_order": {"id": "s1", "stop_price": 11.3}}
        acts = E.manage_position(p, "2026-10-02", set())
        self.assertEqual(acts[0]["rule"], "take_profit")
        self.assertAlmostEqual(acts[0]["qty"], 0.4)
        self.assertIsNone(acts[0]["cancel_order_id"])
        self.assertIsNone(acts[0]["then_stop"])

    def test_mixed_take_profit_three_and_a_bit(self):
        p = {"symbol": "ABC", "qty": 3.4, "avg_cost": 10.0, "price": 15.2, "high_since_entry": 15.3,
             "stop_order": {"id": "s1", "stop_price": 11.3}}
        a = E.manage_position(p, "2026-10-02", set())[0]
        self.assertAlmostEqual(a["qty"], 1.4)
        self.assertEqual(a["then_stop"]["qty"], 2)


class TestQuantV13(unittest.TestCase):
    def test_free_points_no_longer_pass_quality(self):
        # driver group + high beta + low debt, financials show no business criterion
        q = E.quality([], 2.5, "semis", {"debt_to_equity": 0.5})
        self.assertGreaterEqual(q["count"], 2)
        self.assertEqual(q["fundamental"], 0)
        price, c = breakout_candidate()
        c["fin"] = [{"revenue": 100, "gross_profit": 10, "net_income": -20}] * 6
        c["fund"]["debt_to_equity"] = 0.5
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        out = E.decide(s)
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))
        self.assertIn("business criterion", out["skipped"][0]["reason"])

    def test_no_financials_not_banned(self):
        # names Robinhood has no financials for still trade on the other criteria
        price, c = breakout_candidate()
        c["fin"] = []
        c["fund"]["debt_to_equity"] = 0.5
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        self.assertTrue(any(a["action"] == "buy" for a in E.decide(s)["actions"]))

    def test_missing_gross_profit_not_counted(self):
        fin = [dict(r, gross_profit=None) for r in GOOD_FIN]
        q = E.quality(fin, None, "other", {})
        self.assertFalse(any("gross margin" in m for m in q["met"]))

    def test_volume_curve_morning_not_inflated(self):
        # a normal day: 9% of average volume done by 9:45 -> pace about 1.0, not 2.3
        self.assertAlmostEqual(E.volume_fraction(15), 0.09)
        self.assertAlmostEqual(0.09 / E.volume_fraction(15), 1.0)
        self.assertEqual(E.volume_fraction(390), 1.0)
        self.assertLess(E.volume_fraction(200), E.volume_fraction(220))

    def test_atr_stop_clamped(self):
        calm = [{"o": 10, "h": 10.05, "l": 9.95, "c": 10} for _ in range(30)]
        wild = [{"o": 10, "h": 11.0, "l": 9.0, "c": 10} for _ in range(30)]
        self.assertEqual(E.atr_stop_pct(calm, 10), 0.07)
        self.assertEqual(E.atr_stop_pct(wild, 10), 0.15)
        mid = [{"o": 10, "h": 10.2, "l": 9.8, "c": 10} for _ in range(30)]
        self.assertAlmostEqual(E.atr_stop_pct(mid, 10), 0.10)

    def test_buy_carries_atr_stop(self):
        price, c = breakout_candidate()
        s = base_snapshot(candidates={"ABC": c}, quotes={"ABC": {"price": price, "ask": price}})
        b = [a for a in E.decide(s)["actions"] if a["action"] == "buy"][0]
        self.assertTrue(0.07 <= b["stop_after_fill_pct"] <= 0.15)

    def test_position_atr_stop_not_tightened_without_bars(self):
        # a 14% ATR stop on the broker must not be pulled up to a flat 10% when bars are missing
        p = {"symbol": "ABC", "qty": 2, "avg_cost": 100.0, "price": 101.0,
             "stop_order": {"id": "s1", "stop_price": 86.0}}
        acts = E.manage_position(p, "2026-10-02", set())
        self.assertEqual(acts, [])

    def test_relative_strength_filter(self):
        price, c = breakout_candidate()
        iwm = base_snapshot()["iwm"]
        iwm["closes"] = [100 * 1.01 ** i for i in range(80)]   # IWM ripping far ahead
        iwm["price"] = iwm["closes"][-1]
        iwm["prev_close"] = iwm["closes"][-1]
        iwm["open"] = iwm["closes"][-1]
        s = base_snapshot(iwm=iwm, candidates={"ABC": c},
                          quotes={"ABC": {"price": price, "ask": price}})
        out = E.decide(s)
        self.assertFalse(any(a["action"] == "buy" for a in out["actions"]))
        self.assertIn("lagging IWM", out["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
