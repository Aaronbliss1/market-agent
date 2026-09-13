"""Technical indicators implemented in pure numpy (no external TA libs)."""
from __future__ import annotations

import numpy as np


def sma(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    k = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's RSI."""
    c = np.asarray(close, dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up = np.clip(d, 0, None)
    dn = np.clip(-d, 0, None)
    ru = up[:n].mean()
    rd = dn[:n].mean()
    out[n] = 100.0 if rd == 0 else 100.0 - 100.0 / (1 + ru / rd)
    for i in range(n + 1, len(c)):
        ru = (ru * (n - 1) + up[i - 1]) / n
        rd = (rd * (n - 1) + dn[i - 1]) / n
        out[i] = 100.0 if rd == 0 else 100.0 - 100.0 / (1 + ru / rd)
    return out


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    """Returns (macd_line, signal_line, histogram) arrays (nan-padded at start)."""
    c = np.asarray(close, dtype=float)
    line = ema(c, fast) - ema(c, slow)
    sig = np.full(len(c), np.nan)
    valid = np.where(~np.isnan(line))[0]
    if len(valid) >= signal:
        seg = valid[signal - 1:]
        sig[seg] = ema(line[seg], signal)
    hist = line - sig
    return line, sig, hist


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's ATR."""
    h = np.asarray(high, dtype=float)
    l = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) < n + 1:
        return out
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    out[n] = tr[1 : n + 1].mean()
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out
