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
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from strategy import Bar, Config, ConfigError, config_from_dict, load_config, plan, signal

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
    notes: List[str] = field(default_factory=list)


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
    # Faire Startlinie: erst vergleichen, wenn jeder ETF genug Historie fuer SMA_slow hat.
    # Sonst saesse der Bot am Anfang nur deshalb im Cash, weil Daten fehlen.
    ready = [h.dates[cfg.sma_slow - 1] for s, h in hist.items() if len(h.dates) >= cfg.sma_slow]
    effective = max([start] + ready)
    res = Result()
    if effective > start:
        res.notes.append(f"Start von {start} auf {effective} verschoben: Erst ab dann haben alle ETFs "
                         f"{cfg.sma_slow} Handelstage Kurshistorie (kostenloser IEX-Feed beginnt spaeter).")
    calendar = [d for d in hist[cfg.benchmark].dates if d >= effective and (end is None or d <= end)]
    if len(calendar) < 2:
        raise ValueError("Zu wenige Handelstage im gewaehlten Zeitraum")
    slip = slippage_bps / 10_000.0
    ts = cfg.trailing_stop_pct / 100.0

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
                if not bar or t.symbol not in pos:
                    continue
                price = bar.open * (1 - slip)
                p = pos[t.symbol]
                if t.qty >= p["qty"]:
                    close_position(t.symbol, price, d, "Signal")
                else:  # Teilverkauf (Nachjustieren); neuer Stop -> Hochpunkt neu ab heute
                    cash += t.qty * price
                    p["qty"] -= t.qty
                    p["hwm"] = bar.open
                    res.executions += 1
            for t in pending.buys:
                bar = hist[t.symbol].on(d)
                if not bar:
                    continue
                price = bar.open * (1 + slip)
                qty = min(int(t.qty), math.floor(cash / price))
                if qty < 1:
                    continue
                cash -= qty * price
                res.executions += 1
                if t.symbol in pos:  # Aufstocken: Durchschnittspreis, neuer Stop ab heute
                    p = pos[t.symbol]
                    p["entry"] = (p["entry"] * p["qty"] + price * qty) / (p["qty"] + qty)
                    p["qty"] += qty
                    p["hwm"] = bar.open
                else:
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
            if not bar or ts == 0:
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
    out += [""] + [f"_{n}_" for n in res.notes] + ["", verdict(cfg, b, h), "",
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
        risk = ("pro Risiko war der Bot sogar effizienter" if better_sharpe
                else "auch pro Risiko war Buy-and-Hold besser")
        return (f"**Ehrliches Fazit: Bei der Rendite war Buy-and-Hold {cfg.benchmark} klar besser.** "
                f"Der Bot hatte {ret}, dafuer aber {dd}. Sharpe {b['sharpe']:.2f} gegen "
                f"{h['sharpe']:.2f}: {risk}.")
    else:
        head = "**Ehrliches Fazit: Gemischt** –"
    return f"{head} der Bot hatte {ret} und {dd}."


def plot(res: Result, cfg: Config, path: str = "backtest.png") -> bool:
    return plot_curves(res.dates, [("Bot", res.bot, "#2a78d6"),
                                   (f"Buy-and-Hold {cfg.benchmark}", res.bench, "#eb6834")], path)


def plot_curves(dates, series, path: str = "backtest.png") -> bool:
    """series: Liste von (Name, Kontowerte, Farbe)."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib nicht installiert – kein Diagramm.", file=sys.stderr)
        return False
    ink, muted, grid = "#2b2b29", "#6b6a64", "#e4e3de"
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1.3]}, facecolor="#fcfcfb")
    for ax in (ax1, ax2):
        ax.set_facecolor("#fcfcfb")
        ax.grid(axis="y", color=grid, linewidth=0.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(grid)
        ax.tick_params(colors=muted, labelsize=9)
    for name, eq, color in series:
        ax1.plot(dates, eq, color=color, linewidth=2, label=name)
        ax1.annotate(f"{name}\n{eq[-1]:,.0f} $", (dates[-1], eq[-1]), xytext=(6, 0),
                     textcoords="offset points", va="center", fontsize=9, color=ink)
        peak, dd = eq[0], []
        for v in eq:
            peak = max(peak, v)
            dd.append((v / peak - 1) * 100)
        ax2.plot(dates, dd, color=color, linewidth=1.5, label=name)
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
# Varianten ehrlich vergleichen (Einstell-Jahre vs. Pruef-Jahre)
# --------------------------------------------------------------------------

_DYN = {"slot_mode": "dynamic", "max_position_pct": 35, "rebalance_band_pct": 25}
VARIANTS = [
    ("A: aktuell (Stop 10 %)", {}),
    ("B: Stop 15 %", {"trailing_stop_pct": 15}),
    ("C: Stop 20 %", {"trailing_stop_pct": 20}),
    ("D: ohne Stop", {"trailing_stop_pct": 0}),
    ("E: ohne Stop + Nachinvestieren", {"trailing_stop_pct": 0, **_DYN}),
    ("F: Top 3 Momentum, ohne Stop", {"trailing_stop_pct": 0, "top_n": 3, **_DYN}),
    ("G: Top 3 Momentum + Stop 15 %", {"trailing_stop_pct": 15, "top_n": 3, **_DYN}),
]


def variant_config(cfg: Config, overrides: dict) -> Config:
    raw = asdict(cfg)
    raw["watchlist"] = list(raw["watchlist"])
    raw.update(overrides)
    return config_from_dict(raw)


def compare_variants(cfg: Config, data: Dict[str, List[Bar]], start: date, split: date,
                     end: Optional[date] = None, initial: float = 100_000.0):
    """Jede Variante auf drei Zeitraeumen: Einstellen (bis split), Pruefen (ab split), gesamt."""
    rows = []
    for name, overrides in VARIANTS:
        vc = variant_config(cfg, overrides)
        tune = run_backtest(vc, data, start, split - timedelta(days=1), initial)
        test = run_backtest(vc, data, split, end, initial)
        full = run_backtest(vc, data, start, end, initial)
        rows.append({"name": name, "tune": tune, "test": test, "full": full,
                     "m_tune": metrics(tune.dates, tune.bot), "m_test": metrics(test.dates, test.bot),
                     "m_full": metrics(full.dates, full.bot)})
    return rows


def variants_report(cfg: Config, rows, split: date) -> str:
    r0 = rows[0]
    bench = {k: metrics(r0[k].dates, r0[k].bench) for k in ("tune", "test", "full")}

    def p(x):
        return f"{x * 100:+.1f} %"

    tune_span = f"{r0['tune'].dates[0]} bis {r0['tune'].dates[-1]}"
    test_span = f"{r0['test'].dates[0]} bis {r0['test'].dates[-1]}"
    out = [f"### Varianten-Vergleich – ehrlich getrennt in Einstell- und Pruefjahre", "",
           f"- **Einstellen:** {tune_span} (daraus wird die beste Variante gewaehlt)",
           f"- **Pruefen:** {test_span} (diese Jahre wurden zum Auswaehlen NICHT benutzt)", "",
           "| Variante | Rendite/Jahr (Einstellen) | Sharpe (Einstellen) | Rendite/Jahr (Pruefen) | "
           "Sharpe (Pruefen) | Max. Verlust (Pruefen) | Rendite/Jahr gesamt | Max. Verlust gesamt | "
           "investiert | Trades |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        a, b, c = r["m_tune"], r["m_test"], r["m_full"]
        inv = statistics.fmean(r["full"].invested) * 100
        out.append(f"| {r['name']} | {p(a['cagr'])} | {a['sharpe']:.2f} | {p(b['cagr'])} | {b['sharpe']:.2f} | "
                   f"{p(b['mdd'])} | {p(c['cagr'])} | {p(c['mdd'])} | {inv:.0f} % | {len(r['full'].trades)} |")
    a, b, c = bench["tune"], bench["test"], bench["full"]
    out.append(f"| **Buy-and-Hold {cfg.benchmark}** | {p(a['cagr'])} | {a['sharpe']:.2f} | {p(b['cagr'])} | "
               f"{b['sharpe']:.2f} | {p(b['mdd'])} | {p(c['cagr'])} | {p(c['mdd'])} | 100 % | 1 |")

    best = max(rows, key=lambda r: r["m_tune"]["sharpe"])
    bt, cur = best["m_test"], rows[0]["m_test"]
    out += ["", f"**Auswahl nach den Einstell-Jahren (beste Sharpe-Ratio): {best['name']}.**", ""]
    verdict_lines = [
        f"In den Pruefjahren brachte diese Variante {p(bt['cagr'])} pro Jahr "
        f"(aktueller Bot: {p(cur['cagr'])}, Buy-and-Hold {cfg.benchmark}: {p(bench['test']['cagr'])}) "
        f"bei einem groessten Verlust von {p(bt['mdd'])} (Buy-and-Hold: {p(bench['test']['mdd'])}).",
    ]
    if best is rows[0]:
        verdict_lines.append("Der aktuelle Bot bleibt also die beste Wahl unter den Varianten.")
    elif bt["sharpe"] >= cur["sharpe"] + 0.05:
        verdict_lines.append("Sie war also auch auf unbekannten Daten besser (pro Risiko) als der aktuelle Bot.")
    elif bt["sharpe"] > cur["sharpe"] - 0.05:
        verdict_lines.append("Pro Risiko war sie auf unbekannten Daten etwa gleich gut wie der aktuelle Bot.")
    else:
        verdict_lines.append("Auf unbekannten Daten war sie NICHT besser als der aktuelle Bot – "
                             "der Vorsprung in den Einstell-Jahren war wohl Zufall.")
    best_cagr = max(r["m_full"]["cagr"] for r in rows)
    verdict_lines.append(f"Keine Variante erreicht 25 % pro Jahr (beste ueber den ganzen Zeitraum: "
                         f"{p(best_cagr)})." if best_cagr < 0.25 else
                         "Achtung: Eine Variante zeigt ueber 25 % pro Jahr – bei so wenigen Jahren ist das "
                         "sehr wahrscheinlich Glueck und kein verlaesslicher Vorteil.")
    out += [" ".join(verdict_lines), "",
            f"_Hinweise: {len(rows)} Varianten getestet – je mehr man testet, desto eher sieht eine nur "
            "zufaellig gut aus. Annahmen wie beim normalen Backtest (Ausfuehrung zum naechsten Eroeffnungskurs, "
            f"{SLIPPAGE_BPS:g} Basispunkte Slippage, keine Steuern)._"]
    notes = sorted({n for r in rows for k in ("tune", "full") for n in r[k].notes})
    out += [f"_{n}_" for n in notes]
    out.append("")
    return "\n".join(out), best


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
    ap.add_argument("--variants", action="store_true", help="mehrere Varianten ehrlich vergleichen")
    ap.add_argument("--split", default="2024-01-01", help="ab hier beginnen die Pruefjahre (--variants)")
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
        if args.variants:
            rows = compare_variants(cfg, data, start, date.fromisoformat(args.split), end, args.initial)
            text, best = variants_report(cfg, rows, date.fromisoformat(args.split))
        else:
            res = run_backtest(cfg, data, start, end, initial=args.initial)
            text = report(cfg, res)
    except (ValueError, ConfigError) as e:
        print(f"FEHLER: {e}", file=sys.stderr)
        return 2
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if args.variants:
        full0, fullb = rows[0]["full"], best["full"]
        series = [(rows[0]["name"], full0.bot, "#2a78d6"),
                  (f"Buy-and-Hold {cfg.benchmark}", full0.bench, "#eb6834")]
        if best is not rows[0]:
            series.insert(1, (best["name"], fullb.bot, "#1baf7a"))
        ok = args.plot and plot_curves(full0.dates, series, args.plot)
    else:
        ok = args.plot and plot(res, cfg, args.plot)
    if ok:
        print(f"Diagramm gespeichert: {args.plot}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
