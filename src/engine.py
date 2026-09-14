"""Orchestration: market scan -> scoring -> calls, position tracking, daily digest.

All public methods are blocking (sync); the bot runs them in a worker thread.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .charts import make_chart
from .dune import DuneClient
from .exchanges import ExchangeRegistry
from .fundamentals import FundamentalsService
from .news import NewsService
from .onchain import Flow, EtherscanV2, WalletTracker
from .rpc import PublicRpc
from .scoring import CallSpec, build_call, news_score, onchain_score
from .sentiment import MarketSentiment
from .store import Store

log = logging.getLogger("engine")
TZ = ZoneInfo("Africa/Lagos")

# Stablecoins are never called — a "short RLUSD for +100%" is not a trade.
STABLECOINS = {
    "USDT", "USDC", "DAI", "RLUSD", "USDS", "PYUSD", "FDUSD", "TUSD", "USDP",
    "USDE", "SUSDE", "USDBC", "GUSD", "BUSD", "EURC", "XUSD", "USD1", "USDF",
    "FRAX", "LUSD", "USDD", "USDY", "DOLA", "MIM", "CRVUSD", "SUSDS",
}


def _clip(s: str, limit: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _fit_caption(text: str, cap: int = 1020) -> str:
    """Fit a full call write-up into a Telegram photo caption (1024 chars).
    Drops optional lines in priority order; core instruction block is kept."""
    lines = text.split("\n")
    for drop in ("• News:", "• SMC (4h):", "• Fundamentals:"):
        if len("\n".join(lines)) <= cap:
            break
        lines = [l for l in lines if not l.startswith(drop)]
    return "\n".join(lines)


def local_midnight(now: float | None = None) -> float:
    dt = datetime.fromtimestamp(now or time.time(), tz=TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return dt.timestamp()


def _f(p: float) -> str:
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    if p >= 0.01:
        return f"{p:.5f}".rstrip("0")
    if p >= 0.0001:
        return f"{p:.8f}".rstrip("0")
    return f"{p:.10f}".rstrip("0")


def _ex(name: str) -> str:
    return name.capitalize()


def _ago(bars: int) -> str:
    if bars <= 0:
        return "this 4h bar"
    return "1 4h bar ago" if bars == 1 else f"{bars} 4h bars ago"


STATUS_ICON = {"TARGET_HIT": "✅", "STOPPED": "🛑", "EXPIRED": "⌛", "CANCELLED": "✖️"}


# ---------------------------------------------------------------- messages
def format_call_message(spec: CallSpec, call_id: int | None, max_leverage: int) -> str:
    n = f"#{call_id} " if call_id else ""
    others = [e for e in spec.exchanges if e != spec.exchange]
    sgn = 1.0 if spec.direction == "LONG" else -1.0
    target_move = (spec.target / spec.entry - 1) * 100 * sgn
    stop_move = (spec.stop / spec.entry - 1) * 100 * sgn
    mover_tag = "  🚀 MOVER" if spec.is_mover else ""
    side = "long" if spec.direction == "LONG" else "short"
    lines = [
        f"🚨 NEW CALL {n}— {spec.ticker}{mover_tag}",
        "",
        f"📊 Exchange: {_ex(spec.exchange)}" + (f" (also: {', '.join(_ex(e) for e in others)})" if others else ""),
        f"📈 Direction: {spec.direction}",
        "⏱ Timeframes: 1h + 4h",
        f"💵 Entry: {_f(spec.entry)}",
        f"🎯 Target: {_f(spec.target)}  ({target_move:+.1f}%)",
        f"⛔ Stop: {_f(spec.stop)}  ({stop_move:+.1f}%)",
        f"⚡ Leverage: {spec.leverage}x",
        f"🏁 Expected return: +{spec.target_roi:.0f}%",
        f"🧠 Confidence: {spec.confidence:.0f}/100  |  24h volume: ${spec.vol24h / 1e6:.0f}M",
        "",
        f"📋 Instructions: {side.capitalize()} {spec.ticker}USDT on {_ex(spec.exchange)} at "
        f"≈{_f(spec.entry)} with {spec.leverage}x. Stop-loss {_f(spec.stop)} ({stop_move:+.1f}%), "
        f"take-profit {_f(spec.target)} ({target_move:+.1f}%). Exit at either level or on 24h expiry.",
        "",
        "WHY:",
        f"• On-chain: {_clip(spec.onchain_note, 140)}",
        "• Technicals (1h/4h): " + _clip("; ".join(spec.tech["notes"][:3]) + ".", 140),
        "• SMC (4h): " + _clip("; ".join(spec.tech["smc"]["notes"][:3]) + ".", 140),
        f"• Fundamentals: {_clip(spec.fund_note or 'n/a', 100)}",
        f"• Market: {MarketSentiment.summary(spec.sentiment)}",
    ]
    if spec.news:
        i = spec.news[0]
        lines.append(f"• News: “{_clip(i['title'], 60)}” ({i['source']}, {i['age_h']:.0f}h ago)")
    lines += [
        "",
        "⏱ Valid 24h — I'll update you when target/stop is hit.",
        "⚠️ DYOR — NFA.",
    ]
    return "\n".join(lines)


def format_close_message(call: dict, price: float, move: float, ret: float, status: str) -> str:
    held_h = ((call.get("closed_ts") or time.time()) - call["ts"]) / 3600
    icon = STATUS_ICON.get(status, "ℹ️")
    return "\n".join([
        f"{icon} {status.replace('_', ' ')} — #{call['id']} {call['ticker']} {call['direction']}",
        f"📊 {_ex(call['exchange'])} | Entry {_f(call['entry'])} → Exit {_f(price)} ({move:+.1f}%)",
        f"⚡ {call['leverage']}x leverage → Return: {ret:+.0f}% on margin",
        f"⏱ Held: {held_h:.1f}h",
        "",
        "⚠️ DYOR — NFA.",
    ])


def format_digest(calls: list[dict], flows: list[dict], max_daily: int) -> str:
    today = datetime.now(TZ).strftime("%A %d %b %Y")
    lines = [f"📋 DAILY BREAKDOWN — {today}", ""]
    if not calls:
        lines.append("No calls made today (no token cleared the confidence bar).")
    else:
        lines.append(f"Calls made: {len(calls)} / {max_daily}")
        for i, c in enumerate(calls, 1):
            st = c["status"]
            if st == "open":
                detail = f"OPEN — entry {_f(c['entry'])}, target {_f(c['target'])} (expect +{c['target_roi']:.0f}% at {c['leverage']}x)"
            else:
                icon = STATUS_ICON.get(st, "")
                detail = f"{icon} {st.replace('_', ' ')} — return {c['return_pct']:+.0f}% ({c['leverage']}x)"
            lines.append(f"{i}. {c['ticker']} {c['direction']} @ {c['exchange']} — {detail}")
        closed = [c for c in calls if c["status"] in ("TARGET_HIT", "STOPPED", "EXPIRED")]
        if closed:
            wins = [c for c in closed if (c["return_pct"] or 0) > 0]
            lines.append("")
            lines.append(f"Win rate: {len(wins)}/{len(closed)} closed")
            lines.append(f"Avg return (equal sizing): "
                         f"{sum(c['return_pct'] or 0 for c in closed) / len(closed):+.0f}%")
            best = max(closed, key=lambda c: c["return_pct"] or 0)
            worst = min(closed, key=lambda c: c["return_pct"] or 0)
            lines.append(f"Best: {best['ticker']} {best['return_pct']:+.0f}%   "
                         f"Worst: {worst['ticker']} {worst['return_pct']:+.0f}%")
    open_now = [c for c in calls if c["status"] == "open"]
    if open_now:
        lines.append(f"Still open: {', '.join(c['ticker'] for c in open_now)}")
    if flows:
        lines.append("")
        lines.append(f"On-chain today: {len(flows)} tracked-wallet flows across "
                     f"{len(set(f['ticker'] for f in flows))} tokens")
    return "\n".join(lines)


class NullMessenger:
    def send_text(self, text: str):
        print(text, flush=True)

    def send_photo(self, path: str, caption: str):
        print(f"[photo] {path}\n{caption}", flush=True)


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, cfg, store: Store | None, messenger=None):
        self.cfg = cfg
        self.store = store
        self.messenger = messenger or NullMessenger()
        self.x = ExchangeRegistry()
        self.es = EtherscanV2(cfg.etherscan_api_key)
        self.wallets = WalletTracker(self.es, cfg.wallets_path)
        self.dune = DuneClient(cfg.dune_api_key, cfg.dune_query_id)
        self.rpc_by_chain = {1: PublicRpc(1), 56: PublicRpc(56)}
        self.news = NewsService()
        self.sentiment = MarketSentiment()
        self.fundamentals = FundamentalsService()
        self._mover_alerted: dict[str, float] = {}  # ticker -> ts of last pulse alert
        self.started = time.time()
        self._contracts_cache: dict[str, dict] = {}
        self._addr_index: dict[str, tuple[str, int, float]] = {}  # contract -> (ticker, chain, price)
        self.wallets.load()

    # ---------------- contracts ----------------
    def _dexscreener(self, ticker: str) -> dict[int, str]:
        """Map ticker -> {chain_id: token contract} via DexScreener (free, no key)."""
        try:
            r = requests.get("https://api.dexscreener.com/latest/dex/search",
                             params={"q": ticker}, timeout=12)
            pairs = (r.json() or {}).get("pairs") or []
            best: dict[int, tuple[float, str]] = {}
            for p in pairs:
                cid = {"ethereum": 1, "bsc": 56}.get(p.get("chainId"))
                if not cid:
                    continue
                bt = p.get("baseToken") or {}
                if (bt.get("symbol") or "").upper() != ticker:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if liq > best.get(cid, (0, ""))[0]:
                    best[cid] = (liq, bt.get("address", ""))
            return {cid: addr for cid, (_liq, addr) in best.items() if addr}
        except Exception as e:
            log.debug("dexscreener lookup %s failed: %s", ticker, e)
            return {}

    def _contracts_for(self, ticker: str) -> dict[int, str]:
        c = self.store.get_contracts(ticker) if self.store else self._contracts_cache.get(ticker)
        if c:
            return c
        c = self._dexscreener(ticker)
        if c:
            if self.store:
                self.store.set_contracts(ticker, c)
            self._contracts_cache[ticker] = c
            time.sleep(0.22)  # be nice to dexscreener
        return c

    # ---------------- sentiment / movers ----------------
    def _funding_map(self, cands: dict[str, dict]) -> dict[str, float]:
        """Per-token funding rate from whichever exchange publishes one."""
        out: dict[str, float] = {}
        for t, info in cands.items():
            for ex in info["exs"].values():
                if ex.get("funding") is not None:
                    out[t] = ex["funding"]
                    break
        return out

    def detect_movers(self, symbols: dict[str, dict]) -> dict[str, dict]:
        """Tokens making a move on any exchange: |24h chg| >= threshold,
        confirmed by 1h volume expansion or a 24h breakout high.
        `symbols` = raw merged map {ticker: {exchange: info}}."""
        movers: dict[str, dict] = {}
        ranked = sorted(symbols.items(),
                        key=lambda kv: max(abs(i.get("chg24h", 0))
                                           for i in kv[1].values()),
                        reverse=True)
        checked = 0
        for t, exs in ranked:
            if checked >= 30:
                break
            chg = max(exs.values(), key=lambda i: abs(i.get("chg24h", 0)))
            if abs(chg.get("chg24h", 0)) < self.cfg.mover_24h_pct:
                break  # ranked desc: nothing below can qualify
            primary = self.x.primary(exs)
            if not primary:
                continue
            checked += 1
            k1 = self.x.klines(primary, t, "1h", 48)
            if k1 is None or len(k1["v"]) < 27:
                continue
            v, c, h = k1["v"], k1["c"], k1["h"]
            recent = float(v[-3:].mean())
            base = float(v[-27:-3].mean())
            ratio = recent / max(base, 1e-9)
            breakout = float(c[-1]) >= float(h[-25:-1].max()) * 0.999
            if ratio >= self.cfg.mover_vol_ratio or breakout:
                movers[t] = {
                    "chg": float(exs[primary].get("chg24h", 0)),
                    "dir": "up" if chg.get("chg24h", 0) > 0 else "down",
                    "vol_ratio": ratio,
                    "breakout": breakout,
                    "exchange": primary,
                }
        return movers

    # ---------------- dune ----------------
    def _dune_to_flows(self, rows: list[dict]) -> list[tuple[str, Flow]]:
        """Convert Dune result rows -> (ticker, Flow), only for tokens we track.

        Expects DUNE_QUERY_SQL columns: chain ('ethereum'/'bnb'),
        token_address/from_address/to_address (0x… lowercase text),
        amount (display units), amount_usd, ts, from_entity, to_entity.
        """
        out: list[tuple[str, Flow]] = []
        for r in rows:
            try:
                token = str(r.get("token_address") or "").lower()
                frm = str(r.get("from_address") or "").lower()
                to = str(r.get("to_address") or "").lower()
                ts = int(float(r.get("ts") or 0))
                amount = float(r.get("amount") or 0)
                usd_dune = float(r.get("amount_usd") or 0)
            except (TypeError, ValueError):
                continue
            if amount <= 0 or ts <= 0:
                continue
            ent = self._addr_index.get(token)
            if not ent:
                continue  # token not in today's scan set
            ticker, _cid, price = ent
            usd = usd_dune if usd_dune > 0 else amount * price
            chain = 1 if str(r.get("chain") or "").lower() in ("ethereum", "eth") else 56
            dk = f"{token}|{frm}|{to}|{amount:.6f}|{ts // 30}"
            w_from = str(r.get("from_entity") or "").lower()
            w_to = str(r.get("to_entity") or "").lower()
            for wallet, direction in ((w_from, "out"), (w_to, "in")):
                if wallet in self.wallets.index:
                    key, name, etype = self.wallets.index[wallet]
                    out.append((ticker, Flow(
                        ts, chain, key, name, etype, wallet.upper(),
                        direction, amount, usd, "", "dune", dk)))
        return out

    # ---------------- scan ----------------
    def run_scan(self) -> list[CallSpec]:
        t0 = time.time()
        now = time.time()
        self.wallets.load()  # hot-reload wallet list
        log.info("scan: started")

        symbols = self.x.symbols()
        if not symbols:
            self.messenger.send_text("⚠️ Scan skipped — every exchange API is unreachable right now.")
            return []

        cands: dict[str, dict] = {}
        for ticker, exs in symbols.items():
            if ticker.upper() in STABLECOINS:
                continue  # never call stablecoins
            vol = max(i.get("usd_volume", 0) for i in exs.values())
            if vol >= self.cfg.min_24h_usd_volume:
                cands[ticker] = {"exs": exs, "vol": vol}
        ranked = sorted(cands.items(), key=lambda kv: kv[1]["vol"], reverse=True)
        tickers = [t for t, _ in ranked[: self.cfg.top_n_tokens]]
        log.info("scan: %d tradable tokens, scanning top %d by volume", len(cands), len(tickers))

        # movers: tokens making a move on ANY exchange (checked on all USDT pairs)
        movers = self.detect_movers(symbols)
        for t in movers:
            if t.upper() in STABLECOINS:
                continue
            if t not in cands:
                cands[t] = {"exs": symbols[t],
                            "vol": max(i.get("usd_volume", 0) for i in symbols[t].values())}
            if t not in tickers:
                tickers.append(t)
        if movers:
            log.info("scan: %d movers: %s", len(movers), ", ".join(
                f"{t} {m['dir']} {m['chg']:+.1f}% (1h vol x{m['vol_ratio']:.1f})"
                for t, m in list(movers.items())[:10]))

        # token -> contract mapping (cached in sqlite)
        contracts = {t: self._contracts_for(t) for t in tickers}

        # on-chain tracked-wallet flows (Etherscan + Dune, deduped)
        flows_by: dict[str, list] = {t: [] for t in tickers}
        seen_keys: set[str] = set()
        for t in tickers:  # reverse index: token contract -> (ticker, chain, price)
            price = max(i.get("last", 0) for i in cands[t]["exs"].values())
            for cid, addr in contracts.get(t, {}).items():
                if addr:
                    self._addr_index[addr.lower()] = (t, cid, price)
        if self.wallets.enabled:
            since = now - 6 * 3600
            if self.store:
                v = self.store.get_kv("last_scan_ts")
                if v:
                    since = min(float(v), now - 6 * 3600)
            if self.es.enabled:
                for t in tickers:
                    price = max(i.get("last", 0) for i in cands[t]["exs"].values())
                    for f in self.wallets.scan_token(t, contracts.get(t, {}), since, price):
                        if f.dedup_key in seen_keys:
                            continue
                        seen_keys.add(f.dedup_key)
                        flows_by[t].append(f)
                        if self.store:
                            self.store.add_flow(f, t)
                n_es = sum(len(v) for v in flows_by.values())
                log.info("scan: etherscan done, %d tracked-wallet flows", n_es)

            # Dune cross-check: fixed 24h window baked into the saved query.
            # API fetches are throttled (free-tier credits); throttled scans
            # reuse the cached rows so on-chain signal stays on every scan.
            if self.dune.enabled and self.wallets.index:
                last_dune = now - 24 * 3600
                if self.store:
                    v = self.store.get_kv("last_dune_ts")
                    if v:
                        last_dune = float(v)
                throttled = now - last_dune < self.cfg.dune_min_interval_min * 60
                try:
                    if throttled:
                        rows = self.dune.last_rows  # cached 24h snapshot
                    else:
                        rows = self.dune.run_saved_query()
                        if self.store:
                            self.store.set_kv("last_dune_ts", str(now))
                        self.dune.last_fetch = now
                    n_dune = 0
                    for t, f in self._dune_to_flows(rows):
                        if f.dedup_key in seen_keys:
                            continue
                        seen_keys.add(f.dedup_key)
                        flows_by[t].append(f)
                        if self.store:
                            self.store.add_flow(f, t)
                        n_dune += 1
                    self.dune.last_flows = n_dune
                    log.info("scan: dune done, +%d flows (24h window%s)", n_dune,
                             ", cached" if throttled else "")
                except Exception as e:
                    log.warning("dune fetch failed: %s", e)

            # keyless public RPC for chains the (free) Etherscan plan doesn't cover
            for chain_id in (1, 56):
                if self.es.enabled and chain_id in self.es.supported_chains:
                    continue  # Etherscan already covers this chain
                rpc = self.rpc_by_chain.get(chain_id)
                if not rpc or not rpc.enabled:
                    continue
                try:
                    chain_tokens = [addr for addr, (_t, cid, _p)
                                    in self._addr_index.items() if cid == chain_id]
                    if not chain_tokens:
                        continue
                    rows = rpc.scan_tokens(chain_tokens, set(self.wallets.index))
                    n_rpc = 0
                    for t, f in rpc.to_flows(rows, self.wallets.index, self._addr_index, since):
                        if f.dedup_key in seen_keys:
                            continue
                        seen_keys.add(f.dedup_key)
                        flows_by[t].append(f)
                        if self.store:
                            self.store.add_flow(f, t)
                        n_rpc += 1
                    rpc.last_flows = n_rpc
                    log.info("scan: public RPC chain %d done, +%d new flows", chain_id, n_rpc)
                except Exception as e:
                    log.warning("public RPC chain %d failed: %s", chain_id, e)

            log.info("scan: on-chain done, %d unique tracked-wallet flows",
                     sum(len(v) for v in flows_by.values()))
        else:
            log.info("scan: on-chain tracking disabled "
                     f"(etherscan={'on' if self.es.enabled else 'OFF'}, "
                     f"dune={'on' if self.dune.enabled else 'off'})")

        # market context: sentiment + fundamentals (one fetch each, cached)
        sentiment = self.sentiment.fetch(self._funding_map(cands))
        self.fundamentals.fetch()

        # news
        self.news.fetch(24)
        news_by = {t: self.news.for_token(t) for t in tickers}

        # pre-rank by |onchain + news| so we only fetch candles for interesting tokens
        pre = {}
        for t in tickers:
            os_, _, _ = onchain_score(flows_by[t], cands[t]["vol"], now)
            ns_, _ = news_score(news_by[t], now)
            pre[t] = {"oscore": os_, "nscore": ns_, "pre": abs(os_ + ns_), "vol": cands[t]["vol"]}
        kline_tickers = [t for t, _ in sorted(pre.items(),
                                               key=lambda kv: (kv[1]["pre"], kv[1]["vol"]),
                                               reverse=True)[:90]]
        for t, _ in ranked[:20]:
            if t not in kline_tickers:
                kline_tickers.append(t)
        for t in movers:  # momentum gets evaluated even without flow/news
            if t in cands and t not in kline_tickers:
                kline_tickers.append(t)

        # technicals (+SMC) + composite score
        results: list[CallSpec] = []
        for t in kline_tickers:
            if t not in flows_by or t not in cands:
                continue
            exs = cands[t]["exs"]
            primary = self.x.primary(exs)
            if not primary:
                continue
            k4 = self.x.klines(primary, t, "4h", 220)
            k1 = self.x.klines(primary, t, "1h", 220)
            if k4 is None or k1 is None:
                continue
            entry_price = exs[primary].get("last")
            mv = movers.get(t)
            try:
                spec = build_call(t, exs, k1, k4, flows_by[t], news_by[t], self.cfg, now,
                                  entry_price,
                                  fund=self.fundamentals.for_token(t),
                                  sentiment=sentiment,
                                  is_mover=mv is not None,
                                  mover_up=mv["dir"] == "up" if mv else True)
            except Exception as ex:
                log.warning("scan: %s skipped — build_call failed (%s)", t, ex)
                continue
            if spec:
                results.append(spec)
        results.sort(key=lambda s: s.confidence, reverse=True)
        n_long = sum(1 for s in results if s.direction == "LONG")
        log.info("scan: %d candidates above confidence threshold (%.0f) — %d long / %d short",
                 len(results), self.cfg.min_confidence, n_long, len(results) - n_long)

        # daily cap (max 10) + no re-calls of open / already-called tickers
        since_day = local_midnight()
        today = self.store.calls_since(since_day) if self.store else []
        open_tickers = {c["ticker"] for c in (self.store.open_calls() if self.store else [])}
        called_today = {c["ticker"] for c in today}
        slots = self.cfg.max_daily_calls - len(today)
        made: list[CallSpec] = []
        for spec in results:
            if slots <= 0:
                break
            if spec.ticker in open_tickers or spec.ticker in called_today:
                continue
            made.append(spec)
            slots -= 1
            if len(made) >= self.cfg.max_calls_per_scan:
                break  # never call more than MAX_CALLS_PER_SCAN tokens at once

        for spec in made:
            self._post_call(spec)

        if self.store:
            self.store.set_kv("last_scan_ts", str(now))
        log.info("scan: finished in %.0fs — %d posted", time.time() - t0, len(made))
        return results

    def _post_call(self, spec: CallSpec):
        call_id = self.store.add_call(spec) if self.store else None
        chart = None
        try:
            k4 = self.x.klines(spec.exchange, spec.ticker, "4h", 120)
            if k4 is not None:
                path = os.path.join(self.cfg.charts_dir,
                                    f"call_{call_id or int(spec.ts)}_{spec.ticker}.png")
                chart = make_chart(path, spec.ticker, spec.exchange, spec.direction, k4,
                                   spec.tech.get("smc"), spec.entry, spec.target,
                                   spec.stop, spec.leverage, spec.target_roi)
        except Exception as e:
            log.warning("chart failed for %s: %s", spec.ticker, e)
        text = _fit_caption(format_call_message(spec, call_id, self.cfg.max_leverage))
        if chart:
            if len(text) <= 1020:
                self.messenger.send_photo(chart, text)  # chart + full write-up in one message
            else:  # pathological length — last resort, split the two
                self.messenger.send_text(text)
                self.messenger.send_photo(
                    chart, f"📊 {spec.ticker} {spec.direction} — entry/target/stop marked")
        else:
            self.messenger.send_text(text + "\n(chart unavailable)")
        log.info("posted call %s %s %s %dx target %.0f%%",
                 call_id, spec.ticker, spec.direction, spec.leverage, spec.target_roi)

    # ---------------- tracking ----------------
    def run_track(self):
        if not self.store:
            return
        for call in self.store.open_calls():
            # quote from the traded exchange first, then fall back to the
            # other listed exchanges (some are geo-blocked from the runner,
            # e.g. Bybit — without a fallback those calls go untracked)
            price = None
            cands = [call["exchange"]]
            listed = call.get("exchanges") or ""
            if isinstance(listed, str):
                try:
                    listed = json.loads(listed)
                except Exception:
                    listed = []
            for name in listed:
                if name not in cands:
                    cands.append(name)
            for name in cands:
                price = self.x.price(name, call["ticker"])
                if price is not None:
                    break
            if price is None:
                log.warning("track %s: no quote from %s — skipped", call["ticker"], cands)
                continue
            # quote sanity: our stops/targets sit within ±60% of entry, so a
            # deviation beyond 100% in one check is bad data (e.g. a symbol
            # mis-mapped by an exchange API), not a market move — skip it.
            if abs(price / call["entry"] - 1) > 1.0:
                log.warning("track %s: implausible quote %.6g vs entry %.6g — skipped",
                            call["ticker"], price, call["entry"])
                continue
            now = time.time()
            long_call = call["direction"] == "LONG"
            status = None
            if long_call:
                if price >= call["target"]:
                    status = "TARGET_HIT"
                elif price <= call["stop"]:
                    status = "STOPPED"
            else:
                if price <= call["target"]:
                    status = "TARGET_HIT"
                elif price >= call["stop"]:
                    status = "STOPPED"
            if status is None and now - call["ts"] > self.cfg.call_expiry_hours * 3600:
                status = "EXPIRED"
            if status is None:
                continue
            sign = 1.0 if long_call else -1.0
            move = (price / call["entry"] - 1) * 100 * sign
            ret = move * call["leverage"]
            self.store.close_call(call["id"], status, price, ret)
            self.messenger.send_text(format_close_message(call, price, move, ret, status))
            log.info("call %d %s closed: %s return %+.1f%%", call["id"], call["ticker"], status, ret)

    # ---------------- mover pulse (fast, any token making a move) ----------------
    def run_mover_pulse(self):
        """Light check (every 15 min): any token moving on any exchange.
        If a fresh mover clears the confidence bar, post it as a call."""
        if not self.store:
            return
        symbols = self.x.symbols(max_age=600)
        if not symbols:
            return
        movers = self.detect_movers(symbols)
        if not movers:
            return
        now = time.time()
        fresh = {t: m for t, m in movers.items()
                 if now - self._mover_alerted.get(t, 0) > 2 * 3600}
        if not fresh:
            return
        since_day = local_midnight()
        today = self.store.calls_since(since_day)
        open_tickers = {c["ticker"] for c in self.store.open_calls()}
        called_today = {c["ticker"] for c in today}
        slots = self.cfg.max_daily_calls - len(today)
        sentiment = self.sentiment.fetch(self._funding_map(
            {t: {"exs": exs, "vol": 0} for t, exs in symbols.items()}))
        self.fundamentals.fetch()
        self.news.fetch(24)
        posted = 0
        for t, mv in list(fresh.items())[:6]:
            if slots <= 0:
                break
            if posted >= self.cfg.max_calls_per_scan:
                break  # never call more than MAX_CALLS_PER_SCAN tokens at once
            self._mover_alerted[t] = now
            if t.upper() in STABLECOINS:
                continue
            if t in open_tickers or t in called_today:
                continue
            exs = symbols[t]
            primary = self.x.primary(exs)
            if not primary:
                continue
            k4 = self.x.klines(primary, t, "4h", 220)
            k1 = self.x.klines(primary, t, "1h", 220)
            if k4 is None or k1 is None:
                continue
            spec = build_call(t, exs, k1, k4, [], self.news.for_token(t),
                              self.cfg, now, exs[primary].get("last"),
                              fund=self.fundamentals.for_token(t),
                              sentiment=sentiment,
                              is_mover=True, mover_up=mv["dir"] == "up")
            if spec:
                self._post_call(spec)
                slots -= 1
                posted += 1
                log.info("pulse: posted mover call %s %s %dx", t, spec.direction, spec.leverage)

    def movers_snapshot(self, limit: int = 10) -> str:
        symbols = self.x.symbols(max_age=300)
        if not symbols:
            return "No exchange data right now."
        rows = []
        for t, exs in symbols.items():
            best = max(exs.values(), key=lambda i: abs(i.get("chg24h", 0)))
            if abs(best.get("chg24h", 0)) >= 8:
                rows.append((t, best.get("chg24h", 0), self.x.primary(exs),
                             best.get("usd_volume", 0)))
        rows.sort(key=lambda r: abs(r[1]), reverse=True)
        if not rows:
            return "No tokens moving ≥8% in 24h right now."
        out = ["🚀 Current movers (|24h| ≥ 8%):", ""]
        open_t = {c["ticker"] for c in (self.store.open_calls() if self.store else [])}
        for t, chg, ex, vol in rows[:limit]:
            tag = "  · OPEN CALL" if t in open_t else ""
            out.append(f"• {t} {chg:+.1f}% @ {ex} — ${vol / 1e6:.0f}M vol{tag}")
        out.append(f"\nMover threshold: |24h| ≥ {self.cfg.mover_24h_pct:.0f}% with volume "
                   f"expansion or breakout (checked every {self.cfg.pulse_interval_min} min)")
        return "\n".join(out)

    # ---------------- digest ----------------
    def run_digest(self):
        if not self.store:
            return
        since = local_midnight()
        calls = self.store.calls_since(since)
        flows = self.store.flows_since(since)
        self.messenger.send_text(format_digest(calls, flows, self.cfg.max_daily_calls))
        log.info("digest sent: %d calls today", len(calls))

    # ---------------- misc ----------------
    def status_text(self) -> str:
        now = time.time()
        open_calls = self.store.open_calls() if self.store else []
        open_calls = sorted(open_calls, key=lambda c: c["ts"])
        today = self.store.calls_since(local_midnight()) if self.store else []

        src_label = {"dune": "Dune", "etherscan": "Etherscan", "rpc": "Public RPC"}
        src_map = {}
        if open_calls and self.store:
            src_map = self.store.flow_sources([c["ticker"] for c in open_calls])

        lines = ["🤖 MARKET AGENT — STATUS UPDATE", ""]

        # ---- active trades (unsettled, live)
        lines.append("📈 Active Trades")
        if open_calls:
            for c in open_calls:
                held_h = (now - c["ts"]) / 3600
                sgn = 1.0 if c["direction"] == "LONG" else -1.0
                price = self.x.price(c["exchange"], c["ticker"])
                price_txt = ""
                if price:
                    upnl = (price / c["entry"] - 1) * 100 * sgn
                    price_txt = f" · now {_f(price)} ({upnl:+.1f}%)"
                lines.append(
                    f"  {c['ticker']} {c['direction']} — entry {_f(c['entry'])}{price_txt}"
                    f" · stop {_f(c['stop'])} · target {_f(c['target'])}"
                    f" · {c['leverage']}x · open {held_h:.1f}h")
        else:
            lines.append("  None — no unsettled trades")
        lines.append("")

        # ---- exchanges the active trades were called on
        lines.append("📊 Exchanges")
        if open_calls:
            by_ex: dict[str, list[str]] = {}
            for c in open_calls:
                by_ex.setdefault(c["exchange"].capitalize(), []).append(c["ticker"])
            for ex, tickers in by_ex.items():
                lines.append(f"  {ex} — {', '.join(tickers)}")
        else:
            lines.append("  None — no active trades")
        lines.append("")

        # ---- on-chain data used, per token
        lines.append("🔗 On-chain")
        if open_calls:
            for c in open_calls:
                per = src_map.get(c["ticker"], {})
                if per:
                    parts = " · ".join(f"{src_label.get(k, k)} ({v} flows)"
                                       for k, v in sorted(per.items()))
                    lines.append(f"  {c['ticker']} — {parts}")
                else:
                    lines.append(f"  {c['ticker']} — no tracked-wallet flows recorded")
        else:
            lines.append("  No active trades to report")
        lines.append("")

        # ---- open calls summary
        lines.append("📋 Open Calls")
        if open_calls:
            lines.append(f"  {len(open_calls)} — "
                         + ", ".join(f"{c['ticker']} {c['direction']}" for c in open_calls))
        else:
            lines.append("  0")
        lines.append("")

        # ---- calls made today (as at now)
        settled = [c for c in today if c["status"] != "open"]
        sub = f" ({len(open_calls)} open · {len(settled)} settled)" if today else ""
        lines.append(f"📊 Calls Today: {len(today)} of {self.cfg.max_daily_calls}{sub}")
        return "\n".join(lines)
