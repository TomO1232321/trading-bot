import csv
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

from bot import Bot
from strategy import Bar
from tests.helpers import (LAST_CLOSE_DAY, TODAY, FakeBroker, all_up_bars, bars_from_closes, cfg,
                           downtrend)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def csv_rows(path):
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = os.path.join(self.tmp.name, "journal")
        self.summary = os.path.join(self.tmp.name, "summary.md")

    def tearDown(self):
        self.tmp.cleanup()

    def run_bot(self, broker, dry_run=False, **kw):
        bot = Bot(broker, kw.pop("config", cfg()), journal_dir=self.journal, dry_run=dry_run,
                  sleep=lambda s: None, summary_path=self.summary, **kw)
        out = StringIO()
        with redirect_stdout(out), redirect_stderr(StringIO()):
            code = bot.run()
        self.stdout = out.getvalue()
        return bot, code


class TestBuying(BotTestCase):
    def test_first_run_buys_all_and_sets_stops(self):
        b = FakeBroker(all_up_bars())
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0, bot.errors)
        self.assertEqual(set(b.positions), set(cfg().watchlist))
        for sym, qty in b.positions.items():
            stops = b.open_stops(sym)
            self.assertEqual(len(stops), 1)
            self.assertEqual(stops[0]["qty"], f"{int(qty)}")
            self.assertEqual(stops[0]["trail_percent"], "10")
            self.assertEqual(stops[0]["time_in_force"], "gtc")
        equity = float(b.get_account()["equity"])
        self.assertGreaterEqual(b.cash, 0.05 * equity)
        self.assertTrue(all(o["client_order_id"].startswith("tb-") for o in b.orders))

    def test_journal_files(self):
        b = FakeBroker(all_up_bars())
        self.run_bot(b)
        md = read(os.path.join(self.journal, "2024-06-03.md"))
        self.assertIn("| SPY |", md)
        self.assertIn("HOLD", md)
        self.assertIn("Seit 2024-06-03: Bot", md)
        self.assertIn("vs. Buy-and-Hold SPY", md)
        start = json.loads(read(os.path.join(self.journal, "start.json")))
        self.assertEqual(start["date"], "2024-06-03")
        self.assertAlmostEqual(start["equity"], 100_000)
        rows = csv_rows(os.path.join(self.journal, "history.csv"))
        self.assertEqual(len(rows), 1)
        self.assertIn("Trading-Bot 2024-06-03", read(self.summary))

    def test_second_run_same_day_does_not_trade_again(self):
        b = FakeBroker(all_up_bars())
        self.run_bot(b)
        n_orders = len(b.orders)
        positions = dict(b.positions)
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0, bot.errors)
        self.assertEqual(len(b.orders), n_orders)
        self.assertEqual(b.positions, positions)
        rows = csv_rows(os.path.join(self.journal, "history.csv"))
        self.assertEqual(len(rows), 1)  # eine Zeile pro Tag

    def test_second_run_with_unfilled_buy_does_not_buy_again(self):
        b = FakeBroker(all_up_bars(), fill_market=False)
        self.run_bot(b)
        buys = [o for o in b.orders if o["side"] == "buy"]
        self.assertEqual(len(buys), 6)
        self.run_bot(b)
        self.assertEqual(len([o for o in b.orders if o["side"] == "buy"]), 6)

    def test_cooldown_after_trailing_stop(self):
        b = FakeBroker(all_up_bars())
        b.add_order(symbol="GLD", side="sell", type="trailing_stop", qty="10", status="filled",
                    client_order_id="tb-20240529-stop-GLD-abc", filled_at="2024-05-29T18:00:00Z",
                    trail_percent="10", time_in_force="gtc")
        bot, code = self.run_bot(b)
        self.assertNotIn("GLD", b.positions)
        self.assertIn("SPY", b.positions)
        self.assertEqual(bot.cooldown["GLD"].isoformat(), "2024-06-08")
        md = read(os.path.join(self.journal, "2024-06-03.md"))
        self.assertIn("bis 2024-06-08", md)

    def test_cooldown_expired(self):
        b = FakeBroker(all_up_bars())
        b.add_order(symbol="GLD", side="sell", type="trailing_stop", qty="10", status="filled",
                    client_order_id="tb-20240501-stop-GLD-abc", filled_at="2024-05-01T18:00:00Z")
        self.run_bot(b)
        self.assertIn("GLD", b.positions)

    def test_todays_partial_bar_is_ignored(self):
        bars = all_up_bars()
        bars["SPY"].append(Bar(TODAY, 500, 500, 1, 1))  # heutige Kerze: Absturz (noch nicht fertig)
        b = FakeBroker(bars)
        bot, _ = self.run_bot(b, dry_run=True)
        self.assertEqual(bot.signals["SPY"].state, "HOLD")
        self.assertEqual(bot.signals["SPY"].as_of, LAST_CLOSE_DAY)


