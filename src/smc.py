"""Smart Money Concepts (SMC) on 4h candles.

Detected concepts (all from raw OHLC, no external data):
  - Swing structure & trend (HH/HL vs LH/LL)
  - BOS  (Break of Structure): close breaks the last opposing swing
  - CHoCH (Change of Character): first structural break against the trend
  - Order blocks: last opposite candle before an impulsive break;
    tracked as "unmitigated" until price returns into the zone
  - FVG (Fair Value Gap): 3-candle imbalance, tracked while unfilled
  - Liquidity sweeps: wick through a recent extreme that closes back inside
  - Premium / discount: position inside the current dealing range

Score (clamped +/-15, positive = bullish evidence, negative = bearish):
  BOS with trend           +/-5
  CHoCH (reversal signal)  +/-6
  price at unmitigated OB  +/-4
  recent opposite sweep    +/-4
  unfilled FVG nearby      +/-2
  discount/premium         +/-2
Chart marks are returned for drawing on the chart picture.
"""
from __future__ import annotations

import numpy as np

from .indicators import atr as _atr


def _ago(bars: int) -> str:
    if bars <= 0:
        return "this 4h bar"
    return "1 4h bar ago" if bars == 1 else f"{bars} 4h bars ago"


def _swing_highs(h: np.ndarray, k: int = 2) -> list[int]:
    out = []
    n = len(h)
    for i in range(k, n - k):
        if h[i] >= h[i - k:i + k + 1].max():
            out.append(i)
    return out


def _swing_lows(l: np.ndarray, k: int = 2) -> list[int]:
    out = []
    n = len(l)
    for i in range(k, n - k):
        if l[i] <= l[i - k:i + k + 1].min():
            out.append(i)
    return out


