"""Public market data for Binance, Bybit and Bitget (USDT-quoted perps / spot).

All three are optional at runtime: if an exchange API is unreachable (e.g. geo
blocking on the current host) the agent continues with the remaining ones.
"""
from __future__ import annotations

import logging
import time

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("exchanges")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) market-agent/1.0"}

# Stablecoins, wrapped assets and liquid staking tokens are not trade targets.
EXCLUDED_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDS", "USDE", "USDP", "GUSD",
    "PYUSD", "USDBC", "WBTC", "CBBTC", "WBETH", "WETH", "STETH", "WSTETH",
    "RETH", "EZETH", "BUSD", "EURC", "AEUR", "XUSD", "USDF", "SUSD", "FRAX",
    "LUSD", "GHO", "CRVUSD", "DEUSD", "DOLA", "MIM", "CBBTC", "CBEETH",
    "WEETH", "METH", "RBETH", "SUSDS", "BNSOL", "SOLVBTC", "JITOSOL",
    "MSOL", "BSONA", "USDF", "USDE", "USD0", "USD1",
}


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(UA)
    return s


class Exchange:
    key: str = ""

    def __init__(self, session: requests.Session):
        self.s = session
        self.ok = True
        self.err = ""

    def tickers(self) -> dict[str, dict]:
        raise NotImplementedError

    def klines(self, ticker: str, interval: str, limit: int):
        raise NotImplementedError

    def price(self, ticker: str) -> float | None:
        raise NotImplementedError


class BinanceExchange(Exchange):
    """Spot market data. Prefers data-api.binance.vision (global public mirror),
    falls back to api.binance.com."""
    key = "binance"
    BASES = ["https://data-api.binance.vision", "https://api.binance.com"]

    def __init__(self, session: requests.Session):
        super().__init__(session)
        self.base = None
        for b in self.BASES:
            try:
                if session.get(b + "/api/v3/ping", timeout=8).status_code == 200:
                    self.base = b
                    return
            except Exception:
                continue
        self.ok = False
        self.err = "all Binance endpoints unreachable"
        log.warning("Binance unavailable: %s", self.err)

    def tickers(self) -> dict[str, dict]:
        d = self.s.get(self.base + "/api/v3/ticker/24hr", timeout=20).json()
        out: dict[str, dict] = {}
        for row in d:
            sym = row.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in EXCLUDED_BASES:
                continue
            try:
                qv = float(row.get("quoteVolume") or 0)
                if qv <= 0:
                    continue
                out[base] = {
                    "last": float(row["lastPrice"]), "usd_volume": qv,
                    "chg24h": float(row.get("priceChangePercent") or 0),
                    "funding": None,
                }
            except Exception:
                pass
        return out

    def klines(self, ticker: str, interval: str, limit: int):
        r = self.s.get(self.base + "/api/v3/klines",
                       params={"symbol": ticker + "USDT", "interval": interval, "limit": limit},
                       timeout=20).json()
        arr = np.array(r, dtype=float)
        return {"t": arr[:, 0] / 1000.0, "o": arr[:, 1], "h": arr[:, 2],
                "l": arr[:, 3], "c": arr[:, 4], "v": arr[:, 5]}

    def price(self, ticker: str) -> float | None:
        d = self.s.get(self.base + "/api/v3/ticker/price",
                       params={"symbol": ticker + "USDT"}, timeout=10).json()
        return float(d["price"])


class BybitExchange(Exchange):
    key = "bybit"
    base = "https://api.bybit.com"
    INTERVALS = {"1h": "60", "4h": "240"}

    def tickers(self) -> dict[str, dict]:
        d = self.s.get(self.base + "/v5/market/tickers",
                       params={"category": "linear"}, timeout=20).json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"bybit tickers error: {d.get('retMsg')}")
        out: dict[str, dict] = {}
        for row in d["result"]["list"]:
            sym = row.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in EXCLUDED_BASES:
                continue
            try:
                fr = row.get("fundingRate")
                out[base] = {
                    "last": float(row["lastPrice"]),
                    "usd_volume": float(row.get("turnover24h") or 0),
                    "chg24h": float(row.get("price24h") or 0) * 100,
                    "funding": float(fr) if fr not in (None, "") else None,
                }
            except Exception:
                pass
        return out

    def klines(self, ticker: str, interval: str, limit: int):
        d = self.s.get(self.base + "/v5/market/kline",
                       params={"category": "linear", "symbol": ticker + "USDT",
                               "interval": self.INTERVALS[interval], "limit": limit},
                       timeout=20).json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"bybit kline error: {d.get('retMsg')}")
        rows = list(reversed(d["result"]["list"]))  # API returns newest first
        arr = np.array(rows, dtype=float)
        return {"t": arr[:, 0] / 1000.0, "o": arr[:, 1], "h": arr[:, 2],
                "l": arr[:, 3], "c": arr[:, 4], "v": arr[:, 5]}

    def price(self, ticker: str) -> float | None:
        d = self.s.get(self.base + "/v5/market/tickers",
                       params={"category": "linear", "symbol": ticker + "USDT"},
                       timeout=10).json()
        return float(d["result"]["list"][0]["lastPrice"])


