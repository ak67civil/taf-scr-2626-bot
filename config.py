"""
config.py  –  Centralised configuration and runtime state.

v6.0 changes (Telethon → Pyrofork):
  • Removed UPLOAD_PART_SIZE (Pyrogram manages upload parts internally).
  • Removed MAX_RAM_BUFFER (no streaming pipeline — disk-based now).
  • Added DOWNLOAD_PROGRESS_INTERVAL / UPLOAD_PROGRESS_INTERVAL:
    Pyrogram progress callbacks fire every ~512KB chunk. For a 500MB file
    at 10MB/s that's ~500 callbacks in 50 seconds. Throttle to 8 seconds
    to prevent FloodWait across multiple concurrent users.
  • get_ptb_bot(): unchanged — PTB Bot API path still lives for small files.
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()

# ── TELEGRAM ──────────────────────────────────────────────────────────────────
API_ID         = int(os.environ.get("API_ID", 0))
API_HASH       = os.environ.get("API_HASH")
BOT_TOKEN      = os.environ.get("BOT_TOKEN")
MONGO_URI      = os.environ.get("MONGO_URI")
ADMIN_ID       = int(os.environ.get("ADMIN_ID", 0))
PORT           = int(os.environ.get("PORT", 8080))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "Owner")

# ── HEROKU ────────────────────────────────────────────────────────────────────
HEROKU_API_TOKEN = os.environ.get("HEROKU_API_TOKEN")
HEROKU_APP_NAME  = os.environ.get("HEROKU_APP_NAME")
WORKER_DYNO_SIZE = os.environ.get("WORKER_DYNO_SIZE", "standard-1x")

# ── TRANSFER CORE ─────────────────────────────────────────────────────────────
CHUNK_SIZE   = 1 * 1024 * 1024   # 1 MB — batch-size hint for get_messages
REQUEST_SIZE = 512 * 1024         # 512 KB — kept for legacy reference
UPDATE_INTERVAL = 5               # seconds between general status edits
MAX_RETRIES     = 5
REQUEST_RETRIES = 10

# ── PROGRESS CALLBACK THROTTLE ────────────────────────────────────────────────
#
# Pyrogram fires download/upload progress callbacks on every network chunk
# (~512 KB). For a 500 MB file at 10 MB/s that is ~500 callbacks in 50 s.
# Without throttling every callback triggers a message.edit → instant FloodWait.
#
# 8 seconds = safe for up to ~30 concurrent users each with active transfers.
# At 8 s: maximum ~7 edits/minute per file per user.
#
DOWNLOAD_PROGRESS_INTERVAL = 8   # seconds between download progress edits
UPLOAD_PROGRESS_INTERVAL   = 8   # seconds between upload progress edits

# ── PTB ROUTING ───────────────────────────────────────────────────────────────
#
# Files smaller than PTB_SMALL_FILE_LIMIT are downloaded into a BytesIO buffer
# and sent via Telegram Bot API (PTB / HTTP). Bot API has its own separate
# rate-limit pool from MTProto, so FloodWait here never affects main.py's
# Pyrogram command-handler.
#
# Files at or above this threshold are downloaded to disk and re-uploaded
# via Pyrogram bot_client (MTProto), supporting up to ~1.9 GB per file.
# Files above SPLIT_FILE_THRESHOLD are split into 1.9 GB parts automatically.
#
PTB_SMALL_FILE_LIMIT  = 45  * 1024 * 1024    # 45 MB
SPLIT_FILE_THRESHOLD  = int(1.9 * 1024 * 1024 * 1024)  # 1.9 GB

# ── iter_messages ZERO-PROGRESS PROTECTION ────────────────────────────────────
#
# Pyrogram get_messages() can return empty batches on network glitches or
# when a range has only deleted messages. We retry with backoff before
# concluding "done" to prevent fake "Transfer Complete" messages.
#
ZERO_PROGRESS_RETRIES = 5
ZERO_PROGRESS_DELAYS  = [20, 40, 60, 90, 120]   # seconds per retry attempt

# ── INTER-FILE SLEEP ──────────────────────────────────────────────────────────
SLEEP_BETWEEN_FILES = 2    # seconds between each file transfer
SLEEP_EVERY_10      = 4    # extra pause every 10 files

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ── RUNTIME STATE ─────────────────────────────────────────────────────────────
active_sessions  = {}
global_stop_flag = False

# ── PTB SINGLETON ─────────────────────────────────────────────────────────────
#
# One PTB Bot object per process, lazily created on first use.
# PTB uses HTTP — completely separate rate-limit infra from Pyrogram MTProto.
#
_ptb_bot_instance = None

async def get_ptb_bot():
    """
    Return the process-level PTB Bot singleton, initialising it on first call.
    """
    global _ptb_bot_instance
    if _ptb_bot_instance is None:
        from telegram import Bot as _PTBBot
        _ptb_bot_instance = _PTBBot(token=BOT_TOKEN)
        await _ptb_bot_instance.initialize()
        logger.info("✅ PTB Bot singleton initialised")
    return _ptb_bot_instance
