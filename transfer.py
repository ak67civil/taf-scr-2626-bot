"""
transfer.py  –  Main transfer engine  (v6.2 — Pyrofork)

CHANGES in v6.2:
  1. Removed "Direct (Fast)" forward path for public channels entirely.
     All sources (public + private) now go through download→upload pipeline.
     This ensures:
       - Custom thumbnails work on public channel videos too
       - Caption/filename manipulations apply correctly
       - Consistent behaviour regardless of source type

  2. Text / web-page messages sent via bot_client (not user_client).

  3. Progress updates via 8-second throttled TransferProgress callbacks only.

  4. Custom transfer thumbnail: downloaded once, applied to all videos.

  5. PTB send_video passes duration / width / height / thumbnail.

  6. Media metadata extracted before PTB/disk branch.
"""

import asyncio
import math
import os
import time
from io import BytesIO

from pyrogram.enums import ParseMode
from pyrogram.errors import (
    FloodWait, SlowmodeWait,
    ChatWriteForbidden, ChatAdminRequired,
    UserBannedInChannel, BroadcastForbidden, ChannelPrivate,
    ChatForwardsRestricted,
    SessionExpired, AuthKeyUnregistered, AuthKeyInvalid,
    PeerIdInvalid,
)

import config
from utils import (
    human_readable_size, time_formatter,
    get_target_info, get_media_file_size,
    apply_filename_manipulations, apply_caption_manipulations,
    sanitize_filename, is_special_media,
)
from progress import TransferProgress
from keyboards import get_progress_keyboard
from session_manager import session_manager
import database as db


# ── CONSTANTS ─────────────────────────────────────────────────────────────────

SPLIT_THRESHOLD     = config.SPLIT_FILE_THRESHOLD   # 1.9 GB
CHECKPOINT_INTERVAL = 50
GET_MESSAGES_BATCH  = 200    # Pyrogram max per get_messages() call


# ── SMALL HELPERS ─────────────────────────────────────────────────────────────

def _ram_bar(ram_used: int, ram_total: int) -> str:
    if not ram_total:
        return ""
    pct      = ram_used / ram_total * 100
    used_mb  = ram_used  / (1024 * 1024)
    total_mb = ram_total / (1024 * 1024)
    filled   = min(10, int(pct / 10))
    bar      = "█" * filled + "░" * (10 - filled)
    status   = "✅ Safe" if pct < 60 else ("⚠️ High" if pct < 80 else "🔴 Critical")
    return (
        f"🧠 RAM: **{bar} {pct:.1f}%**\n"
        f"      `{used_mb:.0f}MB / {total_mb:.0f}MB` {status}\n"
    )


async def safe_edit_message(message, text: str, reply_markup=None):
    """Fire-and-forget message edit. Never raises."""
    async def _edit():
        try:
            await message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
    asyncio.create_task(_edit())


def _is_in_topic(msg, topic_id: int) -> bool:
    """Return True if msg belongs to the given forum topic."""
    if msg.id == topic_id:
        return True
    thread_id = getattr(msg, 'message_thread_id', None)
    if thread_id == topic_id:
        return True
    top  = getattr(msg, 'reply_to_top_message_id', None)
    repl = getattr(msg, 'reply_to_message_id', None)
    return top == topic_id or repl == topic_id


# ── PREFLIGHT CHECK ───────────────────────────────────────────────────────────

async def _preflight_check(bot_client, dest_id) -> tuple:
    """
    Resolve the destination chat peer in Pyrogram's cache.
    Returns (ok: bool, error_message: str | None).
    """
    try:
        await bot_client.get_chat(dest_id)
        config.logger.info(f"✅ Preflight OK: dest peer resolved ({dest_id})")
        return True, None
    except ChatAdminRequired:
        return False, (
            "❌ **Bot is not admin in the destination channel.**\n\n"
            "Please add the bot as **Full Admin** in your destination channel/group, "
            "then start the transfer again."
        )
    except ChatWriteForbidden:
        return False, (
            "❌ **Bot cannot post in the destination channel.**\n\n"
            "Check that the bot has the **Post Messages** permission."
        )
    except ChannelPrivate:
        return False, (
            "❌ **Destination channel is private or inaccessible.**\n\n"
            "Make sure the bot is a member AND an admin of the channel."
        )
    except FloodWait as e:
        config.logger.warning(f"Preflight FloodWait {e.x}s — waiting…")
        await asyncio.sleep(e.x + 2)
        return True, None
    except Exception as e:
        config.logger.warning(f"Preflight warning (non-fatal): {type(e).__name__}: {e}")
        return True, None


