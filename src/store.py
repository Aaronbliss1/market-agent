"""SQLite state: trade calls, token contract mapping, wallet flows, misc kv."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from .onchain import Flow

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ticker TEXT NOT NULL,
    exchange TEXT NOT NULL,
    exchanges TEXT,
    direction TEXT NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    leverage INTEGER NOT NULL,
    target_pct REAL NOT NULL,
    target_roi REAL NOT NULL,
    confidence REAL,
    breakdown TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    closed_ts REAL,
    close_price REAL,
    return_pct REAL
);
CREATE TABLE IF NOT EXISTS token_meta(
    ticker TEXT PRIMARY KEY,
    contracts TEXT,
    first_seen REAL
);
CREATE TABLE IF NOT EXISTS wallet_flows(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    ticker TEXT,
    chain INTEGER,
    entity TEXT,
    wallet TEXT,
    direction TEXT,
    amount REAL,
    usd REAL,
    source TEXT,
    txhash TEXT,
    dedup_key TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # v1 -> v2 migration: old wallet_flows lacked source/dedup_key
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(wallet_flows)").fetchall()}
        if cols and "dedup_key" not in cols:
            self.db.execute("DROP TABLE wallet_flows")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            rows = self.db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------- calls ----------
    def add_call(self, spec) -> int:
        cur = self._exec(
            "INSERT INTO calls(ts,ticker,exchange,exchanges,direction,entry,stop,target,"
            "leverage,target_pct,target_roi,confidence,breakdown) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (spec.ts, spec.ticker, spec.exchange, json.dumps(spec.exchanges), spec.direction,
             spec.entry, spec.stop, spec.target, spec.leverage, spec.target_pct,
             spec.target_roi, spec.confidence, json.dumps({
                 "tech": spec.tech, "onchain": spec.onchain_note,
                 "news": [i["title"] for i in spec.news],
             })))
        return int(cur.lastrowid)

    def open_calls(self) -> list[dict]:
        return self._query("SELECT * FROM calls WHERE status='open' ORDER BY ts DESC")

    def calls_since(self, since_ts: float) -> list[dict]:
        return self._query("SELECT * FROM calls WHERE ts >= ? ORDER BY ts DESC", (since_ts,))

    def recent_calls(self, n: int = 10) -> list[dict]:
        return self._query("SELECT * FROM calls ORDER BY ts DESC LIMIT ?", (n,))

    def get_call(self, call_id: int) -> dict | None:
        rows = self._query("SELECT * FROM calls WHERE id=?", (call_id,))
        return rows[0] if rows else None

    def close_call(self, call_id: int, status: str, close_price: float, return_pct: float):
        self._exec("UPDATE calls SET status=?, closed_ts=?, close_price=?, return_pct=? WHERE id=?",
                   (status, time.time(), close_price, return_pct, call_id))

    def cancel_call(self, call_id: int) -> bool:
        rows = self.get_call(call_id)
        if rows and rows["status"] == "open":
            self.close_call(call_id, "CANCELLED", rows["entry"], 0.0)
            return True
        return False

    # ---------- token meta ----------
    def get_contracts(self, ticker: str) -> dict | None:
        rows = self._query("SELECT contracts FROM token_meta WHERE ticker=?", (ticker,))
        if rows and rows[0]["contracts"]:
            try:
                return json.loads(rows[0]["contracts"])
            except Exception:
                return None
        return None

    def set_contracts(self, ticker: str, contracts: dict):
        self._exec(
            "INSERT INTO token_meta(ticker,contracts,first_seen) VALUES(?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET contracts=excluded.contracts",
            (ticker, json.dumps(contracts), time.time()))

    # ---------- flows ----------
    def add_flow(self, f: Flow, ticker: str) -> bool:
        try:
            cur = self._exec(
                "INSERT INTO wallet_flows(ts,ticker,chain,entity,wallet,direction,amount,usd,source,txhash,dedup_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f.ts, ticker, f.chain, f.entity, f.wallet, f.direction,
                 f.amount, f.usd, f.source, f.txhash or None, f.dedup_key or None))
            return cur.lastrowid is not None
        except sqlite3.IntegrityError:
            return False

    def flows_since(self, since_ts: float) -> list[dict]:
        return self._query("SELECT * FROM wallet_flows WHERE ts >= ? ORDER BY ts DESC", (since_ts,))

    def prune_flows(self, max_age_days: int = 7) -> int:
        """Delete flow history older than max_age_days (keeps the repo DB small)."""
        cutoff = time.time() - max_age_days * 86400
        cur = self._exec("DELETE FROM wallet_flows WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0

    # ---------- kv ----------
    def get_kv(self, key: str, default=None):
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_kv(self, key: str, value: str):
        self._exec("INSERT INTO kv(key,value) VALUES(?,?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
