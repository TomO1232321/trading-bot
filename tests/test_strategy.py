import json
import math
import os
import tempfile
import unittest
from datetime import date

from strategy import (HOLD, NODATA, OUT, Bar, ConfigError, completed_bars, config_from_dict,
                      load_config, plan, signal, sma)
from tests.helpers import CFG_DICT, bars_from_closes, cfg, downtrend, uptrend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestSmaAndSignal(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertEqual(sma([1, 2, 3, 4], 4), 2.5)
        self.assertIsNone(sma([1, 2, 3], 4))

    def test_hold_in_uptrend(self):
        s = signal("SPY", bars_from_closes(uptrend(), date(2024, 5, 31)), cfg())
        self.assertEqual(s.state, HOLD)
        self.assertGreater(s.close, s.sma_slow)
        self.assertGreater(s.sma_fast, s.sma_slow)

    def test_out_in_downtrend(self):
        self.assertEqual(signal("SPY", bars_from_closes(downtrend(), date(2024, 5, 31)), cfg()).state, OUT)

    def test_out_when_price_below_slow_even_if_fast_above(self):
        closes = uptrend(250) + [50.0]  # letzter Schluss stuerzt unter SMA200
        s = signal("SPY", bars_from_closes(closes, date(2024, 5, 31)), cfg())
        self.assertGreater(s.sma_fast, s.sma_slow)
        self.assertEqual(s.state, OUT)

    def test_nodata_with_short_history(self):
        s = signal("SPY", bars_from_closes(uptrend(199), date(2024, 5, 31)), cfg())
        self.assertEqual(s.state, NODATA)
        self.assertIsNone(s.sma_slow)

    def test_today_partial_bar_ignored(self):
        bars = bars_from_closes(uptrend(260), date(2024, 5, 31))
        today = date(2024, 6, 3)
        bars.append(Bar(today, 1, 1, 1, 1))  # laufende Kerze mit Absturz
        done = completed_bars(bars, today)
        self.assertEqual(done[-1].date, date(2024, 5, 31))
        self.assertEqual(signal("SPY", done, cfg()).state, HOLD)


class TestConfig(unittest.TestCase):
    def test_repo_config_is_valid(self):
        c = load_config(os.path.join(ROOT, "config.json"))
        self.assertEqual(c.watchlist, ("SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"))
        self.assertEqual(c.benchmark, "SPY")

    def bad(self, **kw):
        d = dict(CFG_DICT)
        d.update(kw)
        with self.assertRaises(ConfigError):
            config_from_dict(d)

    def test_fast_must_be_smaller_than_slow(self):
        self.bad(sma_fast=200, sma_slow=200)
        self.bad(sma_fast=210, sma_slow=200)

    def test_duplicates(self):
        self.bad(watchlist=["SPY", "QQQ", "spy"])

    def test_other_invalid_values(self):
        self.bad(watchlist=[])
        self.bad(max_position_pct=0)
        self.bad(max_position_pct=150)
        self.bad(cash_reserve_pct=100)
        self.bad(cash_reserve_pct=-1)
        self.bad(trailing_stop_pct=0)
        self.bad(cooldown_days=-1)
        self.bad(sma_fast=2.5)
        self.bad(sma_fast=True)

    def test_missing_and_unknown_keys(self):
        d = dict(CFG_DICT)
        del d["cooldown_days"]
        with self.assertRaises(ConfigError):
            config_from_dict(d)
        d = dict(CFG_DICT, foo=1)
        with self.assertRaises(ConfigError):
            config_from_dict(d)

    def test_broken_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "c.json")
            with open(p, "w") as f:
                f.write("{nope")
            with self.assertRaises(ConfigError):
                load_config(p)
            with self.assertRaises(ConfigError):
                load_config(os.path.join(tmp, "missing.json"))


def sigs(states, price=100.0):
    from strategy import Signal
    return {s: Signal(s, st, price, price, price) for s, st in states.items()}


