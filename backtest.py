"""Backtest: dieselben Regeln (strategy.plan) auf historischen Tageskursen.

Kein Blick in die Zukunft: Entscheidung mit dem Schlusskurs von Tag D,
Ausfuehrung zum Eroeffnungskurs von Tag D+1.

    python backtest.py                         # Daten von Alpaca (braucht API-Schluessel)
    python backtest.py --csv-dir data/         # Daten aus CSV-Dateien (SPY.csv, QQQ.csv, ...)
"""

from __future__ import annotations

import argparse
import bisect
import csv
import math
import os
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from strategy import Bar, Config, ConfigError, load_config, plan, signal

SLIPPAGE_BPS = 5.0


@dataclass
class Result:
    dates: List[date] = field(default_factory=list)
    bot: List[float] = field(default_factory=list)
    bench: List[float] = field(default_factory=list)
    invested: List[float] = field(default_factory=list)
    trades: List[dict] = field(default_factory=list)  # abgeschlossene Positionen
    stops: int = 0
    executions: int = 0
    open_positions: int = 0


class History:
    """Kurse eines Symbols mit schnellem Zugriff 'alles bis einschliesslich Tag D'."""

    def __init__(self, bars: List[Bar]):
        self.bars = sorted(bars, key=lambda b: b.date)
        self.dates = [b.date for b in self.bars]
        self.by_date = {b.date: b for b in self.bars}

    def upto(self, d: date) -> List[Bar]:
        return self.bars[: bisect.bisect_right(self.dates, d)]

    def on(self, d: date) -> Optional[Bar]:
        return self.by_date.get(d)

    def last_close(self, d: date) -> Optional[float]:
        i = bisect.bisect_right(self.dates, d)
        return self.bars[i - 1].close if i else None


def run_backtest(cfg: Config, data: Dict[str, List[Bar]], start: date, end: Optional[date] = None,
                 initial: float = 100_000.0, slippage_bps: float = SLIPPAGE_BPS) -> Result:
    if cfg.benchmark not in data or not data[cfg.benchmark]:
        raise ValueError(f"Keine Kurse fuer die Benchmark {cfg.benchmark}")
    hist = {s: History(data.get(s, [])) for s in set(cfg.watchlist) | {cfg.benchmark}}
    calendar = [d for d in hist[cfg.benchmark].dates if d >= start and (end is None or d <= end)]
    if len(calendar) < 2:
        raise ValueError("Zu wenige Handelstage im gewaehlten Zeitraum")
    slip = slippage_bps / 10_000.0
    ts = cfg.trailing_stop_pct / 100.0

    res = Result()
    cash = initial
    pos: Dict[str, dict] = {}
    cooldown_until: Dict[str, date] = {}
    pending = None
    bench_qty, bench_cash = 0, initial

    def close_position(sym, price, d, reason):
        nonlocal cash
        p = pos.pop(sym)
        cash += p["qty"] * price
        res.executions += 1
        res.trades.append({"symbol": sym, "entry": p["entry"], "exit": price, "qty": p["qty"],
                           "entry_date": p["date"], "exit_date": d, "reason": reason,
                           "pnl": (price - p["entry"]) * p["qty"]})

    for i, d in enumerate(calendar):
        # 1) Gestern beschlossene Orders zum heutigen Eroeffnungskurs ausfuehren
        if pending is not None:
            for t in pending.sells:
                bar = hist[t.symbol].on(d)
                if bar and t.symbol in pos:
                    close_position(t.symbol, bar.open * (1 - slip), d, "Signal")
            for t in pending.buys:
                bar = hist[t.symbol].on(d)
                if not bar or t.symbol in pos:
                    continue
                price = bar.open * (1 + slip)
                qty = min(int(t.qty), math.floor(cash / price))
                if qty >= 1:
                    cash -= qty * price
                    res.executions += 1
                    pos[t.symbol] = {"qty": qty, "entry": price, "date": d, "hwm": bar.open}
            pending = None
        if i == 1:  # Buy-and-Hold kauft am selben Tag wie die erste moegliche Bot-Ausfuehrung
            bar = hist[cfg.benchmark].on(d)
            price = bar.open * (1 + slip)
            bench_qty = math.floor(initial / price)
            bench_cash = initial - bench_qty * price

        # 2) Trailing Stops im Tagesverlauf (gegen Tagestief, bei Kurslücke zum Eroeffnungskurs)
        for sym in list(pos):
            bar = hist[sym].on(d)
            if not bar:
                continue
            stop = pos[sym]["hwm"] * (1 - ts)
            if bar.open <= stop:
                fill = bar.open * (1 - slip)
            elif bar.low <= stop:
                fill = stop * (1 - slip)
            else:
                pos[sym]["hwm"] = max(pos[sym]["hwm"], bar.high)
                continue
            close_position(sym, fill, d, "Trailing Stop")
            res.stops += 1
            cooldown_until[sym] = d + timedelta(days=cfg.cooldown_days)

        # 3) Tagesschluss: bewerten
        prices = {s: hist[s].last_close(d) for s in hist}
        equity = cash + sum(p["qty"] * (prices[s] or p["entry"]) for s, p in pos.items())
        bench_eq = bench_cash + bench_qty * prices[cfg.benchmark]
        res.dates.append(d)
        res.bot.append(equity)
        res.bench.append(bench_eq)
        res.invested.append((equity - cash) / equity if equity else 0.0)

        # 4) Entscheidung mit dem Schluss von Tag D (nur Daten bis D!) fuer Tag D+1
        if i + 1 < len(calendar):
            nxt = calendar[i + 1]
            sigs = {s: signal(s, hist[s].upto(d), cfg) for s in cfg.watchlist}
            cd = {s for s, until in cooldown_until.items() if nxt < until}
            pending = plan(cfg, equity, cash, {s: p["qty"] for s, p in pos.items()},
                           {s: v for s, v in prices.items() if v}, sigs, cd)

    res.open_positions = len(pos)
    return res


