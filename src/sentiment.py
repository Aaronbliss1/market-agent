"""Market sentiment: Fear & Greed index + total market-cap momentum +
average perpetual funding rate (crowdedness).

Free sources:
  - https://api.alternative.me/fng/          (Fear & Greed, no key)
  - https://api.coingecko.com/api/v3/global  (total mcap 24h change, no key)
  - funding rates come from exchange ticker data (Bitget USDT-FUTURES)

Interpretation (contrarian at extremes, momentum in the middle):
  F&G <= 20 extreme fear -> long bias ; F&G >= 80 extreme greed -> short bias
  avg funding >= +0.05%  -> longs crowded  -> short bias
  avg funding <= -0.05%  -> shorts crowded -> long bias
  mcap change >= +4%/<=-4% 24h -> follows market momentum
Clamped to +/-10.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("sentiment")


class MarketSentiment:
    def __init__(self):
        self._cache: dict | None = None
        self._cache_ts = 0.0

    def fetch(self, funding_rates: dict[str, float] | None = None,
              force: bool = False) -> dict:
        now = time.time()
        if not force and self._cache is not None and now - self._cache_ts < 1800:
            return self._cache

        fng = None
        fng_label = None
        try:
            d = requests.get("https://api.alternative.me/fng/?limit=1",
                             timeout=12).json()
            fng = int(d["data"][0]["value"])
            fng_label = d["data"][0]["value_classification"]
        except Exception as e:
            log.debug("fng fetch failed: %s", e)

        mcap_chg = None
        try:
            g = requests.get("https://api.coingecko.com/api/v3/global",
                             timeout=12).json()
            mcap_chg = g.get("data", {}).get("market_cap_change_percentage_24h_usd")
        except Exception as e:
            log.debug("coingecko global failed: %s", e)

        avg_funding = None
        if funding_rates:
            vals = [v for v in funding_rates.values() if v is not None]
            if vals:
                avg_funding = sum(vals) / len(vals)

        score = 0.0
        parts: list[str] = []
        if fng is not None:
            parts.append(f"F&G {fng} ({fng_label})")
            if fng <= 20:
                score += 8
            elif fng <= 40:
                score += 3
            elif fng >= 80:
                score -= 8
            elif fng >= 60:
                score -= 3
        if mcap_chg is not None:
            parts.append(f"mkt cap {mcap_chg:+.1f}%/24h")
            if mcap_chg >= 4:
                score += 4
            elif mcap_chg <= -4:
                score -= 4
        if avg_funding is not None:
            fpct = avg_funding * 100
            parts.append(f"funding {fpct:+.3f}%")
            if fpct >= 0.05:
                score -= 8
            elif fpct >= 0.02:
                score -= 3
            elif fpct <= -0.05:
                score += 8
            elif fpct <= -0.02:
                score += 3
        score = max(-10.0, min(10.0, score))

        ctx = {
            "fng": fng, "fng_label": fng_label,
            "mcap_chg": mcap_chg, "avg_funding": avg_funding,
            "score": score, "parts": parts,
        }
        self._cache = ctx
        self._cache_ts = now
        return ctx

    @staticmethod
    def summary(ctx: dict) -> str:
        if not ctx or not ctx.get("parts"):
            return "market context unavailable"
        tilt = ""
        if ctx["score"] >= 4:
            tilt = " — sentiment tilts LONG"
        elif ctx["score"] <= -4:
            tilt = " — sentiment tilts SHORT"
        elif ctx["score"] != 0:
            tilt = f" — mild {'long' if ctx['score'] > 0 else 'short'} tilt ({ctx['score']:+.0f})"
        return " · ".join(ctx["parts"]) + tilt
