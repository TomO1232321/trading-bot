"""Schlanker REST-Client fuer Alpaca (nur `requests`, kein SDK).

Standardmaessig wird NUR das Paper-Konto (Spielgeld) benutzt.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import requests

from strategy import Bar

PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
RETRY_STATUS = {429, 500, 502, 503, 504}


class AlpacaError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class LiveTradingRefused(RuntimeError):
    pass


# --------------------------------------------------------------------------
# .env-Loader (ohne python-dotenv)
# --------------------------------------------------------------------------


def load_dotenv(path: str = ".env", environ=None) -> None:
    """Liest KEY=VALUE-Zeilen. Bereits gesetzte Umgebungsvariablen gewinnen."""
    environ = os.environ if environ is None else environ
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in environ:
                environ[key] = value


# --------------------------------------------------------------------------
# Zeitzone New York (zoneinfo, mit eingebautem Ersatz falls tzdata fehlt)
# --------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # z. B. Windows ohne tzdata-Paket
    NY = None


def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _ny_offset_hours(utc_dt: datetime) -> int:
    # US-Sommerzeit: 2. Sonntag im Maerz 07:00 UTC bis 1. Sonntag im November 06:00 UTC
    y = utc_dt.year
    start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
    end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
    return -4 if start <= utc_dt < end else -5


def parse_ts(ts: str) -> datetime:
    """RFC3339-Zeitstempel (auch mit Nanosekunden) -> aware datetime in UTC."""
    s = ts.strip().replace("Z", "+00:00")
    # Nachkommastellen auf 6 kuerzen (Alpaca liefert teils 9)
    if "." in s:
        head, _, rest = s.partition(".")
        frac = ""
        i = 0
        while i < len(rest) and rest[i].isdigit():
            frac += rest[i]
            i += 1
        s = f"{head}.{frac[:6].ljust(6, '0')}{rest[i:]}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ny(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc)
    if NY is not None:
        return dt.astimezone(NY)
    return (dt + timedelta(hours=_ny_offset_hours(dt))).replace(tzinfo=None)


def ny_date(ts: str) -> date:
    """Zeitstempel -> Kalenderdatum in New York (so datiert Alpaca Tageskerzen)."""
    return to_ny(parse_ts(ts)).date()


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def check_base_url(base_url: str, environ=None) -> str:
    environ = os.environ if environ is None else environ
    url = (base_url or PAPER_URL).strip().rstrip("/")
    if url.endswith("/v2"):
        url = url[:-3]
    if url != PAPER_URL and environ.get("I_UNDERSTAND_LIVE_TRADING") != "yes":
        raise LiveTradingRefused(
            f"APCA_BASE_URL={url} ist NICHT die Paper-URL ({PAPER_URL}). "
            "Aus Sicherheitsgruenden wird abgebrochen. Nur wer wirklich mit echtem Geld "
            "handeln will, setzt zusaetzlich I_UNDERSTAND_LIVE_TRADING=yes."
        )
    return url


class AlpacaClient:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        base_url: str = PAPER_URL,
        data_url: str = DATA_URL,
        session=None,
        sleep=time.sleep,
        max_retries: int = 5,
        environ=None,
        timeout: float = 30.0,
    ):
        if not key_id or not secret_key:
            raise AlpacaError(
                "API-Schluessel fehlen. Setze APCA_API_KEY_ID und APCA_API_SECRET_KEY "
                "(als GitHub-Secret oder in einer lokalen .env-Datei)."
            )
        self.base_url = check_base_url(base_url, environ)
        self.data_url = data_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self.sleep = sleep
        self.max_retries = max_retries
        self.timeout = timeout

    @classmethod
    def from_env(cls, environ=None, dotenv_path: str = ".env", **kw) -> "AlpacaClient":
        environ = os.environ if environ is None else environ
        load_dotenv(dotenv_path, environ)
        return cls(
            environ.get("APCA_API_KEY_ID", "").strip(),
            environ.get("APCA_API_SECRET_KEY", "").strip(),
            base_url=environ.get("APCA_BASE_URL") or PAPER_URL,
            environ=environ,
            **kw,
        )

    # -- HTTP --------------------------------------------------------------

    def _request(self, method: str, url: str, params=None, json=None):
        last_err = None
        for attempt in range(self.max_retries):
            try:
                r = self.session.request(
                    method, url, params=params, json=json, headers=self.headers, timeout=self.timeout
                )
            except requests.RequestException as e:
                last_err = AlpacaError(f"Netzwerkfehler bei {method} {url}: {e}")
            else:
                if r.status_code in RETRY_STATUS:
                    last_err = AlpacaError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:300]}", r.status_code)
                elif r.status_code >= 400:
                    raise AlpacaError(f"{method} {url} -> HTTP {r.status_code}: {r.text[:300]}", r.status_code)
                else:
                    if r.status_code == 204 or not r.content:
                        return None
                    return r.json()
            if attempt < self.max_retries - 1:
                self.sleep(min(2 ** attempt, 30))
        raise last_err

    def _get(self, path, params=None):
        return self._request("GET", self.base_url + path, params=params)

    # -- Trading-API -------------------------------------------------------

    def get_clock(self) -> dict:
        return self._get("/v2/clock")

    def get_account(self) -> dict:
        return self._get("/v2/account")

    def get_positions(self) -> List[dict]:
        return self._get("/v2/positions") or []

    def list_orders(self, status: str = "open", limit: int = 500, after: Optional[str] = None) -> List[dict]:
        params = {"status": status, "limit": limit, "direction": "desc"}
        if after:
            params["after"] = after
        return self._get("/v2/orders", params) or []

    def get_order(self, order_id: str) -> dict:
        return self._get(f"/v2/orders/{order_id}")

    def submit_order(self, payload: dict) -> dict:
        return self._request("POST", self.base_url + "/v2/orders", json=payload)

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", self.base_url + f"/v2/orders/{order_id}")

    # -- Markt-Daten -------------------------------------------------------

    def get_daily_bars(self, symbols: Iterable[str], start: date, end: Optional[date] = None) -> Dict[str, List[Bar]]:
        """Tageskerzen (feed=iex, adjustment=all) mit Pagination. Datum = New-York-Datum."""
        symbols = list(dict.fromkeys(symbols))
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start.isoformat(),
            "adjustment": "all",
            "feed": "iex",
            "limit": 10000,
            "sort": "asc",
        }
        if end:
            params["end"] = end.isoformat()
        out: Dict[str, Dict[date, Bar]] = {s: {} for s in symbols}
        token = None
        for _ in range(1000):
            if token:
                params["page_token"] = token
            else:
                params.pop("page_token", None)
            data = self._request("GET", self.data_url + "/v2/stocks/bars", params=dict(params)) or {}
            for sym, rows in (data.get("bars") or {}).items():
                bucket = out.setdefault(sym, {})
                for row in rows or []:
                    d = ny_date(row["t"])
                    bucket[d] = Bar(d, float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]))
            token = data.get("next_page_token")
            if not token:
                break
        return {s: [bucket[d] for d in sorted(bucket)] for s, bucket in out.items()}