class TestSelling(BotTestCase):
    def test_sell_cancels_stop_first(self):
        bars = all_up_bars()
        bars["SPY"] = bars_from_closes(downtrend(), LAST_CLOSE_DAY)
        b = FakeBroker(bars, cash=50_000, positions={"SPY": 50})
        stop = b.add_order(symbol="SPY", side="sell", type="trailing_stop", qty="50", trail_percent="10",
                           time_in_force="gtc", client_order_id="tb-20240520-stop-SPY-x")
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0, bot.errors)
        self.assertNotIn("SPY", b.positions)
        i_cancel = b.calls.index(("cancel_order", stop["id"]))
        i_sell = next(i for i, c in enumerate(b.calls) if c[:4] == ("submit_order", "SPY", "sell", "market"))
        self.assertLess(i_cancel, i_sell)
        self.assertEqual(b.open_stops("SPY"), [])  # kein neuer Stop auf verkaufte Aktien

    def test_without_cancel_the_fake_would_block(self):
        # Gegenprobe: Der Fake-Broker blockiert Verkaeufe, solange ein Stop offen ist.
        b = FakeBroker(all_up_bars(), positions={"SPY": 5})
        b.add_order(symbol="SPY", side="sell", type="trailing_stop", qty="5", client_order_id="tb-x")
        with self.assertRaises(Exception):
            b.submit_order({"symbol": "SPY", "qty": "5", "side": "sell", "type": "market"})

    def test_wrong_stop_size_is_replaced_correct_one_kept(self):
        b = FakeBroker(all_up_bars(), cash=1_000, positions={s: 10 for s in cfg().watchlist})
        b.positions["QQQ"] = 30
        wrong = b.add_order(symbol="QQQ", side="sell", type="trailing_stop", qty="20", trail_percent="10",
                            time_in_force="gtc", client_order_id="tb-20240520-stop-QQQ-a")
        right = b.add_order(symbol="SPY", side="sell", type="trailing_stop", qty="10", trail_percent="10",
                            time_in_force="gtc", client_order_id="tb-20240520-stop-SPY-a")
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0, bot.errors)
        self.assertEqual(b._find(wrong["id"])["status"], "canceled")
        self.assertEqual(b._find(right["id"])["status"], "new")
        q = b.open_stops("QQQ")
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["qty"], "30")
        self.assertEqual(len(b.open_stops()), 6)

    def test_duplicate_stops_reduced_to_one(self):
        b = FakeBroker(all_up_bars(), cash=0, positions={"SPY": 10})
        for _ in range(2):
            b.add_order(symbol="SPY", side="sell", type="trailing_stop", qty="10", trail_percent="10",
                        time_in_force="gtc", client_order_id=f"tb-dup-{_}")
        self.run_bot(b)
        self.assertEqual(len(b.open_stops("SPY")), 1)

    def test_foreign_positions_and_orders_untouched(self):
        b = FakeBroker(all_up_bars() | {"AAPL": bars_from_closes(downtrend(), LAST_CLOSE_DAY)},
                       positions={"AAPL": 7})
        foreign = b.add_order(symbol="AAPL", side="sell", type="trailing_stop", qty="7",
                              client_order_id="my-own-order")
        foreign_spy = b.add_order(symbol="SPY", side="sell", type="limit", qty="1", client_order_id="manual-1")
        self.run_bot(b)
        self.assertEqual(b.positions["AAPL"], 7)
        self.assertEqual(b._find(foreign["id"])["status"], "new")
        self.assertEqual(b._find(foreign_spy["id"])["status"], "new")
        self.assertFalse([c for c in b.submitted() if c[1] == "AAPL"])


class TestSafety(BotTestCase):
    def test_market_closed_does_nothing(self):
        b = FakeBroker(all_up_bars(), is_open=False)
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0)
        self.assertEqual(b.submitted(), [])
        self.assertFalse([c for c in b.calls if c[0] == "cancel_order"])
        md = read(os.path.join(self.journal, "2024-06-03.md"))
        self.assertIn("Markt geschlossen", md)
        self.assertFalse(os.path.exists(os.path.join(self.journal, "start.json")))

    def test_dry_run_writes_nothing_and_places_no_orders(self):
        bars = all_up_bars()
        bars["TLT"] = bars_from_closes(downtrend(), LAST_CLOSE_DAY)
        b = FakeBroker(bars, positions={"TLT": 10}, is_open=False)
        b.add_order(symbol="TLT", side="sell", type="trailing_stop", qty="3", client_order_id="tb-x")
        bot, code = self.run_bot(b, dry_run=True)
        self.assertEqual(code, 0, bot.errors)
        self.assertEqual(b.submitted(), [])
        self.assertFalse([c for c in b.calls if c[0] == "cancel_order"])
        self.assertFalse(os.path.exists(self.journal))
        self.assertFalse(os.path.exists(self.summary))
        self.assertIn("(geplant) VERKAUFE 10 TLT", self.stdout)
        self.assertIn("(geplant) KAUFE", self.stdout)
        self.assertIn("Markt geschlossen", self.stdout)

    def test_blocked_account(self):
        b = FakeBroker(all_up_bars(), account_extra={"trading_blocked": True})
        bot, code = self.run_bot(b)
        self.assertEqual(code, 1)
        self.assertEqual(b.submitted(), [])
        self.assertIn("gesperrt", read(os.path.join(self.journal, "2024-06-03.md")))

    def test_not_enough_history_does_nothing(self):
        bars = {s: bars_from_closes([100.0] * 50, LAST_CLOSE_DAY) for s in cfg().watchlist}
        b = FakeBroker(bars)
        bot, code = self.run_bot(b)
        self.assertEqual(code, 0)
        self.assertEqual(b.submitted(), [])
        self.assertIn("zu wenig", read(os.path.join(self.journal, "2024-06-03.md")))


if __name__ == "__main__":
    unittest.main()
