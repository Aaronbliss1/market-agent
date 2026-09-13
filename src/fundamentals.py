"""Token fundamentals from CoinGecko (free, no key, 1 call / 30 min).

One /coins/markets call returns market cap, volume, ATH and ATH distance for
the top 250 tokens by volume — plenty to cover everything we scan.

Score (clamped +/-10):
  turnover = 24h vol / mcap:  <0.02 illiquid (-4), >=0.05 healthy (+2)
  ATH distance:               near ATH (+2), >75% below ATH (-2)
  mcap tier:                  < $20M micro-cap risk (-2), >= $2B large-cap (+1)
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("fundamentals")

URL = "https://api.coingecko.com/api/v3/coins/markets"
PARAMS = {
    "vs_currency": "usd",
    "order": "volume_desc",
    "per_page": 250,
    "page": 1,
    "sparkline": "false",
    "price_change_percentage": "24h",
}


class FundamentalsService:
    def __init__(self):
        self._map: dict[str, dict] | None = None
        self._cache_ts = 0.0

    def fetch(self, force: bool = False) -> dict[str, dict]:
        now = time.time()
        if not force and self._map is not None and now - self._cache_ts < 1800:
            return self._map
        try:
            rows = requests.get(URL, params=PARAMS, timeout=25).json()
            m: dict[str, dict] = {}
            for c in rows:
                sym = (c.get("symbol") or "").upper()
                if not sym:
                    continue
                m[sym] = {
                    "mcap": c.get("market_cap"),
                    "vol": c.get("total_volume"),
                    "ath": c.get("ath"),
                    "ath_pct": c.get("ath_change_percentage"),
                }
            self._map = m
            self._cache_ts = now
            log.info("fundamentals: %d tokens loaded", len(m))
        except Exception as e:
            log.warning("fundamentals fetch failed: %s", e)
            if self._map is None:
                self._map = {}
                self._cache_ts = now
        return self._map

    def for_token(self, ticker: str) -> dict | None:
        self.fetch()
        m = self._map or {}
        t = ticker.upper()
        if t in m:
            return m[t]
        if t.startswith("1000") and t[4:] in m:  # 1000SHIB -> SHIB
            return {**m[t[4:]], "scaled": True}
        return None

    @staticmethod
    def score(d: dict | None) -> tuple[float, str]:
        if not d:
            return 0.0, "not in CoinGecko top-250"
        mcap = d.get("mcap") or 0
        vol = d.get("vol") or 0
        s = 0.0
        notes: list[str] = []
        if mcap >= 1e9:
            notes.append(f"mcap ${mcap / 1e9:.2f}B")
        elif mcap >= 1e6:
            notes.append(f"mcap ${mcap / 1e6:.0f}M")
        else:
            notes.append(f"mcap ${mcap / 1e3:.0f}K")
        if mcap and vol:
            to = vol / mcap
            if to < 0.02:
                s -= 4
                notes.append(f"turnover {to:.2f} (illiquid)")
            elif to >= 0.05:
                s += 2
                notes.append(f"turnover {to:.2f} (healthy)")
            else:
                notes.append(f"turnover {to:.2f}")
        if d.get("ath_pct") is not None:
            a = d["ath_pct"]
            if a > -3:
                s += 2
                notes.append("near all-time high")
            elif a < -75:
                s -= 2
                notes.append(f"{abs(a):.0f}% below ATH (broken chart)")
            else:
                notes.append(f"{abs(a):.0f}% below ATH")
        if mcap and mcap < 20e6:
            s -= 2
            notes.append("micro-cap risk")
        elif mcap and mcap >= 2e9:
            s += 1
            notes.append("large-cap")
        s = max(-10.0, min(10.0, s))
        return s, " | ".join(notes)

    @staticmethod
    def microcap(d: dict | None) -> bool:
        return bool(d and (d.get("mcap") or 0) < 20e6)
