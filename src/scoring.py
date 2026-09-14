"""Composite scoring: on-chain flow + news + technicals (incl. SMC) +
fundamentals + market sentiment + mover bonus -> trade calls.

Signal budget (total range ~ +/-130, confidence = min(100, |total|)):
  technicals (incl. SMC) : +/-40  (RSI 1h, MACD 4h + crosses, EMA stack, volume,
                                       BOS/CHoCH, order blocks, FVG, sweeps,
                                       premium/discount)
  on-chain               : +/-40  (net tracked-wallet flow vs 24h vol, decayed)
  news                   : +/-20  (keyword sentiment of fresh headlines)
  fundamentals           : +/-10  (turnover, ATH distance, mcap tier)
  market sentiment       : +/-10  (Fear&Greed, mcap momentum, funding)
  mover bonus            : +10    (token making a move, momentum w/ the move)

Sign: + = long bias, - = short bias. A token is called when
|total| >= MIN_CONFIDENCE (default 35).

Risk model ("calculated risk"):
  stop   = 1.5 x ATR(14) on 4h, clamped 2.5%..12%
  target = stop x R, R = 2.0..3.5 (scales with confidence), capped at 60%
  leverage = confidence-scaled 3..30x, reduced so that (a) expected return
             never exceeds MAX_TARGET_PCT (500%), (b) liquidation buffer stays
             beyond the stop, (c) micro-caps capped at 10x
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from .fundamentals import FundamentalsService
from .indicators import atr, ema, macd, rsi
from .onchain import Flow
from .smc import analyze_smc

log = logging.getLogger("scoring")


def tech_signals(k1: dict, k4: dict) -> dict:
    """Technical analysis incl. SMC. k1 = 1h candles, k4 = 4h candles."""
    notes: list[str] = []
    score = 0.0
    c1, c4 = k1["c"], k4["c"]

    # insufficient history (fresh listing / thin series): degrade to a neutral
    # signal instead of crashing the MACD/RSI indexing — the token simply
    # won't earn a technical score.
    if len(c4) < 35 or len(c1) < 20:
        return {"score": 0.0,
                "notes": [f"Insufficient candle history ({len(c4)} 4h bars) — no technical signal"],
                "rsi": float("nan"), "atr_pct": 0.0,
                "close": float(c4[-1]) if len(c4) else 0.0,
                "smc": analyze_smc(k4)}

    # --- RSI (1h) ---
    r = float(rsi(c1, 14)[-1])
    if r <= 25:
        score += 8
        notes.append(f"RSI(14) 1h = {r:.0f} — deeply oversold (mean-reversion long bias)")
    elif r <= 35:
        score += 5
        notes.append(f"RSI(14) 1h = {r:.0f} — oversold zone")
    elif r >= 75:
        score -= 8
        notes.append(f"RSI(14) 1h = {r:.0f} — deeply overbought (mean-reversion short bias)")
    elif r >= 65:
        score -= 5
        notes.append(f"RSI(14) 1h = {r:.0f} — overbought zone")
    else:
        notes.append(f"RSI(14) 1h = {r:.0f} — neutral")

    # --- MACD (4h) ---
    line, sig, hist = macd(c4)
    hv, prev = float(hist[-1]), float(hist[-2])
    if hv > 0:
        score += 8 if hv > prev else 5
        notes.append("MACD(12,26,9) 4h positive" + (" and rising" if hv > prev else ""))
    else:
        score += -8 if hv < prev else -5
        notes.append("MACD(12,26,9) 4h negative" + (" and falling" if hv < prev else ""))
    if not np.any(np.isnan(hist[-4:])):
        signs = np.sign(hist[-4:])
        if signs[-1] > 0 and np.any(signs[:-1] < 0):
            score += 5
            notes.append("MACD crossed bullish within the last 3 4h bars")
        elif signs[-1] < 0 and np.any(signs[:-1] > 0):
            score -= 5
            notes.append("MACD crossed bearish within the last 3 4h bars")

    # --- Trend: EMA stack (4h) ---
    close = float(c4[-1])
    e20, e50 = float(ema(c4, 20)[-1]), float(ema(c4, 50)[-1])
    e200 = float(ema(c4, 200)[-1])
    trend = 0
    trend += 3 if close > e20 else -3
    trend += 3 if close > e50 else -3
    trend += 2 if e20 > e50 else -2
    if not np.isnan(e200):
        trend += 2 if close > e200 else -2
    trend = max(-10, min(10, trend))
    score += trend
    e200_txt = f"EMA200 {'above' if close > e200 else 'below'}" if not np.isnan(e200) else "no EMA200 history"
    notes.append(
        f"Price {'above' if close > e20 else 'below'} EMA20, "
        f"{'above' if close > e50 else 'below'} EMA50 (4h), "
        f"EMA20/50 {'rising' if e20 > e50 else 'falling'}, {e200_txt}")

    # --- Volume expansion (4h) ---
    v = k4["v"]
    if len(v) >= 30:
        ratio = float(v[-6:].mean() / max(v[-30:].mean(), 1e-9))
        chg = (float(c4[-1]) / float(c4[-7]) - 1) * 100 if len(c4) > 7 else 0.0
        if ratio > 1.3:
            if chg > 0:
                score += 4
                notes.append(f"Volume expanding {ratio:.1f}x average while price rises (+{chg:.1f}% / 24h)")
            else:
                score -= 4
                notes.append(f"Volume expanding {ratio:.1f}x average while price falls ({chg:.1f}% / 24h)")
        elif ratio < 0.6:
            notes.append(f"Volume fading ({ratio:.1f}x of 30-bar average)")

    # --- SMC (4h) ---
    smc = analyze_smc(k4)
    score += smc["score"]

    score = max(-40.0, min(40.0, score))
    a = float(atr(k4["h"], k4["l"], c4, 14)[-1])
    atr_pct = a / close * 100 if close > 0 else 0.0
    return {"score": score, "notes": notes, "rsi": r, "atr_pct": atr_pct,
            "close": close, "smc": smc}


def onchain_score(flows: list[Flow], vol24h_usd: float, now: float) -> tuple[float, str, list[Flow]]:
    """Net tracked-wallet flow vs the token's 24h volume, time-decayed.

    VC wallets:   token inflow  = accumulation (bullish), outflow = distribution (bearish)
    CEX wallets:  deposit inflow = mild bearish (supply to sell), withdrawal = mild bullish
    """
    if not flows:
        return 0.0, "No tracked-wallet (VC/CEX) activity in the scan window", []
    net = 0.0
    by_entity: dict[str, float] = {}
    counted = 0
    for f in flows:
        if f.usd < 20_000:
            continue
        counted += 1
        age_h = (now - f.ts) / 3600
        w = 1.0 if age_h < 2 else 0.6 if age_h < 6 else 0.3 if age_h < 24 else 0.1
        sign = 1.0 if f.direction == "in" else -1.0
        weight = 1.0
        if f.entity_type == "cex":
            sign = -sign      # deposit = bearish, withdrawal = bullish
            weight = 0.5
        contrib = w * sign * weight * f.usd
        net += contrib
        by_entity[f.entity_name] = by_entity.get(f.entity_name, 0.0) + contrib
    if counted == 0:
        return 0.0, "Tracked-wallet activity too small (< $20k) to matter", []
    ratio = net / max(vol24h_usd, 500_000)
    score = max(-40.0, min(40.0, ratio * 250))
    top = sorted(by_entity.items(), key=lambda kv: abs(kv[1]), reverse=True)[:3]
    det = "; ".join(f"{name} {'+' if v2 > 0 else '-'}${abs(v2) / 1e6:.2f}M" for name, v2 in top)
    if abs(ratio) > 0.005:
        direction_word = "INFLOW INTO" if net > 0 else "OUTFLOW FROM"
        note = (f"Net ${abs(net) / 1e6:.2f}M {direction_word} tracked wallets "
                f"({abs(ratio) * 100:.1f}% of 24h volume): {det}")
    else:
        note = f"Tracked-wallet activity is small vs volume (net ${net / 1e6:+.2f}M): {det}"
    return score, note, flows


def news_score(items: list[dict], now: float) -> tuple[float, list[dict]]:
    if not items:
        return 0.0, []
    total = 0.0
    shown = []
    for it in items[:5]:
        age_h = max(0.0, (now - it["ts"]) / 3600)
        w = 1.5 if age_h < 6 else 1.2 if age_h < 12 else 1.0
        total += it["sentiment"] * w
        shown.append({**it, "age_h": age_h})
    score = max(-20.0, min(20.0, total * 8))
    return score, shown


@dataclass
class CallSpec:
    ticker: str
    exchange: str
    exchanges: list[str]
    direction: str          # "LONG" | "SHORT"
    entry: float
    stop: float
    target: float
    leverage: int
    target_pct: float       # price move % to target
    target_roi: float       # expected return on margin %
    confidence: float       # 0..100
    vol24h: float
    atr_pct: float
    tech: dict
    onchain_note: str
    onchain_flows: list[Flow]
    news: list[dict]
    ts: float
    fund_note: str = ""
    fund_score: float = 0.0
    sentiment: dict = field(default_factory=dict)
    is_mover: bool = False
    mover_up: bool = True


def build_call(ticker: str,
               exs: dict[str, dict],
               k1: dict,
               k4: dict,
               flows: list[Flow],
               news_items: list[dict],
               cfg,
               now: float,
               entry_price: float | None = None,
               fund: dict | None = None,
               sentiment: dict | None = None,
               is_mover: bool = False,
               mover_up: bool = True) -> CallSpec | None:
    tech = tech_signals(k1, k4)
    vol24h = max(i.get("usd_volume", 0) for i in exs.values()) if exs else 0.0
    oscore, onote, oflows = onchain_score(flows, vol24h, now)
    nscore, nshown = news_score(news_items, now)
    fscore, fnote = FundamentalsService.score(fund)
    sscore = float(sentiment.get("score", 0)) if sentiment else 0.0

    # direction from the FULL composite (tech + onchain + news + fundamentals +
    # sentiment) — keeps the book balanced long/short with the market, not just
    # price action
    base = tech["score"] + oscore + nscore + fscore + sscore
    direction = "LONG" if base >= 0 else "SHORT"
    mover_bonus = 10.0 if (is_mover and ((direction == "LONG") == mover_up)) else 0.0
    total = base + mover_bonus

    if abs(total) < cfg.min_confidence:
        return None

    # anti-chase guard: when the call comes from a fresh mover (already moved
    # >= MOVER_24H_PCT% in 24h), never enter long into an overbought 1h RSI
    # (or short into oversold) — that's the top/bottom of the impulse, where
    # the easy money was already made. Movers get the momentum bonus instead;
    # quiet setups are unaffected.
    if is_mover:
        if direction == "LONG" and tech["rsi"] >= 65:
            log.info("%s: mover but 1h RSI %.0f overbought — not chasing long",
                     ticker, tech["rsi"])
            return None
        if direction == "SHORT" and tech["rsi"] <= 35:
            log.info("%s: mover but 1h RSI %.0f oversold — not chasing short",
                     ticker, tech["rsi"])
            return None

    conf = min(100.0, abs(total))

    # --- calculated risk ---
    stop_pct = max(2.5, min(12.0, tech["atr_pct"] * 1.5))
    R = 2.0 + (conf / 100.0) * 1.5                      # 2.0R .. 3.5R
    target_pct = max(2.0, min(60.0, stop_pct * R))      # price move %
    entry = entry_price if entry_price and entry_price > 0 else tech["close"]

    lev_by_conf = 3 + int(round(conf / 100.0 * (cfg.max_leverage - 3)))
    lev_by_target = int(cfg.max_target_pct // target_pct) if target_pct > 0 else cfg.max_leverage
    lev_by_stop = int(100.0 / (stop_pct * 2.5))        # ~40% max loss at stop,
                                                       # liquidation well beyond it
    leverage = max(2, min(cfg.max_leverage, lev_by_conf, lev_by_target, lev_by_stop))
    if FundamentalsService.microcap(fund):
        leverage = min(leverage, 10)                    # micro-cap safety cap
    target_roi = min(float(cfg.max_target_pct), target_pct * leverage)

    if direction == "LONG":
        stop = entry * (1 - stop_pct / 100)
        target = entry * (1 + target_pct / 100)
    else:
        stop = entry * (1 + stop_pct / 100)
        target = entry * (1 - target_pct / 100)

    exchanges = sorted(exs.keys())
    primary = max(exs.items(), key=lambda kv: kv[1].get("usd_volume", 0))[0]

    return CallSpec(
        ticker=ticker, exchange=primary, exchanges=exchanges, direction=direction,
        entry=entry, stop=stop, target=target, leverage=leverage,
        target_pct=target_pct, target_roi=target_roi, confidence=conf,
        vol24h=vol24h, atr_pct=tech["atr_pct"], tech=tech,
        onchain_note=onote, onchain_flows=oflows, news=nshown, ts=now,
        fund_note=fnote, fund_score=fscore, sentiment=sentiment or {},
        is_mover=is_mover, mover_up=mover_up,
    )
