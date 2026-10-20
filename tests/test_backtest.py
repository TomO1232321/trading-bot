import os
import random
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO

import backtest
from backtest import metrics, run_backtest
from strategy import Bar
from tests.helpers import business_days, cfg

START = date(2019, 1, 2)


def random_walk(seed, n=700, end=date(2020, 9, 30)):
    rnd = random.Random(seed)
    price, bars = 100.0, []
    for d in business_days(end, n):
        o = price * (1 + rnd.gauss(0, 0.004))
        c = o * (1 + rnd.gauss(0.0006, 0.01))
        bars.append(Bar(d, o, max(o, c) * 1.003, min(o, c) * 0.997, c))
        price = c
    return bars


def dataset():
    return {s: random_walk(i) for i, s in enumerate(cfg().watchlist)}


class TestNoLookahead(unittest.TestCase):
    def test_future_prices_do_not_change_the_past(self):
        c = cfg()
        data = dataset()
        cut = date(2020, 3, 2)
        full = run_backtest(c, data, START)
        # Alles nach dem Stichtag durch wilde Kurse ersetzen
        changed = {}
        for s, bars in data.items():
            changed[s] = [b if b.date <= cut else Bar(b.date, b.open * 3, b.high * 3, b.low * 0.2, b.close * 0.5)
                          for b in bars]
        other = run_backtest(c, changed, START)
        n = full.dates.index(cut) + 1
        self.assertEqual(full.dates[:n], other.dates[:n])
        self.assertEqual(full.bot[:n], other.bot[:n])
        self.assertEqual(full.bench[:n], other.bench[:n])
        self.assertNotEqual(full.bot[-1], other.bot[-1])

    def test_truncated_data_gives_same_history(self):
        c = cfg()
        data = dataset()
        cut = date(2020, 1, 15)
        full = run_backtest(c, data, START)
        short = run_backtest(c, {s: [b for b in bars if b.date <= cut] for s, bars in data.items()}, START)
        self.assertEqual(full.bot[:len(short.bot)], short.bot)

    def test_execution_at_next_open(self):
        c = cfg(watchlist=["SPY"], max_position_pct=50)
        days = business_days(date(2020, 6, 30), 260)
        bars = [Bar(d, 100 + i, 100 + i, 100 + i, 100 + i) for i, d in enumerate(days)]
        # Eroeffnung jeden Tag 0.5 ueber dem Vortagesschluss
        bars = [Bar(b.date, b.close - 0.5, b.close, b.close - 0.5, b.close) for b in bars]
        res = run_backtest(c, {"SPY": bars}, days[205])
        first_buy_day = next(i for i, v in enumerate(res.invested) if v > 0)
        # Entscheidung am ersten Tag (Schluss), Ausfuehrung frühestens am zweiten
        self.assertGreaterEqual(first_buy_day, 1)


class TestStops(unittest.TestCase):
    def crash_data(self, gap):
        days = business_days(date(2020, 6, 30), 300)
        bars = []
        for i, d in enumerate(days):
            p = 100 + i * 0.3
            bars.append(Bar(d, p, p + 0.2, p - 0.2, p))
        k = 260
        crash = bars[k - 1].close * 0.6
        if gap:  # Kurslücke: Eroeffnung schon weit unter dem Stop
            bars[k] = Bar(days[k], crash, crash, crash * 0.99, crash)
        else:    # nur das Tagestief bricht ein
            p = bars[k - 1].close
            bars[k] = Bar(days[k], p, p, p * 0.8, p * 0.95)
        for j in range(k + 1, len(days)):
            c = bars[k].close
            bars[j] = Bar(days[j], c, c * 1.001, c * 0.999, c)
        return bars, days, k

    def test_stop_triggers_in_crash_at_open_on_gap(self):
        bars, days, k = self.crash_data(gap=True)
        c = cfg(watchlist=["SPY"])
        res = run_backtest(c, {"SPY": bars}, days[220])
        self.assertEqual(res.stops, 1)
        t = res.trades[0]
        self.assertEqual(t["reason"], "Trailing Stop")
        self.assertEqual(t["exit_date"], days[k])
        self.assertAlmostEqual(t["exit"], bars[k].open * (1 - 0.0005))
        # Bot verliert deutlich weniger als Buy-and-Hold
        self.assertGreater(metrics(res.dates, res.bot)["mdd"], metrics(res.dates, res.bench)["mdd"])

    def test_stop_triggers_on_intraday_low(self):
        bars, days, k = self.crash_data(gap=False)
        res = run_backtest(cfg(watchlist=["SPY"]), {"SPY": bars}, days[220])
        self.assertEqual(res.stops, 1)
        t = res.trades[0]
        self.assertLess(t["exit"], bars[k].open)
        self.assertGreater(t["exit"], bars[k].low)
        # Cooldown: in den naechsten 10 Tagen kein Neukauf
        later = [i for i, d in enumerate(res.dates) if d > days[k]]
        within = [i for i in later if (res.dates[i] - days[k]).days <= 10]
        self.assertTrue(all(res.invested[i] == 0 for i in within))


class TestReportAndCli(unittest.TestCase):
    def test_report_and_csv_cli(self):
        data = dataset()
        with tempfile.TemporaryDirectory() as tmp:
            for s, bars in data.items():
                with open(os.path.join(tmp, f"{s}.csv"), "w") as f:
                    f.write("date,open,high,low,close\n")
                    for b in bars:
                        f.write(f"{b.date},{b.open},{b.high},{b.low},{b.close}\n")
            out = StringIO()
            env_backup = os.environ.pop("GITHUB_STEP_SUMMARY", None)
            try:
                with redirect_stdout(out), redirect_stderr(StringIO()):
                    code = backtest.main(["--csv-dir", tmp, "--start", "2019-01-02",
                                          "--plot", os.path.join(tmp, "b.png"),
                                          "--config", os.path.join(os.path.dirname(backtest.__file__), "config.json")])
            finally:
                if env_backup is not None:
                    os.environ["GITHUB_STEP_SUMMARY"] = env_backup
        self.assertEqual(code, 0)
        text = out.getvalue()
        for word in ("Gesamtrendite", "CAGR", "Sharpe", "Drawdown", "Stop-Ausloesungen", "Ehrliches Fazit"):
            self.assertIn(word, text)

    def test_metrics(self):
        d = [date(2020, 1, 1), date(2021, 1, 1)]
        m = metrics(d, [100, 110])
        self.assertAlmostEqual(m["total"], 0.10)
        self.assertAlmostEqual(m["cagr"], 0.10, places=2)
        m = metrics([date(2020, 1, i) for i in range(1, 5)], [100, 120, 60, 90])
        self.assertAlmostEqual(m["mdd"], -0.5)


if __name__ == "__main__":
    unittest.main()