# --------------------------------------------------------------------------
# Kennzahlen
# --------------------------------------------------------------------------


def metrics(dates: List[date], equity: List[float]) -> dict:
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity)) if equity[i - 1]]
    years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    total = equity[-1] / equity[0] - 1
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if equity[-1] > 0 else -1.0
    sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
    vol = sd * math.sqrt(252)
    sharpe = (statistics.fmean(rets) / sd * math.sqrt(252)) if sd > 0 else 0.0
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return {"total": total, "cagr": cagr, "vol": vol, "sharpe": sharpe, "mdd": mdd}


def report(cfg: Config, res: Result) -> str:
    b = metrics(res.dates, res.bot)
    h = metrics(res.dates, res.bench)
    wins = sum(1 for t in res.trades if t["pnl"] > 0)
    losses = len(res.trades) - wins
    inv = statistics.fmean(res.invested) * 100

    def p(x):
        return f"{x * 100:+.1f} %"

    rows = [
        ("Gesamtrendite", p(b["total"]), p(h["total"])),
        ("Rendite pro Jahr (CAGR)", p(b["cagr"]), p(h["cagr"])),
        ("Volatilitaet (Schwankung/Jahr)", f"{b['vol'] * 100:.1f} %", f"{h['vol'] * 100:.1f} %"),
        ("Sharpe-Ratio (Rendite je Risiko)", f"{b['sharpe']:.2f}", f"{h['sharpe']:.2f}"),
        ("Maximaler Drawdown (groesster Verlust)", p(b["mdd"]), p(h["mdd"])),
        ("Anzahl Trades (abgeschlossen)", str(len(res.trades)), "1 Kauf"),
        ("Gewinner / Verlierer", f"{wins} / {losses}", "–"),
        ("Stop-Ausloesungen", str(res.stops), "–"),
        ("Durchschnittlich investiert", f"{inv:.0f} %", "100 %"),
        ("Endwert (Start 100.000 $)", f"{res.bot[-1]:,.0f} $", f"{res.bench[-1]:,.0f} $"),
    ]
    out = [f"### Backtest {res.dates[0]} bis {res.dates[-1]} – Bot vs. Buy-and-Hold {cfg.benchmark}", "",
           f"| Kennzahl | Bot | Buy-and-Hold {cfg.benchmark} |", "|---|---:|---:|"]
    out += [f"| {a} | {x} | {y} |" for a, x, y in rows]
    out += ["", verdict(cfg, b, h), "",
            f"_Annahmen: Entscheidung zum Schlusskurs, Ausfuehrung am naechsten Morgen zum Eroeffnungskurs, "
            f"{SLIPPAGE_BPS:g} Basispunkte Slippage je Ausfuehrung, Kurse dividendenbereinigt (adjustment=all), "
            f"keine Steuern. {res.open_positions} Position(en) am Ende noch offen. "
            f"Vergangene Ergebnisse sind keine Garantie fuer die Zukunft._", ""]
    return "\n".join(out)


