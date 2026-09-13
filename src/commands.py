"""Chat command handling for headless mode (GitHub Actions).

A scheduled job polls Telegram for pending updates (getUpdates with timeout=0),
answers the supported commands, and stores the last processed update id in the
DB so every update is handled exactly once.

Available in headless mode: /start /status /movers /cancel /dune_setup
"""
from __future__ import annotations

import logging

import requests

from .dune import DUNE_QUERY_SQL
from .messenger import HeadlessMessenger

log = logging.getLogger("commands")

SUPPORTED = {"/start", "/status", "/movers", "/cancel", "/dune_setup"}


def poll_commands(token: str, chat_id, engine, store) -> None:
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"timeout": 0, "allowed_updates": '["message"]'},
                         timeout=30).json()
    except Exception as e:
        log.warning("commands: getUpdates failed: %s", e)
        return
    updates = r.get("result") or []
    if not updates:
        return

    last_id = int(store.get_kv("gh_last_update_id") or 0)
    m = HeadlessMessenger(token, chat_id)
    processed = 0
    for u in updates:
        uid = int(u.get("update_id") or 0)
        if uid <= last_id:
            continue
        msg = u.get("message") or {}
        text = (msg.get("text") or "").strip()
        if msg.get("chat", {}).get("id") != int(chat_id) or not text.startswith("/"):
            last_id = max(last_id, uid)
            continue
        cmd = text.split()[0].lower()
        if cmd not in SUPPORTED:
            last_id = max(last_id, uid)
            continue
        try:
            if cmd == "/start":
                m.send_text("🤖 Market Agent (headless mode) online.\n"
                            "Commands: /status /movers /cancel /dune_setup")
            elif cmd == "/status":
                m.send_text(engine.status_text())
            elif cmd == "/movers":
                m.send_text(engine.movers_snapshot())
            elif cmd == "/dune_setup":
                m.send_text("SQL to save:\n```\n" + DUNE_QUERY_SQL.rstrip() + "\n```")
            elif cmd == "/cancel":
                open_calls = store.open_calls()
                if not open_calls:
                    m.send_text("No open calls to cancel.")
                    last_id = max(last_id, uid)
                    continue
                parts = text.split()
                arg = parts[1] if len(parts) > 1 else None
                targets = [c for c in open_calls
                           if arg is None or str(c["id"]) == arg
                           or c["ticker"].upper() == arg.upper()]
                names = []
                for c in targets:
                    if store.cancel_call(c["id"]):
                        names.append(f"#{c['id']} {c['ticker']} {c['direction']}")
                m.send_text(("✖️ Cancelled: " + ", ".join(names)) if names
                            else "Nothing matched that id/ticker.")
            processed += 1
        except Exception as e:
            log.warning("commands: handler %s failed: %s", cmd, e)
        last_id = max(last_id, uid)
    if processed:
        log.info("commands: processed %d update(s)", processed)
    if last_id:
        store.set_kv("gh_last_update_id", str(last_id))
