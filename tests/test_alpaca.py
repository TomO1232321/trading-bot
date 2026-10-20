import os
import tempfile
import unittest
from datetime import date, datetime, timezone

import alpaca
from alpaca import (PAPER_URL, AlpacaClient, AlpacaError, LiveTradingRefused, check_base_url,
                    load_dotenv, ny_date)


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data
        self.content = b"x" if data is not None else b""
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.requests.append((method, url, dict(params or {}), json))
        return self.responses.pop(0)


def client(responses, **kw):
    sess = FakeSession(responses)
    sleeps = []
    c = AlpacaClient("key", "secret", session=sess, sleep=sleeps.append, environ={}, **kw)
    return c, sess, sleeps


class TestLiveGuard(unittest.TestCase):
    def test_paper_is_allowed(self):
        self.assertEqual(check_base_url(PAPER_URL, {}), PAPER_URL)
        self.assertEqual(check_base_url(PAPER_URL + "/v2/", {}), PAPER_URL)
        self.assertEqual(check_base_url("", {}), PAPER_URL)

    def test_live_is_refused(self):
        with self.assertRaises(LiveTradingRefused):
            check_base_url("https://api.alpaca.markets", {})
        with self.assertRaises(LiveTradingRefused):
            check_base_url("https://api.alpaca.markets", {"I_UNDERSTAND_LIVE_TRADING": "true"})
        with self.assertRaises(LiveTradingRefused):
            AlpacaClient.from_env(environ={"APCA_API_KEY_ID": "k", "APCA_API_SECRET_KEY": "s",
                                           "APCA_BASE_URL": "https://api.alpaca.markets"},
                                  dotenv_path="/nonexistent")

    def test_live_only_with_explicit_flag(self):
        url = "https://api.alpaca.markets"
        self.assertEqual(check_base_url(url, {"I_UNDERSTAND_LIVE_TRADING": "yes"}), url)

    def test_missing_keys(self):
        with self.assertRaises(AlpacaError):
            AlpacaClient.from_env(environ={}, dotenv_path="/nonexistent")


class TestDotenv(unittest.TestCase):
    def test_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, ".env")
            with open(p, "w") as f:
                f.write("# Kommentar\nAPCA_API_KEY_ID=abc\nexport APCA_API_SECRET_KEY=\"x y\"\n"
                        "EXISTING=from_file\n\nnonsense\n")
            env = {"EXISTING": "keep"}
            load_dotenv(p, env)
            self.assertEqual(env, {"APCA_API_KEY_ID": "abc", "APCA_API_SECRET_KEY": "x y", "EXISTING": "keep"})
            c = AlpacaClient.from_env(environ=env, dotenv_path=p)
            self.assertEqual(c.base_url, PAPER_URL)


class TestNewYorkDate(unittest.TestCase):
    def test_daily_bar_timestamps(self):
        self.assertEqual(ny_date("2024-01-02T05:00:00Z"), date(2024, 1, 2))  # Winter
        self.assertEqual(ny_date("2024-07-01T04:00:00Z"), date(2024, 7, 1))  # Sommer

    def test_utc_date_differs_from_new_york(self):
        # 02:00 UTC am 3. Januar = 21:00 New York am 2. Januar
        self.assertEqual(ny_date("2024-01-03T02:00:00Z"), date(2024, 1, 2))
        self.assertEqual(ny_date("2024-07-02T03:30:00.123456789Z"), date(2024, 7, 1))
        self.assertEqual(ny_date("2024-07-02T03:30:00-00:00"), date(2024, 7, 1))

    def test_fallback_offset_matches_rules(self):
        cases = [(datetime(2024, 3, 10, 6, 59, tzinfo=timezone.utc), -5),
                 (datetime(2024, 3, 10, 7, 0, tzinfo=timezone.utc), -4),
                 (datetime(2024, 11, 3, 5, 59, tzinfo=timezone.utc), -4),
                 (datetime(2024, 11, 3, 6, 0, tzinfo=timezone.utc), -5)]
        for dt, off in cases:
            self.assertEqual(alpaca._ny_offset_hours(dt), off)


class TestClient(unittest.TestCase):
    def test_bars_pagination_and_params(self):
        page1 = {"bars": {"SPY": [{"t": "2024-01-02T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5}]},
                 "next_page_token": "abc"}
        page2 = {"bars": {"SPY": [{"t": "2024-01-03T05:00:00Z", "o": 2, "h": 3, "l": 1, "c": 2.5}],
                          "TLT": [{"t": "2024-01-03T05:00:00Z", "o": 9, "h": 9, "l": 9, "c": 9}]},
                 "next_page_token": None}
        c, sess, _ = client([FakeResponse(200, page1), FakeResponse(200, page2)])
        bars = c.get_daily_bars(["SPY", "TLT"], date(2023, 1, 1))
        self.assertEqual([b.date for b in bars["SPY"]], [date(2024, 1, 2), date(2024, 1, 3)])
        self.assertEqual(bars["TLT"][0].close, 9.0)
        self.assertEqual(len(sess.requests), 2)
        p1, p2 = sess.requests[0][2], sess.requests[1][2]
        self.assertEqual(sess.requests[0][1], "https://data.alpaca.markets/v2/stocks/bars")
        for p in (p1, p2):
            self.assertEqual(p["timeframe"], "1Day")
            self.assertEqual(p["start"], "2023-01-01")
            self.assertEqual(p["adjustment"], "all")
            self.assertEqual(p["feed"], "iex")
        self.assertNotIn("page_token", p1)
        self.assertEqual(p2["page_token"], "abc")

    def test_retries_on_429_and_5xx(self):
        c, sess, sleeps = client([FakeResponse(429, {}), FakeResponse(503, {}),
                                  FakeResponse(200, {"is_open": True})])
        self.assertEqual(c.get_clock(), {"is_open": True})
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(sess.requests[0][1], PAPER_URL + "/v2/clock")

    def test_gives_up_after_max_retries(self):
        c, sess, _ = client([FakeResponse(500, {})] * 3, max_retries=3)
        with self.assertRaises(AlpacaError):
            c.get_account()
        self.assertEqual(len(sess.requests), 3)

    def test_client_error_not_retried(self):
        c, sess, sleeps = client([FakeResponse(403, {"message": "forbidden"})])
        with self.assertRaises(AlpacaError) as cm:
            c.submit_order({"symbol": "SPY"})
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(sleeps, [])

    def test_order_endpoints(self):
        c, sess, _ = client([FakeResponse(200, []), FakeResponse(204), FakeResponse(200, {"id": "1"})])
        c.list_orders(status="all")
        c.cancel_order("abc")
        c.get_order("1")
        self.assertEqual([(r[0], r[1]) for r in sess.requests], [
            ("GET", PAPER_URL + "/v2/orders"), ("DELETE", PAPER_URL + "/v2/orders/abc"),
            ("GET", PAPER_URL + "/v2/orders/1")])
        self.assertEqual(sess.requests[0][2]["status"], "all")


if __name__ == "__main__":
    unittest.main()
