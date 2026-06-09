#!/usr/bin/env python3
"""
worker.py  –  Per-user transfer worker  (v6.0 — Pyrofork)

Runs inside a dedicated Heroku one-off dyno.
Usage (spawned by heroku_manager.py):
    python3 worker.py --user-id=12345 --task-id=abc-def-ghi

v6.0 changes:
  • TelegramClient → pyrogram.Client
  • receive_updates=False → no_updates=True  (Pyrofork param — CRITICAL)
    Without this the worker competes with main.py for the bot-token update
    stream causing the bot-becomes-unresponsive bug.
  • GetFullChannelRequest → bot_client.get_chat()  (preflight peer resolution)
  • user client: StringSession → session_string=  with in_memory=True
  • bot_client session: StringSession() → name=':memory:'
  • MockEvent.respond() signature updated: buttons= → reply_markup=
  • Session validity check: is_user_authorized() → get_me()
  • Disconnect: client.disconnect() → client.stop()
"""

import asyncio
import argparse
import gc
import logging
import os
import sys
import time

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

import psutil
from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    ChatAdminRequired, ChatWriteForbidden,
    ChannelPrivate,
    SessionExpired, AuthKeyUnregistered, AuthKeyInvalid,
    UserDeactivated, UserDeactivatedBan,
)

import config
import database as db
from transfer import transfer_process
from session_manager import session_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [WORKER] %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ── DYNO RAM LIMIT MAP ────────────────────────────────────────────────────────

DYNO_RAM_LIMITS = {
    "free":            512  * 1024 * 1024,
    "eco":             512  * 1024 * 1024,
    "basic":           512  * 1024 * 1024,
    "standard-1x":     512  * 1024 * 1024,
    "standard-2x":    1024  * 1024 * 1024,
    "performance-m":  2560  * 1024 * 1024,
    "performance-l": 14336  * 1024 * 1024,
}

def get_dyno_ram_limit() -> int:
    size = os.environ.get("WORKER_DYNO_SIZE", "standard-1x").lower().strip()
    return DYNO_RAM_LIMITS.get(size, 512 * 1024 * 1024)


# ── MOCK EVENT ────────────────────────────────────────────────────────────────

class MockEvent:
    """
    Thin adapter so transfer_process can call event.respond() without a real
    Pyrogram Message object available.

    respond() uses bot_client.send_message() directly.
    reply_markup= is the Pyrogram param name (not buttons= as in Telethon).
    """
    def __init__(self, bot_client: Client, chat_id: int):
        self.chat_id   = chat_id
        self._bot      = bot_client
        self.sender_id = None

    async def respond(self, text: str, reply_markup=None):
        try:
            return await self._bot.send_message(
                self.chat_id, text, reply_markup=reply_markup
            )
        except Exception as e:
            logger.warning(f"MockEvent.respond failed: {e}")
            return None


# ── RAM REPORTER ──────────────────────────────────────────────────────────────

async def ram_reporter(user_id: int, task_id: str, stop_event: asyncio.Event):
    process   = psutil.Process(os.getpid())
    interval  = 10
    ram_total = get_dyno_ram_limit()
    dyno_size = os.environ.get("WORKER_DYNO_SIZE", "standard-1x")
    total_mb  = ram_total / (1024 * 1024)
    logger.info(f"💾 Dyno RAM limit: {total_mb:.0f} MB ({dyno_size})")

    while not stop_event.is_set():
        try:
            mem_info = process.memory_info()
            ram_used = min(mem_info.rss, ram_total)
            used_mb  = ram_used / (1024 * 1024)
            pct      = ram_used / ram_total * 100 if ram_total else 0
            label    = f"{used_mb:.0f}MB / {total_mb:.0f}MB ({pct:.1f}%)"

            await db.update_dyno_ram(user_id, ram_used, ram_total, label)

            if await db.is_cleanup_requested(user_id):
                logger.info(f"🧹 RAM cleanup triggered for user {user_id}")
                before = process.memory_info().rss
                gc.collect()
                after  = process.memory_info().rss
                freed  = (before - after) / (1024 * 1024)
                logger.info(f"✅ gc.collect freed ~{freed:.1f} MB")
                await db.clear_cleanup_flag(user_id)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"ram_reporter error: {e}")

        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()),
                timeout=interval
            )
        except asyncio.TimeoutError:
            pass


