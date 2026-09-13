"""On-chain tracking of VC / CEX wallets via the Etherscan V2 API.

The wallet list lives in tracked_wallets.json and is reloaded on every scan,
so you can extend it (e.g. with Arkham entity addresses) without restarting.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("onchain")

# How many blocks back to look when scanning token transfers per chain.
# ETH ~12s/block -> 12000 blocks ~= 40h ; BSC ~3s/block -> 24000 blocks ~= 20h
WINDOW_BLOCKS = {1: 12000, 56: 24000}
MIN_FLOW_USD = 20_000  # ignore tracked-wallet dust


class EtherscanV2:
    BASE = "https://api.etherscan.io/v2/api"

    def __init__(self, api_key: str | None):
        self.key = api_key or ""
        self.enabled = bool(self.key)
        # free tier covers Ethereum mainnet; other chains need a paid plan
        self.supported_chains: set[int] = {1, 56}
        self.s = requests.Session()
        self._lock = threading.Lock()
        self._last = 0.0
        self._block_cache: dict[int, int | None] = {}
        self._block_cache_ts = 0.0

    def _throttle(self):
        # ~4.5 req/s, well under the free 5 req/s tier
        with self._lock:
            wait = 0.22 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _get(self, chain_id: int, params: dict, tries: int = 3):
        for i in range(tries):
            self._throttle()
            try:
                r = self.s.get(self.BASE, params={**params, "chainid": chain_id, "apikey": self.key},
                               timeout=25)
                d = r.json()
            except Exception as e:
                log.debug("etherscan request failed: %s", e)
                time.sleep(1.5)
                continue
            if d.get("status") == "1":
                return d.get("result", [])
            msg = str(d.get("message", ""))
            res = d.get("result")
            res_txt = res if isinstance(res, str) else ""
            if msg == "No transactions found" or res_txt == "":
                return []
            if "not supported for this chain" in res_txt.lower():
                self.supported_chains.discard(chain_id)
                log.warning("Etherscan free plan does not cover chain %d - "
                            "falling back to public RPC for it", chain_id)
                return []
            if "rate" in msg.lower():
                time.sleep(2 * (i + 1))
                continue
            log.debug("etherscan error: %s %s", d.get("code", ""), res_txt or msg)
            time.sleep(1)
        return None

    def latest_block(self, chain_id: int) -> int | None:
        if chain_id not in self.supported_chains:
            return None
        now = time.time()
        if now - self._block_cache_ts > 300:
            self._block_cache = {}
            self._block_cache_ts = now
        if chain_id not in self._block_cache:
            self._throttle()
            try:
                r = self.s.get(self.BASE, params={
                    "chainid": chain_id, "module": "proxy",
                    "action": "eth_blockNumber", "apikey": self.key}, timeout=25)
                d = r.json()
                res = d.get("result", "")
                if isinstance(res, str) and "not supported for this chain" in res.lower():
                    self.supported_chains.discard(chain_id)
                    log.warning("Etherscan free plan does not cover chain %d - "
                                "falling back to public RPC for it", chain_id)
                    self._block_cache[chain_id] = None
                    return None
                # JSON-RPC shape: {"jsonrpc":"2.0","id":1,"result":"0x18c17cb"}
                self._block_cache[chain_id] = int(res, 16)
            except Exception as e:
                log.debug("eth_blockNumber failed: %s", e)
                self._block_cache[chain_id] = None
        return self._block_cache[chain_id]

    def token_transfers_since(self, chain_id: int, contract: str, since_ts: float,
                              window_blocks: int, max_pages: int = 2,
                              per_page: int = 100) -> list[dict]:
        """Recent ERC-20/BEP-20 transfers for a token contract, oldest >= since_ts.

        V2 quirk: `offset` is ignored (up to 1000 newest rows returned), so we
        paginate by shrinking endblock to below the oldest row seen.
        """
        if not self.enabled or chain_id not in self.supported_chains:
            return []
        lb = self.latest_block(chain_id)
        if not lb:
            return []
        start = max(1, lb - window_blocks)
        end = lb
        out: list[dict] = []
        for _page in range(max_pages):
            res = self._get(chain_id, {
                "module": "account", "action": "tokentx",
                "contractaddress": contract,
                "startblock": start, "endblock": end,
                "sort": "desc", "offset": per_page,
            })
            if res is None:
                break
            rows = res if isinstance(res, list) else []
            if not rows:
                break
            oldest_ts = None
            oldest_block = None
            for row in rows:
                ts = int(row.get("timeStamp", 0))
                bn = int(row.get("blockNumber", 0))
                oldest_ts = ts if oldest_ts is None else min(oldest_ts, ts)
                oldest_block = bn if oldest_block is None else min(oldest_block, bn)
                if ts >= since_ts:
                    out.append(row)
            if oldest_ts is not None and oldest_ts < since_ts:
                break  # reached the window edge
            if len(rows) < 1000 or oldest_block is None:
                break
            end = oldest_block - 1  # next page: everything older than this
        return out


@dataclass
class Flow:
    ts: int            # unix seconds
    chain: int         # 1 = ETH, 56 = BSC
    entity: str        # entity key in tracked_wallets.json
    entity_name: str   # display name (e.g. "Jump Crypto")
    entity_type: str   # "vc" | "cex"
    wallet: str        # 0x address
    direction: str     # "in" = to tracked wallet, "out" = from tracked wallet
    amount: float      # in token units
    usd: float
    txhash: str
    source: str = "etherscan"   # "etherscan" | "dune"
    dedup_key: str = ""         # cross-source dedup: token|from|to|amount|30s bucket


class WalletTracker:
    def __init__(self, es: EtherscanV2, wallets_path: str):
        self.es = es
        self.path = wallets_path
        self.index: dict[str, tuple[str, str, str]] = {}
        self.entities: dict[str, dict] = {}
        self.enabled = False

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            self.entities = d.get("entities", {})
            self.index = {}
            for key, ent in self.entities.items():
                for addr in ent.get("addresses", {}):
                    self.index[addr.lower()] = (key, ent.get("name", key), ent.get("type", "vc"))
            self.enabled = len(self.index) > 0
            if self.enabled and not self.es.enabled:
                log.info("Wallet list loaded (%d addresses); no Etherscan key - "
                         "flows will come from public RPC / Dune", len(self.index))
        except Exception as e:
            log.warning("failed to load wallets from %s: %s", self.path, e)
            self.enabled = False

    def summary(self) -> str:
        if not self.entities:
            return "wallet list not loaded"
        parts = []
        for key, ent in self.entities.items():
            parts.append(f"{ent.get('name', key)} ({ent.get('type')}, {len(ent.get('addresses', {}))} addrs)")
        return ", ".join(parts)

    def scan_token(self, ticker: str, contracts: dict[int, str], since_ts: float,
                   price_usd: float) -> list[Flow]:
        """Find token transfers involving tracked wallets since since_ts (Etherscan only)."""
        flows: list[Flow] = []
        if not self.enabled or not self.es.enabled or not contracts or price_usd <= 0:
            return flows
        for chain_id, contract in contracts.items():
            if not contract:
                continue
            try:
                rows = self.es.token_transfers_since(
                    chain_id, contract, since_ts,
                    WINDOW_BLOCKS.get(chain_id, 15000))
            except Exception as e:
                log.debug("scan_token %s chain %s failed: %s", ticker, chain_id, e)
                continue
            for row in rows:
                frm = (row.get("from") or "").lower()
                to = (row.get("to") or "").lower()
                try:
                    dec = int(row.get("tokenDecimal", 18) or 18)
                    amount = float(row.get("value", 0)) / (10 ** dec)
                except Exception:
                    continue
                if amount <= 0:
                    continue
                usd = amount * price_usd
                ts = int(row.get("timeStamp", 0))
                txh = row.get("hash", "")
                token = contract.lower()
                dk = f"{token}|{frm}|{to}|{amount:.6f}|{ts // 30}"
                if frm in self.index:
                    key, name, etype = self.index[frm]
                    flows.append(Flow(ts, chain_id, key, name, etype, frm.upper(),
                                      "out", amount, usd, txh, "etherscan", dk))
                if to in self.index:
                    key, name, etype = self.index[to]
                    flows.append(Flow(ts, chain_id, key, name, etype, to.upper(),
                                      "in", amount, usd, txh, "etherscan", dk))
        return flows
