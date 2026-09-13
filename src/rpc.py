"""Keyless on-chain tracking via public JSON-RPC endpoints.

Covers the chains the free Etherscan plan does not support (currently BSC),
and acts as a full fallback if no Etherscan key is set at all.

Per scan it queries, per tracked wallet:
  eth_getLogs(Transfer, from = wallet)   -> every token the wallet sent
  eth_getLogs(Transfer, to   = wallet)   -> every token the wallet received
(~2 calls per wallet; no per-token fan-out, no API key, no signup.)

Endpoint rotation: tries a small list of public endpoints and sticks to the
last one that worked.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .onchain import Flow

log = logging.getLogger("rpc")

TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DEFAULT_ENDPOINTS = {
    1: [
        "https://ethereum-rpc.publicnode.com",
        "https://eth.llamarpc.com",
        "https://cloudflare-eth.com",
    ],
    56: [
        "https://bsc-rpc.publicnode.com",
        "https://binance.llamarpc.com",
        "https://bsc-dataseed.binance.org",
    ],
}

# scan window in blocks (~8h)
DEFAULT_WINDOWS = {1: 2400, 56: 8000}


def _pad(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


class PublicRpc:
    def __init__(self, chain_id: int, endpoints: list[str] | None = None,
                 window_blocks: int | None = None):
        self.chain_id = chain_id
        self.endpoints = list(endpoints or DEFAULT_ENDPOINTS.get(chain_id, []))
        self.window_blocks = window_blocks or DEFAULT_WINDOWS.get(chain_id, 3000)
        self.active: str | None = None
        self.s = requests.Session()
        self.enabled = bool(self.endpoints)
        self.last_error = ""
        self.last_fetch = 0.0
        self.last_flows = 0
        self._decimals: dict[str, int] = {}

    def _post(self, method: str, params: list, timeout: float = 25):
        order = ([self.active] if self.active else []) + \
                [e for e in self.endpoints if e != self.active]
        last: Exception | None = None
        for ep in order:
            try:
                r = self.s.post(ep, json={"jsonrpc": "2.0", "id": 1,
                                          "method": method, "params": params},
                                timeout=timeout)
                d = r.json()
                if isinstance(d, dict) and "error" in d:
                    raise RuntimeError(str(d["error"].get("message", d["error"])))
                self.active = ep
                return d.get("result") if isinstance(d, dict) else None
            except Exception as e:
                last = e
                continue
        self.last_error = str(last)
        raise last  # type: ignore[misc]

    def block_number(self) -> int:
        return int(self._post("eth_blockNumber", []), 16)

    def decimals(self, token: str) -> int:
        if token in self._decimals:
            return self._decimals[token]
        try:
            res = self._post("eth_call", [{"to": token, "data": "0x313ce567"}], timeout=10)
            dec = int(res, 16) if res else 18
            if 0 <= dec <= 36:
                self._decimals[token] = dec
                return dec
        except Exception:
            pass
        self._decimals[token] = 18
        return 18

    def _get_logs(self, flt: dict, from_block: int, to_block: int) -> list[dict]:
        """getLogs that halves the range on 'limit exceeded' (recursive)."""
        time.sleep(0.25)
        try:
            res = self._post("eth_getLogs", [{**flt, "fromBlock": hex(from_block),
                                              "toBlock": hex(to_block)}])
            return res or []
        except Exception as e:
            if "limit" in str(e).lower() and to_block - from_block > 500:
                mid = (from_block + to_block) // 2
                return self._get_logs(flt, from_block, mid) + \
                       self._get_logs(flt, mid + 1, to_block)
            log.warning("getLogs chain %d failed: %s", self.chain_id, e)
            return []

    def scan_tokens(self, tokens: list[str], wallets: set[str], workers: int = 4) -> list[dict]:
        """Token Transfer logs for the given token contracts where any tracked
        wallet is sender or receiver. Two getLogs per token (wallets passed as
        an OR-topic list), so the result sets stay small and node-friendly.
        Queries run in a small thread pool to stay under node rate limits."""
        latest = self.block_number()
        from_block = max(1, latest - self.window_blocks)
        w_topics = [_pad(w) for w in sorted(wallets)]
        jobs = []
        for token in tokens:
            jobs.append({"address": token, "topics": [TRANSFER_SIG, w_topics, None]})
            jobs.append({"address": token, "topics": [TRANSFER_SIG, None, w_topics]})
        rows: list[dict] = []

        def run(flt):
            return self._get_logs(flt, from_block, latest)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for res in pool.map(run, jobs):
                rows.extend(res)
        self.last_fetch = time.time()
        return rows

    def to_flows(self, rows: list[dict], wallet_index: dict,
                 addr_index: dict, since_ts: float) -> list[tuple[str, Flow]]:
        """raw logs -> (ticker, Flow) for tokens in the current scan set."""
        flows: list[tuple[str, Flow]] = []
        if not rows:
            return flows
        bns = sorted({int(r["blockNumber"], 16) for r in rows if r.get("blockNumber")})
        if not bns:
            return flows
        bmin, bmax = bns[0], bns[-1]
        try:
            tmin = int(self._post("eth_getBlockByNumber", [hex(bmin), False])["timestamp"], 16)
            tmax = int(self._post("eth_getBlockByNumber", [hex(bmax), False])["timestamp"], 16)
        except Exception:
            tmin = tmax = int(time.time())
        span_b = max(1, bmax - bmin)
        span_t = max(1, tmax - tmin)
        for r in rows:
            try:
                bn = int(r["blockNumber"], 16)
            except (ValueError, KeyError):
                continue
            ts = int(tmin + (bn - bmin) * span_t / span_b)
            if ts < since_ts:
                continue
            token = (r.get("address") or "").lower()
            ent = addr_index.get(token)
            if not ent:
                continue
            topics = r.get("topics") or []
            if len(topics) < 3:
                continue
            frm = (topics[1] or "0x")[-40:].lower()
            to = (topics[2] or "0x")[-40:].lower()
            ticker, _cid, price = ent
            dec = self.decimals(token)
            try:
                amount = int(r.get("data") or "0x0", 16) / (10 ** dec)
            except (ValueError, TypeError):
                continue
            if amount <= 0:
                continue
            usd = amount * price
            dk = f"{token}|{frm}|{to}|{amount:.6f}|{ts // 30}"
            for wallet, direction in ((frm, "out"), (to, "in")):
                if wallet in wallet_index:
                    key, name, etype = wallet_index[wallet]
                    flows.append((ticker, Flow(
                        ts, self.chain_id, key, name, etype, wallet.upper(),
                        direction, amount, usd, "", "rpc", dk)))
        return flows