def analyze_smc(k4: dict) -> dict:
    o, h, l, c = k4["o"], k4["h"], k4["l"], k4["c"]
    n = len(c)
    res = {"score": 0.0, "notes": [], "marks": [], "trend": "range",
           "bos": None, "ob_zones": [], "fvg_zones": [], "sweeps": []}
    if n < 50:
        return res
    a = float(_atr(h, l, c, 14)[-1])
    if not np.isfinite(a) or a <= 0:
        return res

    sh = _swing_highs(h)
    sl = _swing_lows(l)

    # ---- trend from the last two swings of each kind ----
    trend = "range"
    if len(sh) >= 2 and len(sl) >= 2:
        hh = h[sh[-1]] > h[sh[-2]]
        hl = l[sl[-1]] > l[sl[-2]]
        if hh and hl:
            trend = "up"
        elif (not hh) and (not hl):
            trend = "down"
    res["trend"] = trend

    score = 0.0

    # ---- BOS / CHoCH: last close that broke the most recent opposing swing ----
    bos_i, bos_dir, is_choch = None, None, False
    for i in range(max(3, n - 10), n):
        # bullish break: previous close under the swing high, current close above
        cand = [j for j in sh if j < i - 1 and c[i - 1] <= h[j] < c[i]
                and c[i] > h[j] + 0.05 * a]
        if cand:
            j = max(cand)
            prev = [x for x in sh if x < j]
            lower_high = (not prev) or h[j] < h[prev[-1]]
            is_choch = lower_high and trend != "up"
            bos_i, bos_dir = i, "bull"
        cand = [j for j in sl if j < i - 1 and c[i - 1] >= l[j] > c[i]
                and c[i] < l[j] - 0.05 * a]
        if cand:
            j = max(cand)
            prev = [x for x in sl if x < j]
            higher_low = (not prev) or l[j] > l[prev[-1]]
            is_choch = higher_low and trend != "down"
            bos_i, bos_dir = i, "bear"

    if bos_dir:
        sgn = 1.0 if bos_dir == "bull" else -1.0
        side = "bullish" if sgn > 0 else "bearish"
        if is_choch:
            score += 6 * sgn
            res["notes"].append(
                f"CHoCH — {side} change of character, {_ago(n - 1 - bos_i)}")
        else:
            score += 5 * sgn
            res["notes"].append(
                f"BOS — {side} break of structure, {_ago(n - 1 - bos_i)}")
        res["bos"] = {"i": bos_i, "dir": bos_dir}
        res["marks"].append({"type": "bos", "i": bos_i, "dir": bos_dir,
                             "price": float(c[bos_i])})

    # ---- order block: last opposite candle before the impulsive break ----
    ob_zones = []
    if bos_dir:
        i0 = bos_i
        sgn = 1.0 if bos_dir == "bull" else -1.0
        ob_i = None
        for k in range(i0 - 1, max(0, i0 - 4), -1):
            if sgn > 0 and c[k] < o[k]:      # last bearish candle before up-impulse
                ob_i = k
                break
            if sgn < 0 and c[k] > o[k]:      # last bullish candle before down-impulse
                ob_i = k
                break
        if ob_i is not None:
            lo, hi = float(l[ob_i]), float(h[ob_i])
            mitigated = any((sgn > 0 and l[k2] <= hi) or
                            (sgn < 0 and h[k2] >= lo)
                            for k2 in range(i0 + 1, n))
            ob_zones.append({"i0": ob_i, "i1": n - 1, "lo": lo, "hi": hi,
                             "dir": bos_dir, "mitigated": mitigated})
            if not mitigated:
                near = (sgn > 0 and abs(c[-1] - hi) <= 0.75 * a and c[-1] >= lo) or \
                       (sgn < 0 and abs(c[-1] - lo) <= 0.75 * a and c[-1] <= hi)
                if near:
                    score += 4 * sgn
                    res["notes"].append(
                        f"Price at unmitigated {'bullish' if sgn > 0 else 'bearish'} "
                        f"order block ({lo:.6g}–{hi:.6g})")
                else:
                    res["notes"].append(
                        f"Unmitigated {'bullish' if sgn > 0 else 'bearish'} order block "
                        f"at {lo:.6g}–{hi:.6g}")
    res["ob_zones"] = ob_zones
    res["marks"].extend(
        {"type": "ob", **z} for z in ob_zones if not z["mitigated"])

    # ---- fair value gaps (last 20 bars), unfilled ----
    fvg_zones = []
    for i in range(max(2, n - 20), n - 1):
        if l[i] > h[i - 2]:  # bullish FVG: gap between candle i-2 high and i low
            lo_g, hi_g = float(h[i - 2]), float(l[i])
            filled = (i + 1 < n) and float(np.min(l[i + 1:])) <= lo_g
            fvg_zones.append({"i0": i, "i1": n - 1, "lo": lo_g, "hi": hi_g,
                              "dir": "bull", "filled": filled})
        if h[i] < l[i - 2]:  # bearish FVG
            lo_g, hi_g = float(h[i]), float(l[i - 2])
            filled = (i + 1 < n) and float(np.max(h[i + 1:])) >= hi_g
            fvg_zones.append({"i0": i, "i1": n - 1, "lo": lo_g, "hi": hi_g,
                              "dir": "bear", "filled": filled})
    open_fvgs = [z for z in fvg_zones if not z["filled"]]
    for z in open_fvgs:
        sgn = 1.0 if z["dir"] == "bull" else -1.0
        near = z["lo"] <= c[-1] <= z["hi"] or \
               (sgn > 0 and z["lo"] <= c[-1] + 1.5 * a) or \
               (sgn < 0 and z["hi"] >= c[-1] - 1.5 * a)
        if near:
            score += 2 * sgn
            res["notes"].append(
                f"Unfilled {'bullish' if sgn > 0 else 'bearish'} FVG nearby "
                f"({z['lo']:.6g}–{z['hi']:.6g})")
    res["fvg_zones"] = open_fvgs
    res["marks"].extend({"type": "fvg", **z} for z in open_fvgs[:3])

    # ---- liquidity sweeps of recent extremes (last 8 bars) ----
    sweeps = []
    for i in range(max(12, n - 8), n):
        prev_hi = float(h[i - 12:i].max())
        prev_lo = float(l[i - 12:i].min())
        if h[i] > prev_hi and c[i] < prev_hi:
            sgn = 1.0
            sweeps.append({"i": i, "dir": "bull", "price": float(h[i])})
            score += 4 * sgn
            res["notes"].append(
                f"Liquidity sweep of highs, {_ago(n - 1 - i)} — closed back inside "
                f"(buy-side liquidity taken, bullish)")
        if l[i] < prev_lo and c[i] > prev_lo:
            sgn = -1.0
            sweeps.append({"i": i, "dir": "bear", "price": float(l[i])})
            score += 4 * sgn
            res["notes"].append(
                f"Liquidity sweep of lows, {_ago(n - 1 - i)} — closed back inside "
                f"(sell-side liquidity taken, bearish)")
    res["sweeps"] = sweeps
    res["marks"].extend({"type": "sweep", **s} for s in sweeps)

    # ---- premium / discount inside the current dealing range ----
    rng_hi = float(h[-40:].max())
    rng_lo = float(l[-40:].min())
    if rng_hi > rng_lo:
        pos = (float(c[-1]) - rng_lo) / (rng_hi - rng_lo)
        if pos < 0.3:
            score += 2
            res["notes"].append(f"Price in discount zone ({pos * 100:.0f}% of range)")
        elif pos > 0.7:
            score -= 2
            res["notes"].append(f"Price in premium zone ({pos * 100:.0f}% of range)")
    res["range_pos"] = pos if rng_hi > rng_lo else 0.5

    res["score"] = max(-15.0, min(15.0, score))
    if not res["notes"]:
        res["notes"].append("No significant SMC structure in the last 10 4h bars")
    return res