class BitgetExchange(Exchange):
    key = "bitget"
    base = "https://api.bitget.com"
    INTERVALS = {"1h": "1H", "4h": "4H"}

    def tickers(self) -> dict[str, dict]:
        d = self.s.get(self.base + "/api/v2/mix/market/tickers",
                       params={"productType": "USDT-FUTURES"}, timeout=20).json()
        if d.get("code") != "00000":
            raise RuntimeError(f"bitget tickers error: {d.get('msg')}")
        out: dict[str, dict] = {}
        for row in d["data"]:
            sym = row.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in EXCLUDED_BASES:
                continue
            try:
                fr = row.get("fundingRate")
                out[base] = {
                    "last": float(row["lastPr"]),
                    "usd_volume": float(row.get("usdtVolume") or 0),
                    "chg24h": float(row.get("change24h") or 0) * 100,
                    "funding": float(fr) if fr not in (None, "") else None,
                }
            except Exception:
                pass
        return out

    def klines(self, ticker: str, interval: str, limit: int):
        d = self.s.get(self.base + "/api/v2/mix/market/candles",
                       params={"symbol": ticker + "USDT",
                               "granularity": self.INTERVALS[interval],
                               "productType": "USDT-FUTURES", "limit": limit},
                       timeout=20).json()
        if d.get("code") != "00000":
            raise RuntimeError(f"bitget klines error: {d.get('msg')}")
        arr = np.array(sorted(d["data"], key=lambda r: int(r[0])), dtype=float)
        return {"t": arr[:, 0] / 1000.0, "o": arr[:, 1], "h": arr[:, 2],
                "l": arr[:, 3], "c": arr[:, 4], "v": arr[:, 5]}

    def price(self, ticker: str) -> float | None:
        d = self.s.get(self.base + "/api/v2/mix/market/tickers",
                       params={"productType": "USDT-FUTURES", "symbol": ticker + "USDT"},
                       timeout=10).json()
        return float(d["data"][0]["lastPr"])


class ExchangeRegistry:
    """Aggregates the three exchanges; one being down never breaks the agent."""

    def __init__(self):
        s = _session()
        self.exchanges: dict[str, Exchange] = {
            "binance": BinanceExchange(s),
            "bybit": BybitExchange(s),
            "bitget": BitgetExchange(s),
        }
        self._sym_cache: dict[str, dict] = {}
        self._sym_cache_ts = 0.0

    def status(self) -> dict[str, tuple[bool, str]]:
        return {k: (v.ok, v.err) for k, v in self.exchanges.items()}

    def symbols(self, max_age: float = 300.0) -> dict[str, dict[str, dict]]:
        """ticker -> {exchange: {last, usd_volume}} for every USDT pair."""
        if time.time() - self._sym_cache_ts < max_age and self._sym_cache:
            return self._sym_cache
        merged: dict[str, dict[str, dict]] = {}
        for key, ex in self.exchanges.items():
            if not ex.ok:
                continue
            try:
                t = ex.tickers()
            except Exception as e:
                ex.ok = False
                ex.err = str(e)
                log.warning("exchange %s tickers failed: %s", key, e)
                continue
            for base, info in t.items():
                merged.setdefault(base, {})[key] = info
        if merged:
            self._sym_cache = merged
            self._sym_cache_ts = time.time()
        return merged

    @staticmethod
    def primary(exs: dict[str, dict]) -> str | None:
        """Exchange with the biggest 24h USDT volume for a ticker."""
        if not exs:
            return None
        return max(exs.items(), key=lambda kv: kv[1].get("usd_volume", 0))[0]

    def klines(self, exchange: str, ticker: str, interval: str, limit: int):
        ex = self.exchanges.get(exchange)
        if ex is None or not ex.ok:
            return None
        try:
            return ex.klines(ticker, interval, limit)
        except Exception as e:
            log.debug("klines %s %s %s failed: %s", exchange, ticker, interval, e)
            return None

    def price(self, exchange: str, ticker: str) -> float | None:
        ex = self.exchanges.get(exchange)
        if ex is None or not ex.ok:
            return None
        try:
            return ex.price(ticker)
        except Exception as e:
            log.debug("price %s %s failed: %s", exchange, ticker, e)
            return None