# ── PREFLIGHT PEER RESOLUTION ─────────────────────────────────────────────────

async def worker_preflight(bot_client: Client, dest_id, chat_id: int) -> bool:
    """
    Resolve the destination peer in Pyrogram's internal cache BEFORE any
    file is sent.

    In Pyrogram, bot_client.get_chat() fetches full chat info and caches the
    peer internally. Without this, the first send_document/send_video call to
    a channel that this worker process has never accessed can fail with
    PeerIdInvalid. One get_chat() call permanently fixes this for the worker's
    lifetime.

    Returns True if OK to proceed, False if bot lacks permission (abort).
    """
    logger.info(f"🔍 Worker preflight: resolving dest peer {dest_id}…")
    try:
        chat = await bot_client.get_chat(dest_id)
        logger.info(f"✅ Worker preflight OK: {chat.title!r} ({dest_id})")
        return True

    except ChatAdminRequired:
        logger.error(f"❌ Bot is not admin in dest {dest_id}")
        try:
            await bot_client.send_message(
                chat_id,
                "❌ **Bot is not admin in destination channel.**\n\n"
                "Please add the bot as **Full Admin** in your destination "
                "channel/group and start the transfer again."
            )
        except Exception:
            pass
        return False

    except ChannelPrivate:
        logger.error(f"❌ Dest {dest_id} is private/inaccessible")
        try:
            await bot_client.send_message(
                chat_id,
                "❌ **Destination channel is private or bot is not a member.**\n\n"
                "Make sure the bot is added as **Full Admin** in the channel."
            )
        except Exception:
            pass
        return False

    except ChatWriteForbidden:
        logger.error(f"❌ Bot cannot write in dest {dest_id}")
        try:
            await bot_client.send_message(
                chat_id,
                "❌ **Bot cannot post in destination channel.**\n\n"
                "Check that the bot has the **Post Messages** permission."
            )
        except Exception:
            pass
        return False

    except FloodWait as e:
        logger.warning(f"Preflight FloodWait {e.x}s — waiting…")
        await asyncio.sleep(e.x + 2)
        return True   # non-fatal; just delayed

    except Exception as e:
        # Non-fatal — log and let transfer_process surface any real error.
        logger.warning(f"Worker preflight warning (non-fatal): {type(e).__name__}: {e}")
        return True


# ── MAIN ──────────────────────────────────────────────────────────────────────

