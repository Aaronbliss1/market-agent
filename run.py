"""Entry point.

  python run.py run        -> start the Telegram bot + scheduled jobs
  python run.py dry-run    -> run one full scan in the terminal (no Telegram, no DB writes)
  python run.py digest     -> print today's breakdown
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import load_config  # noqa: E402

TZ = ZoneInfo("Africa/Lagos")


def setup_logging():
    log_file = ROOT / "data" / "agent.log"
    log_file.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ])
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_run(_args):
    cfg = load_config(ROOT)
    if not cfg.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env, add your token, "
              "then run again. (Use `python run.py dry-run` to test without a token.)")
        sys.exit(1)
    from src.bot import build_app
    app = build_app(cfg)
    print("Starting Market Agent (long polling)... Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


def cmd_dry_run(_args):
    cfg = load_config(ROOT)
    from src.engine import Engine, format_call_message

    engine = Engine(cfg, store=None, messenger=None)
    print(f"Exchange status: {engine.x.status()}")
    es_chains = {1: "ETH", 56: "BSC"}
    es_txt = (f"key OK — covers {', '.join(es_chains[c] for c in sorted(engine.es.supported_chains))}"
              if engine.es.enabled else "no key")
    rpc_chains = [es_chains[c] for c in (1, 56)
                  if not (engine.es.enabled and c in engine.es.supported_chains)]
    print(f"On-chain sources: Etherscan [{es_txt}] | public RPC (keyless) [{', '.join(rpc_chains) or 'n/a'}]"
          f" | Dune [{'on' if engine.dune.enabled else 'off'}]")
    print(f"Wallets: {engine.wallets.summary()}\n")
    results = engine.run_scan()
    if not results:
        print("\nNo token cleared the confidence bar. Try lowering MIN_CONFIDENCE in .env.")
        return
    print(f"\n{'=' * 70}\nTOP {len(results)} CANDIDATES (dry run — nothing was recorded)\n{'=' * 70}")
    for s in results[:5]:
        print()
        print(format_call_message(s, None, cfg.max_leverage))
        print("-" * 70)


def cmd_digest(_args):
    cfg = load_config(ROOT)
    from src.engine import Engine
    from src.store import Store

    engine = Engine(cfg, Store(cfg.db_path), messenger=None)
    engine.run_digest()


def _headless_env_setup():
    """Headless mode (GitHub Actions): secrets arrive as environment variables."""
    import os
    db = os.getenv("AGENT_DB")
    if db:
        p = Path(db)
        if not p.is_absolute():
            p = ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
    return db


def cmd_headless(args):
    """Headless entry point — no long polling, state persisted via repo.

    `dispatch` runs every task that is due (scan/track/pulse/digest/commands)
    based on timestamps stored in the DB; safe to invoke on any cadence.
    """
    import os
    import time

    from config import load_config  # noqa: F401
    from src.engine import Engine
    from src.messenger import HeadlessMessenger
    from src.store import Store

    cfg = load_config(ROOT)
    db_override = _headless_env_setup()
    if db_override:
        cfg.db_path = str(Path(db_override))

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID", "").strip()
               or os.getenv("TELEGRAM_OWNER_ID", "").strip())
    store = Store(cfg.db_path)
    messenger = HeadlessMessenger(token, chat_id) if (token and str(chat_id).isdigit()) else None
    if messenger is None:
        print("WARNING: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing — "
              "results will not be delivered.")
    engine = Engine(cfg, store, messenger)

    now = time.time()

    def due(key: str, interval_min: int) -> bool:
        last = store.get_kv(key)
        return last is None or now - float(last) >= interval_min * 60

    def mark(key: str):
        store.set_kv(key, str(time.time()))

    mode = args.mode
    if mode != "dispatch":
        {"scan": engine.run_scan,
         "track": engine.run_track,
         "pulse": engine.run_mover_pulse,
         "digest": engine.run_digest}[mode]()
    else:
        # track first (alerts), then scan, pulse, digest
        if due("gh_last_track", cfg.track_interval_min):
            engine.run_track()
            mark("gh_last_track")
        if due("gh_last_scan", cfg.scan_interval_min):
            engine.run_scan()
            mark("gh_last_scan")
        if due("gh_last_pulse", cfg.pulse_interval_min):
            engine.run_mover_pulse()
            mark("gh_last_pulse")
        now_wat = datetime.fromtimestamp(now, TZ)
        if now_wat.hour * 60 + now_wat.minute >= cfg.digest_hour * 60 + cfg.digest_minute:
            if (store.get_kv("gh_last_digest_date") or "") != now_wat.strftime("%Y-%m-%d"):
                engine.run_digest()
                store.set_kv("gh_last_digest_date", now_wat.strftime("%Y-%m-%d"))
        # command poll every run (cheap: getUpdates timeout=0)
        if token and str(chat_id).isdigit():
            from src.commands import poll_commands
            poll_commands(token, chat_id, engine, store)

    # keep the persisted DB small
    try:
        n = store.prune_flows(7)
        if n:
            print(f"pruned {n} old wallet flows")
    except Exception as e:
        print(f"prune failed: {e}")


def main():
    setup_logging()
    p = argparse.ArgumentParser(description="Market analysis agent (Telegram)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start bot + scheduler")
    sub.add_parser("dry-run", help="one scan in the terminal, no Telegram/DB")
    sub.add_parser("digest", help="print today's breakdown")
    hp = sub.add_parser("headless", help="GitHub Actions mode (no long polling)")
    hp.add_argument("mode", choices=["dispatch", "scan", "track", "pulse", "digest"])
    args = p.parse_args()
    {"run": cmd_run, "dry-run": cmd_dry_run, "digest": cmd_digest,
     "headless": cmd_headless}[args.cmd](args)


if __name__ == "__main__":
    main()
