"""Reine Strategie-Logik ohne Netzwerk.

Wird vom Bot (bot.py) UND vom Backtest (backtest.py) benutzt, damit beide
garantiert dieselben Regeln anwenden.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence

HOLD = "HOLD"
OUT = "OUT"
NODATA = "NODATA"

BUY_BUFFER = 0.01  # 1 % Puffer beim Kaufen, damit das Cash nie ueberzogen wird


# --------------------------------------------------------------------------
# Datentypen
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Config:
    watchlist: tuple
    benchmark: str = "SPY"
    sma_fast: int = 50
    sma_slow: int = 200
    max_position_pct: float = 20.0
    cash_reserve_pct: float = 5.0
    trailing_stop_pct: float = 10.0  # 0 = kein Trailing Stop
    cooldown_days: int = 10
    # Optionale Erweiterungen (Standardwerte = urspruengliches Verhalten)
    top_n: int = 0  # 0 = alle ETFs mit HOLD halten, sonst nur die N mit dem staerksten Momentum
    momentum_days: int = 126  # Momentum = Kursveraenderung ueber so viele Handelstage
    slot_mode: str = "fixed"  # "fixed" = Slot je Watchlist-ETF, "dynamic" = Geld auf gehaltene ETFs verteilen
    rebalance_band_pct: float = 0.0  # 0 = nie nachjustieren, sonst ab dieser Abweichung vom Ziel

    @property
    def slot_pct(self) -> float:
        """Groesse eines Slots in Prozent des Kontos (Modus "fixed")."""
        return min(self.max_position_pct, (100.0 - self.cash_reserve_pct) / len(self.watchlist))

    def target_slot_pct(self, n_targets: int) -> float:
        if self.slot_mode == "dynamic":
            return min(self.max_position_pct, (100.0 - self.cash_reserve_pct) / max(1, n_targets))
        return self.slot_pct


class ConfigError(ValueError):
    pass


CONFIG_KEYS = (
    "watchlist",
    "benchmark",
    "sma_fast",
    "sma_slow",
    "max_position_pct",
    "cash_reserve_pct",
    "trailing_stop_pct",
    "cooldown_days",
)
OPTIONAL_KEYS = {"top_n": 0, "momentum_days": 126, "slot_mode": "fixed", "rebalance_band_pct": 0}


def config_from_dict(raw: dict) -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.json muss ein JSON-Objekt sein")
    unknown = sorted(set(raw) - set(CONFIG_KEYS) - set(OPTIONAL_KEYS))
    if unknown:
        raise ConfigError(f"Unbekannte Einstellung(en) in config.json: {', '.join(unknown)}")
    missing = [k for k in CONFIG_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"Fehlende Einstellung(en) in config.json: {', '.join(missing)}")

    wl = raw["watchlist"]
    if not isinstance(wl, list) or not wl:
        raise ConfigError("watchlist muss eine nicht-leere Liste sein")
    symbols = []
    for s in wl:
        if not isinstance(s, str) or not s.strip():
            raise ConfigError(f"Ungueltiges Symbol in watchlist: {s!r}")
        symbols.append(s.strip().upper())
    dupes = sorted({s for s in symbols if symbols.count(s) > 1})
    if dupes:
        raise ConfigError(f"Doppelte Symbole in watchlist: {', '.join(dupes)}")

    raw = {**OPTIONAL_KEYS, **raw}
    bench = raw["benchmark"]
    if not isinstance(bench, str) or not bench.strip():
        raise ConfigError("benchmark muss ein Symbol sein, z. B. \"SPY\"")

    def _int(key, lo):
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, int) or v < lo:
            raise ConfigError(f"{key} muss eine ganze Zahl >= {lo} sein (ist {v!r})")
        return v

    def _pct(key, lo, hi, lo_inclusive=True):
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
            raise ConfigError(f"{key} muss eine Zahl sein (ist {v!r})")
        if v > hi or v < lo or (not lo_inclusive and v == lo):
            raise ConfigError(f"{key} muss zwischen {lo} und {hi} liegen (ist {v!r})")
        return float(v)

    sma_fast = _int("sma_fast", 1)
    sma_slow = _int("sma_slow", 2)
    if sma_fast >= sma_slow:
        raise ConfigError(f"sma_fast ({sma_fast}) muss kleiner als sma_slow ({sma_slow}) sein")

    top_n = _int("top_n", 0)
    if top_n > len(symbols):
        raise ConfigError(f"top_n ({top_n}) darf nicht groesser als die Watchlist ({len(symbols)}) sein")
    if raw["slot_mode"] not in ("fixed", "dynamic"):
        raise ConfigError(f"slot_mode muss \"fixed\" oder \"dynamic\" sein (ist {raw['slot_mode']!r})")
    trailing = _pct("trailing_stop_pct", 0, 50)
    if 0 < trailing < 0.1:
        raise ConfigError("trailing_stop_pct muss 0 (aus) oder mindestens 0.1 sein")

    return Config(
        watchlist=tuple(symbols),
        benchmark=bench.strip().upper(),
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        max_position_pct=_pct("max_position_pct", 0, 100, lo_inclusive=False),
        cash_reserve_pct=_pct("cash_reserve_pct", 0, 99),
        trailing_stop_pct=trailing,
        cooldown_days=_int("cooldown_days", 0),
        top_n=top_n,
        momentum_days=_int("momentum_days", 1),
        slot_mode=raw["slot_mode"],
        rebalance_band_pct=_pct("rebalance_band_pct", 0, 100),
    )


def load_config(path: str = "config.json") -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Konfigurationsdatei {path} nicht gefunden")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path} ist kein gueltiges JSON: {e}")
    return config_from_dict(raw)


# --------------------------------------------------------------------------
# Indikatoren und Signale
# --------------------------------------------------------------------------


def sma(values: Sequence[float], n: int) -> Optional[float]:
    """Einfacher gleitender Durchschnitt der letzten n Werte (None bei zu wenig Daten)."""
    if n <= 0 or len(values) < n:
        return None
    return sum(values[-n:]) / n


def completed_bars(bars: Iterable[Bar], today: date) -> List[Bar]:
    """Nur abgeschlossene Handelstage: die heutige (laufende) Kerze und alles danach weg."""
    return [b for b in bars if b.date < today]


@dataclass(frozen=True)
class Signal:
    symbol: str
    state: str  # HOLD, OUT oder NODATA
    close: Optional[float]
    sma_fast: Optional[float]
    sma_slow: Optional[float]
    as_of: Optional[date] = None
    momentum: Optional[float] = None  # Kursveraenderung ueber momentum_days (z. B. 0.12 = +12 %)


def signal(symbol: str, bars: Sequence[Bar], cfg: Config) -> Signal:
    """HOLD wenn Schluss > SMA_slow und SMA_fast > SMA_slow, sonst OUT. Zu wenig Daten: NODATA."""
    closes = [b.close for b in bars]
    last = closes[-1] if closes else None
    as_of = bars[-1].date if bars else None
    fast = sma(closes, cfg.sma_fast)
    slow = sma(closes, cfg.sma_slow)
    mom = None
    if len(closes) > cfg.momentum_days and closes[-cfg.momentum_days - 1] > 0:
        mom = last / closes[-cfg.momentum_days - 1] - 1
    if last is None or fast is None or slow is None:
        return Signal(symbol, NODATA, last, fast, slow, as_of, mom)
    state = HOLD if (last > slow and fast > slow) else OUT
    return Signal(symbol, state, last, fast, slow, as_of, mom)


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------


@dataclass
class Trade:
    symbol: str
    side: str  # "buy" oder "sell"
    qty: float
    price: float
    reason: str


@dataclass
class Plan:
    sells: List[Trade] = field(default_factory=list)
    buys: List[Trade] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def size_buys(
    cfg: Config,
    equity: float,
    cash: float,
    candidates: Sequence[str],
    prices: Dict[str, float],
    slot_value: Optional[float] = None,
    wanted: Optional[Dict[str, float]] = None,
) -> tuple:
    """Nur ganze Aktien, Cash-Reserve und 1 % Puffer beachten.

    Jeder Kandidat bekommt hoechstens slot_value (bzw. wanted[symbol]) Dollar.
    Gibt (liste von (symbol, qty, price), notizen) zurueck.
    """
    if slot_value is None:
        slot_value = equity * cfg.slot_pct / 100.0
    wanted = wanted or {}
    reserve = equity * cfg.cash_reserve_pct / 100.0
    available = max(0.0, cash - reserve)
    out, notes = [], []
    for sym in candidates:
        price = prices.get(sym)
        if not price or price <= 0:
            notes.append(f"{sym}: kein Kurs, Kauf uebersprungen")
            continue
        budget = min(wanted.get(sym, slot_value), available)
        qty = math.floor(budget / (price * (1 + BUY_BUFFER)))
        if qty < 1:
            if sym not in wanted:
                notes.append(f"{sym}: zu wenig Cash fuer eine ganze Aktie (Budget {budget:.2f})")
            continue
        available -= qty * price * (1 + BUY_BUFFER)
        out.append((sym, qty, price))
    return out, notes


def plan(
    cfg: Config,
    equity: float,
    cash: float,
    positions: Dict[str, float],
    prices: Dict[str, float],
    signals: Dict[str, Signal],
    cooldown: Iterable[str] = (),
) -> Plan:
    """Entscheidet, was verkauft und gekauft wird. Zuerst verkaufen, dann kaufen.

    positions: Stueckzahl je Symbol (nur Watchlist wird beachtet)
    prices: aktueller Schaetzkurs je Symbol (fuer Groessen)
    cooldown: Symbole, die gerade nicht neu gekauft werden duerfen
    """
    cooldown = set(cooldown)
    p = Plan()
    sell_proceeds = 0.0

    def price_of(sym):
        return prices.get(sym) or signals[sym].close or 0.0

    def sell(sym, qty, reason):
        nonlocal sell_proceeds
        p.sells.append(Trade(sym, "sell", qty, price_of(sym), reason))
        sell_proceeds += qty * price_of(sym)

    eligible = []
    for sym in cfg.watchlist:
        sig = signals.get(sym)
        held = (positions.get(sym, 0) or 0) > 0
        if sig is None or sig.state == NODATA:
            p.notes.append(f"{sym}: zu wenig Kurshistorie, nichts getan")
        elif sig.state == OUT:
            if held:
                sell(sym, positions[sym], "Trend vorbei (OUT)")
        elif held or sym not in cooldown:
            eligible.append(sym)
        else:
            p.notes.append(f"{sym}: HOLD, aber Cooldown nach Trailing Stop - kein Kauf")

    targets = eligible
    if cfg.top_n:
        ranked = sorted(eligible, key=lambda s: -(signals[s].momentum if signals[s].momentum is not None
                                                  else -math.inf))
        targets = ranked[:cfg.top_n]
        for sym in ranked[cfg.top_n:]:
            if (positions.get(sym, 0) or 0) > 0:
                sell(sym, positions[sym], f"nicht mehr unter den Top {cfg.top_n} (Momentum)")
            else:
                p.notes.append(f"{sym}: HOLD, aber nicht unter den Top {cfg.top_n} (Momentum)")

    slot_value = equity * cfg.target_slot_pct(len(targets)) / 100.0
    new = [s for s in targets if not (positions.get(s, 0) or 0) > 0]
    topups = {}
    if cfg.rebalance_band_pct > 0:
        band = cfg.rebalance_band_pct / 100.0
        for sym in targets:
            qty = positions.get(sym, 0) or 0
            price = price_of(sym)
            if qty <= 0 or price <= 0:
                continue
            target_qty = math.floor(slot_value / price)
            if qty > target_qty * (1 + band) and qty - target_qty >= 1:
                sell(sym, qty - target_qty, "Nachjustieren: Position zu gross")
            elif qty < target_qty * (1 - band):
                topups[sym] = (target_qty - qty) * price * (1 + BUY_BUFFER)

    sized, notes = size_buys(cfg, equity, cash + sell_proceeds, new + list(topups), prices,
                             slot_value=slot_value, wanted=topups)
    p.notes.extend(notes)
    for sym, qty, price in sized:
        reason = "Nachjustieren: Position zu klein" if sym in topups else "Trend intakt (HOLD)"
        p.buys.append(Trade(sym, "buy", qty, price, reason))
    return p