def verdict(cfg: Config, b: dict, h: dict) -> str:
    more_ret = b["cagr"] > h["cagr"]
    less_dd = b["mdd"] > h["mdd"]
    better_sharpe = b["sharpe"] > h["sharpe"]
    ret = (f"{'mehr' if more_ret else 'weniger'} Rendite ({b['cagr'] * 100:.1f} % statt "
           f"{h['cagr'] * 100:.1f} % pro Jahr)")
    dd = (f"einen {'kleineren' if less_dd else 'groesseren'} groessten Verlust "
          f"({b['mdd'] * 100:.1f} % statt {h['mdd'] * 100:.1f} %)")
    if more_ret and less_dd:
        head = "**Ehrliches Fazit: Der Bot war in diesem Zeitraum besser** –"
    elif not more_ret and not less_dd:
        head = f"**Ehrliches Fazit: Einfach {cfg.benchmark} kaufen und halten war besser** – der Bot hatte"
        return f"{head} {ret} und {dd}."
    elif not more_ret:
        head = (f"**Ehrliches Fazit: Bei der Rendite war Buy-and-Hold {cfg.benchmark} besser**, "
                f"der Bot war {'aber ruhiger' if better_sharpe else 'trotzdem nicht effizienter'}:")
    else:
        head = "**Ehrliches Fazit: Gemischt** –"
    return f"{head} der Bot hatte {ret} und {dd}."


def plot(res: Result, cfg: Config, path: str = "backtest.png") -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib nicht installiert – kein Diagramm.", file=sys.stderr)
        return False
    blue, orange, ink, muted, grid = "#2a78d6", "#eb6834", "#2b2b29", "#6b6a64", "#e4e3de"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]}, facecolor="#fcfcfb")
    series = [("Bot", res.bot, blue), (f"Buy-and-Hold {cfg.benchmark}", res.bench, orange)]
    for ax in (ax1, ax2):
        ax.set_facecolor("#fcfcfb")
        ax.grid(axis="y", color=grid, linewidth=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(grid)
        ax.tick_params(colors=muted, labelsize=9)
    for name, eq, color in series:
        ax1.plot(res.dates, eq, color=color, linewidth=2, label=name)
        ax1.annotate(f"{name}\n{eq[-1]:,.0f} $", (res.dates[-1], eq[-1]), xytext=(6, 0),
                     textcoords="offset points", va="center", fontsize=9, color=ink)
        peak, dd = eq[0], []
        for v in eq:
            peak = max(peak, v)
            dd.append((v / peak - 1) * 100)
        ax2.plot(res.dates, dd, color=color, linewidth=1.5, label=name)
    ax1.set_title("Kontowert (Start 100.000 $)", loc="left", fontsize=12, color=ink)
    ax1.legend(frameon=False, loc="upper left", fontsize=9, labelcolor=ink)
    ax1.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax2.set_title("Verlust vom bisherigen Hoch (Drawdown, %)", loc="left", fontsize=10, color=ink)
    ax2.axhline(0, color=muted, linewidth=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------
# Daten laden
# --------------------------------------------------------------------------


def load_csv_dir(directory: str, symbols) -> Dict[str, List[Bar]]:
    out = {}
    for sym in symbols:
        path = os.path.join(directory, f"{sym}.csv")
        if not os.path.exists(path):
            print(f"Warnung: {path} fehlt – {sym} wird ohne Daten simuliert", file=sys.stderr)
            continue
        with open(path, encoding="utf-8", newline="") as f:
            rows = csv.DictReader(f)
            out[sym] = [Bar(date.fromisoformat(r["date"][:10]), float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"])) for r in rows]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Backtest des Trendfilter-Bots")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--csv-dir", default=None, help="Ordner mit SYMBOL.csv (Spalten date,open,high,low,close)")
    ap.add_argument("--initial", type=float, default=100_000.0)
    ap.add_argument("--plot", default="backtest.png")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 2
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else None
    symbols = list(dict.fromkeys(list(cfg.watchlist) + [cfg.benchmark]))
    if args.csv_dir:
        data = load_csv_dir(args.csv_dir, symbols)
    else:
        from alpaca import AlpacaClient, AlpacaError, LiveTradingRefused

        try:
            client = AlpacaClient.from_env()
            warmup = start - timedelta(days=int(cfg.sma_slow * 1.5) + 60)
            data = client.get_daily_bars(symbols, warmup, end)
        except (AlpacaError, LiveTradingRefused) as e:
            print(f"FEHLER: {e}", file=sys.stderr)
            return 2
    try:
        res = run_backtest(cfg, data, start, end, initial=args.initial)
    except ValueError as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 2
    text = report(cfg, res)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if args.plot and plot(res, cfg, args.plot):
        print(f"Diagramm gespeichert: {args.plot}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
