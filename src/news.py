"""News scanning via public crypto RSS feeds (no API key needed).

Matches headlines to a ticker/name, scores them with a small keyword lexicon.
"""
from __future__ import annotations

import calendar
import logging
import re
import time

import feedparser
import requests

log = logging.getLogger("news")

FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("The Block", "https://www.theblock.co/rss.xml"),
]

UA = {"User-Agent": "Mozilla/5.0 market-agent/1.0 (news scanner)"}

# weight 2 = strong signal, weight 1 = normal
POSITIVE = {
    "surge": 1, "surges": 1, "rally": 1, "rallies": 1, "adoption": 2,
    "partnership": 2, "listing": 2, "lists": 1, "approval": 2, "approves": 2,
    "integrate": 1, "integration": 1, "burn": 1, "buyback": 1, "upgrade": 1,
    "halving": 1, "inflow": 1, "inflows": 1, "record": 1, "all-time high": 2,
    "launch": 1, "expansion": 1, "institutional": 2, "invests": 1, "raises": 1,
    "etf": 2, "accumulat": 1, "growth": 1, "milestone": 1, "win": 1, "wins": 1,
    "beat": 1, "bullish": 1, "soars": 1, "jumps": 1, "climbs": 1,
}
NEGATIVE = {
    "hack": 2, "hacked": 2, "exploit": 2, "exploited": 2, "lawsuit": 1,
    "sues": 1, "sued": 1, "delist": 2, "delisting": 2, "dump": 1, "crash": 2,
    "crashes": 2, "plunge": 1, "plunges": 1, "bankrupt": 2, "bankruptcy": 2,
    "fraud": 2, "scam": 1, "rug": 1, "unlock": 1, "selloff": 1, "sell-off": 1,
    "outflow": 1, "outflows": 1, "liquidation": 1, "ban": 1, "bans": 1,
    "investigation": 1, "fine": 1, "fined": 1, "penalty": 1, "drops": 1,
    "falls": 1, "decline": 1, "fear": 1, "steal": 1, "stolen": 1, "theft": 2,
    "arrest": 2, "bearish": 1, "slump": 1, "tumble": 1, "tumbles": 1,
}


# Tickers that are common English words: word-boundary matching on the ticker
# alone would grab headlines that just happen to contain "The", "Gas", etc.
# For these, the ticker is not matched on its own (name matching still works).
STOPWORD_TICKERS = {
    "THE", "AND", "ARE", "FOR", "ALL", "CAN", "NEW", "OLD", "ONE", "OUR",
    "OUT", "SUN", "GAS", "OIL", "FUN", "APE", "ICE", "NOT", "NOW", "HOW",
}


def sentiment(title: str) -> tuple[float, list[str]]:
    t = title.lower()
    score = 0.0
    matched: list[str] = []
    for w, wt in POSITIVE.items():
        if w in t:
            score += wt
            matched.append(f"+{w}")
    for w, wt in NEGATIVE.items():
        if w in t:
            score -= wt
            matched.append(f"-{w}")
    clamped = max(-1.0, min(1.0, score / 3.0))
    return clamped, matched


class NewsService:
    def __init__(self):
        self._items: list[dict] | None = None
        self._cache_ts = 0.0

    def fetch(self, hours: int = 24, force: bool = False) -> list[dict]:
        now = time.time()
        if not force and self._items is not None and now - self._cache_ts < 1800:
            return self._items
        items: list[dict] = []
        for source, url in FEEDS:
            try:
                r = requests.get(url, headers=UA, timeout=20)
                feed = feedparser.parse(r.content)
                for e in feed.entries[:60]:
                    tp = e.get("published_parsed") or e.get("updated_parsed")
                    ts = calendar.timegm(tp) if tp else now
                    if now - ts > hours * 3600:
                        continue
                    items.append({
                        "ts": ts,
                        "title": (e.get("title") or "").strip(),
                        "source": source,
                        "url": e.get("link", ""),
                    })
            except Exception as e:
                log.debug("feed %s failed: %s", source, e)
        self._items = items
        self._cache_ts = now
        log.info("news: %d headlines in last %dh", len(items), hours)
        return items

    def for_token(self, ticker: str, name: str | None = None, hours: int = 24) -> list[dict]:
        """Headlines mentioning the ticker (or token name), with sentiment."""
        items = self.fetch(hours)
        out = []
        tick = ticker.upper()
        rx = None
        if len(tick) >= 3 and tick not in STOPWORD_TICKERS:
            rx = re.compile(r"(?<![A-Z0-9])" + re.escape(tick) + r"(?![A-Z0-9])")
        for it in items:
            title = it["title"]
            up = title.upper()
            hit = (rx.search(up) if rx else None) or (name and name.upper() in up)
            if not hit:
                continue
            s, matched = sentiment(title)
            out.append({**it, "sentiment": s, "matched": matched})
        out.sort(key=lambda x: x["ts"], reverse=True)
        return out