class TestPlan(unittest.TestCase):
    def test_slot_size(self):
        self.assertAlmostEqual(cfg().slot_pct, 95 / 6)
        self.assertEqual(cfg(watchlist=["SPY", "QQQ", "TLT"]).slot_pct, 20)

    def test_sizes_whole_shares_and_keeps_cash_reserve(self):
        c = cfg()
        prices = {s: 100.0 + i * 37.3 for i, s in enumerate(c.watchlist)}
        p = plan(c, 100_000, 100_000, {}, prices, sigs({s: HOLD for s in c.watchlist}))
        self.assertEqual(len(p.buys), 6)
        spent = 0
        for t in p.buys:
            self.assertIsInstance(t.qty, int)
            self.assertEqual(t.qty, math.floor(100_000 * c.slot_pct / 100 / (prices[t.symbol] * 1.01)))
            self.assertLessEqual(t.qty * t.price, 100_000 * c.max_position_pct / 100)
            spent += t.qty * t.price * 1.01
        self.assertGreaterEqual(100_000 - spent, 5_000)

    def test_max_position_pct_caps_slot(self):
        c = cfg(watchlist=["SPY", "QQQ"])
        p = plan(c, 100_000, 100_000, {}, {"SPY": 100, "QQQ": 100}, sigs({"SPY": HOLD, "QQQ": HOLD}))
        self.assertEqual([t.qty for t in p.buys], [198, 198])  # 20 % / (100 * 1.01)

    def test_little_cash_limits_buys(self):
        c = cfg()
        # Konto 100k, aber nur 10k Cash: Reserve 5k -> nur 5k verfuegbar
        p = plan(c, 100_000, 10_000, {}, {s: 100 for s in c.watchlist}, sigs({s: HOLD for s in c.watchlist}))
        total = sum(t.qty * 100 * 1.01 for t in p.buys)
        self.assertLessEqual(total, 5_000)
        self.assertEqual(p.buys[0].qty, 49)

    def test_sell_out_and_keep_hold(self):
        c = cfg()
        states = {s: HOLD for s in c.watchlist}
        states["TLT"] = OUT
        p = plan(c, 100_000, 0, {"SPY": 10, "TLT": 20}, {s: 100 for s in c.watchlist}, sigs(states))
        self.assertEqual([(t.symbol, t.qty) for t in p.sells], [("TLT", 20)])
        self.assertNotIn("SPY", [t.symbol for t in p.buys])  # schon gehalten
        # Verkaufserloes 2.000 $ liegt unter der Cash-Reserve von 5.000 $ -> keine Kaeufe
        self.assertFalse(p.buys)

    def test_sell_proceeds_fund_buys(self):
        c = cfg()
        states = {s: OUT for s in c.watchlist}
        states["QQQ"] = HOLD
        p = plan(c, 10_000, 0, {"TLT": 100}, {s: 100 for s in c.watchlist}, sigs(states))
        self.assertEqual(p.sells[0].symbol, "TLT")
        self.assertEqual(p.buys[0].symbol, "QQQ")
        self.assertEqual(p.buys[0].qty, 15)  # 10k * 15.83 % / 101

    def test_cooldown_blocks_buy(self):
        c = cfg()
        p = plan(c, 100_000, 100_000, {}, {s: 100 for s in c.watchlist},
                 sigs({s: HOLD for s in c.watchlist}), cooldown={"GLD"})
        self.assertNotIn("GLD", [t.symbol for t in p.buys])
        self.assertTrue(any("GLD" in n and "Cooldown" in n for n in p.notes))

    def test_nodata_does_nothing(self):
        c = cfg()
        p = plan(c, 100_000, 100_000, {"SPY": 5}, {s: 100 for s in c.watchlist},
                 sigs({s: NODATA for s in c.watchlist}))
        self.assertFalse(p.buys or p.sells)
        self.assertEqual(len(p.notes), 6)


if __name__ == "__main__":
    unittest.main()
