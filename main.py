#!/usr/bin/env python3
"""
main.py  –  Bot entry point  (v6.0 — Pyrofork)

Changes from v5.0 (Telethon):
  • TelegramClient → pyrogram.Client
  • run_until_disconnected() → pyrogram.idle()
  • sleep_threshold=0 so FloodWait is raised, not silently eaten by Pyrogram
  • workers=8 gives 8 concurrent update-handler coroutines
"""

import asyncio

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from pyrogram import Client, idle
from aiohttp import web

import config
from handlers import register_handlers
import database as db


# ── WEB SERVER ────────────────────────────────────────────────────────────────

async def handle(request):
    return web.Response(text="🔥 Content Saver Bot v6.0 — Pyrofork Edition")


async def start_web_server():
    app    = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', config.PORT)
    await site.start()
    config.logger.info(f"⚡ Web Server — Port {config.PORT}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main():
    config.logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    config.logger.info("🚀 Content Saver Bot v6.0 (Pyrofork) Starting…")
    config.logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if not config.API_ID or not config.API_HASH:
        config.logger.error("❌ MISSING CONFIGURATION — API_ID / API_HASH not set. Exiting.")
        return

    # ── Database ──────────────────────────────────────────────────────────────
    await db.init_db()
    config.logger.info("💾 Database Initialised")

    # ── Pyrogram bot client ───────────────────────────────────────────────────
    # sleep_threshold=0:  Pyrogram will RAISE FloodWait instead of silently
    #                     sleeping — we handle it ourselves in each handler.
    # workers=8:          8 concurrent update-handler coroutines.
    bot_client = Client(
        name="bot_session",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        sleep_threshold=0,
        workers=8,
    )

    await bot_client.start()
    config.logger.info("✅ Bot client started")

    # ── Register all handlers ─────────────────────────────────────────────────
    register_handlers(bot_client)

    # ── Web server ────────────────────────────────────────────────────────────
    asyncio.create_task(start_web_server())

    config.logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    config.logger.info("✅ System Online!")
    config.logger.info(f"👑 Admin ID: {config.ADMIN_ID}")
    config.logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ── Run until Ctrl-C ──────────────────────────────────────────────────────
    await idle()
    await bot_client.stop()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        config.logger.info("Bot stopped.")
