"""Chart rendering: 1h candles + EMA20/50, RSI subplot, and
ENTRY / TARGET / STOP lines annotated on the picture."""
from __future__ import annotations

from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from .indicators import ema, rsi


def _fmt(p: float) -> str:
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.10g}"


def make_chart(path: str, ticker: str, exchange: str, direction: str, k4: dict,
               smc: dict | None, entry: float, target: float, stop: float,
               leverage: int, roi: float) -> str:
    t, o, h, l, c = k4["t"], k4["o"], k4["h"], k4["l"], k4["c"]
    n = len(c)
    smc = smc or {}

    fig = plt.figure(figsize=(11, 7), facecolor="white")
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08)
    ax = fig.add_subplot(gs[0])
    axr = fig.add_subplot(gs[1], sharex=ax)

    # candles
    for i in range(n):
        col = "#26a69a" if c[i] >= o[i] else "#ef5350"
        ax.vlines(i, l[i], h[i], color=col, lw=0.7, zorder=2)
        body = max(abs(c[i] - o[i]), (h[i] - l[i]) * 0.012)
        ax.add_patch(Rectangle((i - 0.35, min(o[i], c[i])), 0.7, body,
                               facecolor=col, edgecolor=col, zorder=3))

    # SMC zones (drawn under the candles)
    for z in smc.get("marks", []):
        if z["type"] == "ob" and n:
            x0 = max(0, z["i0"])
            col = "#16a34a" if z["dir"] == "bull" else "#dc2626"
            ax.add_patch(Rectangle((x0, z["lo"]), max(1, n - x0), z["hi"] - z["lo"],
                                   facecolor=col, alpha=0.16, edgecolor="none", zorder=1))
            ax.text(n - 2, (z["lo"] + z["hi"]) / 2, "OB", fontsize=7, color=col,
                    ha="right", va="center", zorder=7, fontweight="bold")
        elif z["type"] == "fvg" and n:
            x0 = max(0, z["i0"])
            col = "#16a34a" if z["dir"] == "bull" else "#dc2626"
            ax.add_patch(Rectangle((x0, z["lo"]), max(1, n - x0), z["hi"] - z["lo"],
                                   facecolor="#6366f1", alpha=0.14,
                                   edgecolor=col, ls="--", lw=0.8, zorder=1))
    for m in smc.get("marks", []):
        if m["type"] == "bos" and m["i"] < n:
            col = "#16a34a" if m["dir"] == "bull" else "#dc2626"
            mk = "^" if m["dir"] == "bull" else "v"
            dy = (m["price"] - float(l.min())) * 0.06
            ax.plot(m["i"], m["price"] + (dy if mk == "^" else -dy), mk,
                    color=col, markersize=11, zorder=8)
            ax.annotate("BOS", (m["i"], m["price"]),
                        xytext=(m["i"] + 1, m["price"] + (dy * 2.2 if mk == "^" else -dy * 2.2)),
                        fontsize=7.5, color=col, fontweight="bold", zorder=8)
        elif m["type"] == "sweep" and m["i"] < n:
            col = "#16a34a" if m["dir"] == "bull" else "#dc2626"
            ax.plot(m["i"], m["price"], "o", mfc="none", mec=col,
                    mew=1.5, markersize=9, zorder=8)

    # EMAs
    e20, e50 = ema(c, 20), ema(c, 50)
    xs = range(n)
    ax.plot(xs, e20, color="#f0b90b", lw=1.2, label="EMA20 (4h)", zorder=4)
    ax.plot(xs, e50, color="#627eea", lw=1.2, label="EMA50 (4h)", zorder=4)

    # key levels
    y0 = min(float(l.min()), stop, target) * 0.999
    y1 = max(float(h.max()), stop, target) * 1.001
    mid = (y0 + y1) / 2
    levels = [
        (entry, f"ENTRY {_fmt(entry)}", "#3b82f6"),
        (target, f"TARGET {_fmt(target)} ({(target / entry - 1) * 100:+.1f}%)", "#16a34a"),
        (stop, f"STOP {_fmt(stop)} ({(stop / entry - 1) * 100:+.1f}%)", "#dc2626"),
    ]
    for price, label, col in levels:
        ax.axhline(price, color=col, ls="--", lw=1.3, zorder=5)
        va = "top" if price > mid else "bottom"
        ax.text(n - 1, price, " " + label, color=col, va=va, fontsize=8.5,
                fontweight="bold", zorder=6,
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec=col, alpha=0.9))

    # x axis
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    ax.set_xticks(ticks)
    ax.set_xticklabels(
        [datetime.fromtimestamp(t[i], tz=timezone.utc).strftime("%d %b %H:%M") for i in ticks],
        fontsize=7)
    ax.set_xlim(-2, n + 2)
    ax.set_ylim(y0, y1)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)

    # RSI subplot (4h)
    rv = rsi(c, 14)
    axr.plot(xs, rv, color="#7c3aed", lw=1.1)
    axr.axhline(70, color="#dc2626", ls=":", lw=0.9)
    axr.axhline(30, color="#16a34a", ls=":", lw=0.9)
    axr.fill_between(xs, 30, 70, color="#7c3aed", alpha=0.06)
    axr.set_ylim(0, 100)
    axr.set_yticks([30, 50, 70])
    axr.set_yticklabels(["30", "50", "70"], fontsize=7)
    axr.text(1, 85, "RSI(14) 4h", fontsize=8, color="#7c3aed")
    axr.grid(True, alpha=0.25)
    axr.set_xticks(ticks)
    axr.set_xticklabels(
        [datetime.fromtimestamp(t[i], tz=timezone.utc).strftime("%d %b %H:%M") for i in ticks],
        fontsize=7)

    arrow = "LONG" if direction == "LONG" else "SHORT"
    trend_txt = smc.get("trend", "range")
    fig.suptitle(
        f"{ticker}  {arrow}  @ {exchange}   |   {leverage}x leverage   |   "
        f"expected +{roi:.0f}% (target <=500%)   |   4h structure: {trend_txt}",
        fontsize=13, fontweight="bold")
    fig.text(0.99, 0.005,
             "Market Agent — OB/FVG/BOS = Smart Money Concepts — analysis only, not financial advice",
             fontsize=6.5, color="#9ca3af", ha="right")

    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path