# ── ROBUST iter_messages (Pyrogram replacement) ────────────────────────────────

async def robust_iter_messages(user_client, source_id, start_msg: int,
                                end_msg: int, topic_id=None):
    """
    Async generator — yields Pyrogram Messages from [start_msg, end_msg].
    Uses get_messages(chat_id, ids=[list]) in batches of 200 IDs.
    Topic-filtered client-side via _is_in_topic().
    """
    current     = start_msg
    zero_misses = 0
    MAX_CONN_RETRIES = 5
    conn_retries     = 0

    while current <= end_msg:
        batch_end = min(current + GET_MESSAGES_BATCH - 1, end_msg)
        ids       = list(range(current, batch_end + 1))

        # ── Connection health check ────────────────────────────────────────
        if not user_client.is_connected:
            config.logger.warning("user_client disconnected — reconnecting…")
            try:
                await user_client.start()
                me = await user_client.get_me()
                if not me:
                    raise Exception("Session invalid after reconnect.")
                conn_retries = 0
            except Exception as e:
                conn_retries += 1
                if conn_retries > MAX_CONN_RETRIES:
                    raise Exception(f"Could not reconnect user_client: {e}")
                await asyncio.sleep(10 * conn_retries)
                continue

        try:
            messages = await user_client.get_messages(source_id, ids)
            conn_retries = 0
        except FloodWait as e:
            wait = e.x + 2
            config.logger.warning(f"FloodWait {e.x}s during source scan — waiting {wait}s")
            await asyncio.sleep(wait)
            continue
        except (SessionExpired, AuthKeyUnregistered, AuthKeyInvalid):
            raise Exception(
                "⚠️ User session expired during transfer.\n"
                "Please use /login to reconnect and try again."
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            conn_retries += 1
            config.logger.error(f"get_messages error (attempt {conn_retries}): {e}")
            if conn_retries > MAX_CONN_RETRIES:
                raise Exception(f"Too many errors fetching messages: {e}")
            await asyncio.sleep(5 * conn_retries)
            continue

        # ── Filter and yield valid messages ────────────────────────────────
        valid_in_batch = 0
        sorted_msgs    = sorted(
            [m for m in messages if m and not m.empty],
            key=lambda m: m.id
        )

        for msg in sorted_msgs:
            if msg.id < start_msg or msg.id > end_msg:
                continue
            if topic_id is not None and not _is_in_topic(msg, topic_id):
                continue
            valid_in_batch += 1
            zero_misses     = 0
            yield msg

        # ── Zero-progress protection ───────────────────────────────────────
        if valid_in_batch == 0:
            zero_misses += 1
            if zero_misses >= config.ZERO_PROGRESS_RETRIES:
                config.logger.warning(
                    f"iter: {config.ZERO_PROGRESS_RETRIES} consecutive empty batches "
                    f"at msg {current}. Concluding done."
                )
                return
            delay = config.ZERO_PROGRESS_DELAYS[
                min(zero_misses - 1, len(config.ZERO_PROGRESS_DELAYS) - 1)
            ]
            config.logger.warning(
                f"iter: empty batch at {current}–{batch_end} "
                f"(retry {zero_misses}/{config.ZERO_PROGRESS_RETRIES} in {delay}s…)"
            )
            await asyncio.sleep(delay)
            continue

        current = batch_end + 1
        await asyncio.sleep(1)


# ── LOG TRANSFER ──────────────────────────────────────────────────────────────

async def log_transfer(bot_client, log_channel, sent_message,
                        session_id, dest_id, file_name, part_num=None):
    if not log_channel or not sent_message or isinstance(sent_message, bool):
        return
    try:
        await bot_client.forward_messages(
            chat_id=log_channel,
            from_chat_id=dest_id,
            message_ids=[sent_message.id],
        )
        note = "📝 **Log**"
        if part_num:
            note += f" (Part {part_num})"
        note += f"\n📂 `{file_name}`\n📤 To: `{dest_id}`"
        await bot_client.send_message(log_channel, note)
    except ChatForwardsRestricted:
        try:
            await sent_message.copy(log_channel)
        except Exception as e:
            config.logger.error(f"Log fallback failed: {e}")
    except Exception as e:
        config.logger.error(f"Log error: {e}")


# ── PTB SEND (< 45 MB) ────────────────────────────────────────────────────────

async def _ptb_send_media(
    ptb_bot, dest_id: int,
    data: bytes, file_name: str, mime_type: str, caption: str,
    dest_topic_id, is_photo: bool, is_video: bool, is_audio: bool,
    custom_thumb_path: str = None,
    duration: int = 0,
    width: int = 0,
    height: int = 0,
) -> bool:
    """
    Upload data (bytes) to dest_id via PTB Bot API.
    Passes duration / width / height / thumbnail for videos.
    """
    from telegram import InputFile
    from telegram.error import RetryAfter, Forbidden, TelegramError
    from telegram.constants import ParseMode as PTBParseMode

    bio    = BytesIO(data)
    common = {
        'chat_id':    dest_id,
        'caption':    caption or '',
        'parse_mode': PTBParseMode.HTML,
    }
    if dest_topic_id:
        common['message_thread_id'] = dest_topic_id

    thumb_inp = None
    if custom_thumb_path and os.path.exists(custom_thumb_path):
        try:
            with open(custom_thumb_path, 'rb') as tf:
                thumb_bio = BytesIO(tf.read())
            thumb_inp = InputFile(thumb_bio, filename="thumb.jpg")
        except Exception as te:
            config.logger.warning(f"PTB thumb prepare failed: {te}")

    for attempt in range(config.MAX_RETRIES):
        try:
            bio.seek(0)
            inp = InputFile(bio, filename=file_name)

            if is_photo:
                await ptb_bot.send_photo(**common, photo=inp)

            elif is_video:
                await ptb_bot.send_video(
                    **common,
                    video=inp,
                    supports_streaming=True,
                    thumbnail=thumb_inp,
                    duration=duration or None,
                    width=width   or None,
                    height=height or None,
                )

            elif is_audio:
                await ptb_bot.send_audio(
                    **common,
                    audio=inp,
                    duration=duration or None,
                )

            else:
                await ptb_bot.send_document(**common, document=inp)

            return True

        except RetryAfter as e:
            wait = e.retry_after + 2
            config.logger.warning(f"⏳ PTB RetryAfter {e.retry_after}s — waiting {wait}s")
            await asyncio.sleep(wait)

        except Forbidden as e:
            raise PermissionError(
                f"❌ **Bot cannot post in destination channel.**\n"
                f"Make sure the bot is **Full Admin** there.\n`{e}`"
            )

        except TelegramError as e:
            backoff = min(10 * (2 ** attempt), 120)
            config.logger.error(
                f"PTB send attempt {attempt+1}/{config.MAX_RETRIES} failed "
                f"for {file_name}: {e} — retrying in {backoff}s"
            )
            await asyncio.sleep(backoff)

        except asyncio.CancelledError:
            raise

        except Exception as e:
            backoff = min(10 * (2 ** attempt), 120)
            config.logger.error(f"PTB unknown error (attempt {attempt+1}): {e} — retrying in {backoff}s")
            await asyncio.sleep(backoff)

    return False


# ── BOT_CLIENT DISK UPLOAD (≥ 45 MB) ─────────────────────────────────────────

async def _bot_disk_upload(
    bot_client, dest_id: int,
    file_path: str, file_name: str, file_size: int,
    caption: str, is_video: bool, is_audio: bool,
    thumb_path, dest_topic_id,
    progress_tracker: TransferProgress,
    duration: int = 0, width: int = 0, height: int = 0,
) -> tuple:
    """
    Upload a file from disk via Pyrogram bot_client (MTProto).
    Supports files up to ~1.9 GB per call.
    Returns (success: bool, sent_message).
    """
    common_kwargs = {
        'chat_id':           dest_id,
        'caption':           caption or '',
        'message_thread_id': dest_topic_id,
        'progress':          progress_tracker.upload_cb,
        'parse_mode':        ParseMode.HTML,
    }

    for attempt in range(config.MAX_RETRIES):
        try:
            if is_video:
                sent = await bot_client.send_video(
                    **common_kwargs,
                    video=file_path,
                    supports_streaming=True,
                    thumb=thumb_path,
                    duration=duration,
                    width=width,
                    height=height,
                )
            elif is_audio:
                sent = await bot_client.send_audio(
                    **common_kwargs,
                    audio=file_path,
                    duration=duration,
                )
            else:
                sent = await bot_client.send_document(
                    **common_kwargs,
                    document=file_path,
                    force_document=True,
                )
            return True, sent

        except FloodWait as e:
            wait = e.x + 5
            config.logger.warning(
                f"⏳ FloodWait {e.x}s on upload attempt {attempt+1} — "
                f"waiting {wait}s (not counted as failure)"
            )
            await asyncio.sleep(wait)

        except SlowmodeWait as e:
            wait = e.x + 2
            config.logger.warning(f"⏳ SlowMode {e.x}s — waiting {wait}s")
            await asyncio.sleep(wait)

        except (
            ChatWriteForbidden, ChatAdminRequired,
            UserBannedInChannel, BroadcastForbidden, ChannelPrivate,
        ) as e:
            raise PermissionError(
                f"❌ **Bot lacks permission in destination channel.**\n"
                f"Ensure the bot is **Full Admin** there.\n`{type(e).__name__}`"
            )

        except asyncio.CancelledError:
            raise

        except Exception as e:
            backoff = min(10 * (2 ** attempt), 120)
            config.logger.error(
                f"Upload attempt {attempt+1}/{config.MAX_RETRIES} failed "
                f"for {file_name}: {type(e).__name__}: {e} — retrying in {backoff}s"
            )
            await asyncio.sleep(backoff)

    return False, None


# ── MAIN TRANSFER FUNCTION ────────────────────────────────────────────────────

async def transfer_process(
    event,
    user_client,
    bot_client,
    source_id,
    dest_id: int,
    start_msg: int,
    end_msg: int,
    session_id: str,
    log_channel=None,
    topic_id=None,
    dest_topic_id=None,
):
    session_data = config.active_sessions.get(session_id, {})
    settings     = session_data.get('settings', {})
    user_id      = session_data.get('user_id')
    task_id      = session_data.get('task_id')

    # All sources use same download→upload pipeline now
    mode_text = "Standard"
    if topic_id      is not None: mode_text += f" | 🧵 Src Topic {topic_id}"
    if dest_topic_id is not None: mode_text += f" | 🎯 Dst Topic {dest_topic_id}"

    async def _event_respond(text, reply_markup=None):
        if hasattr(event, 'respond'):
            return await event.respond(text, reply_markup=reply_markup)
        return await event.reply(text, reply_markup=reply_markup)

    status_message = await _event_respond(
        f"🔍 **Checking permissions…**\n"
        f"⚡ Mode: {mode_text}\n"
        f"📍 Source: `{source_id}` → Dest: `{dest_id}`",
        reply_markup=get_progress_keyboard()
    )
    session_data['task_object'] = asyncio.current_task()

    # ── STEP 0: Preflight ─────────────────────────────────────────────────────
    preflight_ok, preflight_err = await _preflight_check(bot_client, dest_id)
    if not preflight_ok:
        await safe_edit_message(status_message, preflight_err)
        config.active_sessions.pop(session_id, None)
        return

    # ── STEP 0b: PTB Bot singleton ────────────────────────────────────────────
    try:
        ptb_bot = await config.get_ptb_bot()
    except Exception as e:
        config.logger.warning(f"PTB init failed (will use bot_client only): {e}")
        ptb_bot = None

    # ── STEP 0c: Download custom thumbnail once (if user set one) ─────────────
    custom_thumb_path = None
    thumbnail_file_id = settings.get('thumbnail_file_id')
    if thumbnail_file_id:
        try:
            thumb_io = await bot_client.download_media(thumbnail_file_id, in_memory=True)
            custom_thumb_path = f"/tmp/custom_thumb_{user_id}_{int(time.time())}.jpg"
            with open(custom_thumb_path, 'wb') as f:
                f.write(bytes(thumb_io.getvalue()))
            config.logger.info(f"✅ Custom thumbnail ready: {custom_thumb_path}")
        except Exception as e:
            config.logger.warning(f"Custom thumbnail download failed: {e}")
            custom_thumb_path = None

    thumb_note = " | 🖼️ Custom Thumb" if custom_thumb_path else ""
    await safe_edit_message(
        status_message,
        f"🚀 **Starting Transfer…**\n"
        f"⚡ Mode: {mode_text}{thumb_note}\n"
        f"📍 Source: `{source_id}` → Dest: `{dest_id}`",
        reply_markup=get_progress_keyboard()
    )

    # ── State vars ────────────────────────────────────────────────────────────
    total_success      = 0
    total_size         = 0
    total_skipped      = 0
    deleted_msgs       = 0
    consecutive_errors = 0
    idx                = 0
    last_seen_id       = start_msg - 1
    overall_start      = time.time()

    async def _should_stop() -> bool:
        if config.global_stop_flag or session_id not in config.active_sessions:
            return True
        if session_data.get('stop_flag'):
            return True
        if task_id:
            return await db.check_stop_signal(task_id)
        return False

    def _progress_text(title: str, extra: str = "") -> str:
        ram_used  = session_data.get('ram_used',  0)
        ram_total = session_data.get('ram_total', 0)
        ram_line  = _ram_bar(ram_used, ram_total)
        return f"{title}\n{ram_line}{extra}".strip()

    # ── MAIN LOOP ─────────────────────────────────────────────────────────────
    try:
        await safe_edit_message(status_message, "🔍 **Scanning messages…**",
                                reply_markup=get_progress_keyboard())

        async for message in robust_iter_messages(
            user_client, source_id, start_msg, end_msg, topic_id
        ):
            if message.id > last_seen_id + 1:
                deleted_msgs += message.id - last_seen_id - 1
            last_seen_id = message.id
            idx         += 1

            if await _should_stop():
                await safe_edit_message(
                    status_message,
                    _progress_text("🚫 **Stopped by user/admin.**")
                )
                break

            # Skip service messages
            if getattr(message, 'service', None) or getattr(message, 'action', None):
                continue

            if consecutive_errors >= 5:
                await safe_edit_message(
                    status_message,
                    _progress_text(
                        "❌ **Stopped: 5 consecutive upload failures.**\n"
                        "Check destination channel permissions / bot admin status."
                    )
                )
                break

            # ── Inter-file pacing ─────────────────────────────────────────
            await asyncio.sleep(config.SLEEP_BETWEEN_FILES)
            if idx % 10 == 0:
                await asyncio.sleep(config.SLEEP_EVERY_10)

            # ── Periodic connection health check ──────────────────────────
            if idx % 50 == 0:
                if not user_client.is_connected:
                    try: await user_client.start()
                    except Exception as hc_err:
                        config.logger.warning(f"user_client health-check failed: {hc_err}")
                if not bot_client.is_connected:
                    try: await bot_client.start()
                    except Exception as hc_err:
                        config.logger.warning(f"bot_client health-check failed: {hc_err}")

            sent_message = None
            success      = False
            temp_path    = None
            thumb_path   = None

            try:
                # ══ TEXT / WEB-PAGE ═══════════════════════════════════════
                has_web_page = getattr(message, 'web_page', None) or getattr(message, 'web_page_preview', None)
                if has_web_page or not getattr(message, 'media', None):
                    if message.text or message.caption:
                        modified_text = apply_caption_manipulations(message, settings)
                        sent_message  = await bot_client.send_message(
                            dest_id, modified_text,
                            message_thread_id=dest_topic_id,
                            parse_mode=ParseMode.HTML,
                        )
                        total_success += 1
                        if log_channel:
                            try:
                                await bot_client.send_message(
                                    log_channel,
                                    f"📝 **Log**\n{modified_text[:80]}"
                                )
                            except Exception:
                                pass
                    continue

                # ══ SPECIAL MEDIA (polls, geo, contacts, dice…) ═══════════
                if is_special_media(message):
                    try:
                        sent_message = await user_client.copy_message(
                            chat_id=dest_id,
                            from_chat_id=source_id,
                            message_id=message.id,
                            message_thread_id=dest_topic_id,
                        )
                        total_success += 1
                    except Exception as sm_e:
                        config.logger.error(f"Special media failed: {sm_e}")
                    continue

                # ══ FILE INFO ═════════════════════════════════════════════
                file_name, mime_type, is_video_mode = get_target_info(message)
                if not file_name:
                    config.logger.warning(f"Msg {message.id}: cannot derive filename — skipping")
                    total_skipped += 1
                    continue

                file_name        = sanitize_filename(apply_filename_manipulations(file_name, settings))
                modified_caption = apply_caption_manipulations(message, settings)
                file_size        = get_media_file_size(message)

                is_photo = bool(message.photo and not message.document)
                is_image = (not is_photo) and ("image" in (mime_type or ""))
                is_audio = bool(
                    message.audio or message.voice or
                    "audio" in (mime_type or "") or "voice" in (mime_type or "")
                )

                # ── Extract media metadata EARLY (used by both PTB and disk paths)
                media_duration = 0
                media_width    = 0
                media_height   = 0
                if is_video_mode and message.video:
                    media_duration = getattr(message.video, 'duration', 0) or 0
                    media_width    = getattr(message.video, 'width',    0) or 0
                    media_height   = getattr(message.video, 'height',   0) or 0
                elif message.video_note:
                    media_duration = getattr(message.video_note, 'duration', 0) or 0
                elif is_audio:
                    if message.audio:
                        media_duration = getattr(message.audio, 'duration', 0) or 0
                    elif message.voice:
                        media_duration = getattr(message.voice, 'duration', 0) or 0

                start_time = time.time()

                # ══ PATH A: Photo / Image ═════════════════════════════════
                if (is_photo or is_image):
                    try:
                        data_io = await user_client.download_media(message, in_memory=True)
                        data    = bytes(data_io.getvalue())

                        if ptb_bot:
                            ok = await _ptb_send_media(
                                ptb_bot, dest_id, data, file_name,
                                'image/jpeg', modified_caption, dest_topic_id,
                                is_photo=True, is_video=False, is_audio=False,
                            )
                            if ok:
                                success      = True
                                sent_message = True

                        if not success:
                            bio      = BytesIO(data)
                            bio.name = file_name
                            sent_message = await bot_client.send_photo(
                                chat_id=dest_id,
                                photo=bio,
                                caption=modified_caption,
                                message_thread_id=dest_topic_id,
                                parse_mode=ParseMode.HTML,
                            )
                            success = True

                    except PermissionError:
                        raise
                    except Exception as img_e:
                        config.logger.error(f"Image send failed: {img_e}")

                # ══ PATH B: Non-image files (videos, docs, audio…) ════════
                elif not (is_photo or is_image):
                    progress_tracker = TransferProgress(
                        file_name, file_size, status_message, session_data,
                        msg_id=message.id,
                    )

                    # ── PTB path: < 45 MB ──────────────────────────────────
                    if ptb_bot and 0 < file_size < config.PTB_SMALL_FILE_LIMIT:
                        try:
                            data_io = await user_client.download_media(
                                message,
                                in_memory=True,
                                progress=progress_tracker.download_cb,
                            )
                            data = bytes(data_io.getvalue())

                            ok = await _ptb_send_media(
                                ptb_bot, dest_id, data, file_name, mime_type or '',
                                modified_caption, dest_topic_id,
                                is_photo=False,
                                is_video=is_video_mode,
                                is_audio=is_audio,
                                custom_thumb_path=custom_thumb_path if is_video_mode else None,
                                duration=media_duration,
                                width=media_width,
                                height=media_height,
                            )
                            if ok:
                                success      = True
                                sent_message = True

                        except PermissionError:
                            raise
                        except Exception as ptb_e:
                            config.logger.warning(
                                f"PTB path failed for {file_name} ({ptb_e}) "
                                f"— falling back to bot_client disk upload"
                            )

                    # ── bot_client disk path: ≥ 45 MB or PTB failed ────────
                    if not success:
                        temp_path = f"/tmp/tf_{user_id}_{message.id}_{int(time.time())}"

                        # Download video's own thumbnail (custom_thumb takes priority if set)
                        if is_video_mode and message.video and not custom_thumb_path:
                            try:
                                if message.video.thumbs:
                                    thumb_data_io = await user_client.download_media(
                                        message.video.thumbs[-1].file_id,
                                        in_memory=True,
                                    )
                                    thumb_path = f"/tmp/thumb_{message.id}.jpg"
                                    with open(thumb_path, 'wb') as tf:
                                        tf.write(bytes(thumb_data_io.getvalue()))
                            except Exception:
                                thumb_path = None

                        # custom_thumb overrides the video's own thumbnail
                        effective_thumb = custom_thumb_path or thumb_path

                        if file_size > SPLIT_THRESHOLD:
                            # ── Split upload for very large files ─────────
                            config.logger.info(
                                f"✂️ File {file_name} ({human_readable_size(file_size)}) "
                                f"exceeds {human_readable_size(SPLIT_THRESHOLD)} — splitting"
                            )
                            downloaded = await user_client.download_media(
                                message,
                                file_name=temp_path,
                                progress=progress_tracker.download_cb,
                            )
                            if not downloaded or not os.path.exists(str(downloaded)):
                                raise RuntimeError("download_media returned empty path")

                            actual_size = os.path.getsize(str(downloaded))
                            parts       = math.ceil(actual_size / SPLIT_THRESHOLD)
                            config.logger.info(f"✂️ Splitting into {parts} parts")

                            all_parts_ok = True
                            with open(str(downloaded), 'rb') as full_file:
                                for i in range(parts):
                                    if await _should_stop():
                                        all_parts_ok = False
                                        break
                                    part_num  = i + 1
                                    part_name = (
                                        f"{os.path.splitext(file_name)[0]}"
                                        f".part{part_num:03d}"
                                        f"{os.path.splitext(file_name)[1]}"
                                    )
                                    part_path = f"/tmp/tf_part_{user_id}_{message.id}_{i}"
                                    part_data = full_file.read(SPLIT_THRESHOLD)
                                    with open(part_path, 'wb') as pf:
                                        pf.write(part_data)

                                    part_cap = f"{modified_caption}\n\n(Part {part_num}/{parts})"
                                    part_tracker = TransferProgress(
                                        part_name, len(part_data),
                                        status_message, session_data,
                                        msg_id=message.id,
                                    )
                                    part_ok, _ = await _bot_disk_upload(
                                        bot_client, dest_id,
                                        part_path, part_name, len(part_data),
                                        part_cap,
                                        is_video=False, is_audio=False,
                                        thumb_path=None,
                                        dest_topic_id=dest_topic_id,
                                        progress_tracker=part_tracker,
                                    )
                                    try: os.remove(part_path)
                                    except Exception: pass

                                    if not part_ok:
                                        all_parts_ok = False
                                        break

                            try: os.remove(str(downloaded))
                            except Exception: pass
                            temp_path = None

                            if all_parts_ok:
                                success      = True
                                sent_message = True

                        else:
                            # ── Normal disk download → upload ──────────────
                            downloaded = await user_client.download_media(
                                message,
                                file_name=temp_path,
                                progress=progress_tracker.download_cb,
                            )
                            if not downloaded or not os.path.exists(str(downloaded)):
                                raise RuntimeError("download_media returned empty path")
                            temp_path = str(downloaded)

                            progress_tracker.reset_for_upload()

                            ok, sent_message = await _bot_disk_upload(
                                bot_client, dest_id,
                                temp_path, file_name, file_size,
                                modified_caption,
                                is_video=is_video_mode,
                                is_audio=is_audio,
                                thumb_path=effective_thumb,
                                dest_topic_id=dest_topic_id,
                                progress_tracker=progress_tracker,
                                duration=media_duration,
                                width=media_width,
                                height=media_height,
                            )
                            success = ok

                # ══ OUTCOME ══════════════════════════════════════════════
                if success:
                    total_success      += 1
                    consecutive_errors  = 0
                    elapsed             = time.time() - start_time
                    speed               = file_size / elapsed / (1024 * 1024) if elapsed > 0 else 0
                    total_size         += file_size

                    if log_channel and sent_message:
                        await log_transfer(
                            bot_client, log_channel, sent_message,
                            session_id, dest_id, file_name
                        )

                    # Save checkpoint periodically
                    if user_id and total_success % CHECKPOINT_INTERVAL == 0:
                        try:
                            await db.save_transfer_checkpoint(user_id, {
                                'source_id':     str(source_id),
                                'dest_id':       str(dest_id),
                                'current_msg':   last_seen_id,
                                'end_msg':       end_msg,
                                'settings':      settings,
                                'topic_id':      topic_id,
                                'dest_topic_id': dest_topic_id,
                                'log_channel':   log_channel,
                            })
                        except Exception as ckpt_err:
                            config.logger.warning(f"Checkpoint save failed: {ckpt_err}")

                else:
                    config.logger.error(f"❌ All attempts failed: {file_name}")
                    consecutive_errors += 1
                    total_skipped      += 1
                    await safe_edit_message(
                        status_message,
                        _progress_text(
                            f"❌ **Failed:** `{file_name[:35]}`",
                            f"\nSkipping… ({consecutive_errors}/5 consecutive failures)"
                        ),
                        reply_markup=get_progress_keyboard(),
                    )

            except PermissionError as perm_e:
                await safe_edit_message(status_message, str(perm_e))
                config.logger.error(f"Permission error — stopping: {perm_e}")
                break

            except asyncio.CancelledError:
                raise

            except MemoryError:
                config.logger.error(f"💥 OOM on msg {message.id} — skipping")
                total_skipped      += 1
                consecutive_errors += 1
                await asyncio.sleep(5)

            except Exception as e:
                config.logger.error(f"❌ Error on msg {message.id}: {e}", exc_info=True)
                total_skipped      += 1
                consecutive_errors += 1

            finally:
                # Always clean up per-file temp files
                for p in [temp_path, thumb_path]:
                    if p and os.path.exists(str(p)):
                        try: os.remove(str(p))
                        except Exception: pass

        # ── POST-LOOP ─────────────────────────────────────────────────────────
        if last_seen_id < end_msg and last_seen_id >= start_msg:
            deleted_msgs += end_msg - last_seen_id

        overall_time      = time.time() - overall_start
        avg_speed         = total_size / overall_time / (1024 * 1024) if overall_time > 0 else 0
        actually_complete = last_seen_id >= end_msg

        header  = "🏁 **Transfer Complete!**" if actually_complete else "🏁 **Transfer Finished**"
        summary = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ Success:         `{total_success}`\n"
        )
        if deleted_msgs > 0:
            summary += f"🗑️ Not Found:       `{deleted_msgs}` _(deleted/restricted)_\n"
        summary += (
            f"⏭️ Skipped:         `{total_skipped}`\n"
            f"📦 Total Size:      `{human_readable_size(total_size)}`\n"
            f"⚡ Avg Speed:       `{avg_speed:.1f} MB/s`\n"
            f"⏱️ Time:            `{time_formatter(overall_time)}`"
        )
        await safe_edit_message(status_message, summary)

        if actually_complete and user_id:
            await db.clear_transfer_checkpoint(user_id)
            config.logger.info(f"✅ Checkpoint cleared for user {user_id}")

    except asyncio.CancelledError:
        await safe_edit_message(
            status_message,
            "🚫 **Task Forcefully Revoked**\n💡 Use /clone to start a new transfer."
        )

    except Exception as e:
        await safe_edit_message(
            status_message,
            f"💥 **Critical Error:**\n`{str(e)[:200]}`\n💡 Use /clone to start a new transfer."
        )
        config.logger.error(f"Transfer crashed: {e}", exc_info=True)

    finally:
        # Clean up the custom thumbnail temp file
        if custom_thumb_path and os.path.exists(custom_thumb_path):
            try: os.remove(custom_thumb_path)
            except Exception: pass

        config.active_sessions.pop(session_id, None)
        if hasattr(session_manager, 'semaphore') and not task_id:
            try:
                await session_manager.stop_user_session(user_client)
            except Exception:
                pass
        config.logger.info("✅ Transfer cleanup complete")
