"""Dune Analytics client — secondary on-chain wallet tracking (Dune Data API v1).

Dune runs a SAVED query (see DUNE_QUERY_SQL below / the /dune_setup bot command)
that returns every token transfer involving the tracked wallets on Ethereum +
BSC over the last 24 hours. The wallet list and window are baked into the SQL
(no query parameters — the current Dune editor has no parameter UI we can rely
on), so changing wallets means regenerating the SQL via /dune_setup and
re-saving the query.

Dataset: tokens.transfers (Dune's unified token table). Addresses there are
varbinary, so the query converts them to 0x... hex text; `amount_usd` is
pre-computed by Dune.

Flow per fetch (Dune Data API v1):
  POST /api/v1/query/{query_id}/execute   (no params needed)
  poll GET /api/v1/execution/{id}/status until QUERY_STATE_*
  GET  /api/v1/execution/{id}/results (offset pagination)

Dune data lags a few minutes, so the agent re-fetches on a throttled cadence
(DUNE_MIN_INTERVAL_MIN, default 30m) and dedups against Etherscan/RPC rows by
(token, from, to, amount, 30s bucket).
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("dune")

# --------------------------------------------------------------------------
# Save this as a query in your Dune workspace (dune.com):
#   New query -> paste -> Run -> Save. There are NO parameters to configure.
# Copy the numeric query id from the URL (dune.com/queries/<ID>) into .env
# as DUNE_QUERY_ID. To change the tracked wallets, send /dune_setup to the
# bot for fresh SQL and re-save.
# --------------------------------------------------------------------------
DUNE_QUERY_SQL = """\
-- Market Agent: tracked-wallet token flows (Ethereum + BSC, last 24h)
-- Wallet list is baked in below. To change wallets, regenerate the SQL
-- via /dune_setup in the bot and re-save this query.
WITH tracked AS (
    SELECT lower(trim(w.addr)) AS address
    FROM unnest(split('0xf977814e90d4ca1a489c46fa4fe775a06851cd6c,0x28c6c06298d514db089934071355e5743bf21d60,0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be,0x564286362092d8e7936f0549571a803b203aaced,0x21a31ee1afc51d94c2efccaa2092ad1028285549,0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503,0x9696f59e4d72e237be84ffd425dcad154bf96996,0xbe0eb53fcd69955483ae834c84efb1f0d2d6c40a,0x562d6d97a4e2db42a85a751d4e2623b88b551328,0x8894e0a0c962cb72391924da17a26adc26f97704,0x1f9090aae28b8a3dceadf281b0f1452c34ba8f4d,0xf89d7b9c864f589bbf53a82105107622b35eaa40,0xbaed383ede0e5d9d72430661f3285daa77e9439f,0xee5b5b923ffce93a870b3104b7ca09c3db80047a,0x1db92e2eebc8e0c075a02bea49a2935bcd2dfcf4,0x0639556f03714a74a5feeaf5736a4a64ff70d206,0xf584f8728b874a6a5c7a8d4d387c9aae9172d621,0xe5b5cf0e5d044c3e984b910dbc89b19186d24b03', ',')) AS w(addr)
),
transfers AS (
    SELECT
        t.blockchain AS chain,
        '0x' || lower(to_hex(t.contract_address)) AS token_address,
        '0x' || lower(to_hex(t."from")) AS from_address,
        '0x' || lower(to_hex(t."to")) AS to_address,
        t.amount AS amount,
        t.amount_usd AS amount_usd,
        to_unixtime(t.block_time) AS ts
    FROM tokens.transfers t
    WHERE t.blockchain IN ('ethereum', 'bnb')
      AND t.token_standard <> 'native'
      AND t.block_time >= now() - INTERVAL '24' HOUR
      AND t.block_date >= cast(now() - INTERVAL '24' HOUR AS date)
)
SELECT
    tr.chain,
    tr.token_address,
    tr.from_address,
    tr.to_address,
    tr.amount,
    tr.amount_usd,
    tr.ts,
    CASE WHEN tf.address IS NOT NULL THEN 'out' ELSE 'in' END AS direction,
    tf.address AS from_entity,
    tt.address AS to_entity
