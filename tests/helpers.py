"""Testhelfer: kuenstliche Kurse und ein Fake-Broker (kein Internet)."""

import itertools
from datetime import date, timedelta

from alpaca import AlpacaError
from strategy import Bar, config_from_dict

CFG_DICT = {
    "watchlist": ["SPY", "QQQ", "IWM", "EFA", "TLT", "GLD"],
    "benchmark": "SPY",
    "sma_fast": 50,
    "sma_slow": 200,
    "max_position_pct": 20,
    "cash_reserve_pct": 5,
    "trailing_stop_pct": 10,
    "cooldown_days": 10,
}


def cfg(**overrides):
    d = dict(CFG_DICT)
    d.update(overrides)
    return config_from_dict(d)


def business_days(end: date, n: int):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def bars_from_closes(closes, end: date):
    out = []
    for d, c in zip(business_days(end, len(closes)), closes):
        out.append(Bar(d, c, c * 1.005, c * 0.995, c))
    return out


def uptrend(n=260, start=100.0, step=0.2):
    return [start + i * step for i in range(n)]


def downtrend(n=260, start=200.0, step=0.3):
    return [start - i * step for i in range(n)]


# Montag 3. Juni 2024, 11:07 Uhr New York
NOW = "2024-06-03T15:07:00Z"
TODAY = date(2024, 6, 3)
LAST_CLOSE_DAY = date(2024, 5, 31)

_ids = itertools.count(1)


class FakeBroker:
    """Ahmt die Teile von Alpaca nach, die der Bot benutzt."""

    def __init__(self, bars, cash=100_000.0, positions=None, now=NOW, is_open=True,
                 fill_market=True, account_extra=None):
        self.bars = bars
        self.cash = cash
        self.positions = dict(positions or {})
        self.now = now
        self.is_open = is_open
        self.fill_market = fill_market
        self.account_extra = account_extra or {}
        self.orders = []
        self.calls = []

    def price(self, sym):
        return self.bars[sym][-1].close

    # -- API ---------------------------------------------------------------
    def get_clock(self):
        self.calls.append(("get_clock",))
        return {"timestamp": self.now, "is_open": self.is_open}

    def get_account(self):
        self.calls.append(("get_account",))
        equity = self.cash + sum(q * self.price(s) for s, q in self.positions.items())
        acct = {"equity": str(equity), "cash": str(self.cash), "status": "ACTIVE",
                "trading_blocked": False, "account_blocked": False}
        acct.update(self.account_extra)
        return acct

    def get_positions(self):
        self.calls.append(("get_positions",))
        return [{"symbol": s, "qty": f"{q:g}", "market_value": str(q * self.price(s))}
                for s, q in self.positions.items() if q]

    def list_orders(self, status="open", limit=500, after=None):
        self.calls.append(("list_orders", status))
        final = {"filled", "canceled", "expired", "rejected", "replaced"}
        out = []
        for o in reversed(self.orders):
            if status == "open" and o["status"] in final:
                continue
            if status == "closed" and o["status"] not in final:
                continue
            out.append(dict(o))
        return out[:limit]

    def get_order(self, order_id):
        return dict(self._find(order_id))

    def _find(self, order_id):
        for o in self.orders:
            if o["id"] == order_id:
                return o
        raise AlpacaError("not found", 404)

    def add_order(self, **kw):
        o = {"id": f"o{next(_ids)}", "status": "new", "filled_qty": "0", "time_in_force": "day"}
        o.update(kw)
        self.orders.append(o)
        return o

    def submit_order(self, payload):
        self.calls.append(("submit_order", payload["symbol"], payload["side"], payload["type"], payload["qty"]))
        cid = payload.get("client_order_id")
        if cid and any(o.get("client_order_id") == cid for o in self.orders):
            raise AlpacaError("client_order_id must be unique", 422)
        sym, qty = payload["symbol"], float(payload["qty"])
        if payload["side"] == "sell":
            blocked = sum(float(o["qty"]) for o in self.orders
                          if o["symbol"] == sym and o["side"] == "sell"
                          and o["status"] not in {"filled", "canceled", "expired", "rejected", "replaced"})
            if qty > self.positions.get(sym, 0) - blocked:
                raise AlpacaError(f"insufficient qty available for order (requested {qty})", 403)
        o = self.add_order(**payload)
        if payload["type"] == "market" and self.fill_market:
            p = self.price(sym)
            if payload["side"] == "buy":
                self.cash -= qty * p
                self.positions[sym] = self.positions.get(sym, 0) + qty
            else:
                self.cash += qty * p
                self.positions[sym] = self.positions.get(sym, 0) - qty
                if self.positions[sym] <= 0:
                    del self.positions[sym]
            o.update(status="filled", filled_qty=payload["qty"], filled_avg_price=str(p),
                     filled_at=self.now)
        return dict(o)

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", order_id))
        o = self._find(order_id)
        if o["status"] == "filled":
            raise AlpacaError("order already filled", 422)
        o["status"] = "canceled"

    def get_daily_bars(self, symbols, start, end=None):
        self.calls.append(("get_daily_bars", tuple(symbols), start))
        return {s: [b for b in self.bars.get(s, []) if b.date >= start and (end is None or b.date <= end)]
                for s in symbols}

    # -- Testhilfen --------------------------------------------------------
    def submitted(self):
        return [c for c in self.calls if c[0] == "submit_order"]

    def open_stops(self, sym=None):
        return [o for o in self.orders if o["type"] == "trailing_stop" and o["status"] == "new"
                and (sym is None or o["symbol"] == sym)]


def all_up_bars(end=LAST_CLOSE_DAY, symbols=CFG_DICT["watchlist"], n=260):
    return {s: bars_from_closes(uptrend(n, start=50.0 + 10 * i), end) for i, s in enumerate(symbols)}