async def main(user_id: int, task_id: str):
    logger.info(f"━━ Worker starting: user={user_id} task={task_id} ━━")

    await db.init_db()

    # ── Load task ──────────────────────────────────────────────────────────
    task = await db.get_transfer_task(task_id)
    if not task:
        logger.error(f"Task {task_id} not found in DB — exiting.")
        return

    task_data     = task['data']
    chat_id       = task_data['chat_id']
    dest_id       = task_data['dest_id']
    dest_topic_id = task_data.get('dest_topic_id')

    # ── Load user session ──────────────────────────────────────────────────
    is_valid, session_string, phone = await db.check_user(user_id)
    if not session_string:
        logger.error(f"No session for user {user_id} — exiting.")
        await db.update_task_status(task_id, 'failed')
        return

    users      = await db.get_all_users()
    first_name = next((u[3] for u in users if u[0] == user_id), "User")

    dyno_name = os.environ.get("DYNO", "run.unknown")
    await db.register_user_dyno(user_id, dyno_name, task_id, first_name)
    logger.info(f"📌 Registered as dyno: {dyno_name}")

    stop_event    = asyncio.Event()
    reporter_task = asyncio.create_task(ram_reporter(user_id, task_id, stop_event))

    # ── Connect bot_client (Pyrogram, no updates) ──────────────────────────
    # no_updates=True: this worker is a SENDER only. It never polls for updates.
    # Without this, the worker competes with main.py for the same bot-token
    # update stream → causes update conflicts → bot becomes unresponsive.
    bot_client = Client(
        name=":memory:",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        no_updates=True,        # ← CRITICAL: worker is sender only
        sleep_threshold=0,
        workers=4,
        in_memory=True,
    )
    await bot_client.start()
    logger.info("✅ Bot client started (no_updates=True)")

    # ── Connect user_client (Pyrogram, user session) ───────────────────────
    user_client = None
    try:
        user_client = Client(
            name=f":memory:_{user_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session_string,
            no_updates=True,    # user client reads source — no updates needed
            sleep_threshold=0,
            in_memory=True,
        )
        await user_client.start()
        me = await user_client.get_me()
        if not me:
            raise Exception("get_me() returned None — session invalid.")
        logger.info(f"✅ User client authorised: {me.first_name} ({me.id})")

    except (SessionExpired, AuthKeyUnregistered, AuthKeyInvalid):
        logger.error("User session invalid/expired — exiting.")
        await bot_client.send_message(
            chat_id,
            "❌ **Your Telegram session has expired.**\n\n"
            "Please use `/login` to reconnect your account and run `/clone` again."
        )
        await db.update_task_status(task_id, 'failed')
        await db.clear_user_dyno(user_id)
        stop_event.set()
        reporter_task.cancel()
        await bot_client.stop()
        return

    except (UserDeactivated, UserDeactivatedBan):
        logger.error("User account deactivated/banned.")
        await bot_client.send_message(
            chat_id,
            "❌ Your Telegram account has been deactivated or banned."
        )
        await db.update_task_status(task_id, 'failed')
        await db.clear_user_dyno(user_id)
        stop_event.set()
        reporter_task.cancel()
        await bot_client.stop()
        return

    except Exception as e:
        logger.error(f"User client start failed: {e}")
        await bot_client.send_message(
            chat_id,
            f"❌ Could not start transfer session.\n`{e}`\n\nTry `/login` again."
        )
        await db.update_task_status(task_id, 'failed')
        await db.clear_user_dyno(user_id)
        stop_event.set()
        reporter_task.cancel()
        await bot_client.stop()
        return

    # ── Initialise PTB Bot singleton ───────────────────────────────────────
    try:
        ptb_bot = await config.get_ptb_bot()
        logger.info("✅ PTB Bot singleton ready")
    except Exception as e:
        logger.warning(f"PTB init failed (small files will use bot_client): {e}")

    # ── Worker preflight: resolve dest peer ───────────────────────────────
    preflight_ok = await worker_preflight(bot_client, dest_id, chat_id)
    if not preflight_ok:
        await db.update_task_status(task_id, 'failed')
        await db.clear_user_dyno(user_id)
        stop_event.set()
        reporter_task.cancel()
        try: await bot_client.stop()
        except Exception: pass
        try: await user_client.stop()
        except Exception: pass
        return

    # ── Build MockEvent and session stub ───────────────────────────────────
    mock_event = MockEvent(bot_client, chat_id)
    session_id = task_data.get('session_id', task_id)
    config.active_sessions[session_id] = {
        'settings':      task_data.get('settings', {}),
        'user_id':       user_id,
        'step':          'running',
        'stop_flag':     False,
        'task_id':       task_id,
        'dest_topic_id': dest_topic_id,
    }

    await db.update_task_status(task_id, 'running')

    # ── Run transfer ───────────────────────────────────────────────────────
    try:
        await transfer_process(
            event         = mock_event,
            user_client   = user_client,
            bot_client    = bot_client,
            source_id     = task_data['source_id'],
            dest_id       = dest_id,
            start_msg     = task_data['start_msg'],
            end_msg       = task_data['end_msg'],
            session_id    = session_id,
            log_channel   = task_data.get('log_channel'),
            topic_id      = task_data.get('topic_id'),
            dest_topic_id = dest_topic_id,
        )
        await db.update_task_status(task_id, 'done')
        logger.info(f"✅ Transfer completed for user {user_id}")

    except Exception as e:
        logger.error(f"transfer_process crashed: {e}", exc_info=True)
        await db.update_task_status(task_id, 'failed')

    finally:
        stop_event.set()
        reporter_task.cancel()

        try:
            await db.clear_user_dyno(user_id)
            logger.info(f"✅ Dyno record cleared for user {user_id}")
        except Exception as cleanup_err:
            logger.error(f"clear_user_dyno failed: {cleanup_err}")

        try: await bot_client.stop()
        except Exception: pass

        try: await user_client.stop()
        except Exception: pass

        logger.info(f"━━ Worker done: user={user_id} task={task_id} ━━")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Per-user Telegram transfer worker")
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--task-id', type=str, required=True)
    args = parser.parse_args()

    try:
        asyncio.run(main(args.user_id, args.task_id))
    except (KeyboardInterrupt, SystemExit):
        logger.info("Worker interrupted, exiting cleanly.")

    sys.exit(0)