FROM transfers tr
LEFT JOIN tracked tf ON tf.address = tr.from_address
LEFT JOIN tracked tt ON tt.address = tr.to_address
WHERE (tf.address IS NOT NULL AND tt.address IS NULL)
   OR (tt.address IS NOT NULL AND tf.address IS NULL);
"""

TERMINAL_FAIL = {"QUERY_STATE_FAILED", "QUERY_STATE_CANCELED", "QUERY_STATE_EXPIRED"}


class DuneClient:
    BASE = "https://api.dune.com/api/v1"

    def __init__(self, api_key: str | None, query_id: str | None):
        self.key = (api_key or "").strip()
        self.query_id = str(query_id or "").strip()
        self.enabled = bool(self.key and self.query_id.isdigit())
        self.s = requests.Session()
        if self.key:
            self.s.headers["X-Dune-API-Key"] = self.key
        self.last_fetch = 0.0
        self.last_flows = 0
        self.last_error = ""
        self.last_rows: list[dict] = []  # cached for throttled scans

    # ------------------------------------------------------------------ exec
    def run_saved_query(self, params: dict | None = None,
                        timeout: int = 240, poll: int = 5) -> list[dict]:
        """Execute the saved query and return all result rows.

        The saved query (DUNE_QUERY_SQL) is parameter-free, so no body fields
        are sent; `params` is kept for forward compatibility.
        """
        self.last_error = ""
        # 1) start execution
        try:
            body = {"query_parameters": params} if params else {}
            r = self.s.post(
                f"{self.BASE}/query/{self.query_id}/execute",
                json=body,
                timeout=30)
        except Exception as e:
            self.last_error = f"start execution failed: {e}"
            log.warning("%s", self.last_error)
            return []
        if r.status_code not in (200, 201):
            self.last_error = f"start execution: HTTP {r.status_code} {r.text[:200]}"
            log.warning("%s", self.last_error)
            return []
        try:
            d = r.json()
        except Exception:
            self.last_error = f"no JSON in start-execution response: {r.text[:200]}"
            log.warning("%s", self.last_error)
            return []
        execution_id = d.get("execution_id") or d.get("id")
        if not execution_id:
            self.last_error = f"no execution_id in response: {r.text[:200]}"
            log.warning("%s", self.last_error)
            return []
        state = str(d.get("state") or "")

        # 2) poll status
        deadline = time.time() + timeout
        while state not in TERMINAL_FAIL and not state.startswith("QUERY_STATE_COMPLETED"):
            if time.time() > deadline:
                self.last_error = f"timed out after {timeout}s (last state: {state})"
                log.warning("%s", self.last_error)
                return []
            time.sleep(poll)
            try:
                st = self.s.get(f"{self.BASE}/execution/{execution_id}/status",
                                timeout=30).json()
            except Exception as e:
                log.debug("dune status poll error: %s", e)
                continue
            state = str(st.get("state") or state)
            if state in TERMINAL_FAIL:
                err = (st.get("error") or {}).get("message") or st.get("status_info") or ""
                self.last_error = f"execution {state}: {err[:300]}"
                log.warning("%s", self.last_error)
                return []
        if state not in ("QUERY_STATE_COMPLETED", "QUERY_STATE_COMPLETED_PARTIAL"):
            self.last_error = f"unexpected terminal state: {state}"
            log.warning("%s", self.last_error)
            return []

        # 3) fetch results (offset pagination)
        results: list[dict] = []
        offset = 0
        partial = state == "QUERY_STATE_COMPLETED_PARTIAL"
        while True:
            q: dict = {"limit": 1000, "offset": offset}
            if partial:
                q["allow_partial_results"] = "true"
            try:
                r = self.s.get(f"{self.BASE}/execution/{execution_id}/results",
                               params=q, timeout=60)
                d = r.json()
            except Exception as e:
                self.last_error = f"fetching results failed: {e}"
                log.warning("%s", self.last_error)
                break
            rows = (d.get("result") or {}).get("rows") or d.get("results") or []
            results.extend(rows)
            nxt = d.get("next_offset")
            if nxt is not None:
                offset = int(nxt)
                continue
            break
        self.last_rows = results
        return results
