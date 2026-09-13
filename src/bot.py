"""Telegram bot (python-telegram-bot v21) + APScheduler job wiring."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from .dune import DUNE_QUERY_SQL
from .engine import Engine, STATUS_ICON
from .store import Store

log = logging.getLogger("bot")
TZ = ZoneInfo("Africa/Lagos")


class Messenger:
    """Thread-safe bridge from the (blocking) engine to the Telegram bot."""

    def __init__(self, bot, chat_id: int | None):
        self.bot = bot
        self.chat_id = chat_id
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop):
        self.loop = loop

    def _send(self, coro):
        if not (self.chat_id and self.loop):
            log.warning("telegram send dropped — chat not bound yet (send /start) "
                        f"[chat_id={self.chat_id}, loop={bool(self.loop)}]")
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
            fut.result(timeout=120)
        except Exception as e:
            log.warning("telegram send failed: %s", e)

    def send_text(self, text: str):
        self._send(self.bot.send_message(self.chat_id, text[:4000]))

    def send_photo(self, path: str, caption: str):
        with open(path, "rb") as fh:
            self._send(self.bot.send_photo(self.chat_id, photo=fh, caption=caption[:1020]))


def _fmt(p) -> str:
    if p is None:
        return "?"
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.10g}"


def build_app(cfg) -> Application:
    app = Application.builder().token(cfg.telegram_bot_token).build()
    ctx: dict = {}

    # ---------------- scheduled jobs ----------------
    async def scan_job():
        try:
            await asyncio.to_thread(ctx["engine"].run_scan)
        except Exception:
            log.exception("scan job failed")
            await _err(app, "scan")

    async def track_job():
        try:
            await asyncio.to_thread(ctx["engine"].run_track)
        except Exception:
            log.exception("track job failed")
            await _err(app, "track")

    async def digest_job():
        try:
            await asyncio.to_thread(ctx["engine"].run_digest)
        except Exception:
            log.exception("digest job failed")
            await _err(app, "digest")

    async def pulse_job():
        try:
            await asyncio.to_thread(ctx["engine"].run_mover_pulse)
        except Exception:
            log.exception("mover pulse job failed")
            await _err(app, "mover-pulse")

    async def _err(app, what):
        m: Messenger | None = ctx.get("messenger")
        if m:
            m.send_text(f"⚠️ {what} job crashed — see server logs.")

    async def post_init(app: Application):
        store = Store(cfg.db_path)
        chat_id = cfg.owner_id
        if not chat_id:
            saved = store.get_kv("chat_id")
            try:
                chat_id = int(saved) if saved else None
            except (TypeError, ValueError):
                chat_id = None
        if chat_id:
            log.info("restoring chat binding: %s", chat_id)
        messenger = Messenger(app.bot, chat_id=chat_id)
        messenger.attach_loop(asyncio.get_running_loop())
        engine = Engine(cfg, store, messenger)
        ctx["engine"] = engine
        ctx["messenger"] = messenger

        tz = TZ
        scheduler = AsyncIOScheduler(timezone=tz)
        scheduler.add_job(
            scan_job,
            IntervalTrigger(minutes=cfg.scan_interval_min, timezone=tz),
            next_run_time=datetime.now(tz) + timedelta(seconds=20),
            id="scan", max_instances=1, coalesce=True)
        scheduler.add_job(
            track_job,
            IntervalTrigger(minutes=cfg.track_interval_min, timezone=tz),
            id="track", max_instances=1, coalesce=True)
        scheduler.add_job(
            pulse_job,
            IntervalTrigger(minutes=cfg.pulse_interval_min, timezone=tz),
            id="mover-pulse", max_instances=1, coalesce=True)
        scheduler.add_job(
            digest_job,
            CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute, timezone=tz),
            id="digest", max_instances=1)
        scheduler.start()
        ctx["scheduler"] = scheduler
        log.info("agent online — first scan in 20s, then every %d min; "
                 "digest daily at %02d:%02d WAT", cfg.scan_interval_min,
                 cfg.digest_hour, cfg.digest_minute)

    async def post_shutdown(app: Application):
        if "scheduler" in ctx:
            ctx["scheduler"].shutdown(wait=False)

    # ---------------- commands ----------------
    async def cmd_start(update: Update, c: ContextTypes.DEFAULT_TYPE):
        m: Messenger | None = ctx.get("messenger")
        engine: Engine | None = ctx.get("engine")
        if m and m.chat_id != update.effective_chat.id:
            m.chat_id = update.effective_chat.id
            if engine and engine.store:
                engine.store.set_kv("chat_id", str(update.effective_chat.id))
                log.info("chat bound: %s", update.effective_chat.id)
        extra = ""
        if engine and not engine.wallets.enabled:
            extra = ("\n\n⚠️ On-chain wallet tracking is OFF — put your free "
                     "ETHERSCAN_API_KEY in .env and restart to enable it.")
        await update.message.reply_text(
            "🤖 Market Agent online.\n\n"
            "I scan the top tokens on Binance, Bybit & Bitget for VC/CEX on-chain "
            "activity, news and technicals (RSI, MACD, MA/EMA, ATR), then send up to "
            "10 calls a day with direction, leverage, target and a chart. I update you "
            "when a target or stop hits, and post a full daily breakdown at "
            f"{cfg.digest_hour:02d}:{cfg.digest_minute:02d} WAT.\n\n"
            f"Type /help for all commands.{extra}")

    async def cmd_help(update: Update, c: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Commands:\n"
            "/status — agent, market & wallet health\n"
            "/movers — tokens making a move right now\n"
            "/calls — recent calls and their status\n"
            "/scan — run a full market scan right now\n"
            "/dry — test mode: show top candidates without recording them\n"
            "/wallets — tracked VC/CEX wallets\n"
            "/dune_setup — Dune on-chain setup (SQL + steps)\n"
            "/cancel <id> — cancel an open call\n"
            "/start — reconnect after a restart")

    async def cmd_status(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        await update.message.reply_text(await asyncio.to_thread(engine.status_text))

    async def cmd_movers(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        await update.message.reply_text(
            await asyncio.to_thread(engine.movers_snapshot))

    async def cmd_calls(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        rows = await asyncio.to_thread(engine.store.recent_calls, 10)
        if not rows:
            await update.message.reply_text("No calls yet.")
            return
        out = []
        for r in rows:
            icon = STATUS_ICON.get(r["status"], "")
            ret = f" | {r['return_pct']:+.0f}%" if r["status"] != "open" else ""
            out.append(f"{icon} #{r['id']} {r['ticker']} {r['direction']} @ {r['exchange']} "
                       f"{r['leverage']}x — {r['status'].replace('_', ' ')}{ret} "
                       f"(entry {_fmt(r['entry'])}, target {_fmt(r['target'])})")
        await update.message.reply_text("Recent calls:\n\n" + "\n".join(out))

    async def cmd_scan(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        await update.message.reply_text("⏳ Running full market scan (1–5 min)...")
        results = await asyncio.to_thread(engine.run_scan)
        posted = [s.ticker for s in (results or [])]
        await update.message.reply_text(
            f"✅ Scan complete. {len(results)} token(s) cleared the confidence bar"
            + (f": {', '.join(posted)}" if posted else "")
            + (". None posted (daily cap reached or already called today)." if not posted else ""))

    async def cmd_dry(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        from .engine import NullMessenger, format_call_message
        await update.message.reply_text("🧪 Dry run: scanning without recording (1–5 min)...")

        class _Capture:
            def __init__(self):
                self.msgs = []

            def send_text(self, t):
                self.msgs.append(t)

            def send_photo(self, p, cap):
                self.msgs.append(f"[chart {p}]\n{cap}")

        cap = _Capture()
        engine_dry = Engine(cfg, None, cap)
        results = await asyncio.to_thread(engine_dry.run_scan)
        if not results:
            await update.message.reply_text("🧪 Dry run: no token cleared the bar. "
                                            "(Try lowering MIN_CONFIDENCE in .env.)")
            return
        out = ["🧪 DRY RUN — top candidates (NOT recorded, no trading implied):", ""]
        for s in results[:3]:
            out.append(format_call_message(s, None, cfg.max_leverage))
            out.append("")
        await update.message.reply_text(out[0], )
        for msg in out[1:]:
            if msg.strip():
                await update.message.reply_text(msg[:4000])

    async def cmd_wallets(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        await asyncio.to_thread(engine.wallets.load)
        lines = ["📇 Tracked wallets (hot-reloaded each scan):"]
        for key, ent in engine.wallets.entities.items():
            lines.append(f"\n{ent.get('name', key)} — {ent.get('type')} ({len(ent.get('addresses', {}))} addresses)")
            for addr, label in list(ent.get("addresses", {}).items())[:6]:
                lines.append(f"  • {addr[:10]}…{addr[-4:]} — {label}")
            n = len(ent.get("addresses", {}))
            if n > 6:
                lines.append(f"  …and {n - 6} more (see tracked_wallets.json)")
        lines.append("\nExtend the list in tracked_wallets.json (e.g. paste Arkham entity addresses) — no restart needed.")
        await update.message.reply_text("\n".join(lines))

    async def cmd_dune_setup(update: Update, c: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "🐉 Dune setup — 3 steps:\n"
            "1. In dune.com: New query → select ALL existing text (Ctrl+A) → "
            "delete → paste the SQL below → Run (hundreds of rows expected) → "
            "Save. There are NO parameters to configure — the wallet list is "
            "already inside the SQL.\n"
            "2. Copy the numeric query id from the URL (dune.com/queries/123456).\n"
            "3. In .env set DUNE_API_KEY (dune.com → your profile → API → create "
            "key) and DUNE_QUERY_ID=that id, then restart the agent.\n\n"
            "Note: the tracked-wallet list is baked into the SQL. If you later "
            "add wallets to tracked_wallets.json, send /dune_setup again for "
            "fresh SQL and re-save the query.\n\n"
            "SQL to save:\n```\n" + DUNE_QUERY_SQL.rstrip() + "\n```")

    async def cmd_cancel(update: Update, c: ContextTypes.DEFAULT_TYPE):
        engine = ctx.get("engine")
        try:
            call_id = int(update.message.text.split()[1])
        except (IndexError, ValueError):
            await update.message.reply_text("Usage: /cancel <call id>")
            return
        ok = await asyncio.to_thread(engine.store.cancel_call, call_id)
        await update.message.reply_text(
            f"✖️ Call #{call_id} cancelled." if ok else f"Call #{call_id} not found or already closed.")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("movers", cmd_movers))
    app.add_handler(CommandHandler("calls", cmd_calls))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("dry", cmd_dry))
    app.add_handler(CommandHandler("wallets", cmd_wallets))
    app.add_handler(CommandHandler("dune_setup", cmd_dune_setup))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app
