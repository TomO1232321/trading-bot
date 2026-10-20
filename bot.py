"""Trading-Bot: ein Lauf pro Aufruf.

    python bot.py            -> handelt (nur bei offenem Markt)
    python bot.py --dry-run  -> zeigt nur den Plan, keine Orders, keine Dateien
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import sys
import time
import uuid
from datetime import date, timedelta
from typing import Dict, List, Optional

from alpaca import AlpacaClient, AlpacaError, LiveTradingRefused, ny_date, parse_ts, to_ny
from strategy import (
    Config,
    ConfigError,
    completed_bars,
    load_config,
    plan,
    signal,
    size_buys,
)

PREFIX = "tb-"
FINAL_STATUSES = {"filled", "canceled", "expired", "rejected", "replaced"}
STOP_TYPES = {"trailing_stop", "stop", "stop_limit"}
HISTORY_FIELDS = [
    "date",
    "equity",
    "cash",
    "invested_pct",
    "benchmark_close",
    "bot_return_pct",
    "benchmark_return_pct",
    "positions",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def is_own(order: dict) -> bool:
    return (order.get("client_order_id") or "").startswith(PREFIX)


def is_open(order: dict) -> bool:
    return order.get("status") not in FINAL_STATUSES


def is_stop(order: dict) -> bool:
    return order.get("side") == "sell" and order.get("type") in STOP_TYPES


def fnum(x, digits=2) -> str:
    if x is None:
        return "–"
    return f"{x:,.{digits}f}"


def pct(x) -> str:
    return "–" if x is None else f"{x:+.2f} %"


class Bot:
    def __init__(self, broker, cfg: Config, journal_dir="journal", dry_run=False, sleep=time.sleep,
                 summary_path: Optional[str] = None, order_timeout: float = 60.0):
        self.broker = broker
        self.cfg = cfg
        self.journal_dir = journal_dir
        self.dry_run = dry_run
        self.sleep = sleep
        self.summary_path = summary_path
        self.order_timeout = order_timeout
        self.actions: List[str] = []
        self.notes: List[str] = []
        self.errors: List[str] = []
        self.signals = {}
        self.cooldown: Dict[str, date] = {}
        self.positions: Dict[str, float] = {}
        self.account: Optional[dict] = None
        self.planned = None
        self.today: Optional[date] = None
        self.time_str = ""
        self.market_open = False
        self.bench_close: Optional[float] = None
        self._new_start: Optional[dict] = None
        self._perf = None

    # ------------------------------------------------------------------
    # Hilfsfunktionen fuer Orders
    # ------------------------------------------------------------------

    def cid(self, kind: str, sym: str, unique: bool = False) -> str:
        base = f"{PREFIX}{self.today:%Y%m%d}-{kind}-{sym}"
        return f"{base}-{uuid.uuid4().hex[:8]}" if unique else base

    def wait_for(self, order_id: str, wanted: set, timeout: Optional[float] = None) -> dict:
        timeout = self.order_timeout if timeout is None else timeout
        waited = 0.0
        while True:
            o = self.broker.get_order(order_id)
            if o.get("status") in wanted or o.get("status") in FINAL_STATUSES:
                return o
            if waited >= timeout:
                return o
            self.sleep(1.0)
            waited += 1.0

    def cancel_and_wait(self, order: dict) -> dict:
        try:
            self.broker.cancel_order(order["id"])
        except AlpacaError as e:
            if e.status != 422:  # 422 = Order schon erledigt
                raise
        o = self.wait_for(order["id"], {"canceled"}, timeout=30)
        if is_open(o):
            raise RuntimeError(f"Stornierung von Order {order['id']} ({order.get('symbol')}) nicht bestaetigt")
        return o

    def load_positions(self) -> Dict[str, dict]:
        return {p["symbol"]: p for p in self.broker.get_positions()}

    def watch_qty(self, raw_positions: Dict[str, dict]) -> Dict[str, float]:
        out = {}
        for sym in self.cfg.watchlist:
            p = raw_positions.get(sym)
            if p and float(p["qty"]) > 0:
                out[sym] = float(p["qty"])
        return out

    # ------------------------------------------------------------------
    # Ablauf
    # ------------------------------------------------------------------

    def run(self) -> int:
        try:
            self._run()
        except Exception as e:  # alles ins Tagebuch, nichts verschlucken
            self.errors.append(f"{type(e).__name__}: {e}")
        report = self.report()
        if self.dry_run:
            print(report)
        else:
            self.write_files(report)
            log(report)
        return 1 if self.errors else 0

    def _run(self) -> None:
        cfg, b = self.cfg, self.broker
        clock = b.get_clock()
        now_ny = to_ny(parse_ts(clock["timestamp"]))
        self.today = now_ny.date()
        self.time_str = now_ny.strftime("%H:%M")
        self.market_open = bool(clock.get("is_open"))
        if not self.market_open:
            if not self.dry_run:
                self.notes.append("Markt geschlossen (Wochenende/Feiertag/ausserhalb der Handelszeit) – nichts getan.")
                return
            self.notes.append("Markt geschlossen – Plan nur zur Ansicht.")

        acct = b.get_account()
        self.account = acct
        if acct.get("trading_blocked") or acct.get("account_blocked") or acct.get("status", "ACTIVE") != "ACTIVE":
            raise RuntimeError(f"Konto ist gesperrt oder nicht aktiv (status={acct.get('status')}) – kein Handel.")
        equity, cash = float(acct["equity"]), float(acct["cash"])

        raw_pos = self.load_positions()
        self.positions = self.watch_qty(raw_pos)

        orders = [o for o in b.list_orders(status="all", limit=500) if is_own(o)]
        self.cooldown = self.compute_cooldown(orders)

        symbols = list(dict.fromkeys(list(cfg.watchlist) + [cfg.benchmark]))
        start = self.today - timedelta(days=int(cfg.sma_slow * 1.5) + 60)
        bars = b.get_daily_bars(symbols, start)
        prices = {}
        for sym in symbols:
            all_bars = bars.get(sym, [])
            done = completed_bars(all_bars, self.today)
            self.signals[sym] = signal(sym, done, cfg)
            partial = [x for x in all_bars if x.date == self.today]
            # Fuer die Stueckzahl den aktuellsten Kurs (heutige Kerze) nehmen, fuer Signale nie.
            if partial:
                prices[sym] = partial[-1].close
            elif done:
                prices[sym] = done[-1].close
        bsig = self.signals.get(cfg.benchmark)
        self.bench_close = bsig.close if bsig else None

        p = plan(cfg, equity, cash, self.positions, prices, self.signals, self.cooldown.keys())
        self.notes.extend(p.notes)

        # Doppelt handeln verhindern (zweiter Lauf am selben Tag / Order noch offen)
        today_tag = f"{PREFIX}{self.today:%Y%m%d}-"
        pending_sell = {o["symbol"] for o in orders if is_open(o) and o.get("side") == "sell" and not is_stop(o)}
        pending_buy = {o["symbol"] for o in orders if is_open(o) and o.get("side") == "buy"}
        bought_today = {o["symbol"] for o in orders
                        if o.get("client_order_id", "").startswith(today_tag + "buy-")}
        sells = []
        for t in p.sells:
            if t.symbol in pending_sell:
                self.notes.append(f"{t.symbol}: Verkaufsorder laeuft bereits – nicht doppelt verkaufen")
            else:
                sells.append(t)
        buys = []
        for t in p.buys:
            if t.symbol in pending_buy or t.symbol in bought_today:
                self.notes.append(f"{t.symbol}: heute schon gekauft/Order offen – nicht doppelt kaufen")
            else:
                buys.append(t)
        p.sells, p.buys = sells, buys
        self.planned = p

        if self.dry_run:
            for t in p.sells:
                self.actions.append(f"(geplant) VERKAUFE {t.qty:g} {t.symbol} – {t.reason}")
            for t in p.buys:
                self.actions.append(f"(geplant) KAUFE {t.qty} {t.symbol} zu ca. {fnum(t.price)} $ – {t.reason}")
            if not p.sells and not p.buys:
                self.actions.append("(geplant) keine Kaeufe/Verkaeufe")
            return

        open_stops = [o for o in orders if is_open(o) and is_stop(o)]
        self.execute_sells(p.sells, open_stops)
        self.execute_buys(p.buys, prices)
        self.reconcile_stops()

        acct = b.get_account()
        self.account = acct
        self.positions = self.watch_qty(self.load_positions())

    def compute_cooldown(self, own_orders: List[dict]) -> Dict[str, date]:
        out: Dict[str, date] = {}
        for o in own_orders:
            if o.get("type") == "trailing_stop" and o.get("status") == "filled" and o.get("filled_at"):
                until = ny_date(o["filled_at"]) + timedelta(days=self.cfg.cooldown_days)
                if self.today < until and o["symbol"] in self.cfg.watchlist:
                    out[o["symbol"]] = max(until, out.get(o["symbol"], until))
        return out

    def execute_sells(self, sells, open_stops) -> None:
        for t in sells:
            try:
                # Zuerst den Stop dieses ETFs stornieren, sonst sind die Aktien blockiert.
                for o in [o for o in open_stops if o["symbol"] == t.symbol and is_own(o)]:
                    res = self.cancel_and_wait(o)
                    self.actions.append(f"Trailing Stop fuer {t.symbol} storniert (Status: {res.get('status')})")
                pos = self.load_positions().get(t.symbol)
                qty = float(pos["qty"]) if pos else 0.0
                if qty <= 0:
                    self.notes.append(f"{t.symbol}: Position schon weg (Stop ausgeloest?) – kein Verkauf noetig")
                    continue
                o = self.broker.submit_order({
                    "symbol": t.symbol, "qty": pos["qty"], "side": "sell", "type": "market",
                    "time_in_force": "day", "client_order_id": self.cid("sell", t.symbol),
                })
                o = self.wait_for(o["id"], {"filled"})
                if o.get("status") == "filled":
                    self.actions.append(
                        f"VERKAUFT {pos['qty']} {t.symbol} zu {fnum(float(o.get('filled_avg_price') or 0))} $ – {t.reason}")
                else:
                    self.notes.append(f"{t.symbol}: Verkaufsorder noch nicht ausgefuehrt (Status {o.get('status')})")
            except Exception as e:
                self.errors.append(f"Verkauf {t.symbol}: {e}")

    def execute_buys(self, buys, prices) -> None:
        if not buys:
            return
        acct = self.broker.get_account()
        equity, cash = float(acct["equity"]), float(acct["cash"])
        sized, notes = size_buys(self.cfg, equity, cash, [t.symbol for t in buys], prices)
        self.notes.extend(notes)
        planned = {t.symbol: t for t in buys}
        for sym, qty, price in sized:
            qty = min(qty, int(planned[sym].qty))
            try:
                o = self.broker.submit_order({
                    "symbol": sym, "qty": str(qty), "side": "buy", "type": "market",
                    "time_in_force": "day", "client_order_id": self.cid("buy", sym),
                })
                o = self.wait_for(o["id"], {"filled"})
                if o.get("status") == "filled":
                    self.actions.append(
                        f"GEKAUFT {qty} {sym} zu {fnum(float(o.get('filled_avg_price') or 0))} $ – {planned[sym].reason}")
                else:
                    self.notes.append(f"{sym}: Kauforder noch nicht ausgefuehrt (Status {o.get('status')})")
            except Exception as e:
                self.errors.append(f"Kauf {sym}: {e}")

    def reconcile_stops(self) -> None:
        """Jede Watchlist-Position hat genau einen eigenen Trailing Stop in richtiger Groesse."""
        cfg = self.cfg
        positions = self.watch_qty(self.load_positions())
        open_own = [o for o in self.broker.list_orders(status="open", limit=500) if is_own(o) and is_open(o)]
        selling = {o["symbol"] for o in open_own if o.get("side") == "sell" and not is_stop(o)}
        for sym in cfg.watchlist:
            try:
                stops = [o for o in open_own if o["symbol"] == sym and is_stop(o)]
                qty = int(math.floor(positions.get(sym, 0)))
                if sym in selling or qty < 1:
                    for o in stops:
                        self.cancel_and_wait(o)
                        self.actions.append(f"Ueberfluessigen Stop fuer {sym} storniert")
                    continue
                good = [o for o in stops if self.stop_ok(o, qty)]
                if len(stops) == 1 and good:
                    continue
                for o in stops:
                    self.cancel_and_wait(o)
                    self.actions.append(
                        f"Falschen Stop fuer {sym} storniert (Stueck {o.get('qty')}, soll {qty})")
                self.broker.submit_order({
                    "symbol": sym, "qty": str(qty), "side": "sell", "type": "trailing_stop",
                    "trail_percent": f"{cfg.trailing_stop_pct:g}", "time_in_force": "gtc",
                    "client_order_id": self.cid("stop", sym, unique=True),
                })
                self.actions.append(f"Trailing Stop {cfg.trailing_stop_pct:g} % fuer {qty} {sym} gesetzt")
            except Exception as e:
                self.errors.append(f"Stop {sym}: {e}")

    def stop_ok(self, o: dict, qty: int) -> bool:
        try:
            remaining = float(o.get("qty") or 0) - float(o.get("filled_qty") or 0)
            return (o.get("type") == "trailing_stop" and remaining == qty
                    and abs(float(o.get("trail_percent") or 0) - self.cfg.trailing_stop_pct) < 1e-9
                    and o.get("time_in_force") == "gtc")
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------------
    # Tagebuch
    # ------------------------------------------------------------------

    def start_info(self) -> Optional[dict]:
        path = os.path.join(self.journal_dir, "start.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return None

    def performance(self):
        start = self.start_info()
        if not start and not self.dry_run and self.account and self.bench_close and self.market_open:
            start = {"date": self.today.isoformat(), "equity": float(self.account["equity"]),
                     "benchmark": self.cfg.benchmark, "benchmark_price": self.bench_close}
            self._new_start = start
        if not start or not self.account or not self.bench_close:
            return None
        bot = (float(self.account["equity"]) / start["equity"] - 1) * 100
        bench = (self.bench_close / start["benchmark_price"] - 1) * 100
        return start, bot, bench

    def report(self) -> str:
        cfg = self.cfg
        lines = []
        mode = "DRY RUN (nur Plan, keine Orders)" if self.dry_run else "echter Lauf (Paper)"
        day = self.today.isoformat() if self.today else "?"
        lines.append(f"## Lauf {day} {self.time_str} Uhr New York – {mode}")
        lines.append("")
        if self.account:
            eq, cash = float(self.account["equity"]), float(self.account["cash"])
            invested = (1 - cash / eq) * 100 if eq else 0
            lines.append(f"**Konto:** Wert {fnum(eq)} $ · Cash {fnum(cash)} $ · investiert {invested:.1f} %")
            perf = self.performance()
            self._perf = perf
            if perf:
                start, bot, bench = perf
                diff = bot - bench
                verdict = "gleichauf" if abs(diff) < 0.05 else ("Bot liegt vorn" if diff > 0 else "Bot liegt hinten")
                lines.append("")
                lines.append(f"Seit {start['date']}: Bot {pct(bot)} vs. Buy-and-Hold {start.get('benchmark', cfg.benchmark)} "
                             f"{pct(bench)} – {verdict}")
            lines.append("")
        if self.signals:
            lines.append(f"| ETF | Kurs | SMA{cfg.sma_fast} | SMA{cfg.sma_slow} | Signal | gehalten | Cooldown |")
            lines.append("|---|---:|---:|---:|---|---:|---|")
            for sym in cfg.watchlist:
                s = self.signals.get(sym)
                if not s:
                    continue
                state = {"NODATA": "zu wenig Daten"}.get(s.state, s.state)
                held = self.positions.get(sym, 0)
                cd = self.cooldown.get(sym)
                lines.append(f"| {sym} | {fnum(s.close)} | {fnum(s.sma_fast)} | {fnum(s.sma_slow)} | {state} | "
                             f"{held:g} | {'bis ' + cd.isoformat() if cd else '–'} |")
            asof = next((s.as_of for s in self.signals.values() if s.as_of), None)
            if asof:
                lines.append("")
                lines.append(f"_Signale aus dem Schlusskurs vom {asof.isoformat()} (heutige Kerze ignoriert)._")
            lines.append("")
        lines.append("**Aktionen:**")
        lines.extend([f"- {a}" for a in self.actions] or ["- keine"])
        if self.notes:
            lines.append("")
            lines.append("**Hinweise:**")
            lines.extend(f"- {n}" for n in self.notes)
        lines.append("")
        lines.append("**Fehler:**")
        lines.extend([f"- ❌ {e}" for e in self.errors] or ["- keine"])
        lines.append("")
        return "\n".join(lines)

    def write_files(self, report: str) -> None:
        os.makedirs(self.journal_dir, exist_ok=True)
        day = (self.today or date.today()).isoformat()
        path = os.path.join(self.journal_dir, f"{day}.md")
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(f"# Tagebuch {day}\n\n")
            f.write(report + "\n")
        new_start = self._new_start
        if new_start and not os.path.exists(os.path.join(self.journal_dir, "start.json")):
            with open(os.path.join(self.journal_dir, "start.json"), "w", encoding="utf-8") as f:
                json.dump(new_start, f, indent=2)
                f.write("\n")
        if self.account and self.market_open:
            self.write_history(day)
        if self.summary_path:
            try:
                with open(self.summary_path, "a", encoding="utf-8") as f:
                    f.write(f"# Trading-Bot {day}\n\n{report}\n")
            except OSError as e:
                log(f"Konnte GITHUB_STEP_SUMMARY nicht schreiben: {e}")

    def write_history(self, day: str) -> None:
        path = os.path.join(self.journal_dir, "history.csv")
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:
                rows = [r for r in csv.DictReader(f) if r.get("date") != day]
        eq, cash = float(self.account["equity"]), float(self.account["cash"])
        perf = self._perf
        rows.append({
            "date": day,
            "equity": f"{eq:.2f}",
            "cash": f"{cash:.2f}",
            "invested_pct": f"{(1 - cash / eq) * 100 if eq else 0:.2f}",
            "benchmark_close": f"{self.bench_close:.2f}" if self.bench_close else "",
            "bot_return_pct": f"{perf[1]:.2f}" if perf else "",
            "benchmark_return_pct": f"{perf[2]:.2f}" if perf else "",
            "positions": " ".join(f"{s}:{q:g}" for s, q in sorted(self.positions.items())),
        })
        rows.sort(key=lambda r: r["date"])
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=HISTORY_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in HISTORY_FIELDS})
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Regelbasierter Trendfilter-Bot fuer ein Alpaca-PAPER-Konto")
    ap.add_argument("--dry-run", action="store_true", help="nur Plan zeigen, keine Orders, keine Dateien")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--journal-dir", default="journal")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        broker = AlpacaClient.from_env()
    except (ConfigError, LiveTradingRefused, AlpacaError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 2
    bot = Bot(broker, cfg, journal_dir=args.journal_dir, dry_run=args.dry_run,
              summary_path=None if args.dry_run else os.environ.get("GITHUB_STEP_SUMMARY"))
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
