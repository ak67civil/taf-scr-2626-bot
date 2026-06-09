"""
handlers.py  –  All Telegram bot handlers  (v6.1 — Pyrofork)

v6.1 changes:
  • Thumbnail support ADDED:
    - set_thumbnail_{sid} callback → sets step to 'wait_thumbnail'
    - remove_thumbnail_{sid} callback → clears thumbnail from settings
    - wait_thumbnail step in clone_step_handler → saves photo file_id
    - _get_settings_kb() helper for consistent thumbnail_set tracking
  • Premium price isolation via payments_db.py changes (no handler change needed)
"""

import asyncio
import uuid
import time
import datetime
import os

from pyrogram import Client, filters, StopPropagation
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    PhoneNumberInvalid, PasswordHashInvalid, FloodWait,
)

import config
from keyboards import (
    get_settings_keyboard, get_confirm_keyboard,
    get_skip_keyboard, get_clone_info_keyboard,
    get_progress_keyboard,
)
from transfer import transfer_process
import database as db
from session_manager import session_manager
from heroku_manager import heroku_manager
from utils import (
    human_readable_size, time_formatter,
    extract_link_info,
)


# ── MODULE-LEVEL STATE ────────────────────────────────────────────────────────

LOGIN_STATES: dict = {}
HEROKU_MODE = bool(os.environ.get("HEROKU_API_TOKEN") and os.environ.get("HEROKU_APP_NAME"))

DYNO_RAM_LABELS = {
    "free":          "512 MB RAM",
    "eco":           "512 MB RAM",
    "basic":         "512 MB RAM",
    "standard-1x":  "512 MB RAM",
    "standard-2x":  "1 GB RAM",
    "performance-m": "2.5 GB RAM",
    "performance-l": "14 GB RAM",
}

def get_dyno_label() -> str:
    size = os.environ.get("WORKER_DYNO_SIZE", "standard-1x").lower().strip()
    return DYNO_RAM_LABELS.get(size, size)

DYNOS_PER_PAGE = 5


# ── REGISTER ALL HANDLERS ─────────────────────────────────────────────────────

def register_handlers(bot_client: Client):

    # ── Shared utilities ──────────────────────────────────────────────────────

    async def get_user_status(user_id: int) -> str:
        if user_id == config.ADMIN_ID:
            return "ADMIN"
        is_valid, _, _ = await db.check_user(user_id)
        return "PAID" if is_valid else "FREE"

    async def get_active_subscriber_count() -> int:
        try:
            users = await db.get_all_users()
            now   = time.time()
            return sum(1 for u in users if u[1] and u[1] > now)
        except Exception:
            return 0

    def format_expiry_ist(expiry_ts: float) -> str:
        utc_dt = datetime.datetime.fromtimestamp(expiry_ts, datetime.timezone.utc)
        ist_dt = utc_dt + datetime.timedelta(hours=5, minutes=30)
        return ist_dt.strftime('%d %b %Y, %I:%M %p IST')

    def find_session_for_user(user_id: int):
        for sid, data in config.active_sessions.items():
            if data.get('user_id') == user_id:
                return sid
        return None

    def cancel_existing_sessions(user_id: int) -> int:
        to_delete = [
            sid for sid, data in config.active_sessions.items()
            if data.get('user_id') == user_id
        ]
        for sid in to_delete:
            task = config.active_sessions[sid].get('task_object')
            if task and not task.done():
                task.cancel()
            del config.active_sessions[sid]
        return len(to_delete)

    async def _kill_user_dyno(user_id: int) -> str:
        dyno_rec = await db.get_user_dyno(user_id)
        if not dyno_rec:
            return ""
        task_id = dyno_rec.get('task_id')
        if task_id:
            await db.request_task_stop(task_id)
        dyno_name = dyno_rec.get('dyno_name')
        killed    = False
        if dyno_name and HEROKU_MODE:
            killed = await heroku_manager.kill_dyno(dyno_name)
        await db.clear_user_dyno(user_id)
        if killed:
            return f"🖥️ Dyno `{dyno_name}` killed."
        elif dyno_name:
            return f"⚠️ Dyno API call failed but DB cleared. ({dyno_name})"
        return "🛑 Task stopped."

    def _make_ram_bar(ram_used: int, ram_total: int) -> str:
        if not ram_total:
            return "🧠 RAM: Waiting for data..."
        pct      = ram_used / ram_total * 100
        used_mb  = ram_used  / (1024 * 1024)
        total_mb = ram_total / (1024 * 1024)
        filled   = min(10, int(pct / 10))
        bar      = "█" * filled + "░" * (10 - filled)
        icon     = "✅" if pct < 60 else ("⚠️" if pct < 80 else "🔴")
        return f"🧠 {bar} {pct:.1f}% | `{used_mb:.0f}/{total_mb:.0f} MB` {icon}"

    def _build_dynos_page(dynos: list, page: int):
        now         = time.time()
        total       = len(dynos)
        total_pages = max(1, (total + DYNOS_PER_PAGE - 1) // DYNOS_PER_PAGE)
        page        = max(0, min(page, total_pages - 1))
        start       = page * DYNOS_PER_PAGE
        end         = start + DYNOS_PER_PAGE
        page_dynos  = dynos[start:end]
        active_count = sum(
            1 for d in dynos
            if d.get('status') == 'running' and d.get('last_ping') and now - d['last_ping'] < 30
        )
        msg  = f"🖥️ **Dyno Panel** | Page {page+1}/{total_pages}\n"
        msg += f"Active: **{active_count}** / Total: **{total}**\n"
        msg += "━━━━━━━━━━━━━━━━\n\n"
        for rec in page_dynos:
            uid        = rec.get('user_id', '?')
            fname      = rec.get('first_name', 'User')
            raw_tid    = rec.get('task_id')
            task_short = (raw_tid[:8] + "…") if raw_tid else "–"
            dyno_name  = rec.get('dyno_name') or '–'
            status     = rec.get('status', 'unknown')
            started_at = rec.get('started_at', 0)
            last_ping  = rec.get('last_ping', 0)
            ram_used   = rec.get('ram_used', 0)
            ram_total  = rec.get('ram_total', 0)
            label      = rec.get('label', '')
            alive      = (last_ping and now - last_ping < 30)
            if status == 'running' and alive:       status_icon = "🟢 Running"
            elif status == 'running' and not alive: status_icon = "🟡 Stale (no ping >30s)"
            else:                                   status_icon = "⚫ Stopped"
            running_sec = int(now - started_at) if started_at else 0
            running_fmt = time_formatter(running_sec) if running_sec else "–"
            ram_line    = _make_ram_bar(ram_used, ram_total) if ram_total else "🧠 RAM: Waiting..."
            msg += (
                f"👤 [{fname}](tg://user?id={uid}) (`{uid}`)\n"
                f"🖥️ `{dyno_name}` | {status_icon}\n"
                f"{ram_line}\n"
                f"📊 `{label}`\n"
                f"⏱️ Uptime: `{running_fmt}` | Task: `{task_short}`\n"
                f"🛑 `/kill_dyno {dyno_name}`\n\n"
            )
        if not HEROKU_MODE:
            msg += "\n⚠️ HEROKU_API_TOKEN / HEROKU_APP_NAME not set."
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev",    callback_data=f"dynos_page_{page-1}"))
        nav.append(    InlineKeyboardButton("🔄 Refresh", callback_data=f"dynos_page_{page}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️",    callback_data=f"dynos_page_{page+1}"))
        markup = InlineKeyboardMarkup([nav]) if nav else None
        return msg, markup

    async def _safe_cb_edit(query: CallbackQuery, text: str, markup=None):
        """Edit the callback message, catching all exceptions."""
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except Exception as e:
            config.logger.error(f"_safe_cb_edit failed: {e}")

    # ── THUMBNAIL HELPER ──────────────────────────────────────────────────────
    # Returns settings keyboard with correct thumbnail_set status from session.

    def _get_settings_kb(sid: str) -> InlineKeyboardMarkup:
        """Get settings keyboard with current thumbnail status from session."""
        session       = config.active_sessions.get(sid, {})
        dest_topic_id = session.get('dest_topic_id')
        thumbnail_set = session.get('settings', {}).get('thumbnail_set', False)
        return get_settings_keyboard(sid, dest_topic_id, thumbnail_set=thumbnail_set)

    # ═══════════════════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ═══════════════════════════════════════════════════════════════════════════

    @bot_client.on_message(filters.command("id"))
    async def id_handler(client: Client, message: Message):
        await message.reply(f"🆔 Chat ID: `{message.chat.id}`")

    @bot_client.on_message(filters.command("start") & filters.private)
    async def start_handler(client: Client, message: Message):
        user_id    = message.from_user.id
        first_name = message.from_user.first_name or "User"
        await db.update_user_name(user_id, first_name)
        status = await get_user_status(user_id)

        if status == "ADMIN":
            await message.reply(
                "👑 **Admin Panel**\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "**Users:** `/add_user ID DUR` · `/revoke ID` · `/users` · `/paid_users`\n"
                "**Dynos:** `/dynos` · `/kill_dyno NAME`\n"
                "**Export:** `/extract_string`\n"
                "**Broadcast:** Reply `/broadcast` to any message\n"
                "**System:** `/set_log CHANNEL_ID` · `/login` · `/clone`"
            )
            return

        if status == "PAID":
            _, session, _ = await db.check_user(user_id)
            login_status  = "✅ Logged In" if session else "❌ Not Logged In"
            users         = await db.get_all_users()
            expiry_ts     = next((u[1] for u in users if u[0] == user_id), 0)
            expiry_str    = format_expiry_ist(expiry_ts) if expiry_ts else "Unknown"
            remaining_sec = max(0, int(expiry_ts - time.time())) if expiry_ts else 0
            rem_days      = remaining_sec // 86400
            rem_hrs       = (remaining_sec % 86400) // 3600
            if rem_days > 0:   rem_str = f"{rem_days}d {rem_hrs}h remaining"
            elif rem_hrs > 0:  rem_str = f"{rem_hrs}h remaining ⚠️"
            else:              rem_str = "Expiring soon! ⚠️"
            dyno_rec  = await db.get_user_dyno(user_id)
            dyno_line = ""
            if dyno_rec and dyno_rec.get('status') == 'running':
                dyno_line = f"\n🖥️ Active Dyno: `{dyno_rec.get('dyno_name', 'Unknown')}`"
            await message.reply(
                f"🚀 **Content Saver Bot**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 **{first_name}** | {login_status}{dyno_line}\n"
                f"📅 Expires: `{expiry_str}` _(⏳ {rem_str})_\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"`/login` — Connect your Telegram account\n"
                f"`/clone` — Start a new file transfer\n"
                f"`/stop` — Stop transfer & kill dyno\n"
                f"`/kill` — Force stop if transfer is stuck\n"
                f"`/dyno_status` — Check RAM usage\n"
                f"`/cleanup_ram` — Free up RAM during transfer\n"
                f"`/buy` — Renew subscription\n"
                f"`/logout` — Logout\n"
                f"`/help` — Full usage guide",
                reply_markup=get_clone_info_keyboard()
            )
        else:
            active_subs = await get_active_subscriber_count()
            dyno_label  = get_dyno_label()
            await message.reply(
                f"👋 **Welcome to Save Restricted Content Bot!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Kisi bhi **private ya public** Telegram channel/group se files forward karo —\n"
                f"chahe **forwarding band** ho tab bhi. **Topics/Threads** bhi supported hain.\n\n"
                f"✅ Dedicated **{dyno_label}** per user\n"
                f"✅ Forward-restricted channel/group supported\n"
                f"✅ Topics/Threads groups supported\n"
                f"✅ 2GB+ files · Smart caption & filename editing\n"
                f"✅ Custom transfer thumbnail support\n\n"
                f"👥 **{active_subs}** active subscribers\n\n"
                f"Access lene ke liye 👉 `/buy` command send karo"
            )

    @bot_client.on_message(filters.command("help") & filters.private)
    async def help_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id == config.ADMIN_ID:
            await message.reply(
                "👑 **Admin Guide**\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "**Users:** `/add_user ID DUR` (e.g. `30d`, `1h`, `7d`)\n"
                "**Revoke:** `/revoke ID`\n"
                "**Dynos:** `/dynos`\n"
                "**Broadcast:** Reply `/broadcast` to any message\n"
                "**Export sessions:** `/extract_string`"
            )
            return
        await message.reply(
            "📚 **User Guide**\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "**Step 1 — Buy / Renew**\n"
            f"Use `/buy` → Contact {config.OWNER_USERNAME} to purchase your plan.\n\n"
            "**Step 2 — Login**\n"
            "Use `/login` → send phone: `+91XXXXXXXXXX`\n"
            "→ OTP format: `1-2-3-4-5` (dashes optional)\n"
            "→ If 2FA: enter your Telegram password\n\n"
            "**Step 3 — Clone**\n"
            "Use `/clone` → you need 3 things:\n"
            "  1 First message link (start point)\n"
            "  2 Last message link (end point)\n"
            "  3 Destination channel/group ID\n\n"
            "**Thumbnail Set Karne Ka Tarika:**\n"
            "1 `/clone` start karo\n"
            "2 Settings mein **Set Transfer Thumbnail** dabao\n"
            "3 Koi bhi photo bhejo (as image)\n"
            "4 Woh thumbnail saare videos pe lagega!\n\n"
            "**Message link kaise milega?**\n"
            "Message pe long press karo → Copy Link\n"
            "Private: `https://t.me/c/1234567/100`\n"
            "Public: `https://t.me/channelname/100`\n"
            "Forum topic: `https://t.me/c/1234567/5/100`\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "`/login` `/logout` `/clone` `/stop` `/kill`\n"
            "`/cancel` `/dyno_status` `/cleanup_ram` `/buy` `/id`\n\n"
            f"Help chahiye? {config.OWNER_USERNAME}"
        )

    @bot_client.on_message(filters.command("buy") & filters.private)
    async def buy_handler(client: Client, message: Message):
        await message.reply(f"Contact {config.OWNER_USERNAME} to purchase Premium Save Restricted Plan")

    @bot_client.on_message(filters.command("login") & filters.private)
    async def login_start(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != config.ADMIN_ID:
            is_valid, _, _ = await db.check_user(user_id)
            if not is_valid:
                await message.reply("❌ Subscription Required. Use `/buy` first.")
                return
        _, session, _ = await db.check_user(user_id)
        if session:
            await message.reply("✅ Already logged in!\n\nUse `/logout` to switch accounts.")
            return
        LOGIN_STATES[user_id] = {'state': 'PHONE'}
        await message.reply(
            "Login — Step 1/3\n\n"
            "Apna phone number international format mein bhejo.\n\n"
            "Example: `+91XXXXXXXXXX`"
        )

    @bot_client.on_message(filters.command("logout") & filters.private)
    async def logout_handler(client: Client, message: Message):
        await db.update_user_session(message.from_user.id, None, None)
        await message.reply("Logged out successfully.")

    @bot_client.on_message(filters.command("stop") & filters.private)
    async def stop_command_handler(client: Client, message: Message):
        user_id    = message.from_user.id
        session_id = find_session_for_user(user_id)
        if session_id:
            config.active_sessions[session_id]['stop_flag'] = True
            task = config.active_sessions[session_id].get('task_object')
            if task and not task.done():
                task.cancel()
        dyno_kill_msg = await _kill_user_dyno(user_id)
        if session_id or dyno_kill_msg:
            reply = "Transfer Stopped!"
            if dyno_kill_msg: reply += f"\n{dyno_kill_msg}"
            reply += "\n\nUse `/clone` to start a new transfer."
            await message.reply(reply)
        else:
            await message.reply("No active transfer found.")

    @bot_client.on_message(filters.command("cancel") & filters.private)
    async def cancel_command_handler(client: Client, message: Message):
        user_id    = message.from_user.id
        session_id = find_session_for_user(user_id)
        parts      = []
        if session_id:
            del config.active_sessions[session_id]
            parts.append("Session cancelled.")
        if user_id in LOGIN_STATES:
            del LOGIN_STATES[user_id]
            parts.append("Login/purchase cancelled.")
        dyno_kill_msg = await _kill_user_dyno(user_id)
        if dyno_kill_msg: parts.append(dyno_kill_msg)
        if parts:
            await message.reply("Cancelled.\n" + "\n".join(parts) + "\n\nUse `/clone` to start again.")
        else:
            await message.reply("Nothing active to cancel.")

    @bot_client.on_message(filters.command("kill") & filters.private)
    async def user_kill_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != config.ADMIN_ID:
            is_valid, _, _ = await db.check_user(user_id)
            if not is_valid:
                await message.reply("❌ No active subscription.")
                return
        cancel_existing_sessions(user_id)
        dyno_kill_msg = await _kill_user_dyno(user_id)
        if dyno_kill_msg:
            await message.reply(f"Done! {dyno_kill_msg}\n\nUse `/clone` to start a new transfer.")
        else:
            await message.reply("No active dyno found. Use `/clone` directly.")

    @bot_client.on_message(filters.command("dyno_status") & filters.private)
    async def dyno_status_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != config.ADMIN_ID:
            is_valid, _, _ = await db.check_user(user_id)
            if not is_valid:
                await message.reply("❌ No active subscription.")
                return
        dyno_rec = await db.get_user_dyno(user_id)
        if not dyno_rec or dyno_rec.get('status') != 'running':
            await message.reply("No active dyno. Start with `/clone`.")
            return
        ram_used  = dyno_rec.get('ram_used', 0)
        ram_total = dyno_rec.get('ram_total', 0)
        label     = dyno_rec.get('label', '')
        dyno_name = dyno_rec.get('dyno_name', 'Unknown')
        ping_ago  = int(time.time() - dyno_rec.get('last_ping', 0))
        ram_bar   = _make_ram_bar(ram_used, ram_total)
        pct       = ram_used / ram_total * 100 if ram_total else 0
        tip = ""
        if pct >= 80:   tip = "\nRAM critical! Use `/cleanup_ram`."
        elif pct >= 60: tip = "\nRAM high. Consider `/cleanup_ram`."
        await message.reply(
            f"Dyno: `{dyno_name}`\n"
            f"{ram_bar}\n"
            f"`{label}`\n"
            f"Last ping: `{ping_ago}s ago`"
            f"{tip}"
        )

    @bot_client.on_message(filters.command("cleanup_ram") & filters.private)
    async def cleanup_ram_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != config.ADMIN_ID:
            is_valid, _, _ = await db.check_user(user_id)
            if not is_valid:
                await message.reply("❌ No active subscription.")
                return
        dyno_rec = await db.get_user_dyno(user_id)
        if not dyno_rec or dyno_rec.get('status') != 'running':
            await message.reply("No active dyno. Works only during a transfer.")
            return
        await db.request_ram_cleanup(user_id)
        await message.reply("RAM cleanup requested! Check `/dyno_status` in ~10 seconds.")

    # ── ADMIN: User management ────────────────────────────────────────────────

    @bot_client.on_message(filters.command("add_user") & filters.private)
    async def add_user_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 3:
            await message.reply("Usage: `/add_user ID DUR` (e.g. `/add_user 123456 30d`)")
            return
        try:
            target_id    = int(parts[1])
            duration_str = parts[2].lower()
            multiplier   = 86400
            if duration_str.endswith('m'):   multiplier = 60
            elif duration_str.endswith('h'): multiplier = 3600
            elif duration_str.endswith('d'): multiplier = 86400
            duration   = int(duration_str[:-1]) * multiplier
            new_expiry = await db.update_validity(target_id, duration)
            exp_str    = format_expiry_ist(new_expiry)
            await message.reply(f"✅ User `{target_id}` updated. Expires: `{exp_str}`")
            try:
                await bot_client.send_message(
                    target_id,
                    f"Subscription Activated!\n\n"
                    f"Valid until: `{exp_str}`\n\n"
                    f"Use `/login` to connect your account, then `/clone` to start."
                )
            except Exception:
                await message.reply(f"Could not DM user `{target_id}`")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")

    @bot_client.on_message(filters.command("revoke") & filters.private)
    async def revoke_user_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: `/revoke USER_ID`")
            return
        target_id = int(parts[1])
        await db.revoke_user(target_id)
        for sid, data in list(config.active_sessions.items()):
            if data.get('user_id') == target_id:
                config.active_sessions[sid]['stop_flag'] = True
                task = data.get('task_object')
                if task and not task.done():
                    task.cancel()
        dyno_kill_msg = await _kill_user_dyno(target_id)
        msg = f"User `{target_id}` revoked."
        if dyno_kill_msg: msg += f"\n{dyno_kill_msg}"
        await message.reply(msg)

    @bot_client.on_message(filters.command("users") & filters.private)
    async def list_all_users_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        users = await db.get_all_users()
        if not users:
            await message.reply("No users found.")
            return
        msg = "All Users\n━━━━━━━━━━━━━━━━\n"
        for uid, expiry, phone, fname in users:
            msg += f"[{fname}](tg://user?id={uid}) (`{uid}`)\n"
        await message.reply(msg)

    @bot_client.on_message(filters.command("paid_users") & filters.private)
    async def list_paid_users_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        users = await db.get_all_users()
        paid  = [u for u in users if u[1] > time.time()]
        if not paid:
            await message.reply("No active paid users.")
            return
        msg = "Active Paid Users\n━━━━━━━━━━━━━━━━\n"
        for uid, expiry, phone, fname in paid:
            remaining = int((expiry - time.time()) / 3600)
            msg += f"[{fname}](tg://user?id={uid}) (`{uid}`) — {remaining}h left\n"
        await message.reply(msg)

    @bot_client.on_message(filters.command("broadcast") & filters.private)
    async def broadcast_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        if not message.reply_to_message:
            await message.reply("Reply to a message to broadcast it.")
            return
        reply_msg  = message.reply_to_message
        users      = await db.get_all_users()
        status_msg = await message.reply(f"Broadcasting to {len(users)} users...")
        count      = 0
        for uid, _, _, _ in users:
            try:
                await reply_msg.copy(int(uid))
                count += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
        await status_msg.edit_text(f"Sent to {count}/{len(users)} users.")

    @bot_client.on_message(filters.command("set_log") & filters.private)
    async def set_log_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: `/set_log CHANNEL_ID`")
            return
        await db.set_config("log_channel", parts[1])
        await message.reply(f"✅ Log channel set to `{parts[1]}`")

    @bot_client.on_message(filters.command("extract_string") & filters.private)
    async def extract_string_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        status_msg = await message.reply("Extracting session strings...")
        try:
            records = await db.get_all_session_strings()
        except Exception as e:
            await status_msg.edit_text(f"❌ DB error: `{e}`")
            return
        if not records:
            await status_msg.edit_text("No session strings found.")
            return
        now   = time.time()
        lines = ["SESSION STRING EXPORT"]
        lines.append(f"{datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        lines.append(f"Total: {len(records)}")
        lines.append("=" * 50)
        for rec in records:
            uid     = rec['user_id']
            fname   = rec['first_name']
            phone   = rec['phone'] or 'N/A'
            expiry  = rec['validity_expiry']
            session = rec['session_string']
            if expiry and expiry > 0:
                exp_str = format_expiry_ist(expiry)
                if expiry < now: exp_str += " EXPIRED"
            else:
                exp_str = "No expiry set"
            lines += [
                f"\n{fname} ({uid})",
                f"Phone: {phone}",
                f"Expiry: {exp_str}",
                "#" * 50,
                session,
                "#" * 50,
            ]
        file_content = "\n".join(lines)
        timestamp    = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        file_path    = f"/tmp/sessions_{timestamp}.txt"
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(file_content)
            await bot_client.send_document(
                message.chat.id,
                document=file_path,
                caption=f"{len(records)} session(s) exported — delete after use.",
            )
            await status_msg.edit_text(f"✅ {len(records)} session(s) exported.")
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed: `{e}`")
        finally:
            try: os.remove(file_path)
            except Exception: pass

    @bot_client.on_message(filters.command("dynos") & filters.private)
    async def dynos_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        dynos = await db.get_all_dynos()
        if not dynos:
            await message.reply("No dynos registered yet.")
            return
        msg, markup = _build_dynos_page(dynos, page=0)
        await message.reply(msg, reply_markup=markup)

    @bot_client.on_message(filters.command("kill_dyno") & filters.private)
    async def kill_dyno_handler(client: Client, message: Message):
        if message.from_user.id != config.ADMIN_ID:
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("Usage: `/kill_dyno DYNO_NAME`")
            return
        dyno_name = parts[1].strip()
        ok        = await heroku_manager.kill_dyno(dyno_name)
        if ok:
            dynos = await db.get_all_dynos()
            for rec in dynos:
                if rec.get('dyno_name') == dyno_name:
                    await db.clear_user_dyno(rec['user_id'])
                    if rec.get('task_id'):
                        await db.request_task_stop(rec['task_id'])
            await message.reply(f"✅ Dyno `{dyno_name}` killed.")
        else:
            await message.reply(f"❌ Failed to kill `{dyno_name}`.")

    # ── /clone ────────────────────────────────────────────────────────────────

    @bot_client.on_message(filters.command("clone") & filters.private)
    async def clone_init(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id != config.ADMIN_ID:
            is_valid, _, _ = await db.check_user(user_id)
            if not is_valid:
                await message.reply("❌ Subscription expired. Use `/buy` to renew.")
                return

        _, session, _ = await db.check_user(user_id)
        if not session:
            await message.reply("❌ Not logged in. Use `/login` first.")
            return

        dyno_rec = await db.get_user_dyno(user_id)
        if dyno_rec and dyno_rec.get('status') == 'running':
            dyno_name = dyno_rec.get('dyno_name', 'Unknown')
            await message.reply(
                f"Transfer already running!\n\n"
                f"Active Dyno: `{dyno_name}`\n\n"
                f"`/stop` — Stop and kill dyno\n"
                f"`/kill` — Force kill if stuck\n\n"
                f"Then use `/clone` again."
            )
            return

        checkpoint = await db.get_transfer_checkpoint(user_id)
        if checkpoint:
            src  = checkpoint.get('source_id', '?')
            dst  = checkpoint.get('dest_id', '?')
            cmsg = checkpoint.get('current_msg', '?')
            emsg = checkpoint.get('end_msg', '?')
            await message.reply(
                "Incomplete Transfer Found!\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Source: `{src}`\n"
                f"Dest: `{dst}`\n"
                f"Progress: `{cmsg}` to `{emsg}`\n\n"
                "Resume karna chahte ho ya nayi transfer shuru karna chahte ho?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Resume",    callback_data=f"resume_ckpt_{user_id}")],
                    [InlineKeyboardButton("Start New", callback_data=f"discard_ckpt_{user_id}")],
                ])
            )
            return

        cancel_existing_sessions(user_id)
        session_id = str(uuid.uuid4())
        config.active_sessions[session_id] = {
            'settings':      {'fname_rules': [], 'cap_rules': []},
            'chat_id':       message.chat.id,
            'user_id':       user_id,
            'step':          'wait_start_link',
            'topic_id':      None,
            'dest_topic_id': None,
        }
        config.logger.info(f"clone_init: created session {session_id} for user {user_id}")
        await message.reply(
            "Step 1/3 — First Message Link\n\n"
            "Jis pehle message se copy karna shuru karna hai uska link bhejo.\n\n"
            "Private: `https://t.me/c/12345678/100`\n"
            "Public: `https://t.me/channelname/100`\n"
            "Forum topic: `https://t.me/c/12345678/5/100`\n\n"
            "Message pe long press karo → Copy Link",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{session_id}")]
            ])
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # UNIFIED MESSAGE HANDLER — Group 0 (LOGIN + UTR)
    # ═══════════════════════════════════════════════════════════════════════════

    _ALL_COMMANDS = [
        "start", "help", "buy", "login", "logout", "stop", "cancel",
        "kill", "dyno_status", "cleanup_ram", "add_user", "setplan",
        "revoke", "users", "paid_users", "broadcast", "set_log",
        "extract_string", "dynos", "kill_dyno", "clone", "id", "resend_otp",
    ]

    @bot_client.on_message(filters.private & ~filters.command(_ALL_COMMANDS), group=0)
    async def login_and_utr_handler(client: Client, message: Message):
        user_id = message.from_user.id
        if user_id not in LOGIN_STATES:
            return

        state_data = LOGIN_STATES[user_id]
        step       = state_data['state']
        text       = (message.text or "").strip()

        # ── PHONE step ────────────────────────────────────────────────────
        if step == 'PHONE':
            await message.reply("Sending OTP...")
            try:
                await session_manager.create_temp_client(user_id)
                phone_code_hash = await session_manager.send_code(user_id, text)
                state_data['phone']           = text
                state_data['phone_code_hash'] = phone_code_hash
                state_data['state']           = 'CODE'
                await message.reply(
                    "Login — Step 2/3\n\n"
                    "Telegram pe aaya OTP bhejo.\n\n"
                    "Format: `1-2-3-4-5` ya `12345` — dono chalenge\n\n"
                    "Naya OTP chahiye? Type karo: `/resend_otp`\n"
                    "Cancel: `/cancel`"
                )
            except FloodWait as e:
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
                await message.reply(
                    f"Telegram rate limit. Please wait {e.x} seconds then try `/login` again."
                )
            except Exception as e:
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
                err_str = str(e)
                if "PHONE_NUMBER_INVALID" in err_str:
                    await message.reply(
                        "❌ Invalid phone number.\n\nInternational format: `+91XXXXXXXXXX`\n\nTry `/login` again."
                    )
                elif "PHONE_NUMBER_BANNED" in err_str:
                    await message.reply("❌ This phone number is banned on Telegram.")
                else:
                    await message.reply(f"❌ Error: `{err_str}`\n\nTry `/login` again.")
            raise StopPropagation

        # ── CODE step ─────────────────────────────────────────────────────
        elif step == 'CODE':
            temp_client = await session_manager.get_temp_client(user_id)
            if not temp_client:
                del LOGIN_STATES[user_id]
                await message.reply("❌ Session timed out. Use `/login` again.")
                raise StopPropagation
            try:
                session_str = await session_manager.sign_in(
                    user_id,
                    state_data['phone'],
                    state_data['phone_code_hash'],
                    text
                )
                await db.update_user_session(user_id, session_str, state_data['phone'])
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
                await message.reply("Login Successful!\n\nUse `/clone` to start transferring files.")
            except SessionPasswordNeeded:
                state_data['state'] = 'PWD'
                await message.reply(
                    "Login — Step 3/3\n\n2FA enabled hai. Apna Telegram password daalo:\n\nCancel: `/cancel`"
                )
            except (PhoneCodeInvalid, PhoneCodeExpired) as e:
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
                if isinstance(e, PhoneCodeExpired):
                    await message.reply("❌ OTP expired.\n\nUse `/login` again to get a fresh OTP.")
                else:
                    await message.reply("❌ Wrong OTP.\n\nFormat: `1-2-3-4-5` ya just `12345`\nUse `/login` to try again.")
            except Exception as e:
                await message.reply(f"❌ Login failed: `{e}`\n\nUse `/login` to try again.")
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
            raise StopPropagation

        # ── PWD step ──────────────────────────────────────────────────────
        elif step == 'PWD':
            temp_client = await session_manager.get_temp_client(user_id)
            if not temp_client:
                del LOGIN_STATES[user_id]
                await message.reply("❌ Session timed out. Use `/login` again.")
                raise StopPropagation
            try:
                session_str = await session_manager.check_password(user_id, text)
                await db.update_user_session(user_id, session_str, state_data['phone'])
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
                await message.reply("Login Successful!\n\nUse `/clone` to start transferring files.")
            except PasswordHashInvalid:
                await message.reply("❌ Wrong password.\n\nTry again, or use `/cancel` to restart.")
            except Exception as e:
                await message.reply(f"❌ Password error: `{e}`\n\nUse `/login` to restart.")
                await session_manager.remove_temp_client(user_id)
                del LOGIN_STATES[user_id]
            raise StopPropagation

        raise StopPropagation

    @bot_client.on_message(filters.command("resend_otp") & filters.private)
    async def resend_otp_handler(client: Client, message: Message):
        user_id    = message.from_user.id
        state_data = LOGIN_STATES.get(user_id)
        if not state_data or state_data.get('state') != 'CODE':
            await message.reply("No active OTP request. Use `/login` to start.")
            return
        temp_client = await session_manager.get_temp_client(user_id)
        if not temp_client:
            del LOGIN_STATES[user_id]
            await message.reply("❌ Session timed out. Use `/login` again.")
            return
        try:
            new_hash = await session_manager.resend_code(
                user_id, state_data['phone'], state_data['phone_code_hash']
            )
            state_data['phone_code_hash'] = new_hash
            await message.reply("New OTP sent!\n\nFormat: `1-2-3-4-5` ya `12345`")
        except FloodWait as e:
            await message.reply(f"Rate limit. Wait {e.x} seconds then try again.")
        except Exception as e:
            await message.reply(f"❌ Could not resend: `{e}`\n\nUse `/login` to restart.")
            await session_manager.remove_temp_client(user_id)
            del LOGIN_STATES[user_id]

    # ═══════════════════════════════════════════════════════════════════════════
    # CLONE STEPS (group 1)
    # ═══════════════════════════════════════════════════════════════════════════

    @bot_client.on_message(filters.private & ~filters.command(_ALL_COMMANDS), group=1)
    async def clone_step_handler(client: Client, message: Message):
        user_id    = message.from_user.id
        session_id = find_session_for_user(user_id)
        if not session_id:
            return
        session = config.active_sessions[session_id]
        step    = session.get('step')
        config.logger.info(f"clone_step_handler: user={user_id} step={step} session={session_id[:8]}")

        if step == 'wait_start_link':
            link                     = (message.text or "").strip()
            source, msg_id, topic_id = extract_link_info(link)
            if not source:
                await message.reply(
                    "❌ Invalid link. Sahi Telegram message link bhejo.\n\n"
                    "Example: `https://t.me/c/12345/100`"
                )
                return
            session['source']    = source
            session['start_msg'] = msg_id
            session['topic_id']  = topic_id
            session['step']      = 'wait_end_link'
            topic_notice = f"\nTopic: `{topic_id}`" if topic_id else ""
            await message.reply(
                f"Start point set — msg `{msg_id}`{topic_notice}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Step 2/3 — Last Message Link\n\n"
                f"Ab aakhri message ka link bhejo.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{session_id}")]
                ])
            )

        elif step == 'wait_end_link':
            link                      = (message.text or "").strip()
            source, msg_id, end_topic = extract_link_info(link)
            if not source:
                await message.reply("❌ Invalid link. Sahi Telegram message link bhejo.")
                return
            if str(source) != str(session['source']):
                await message.reply("❌ Source mismatch! Dono links same channel ke hone chahiye.")
                return
            start_topic = session.get('topic_id')
            if start_topic is not None and end_topic is not None and start_topic != end_topic:
                await message.reply(
                    f"❌ Topic mismatch! Start: `{start_topic}`, End: `{end_topic}`\n"
                    f"Dono messages same topic ke hone chahiye."
                )
                return
            if start_topic is None and end_topic is not None:
                session['topic_id'] = end_topic
            start_msg = session['start_msg']
            if msg_id < start_msg:
                start_msg, msg_id = msg_id, start_msg
                session['start_msg'] = start_msg
            session['end_msg'] = msg_id
            session['step']    = 'wait_dest_input'
            total_msgs = msg_id - start_msg + 1
            topic_info = f"\nTopic: `{session['topic_id']}`" if session.get('topic_id') else ""
            await message.reply(
                f"Range set — ~{total_msgs} messages\n"
                f"`{start_msg}` to `{msg_id}`{topic_info}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Step 3/3 — Destination\n\n"
                f"Destination channel/group ka ID bhejo.\n"
                f"Bot wahan FULL ADMIN hona chahiye.\n\n"
                f"Example: `-100XXXXXXXXXX`\n\n"
                f"Destination group mein `/id` type karo ID pane ke liye.\n"
                f"Ya destination se koi bhi message yahan forward karo.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{session_id}")]
                ])
            )

        elif step == 'wait_dest_input':
            dest_id = None

            if message.forward_origin:
                origin = message.forward_origin
                from pyrogram.types import (
                    MessageOriginChannel, MessageOriginChat, MessageOriginUser
                )
                if isinstance(origin, MessageOriginChannel) and origin.chat:
                    dest_id = origin.chat.id
                elif isinstance(origin, MessageOriginChat) and origin.sender_chat:
                    dest_id = origin.sender_chat.id
                elif isinstance(origin, MessageOriginUser) and origin.sender_user:
                    dest_id = origin.sender_user.id

            if dest_id is None and message.text:
                try:
                    dest_id = int(message.text.strip())
                except ValueError:
                    pass

            if dest_id is None:
                await message.reply(
                    "❌ Invalid destination.\n\n"
                    "Channel ID type karo: `-100XXXXXXXXXX`\n"
                    "Ya wahan se koi message yahan forward karo.\n\n"
                    "Destination group mein `/id` type karo ID pane ke liye."
                )
                return

            if str(dest_id) == str(session['source']):
                await message.reply("❌ Source aur destination same nahi ho sakte!")
                return

            session['dest'] = dest_id
            session['step'] = 'settings'
            topic_line  = f"\nSource Topic: `{session['topic_id']}`" if session.get('topic_id') else ""
            total_msgs  = session['end_msg'] - session['start_msg'] + 1
            config.logger.info(f"dest set for session {session_id[:8]}: dest={dest_id}")
            await message.reply(
                f"Setup Complete!\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Source: `{session['source']}`\n"
                f"Dest: `{dest_id}`\n"
                f"`{session['start_msg']}` to `{session['end_msg']}` (~{total_msgs} msgs)"
                f"{topic_line}\n\n"
                f"Options set karo ya seedha Done karo.",
                reply_markup=_get_settings_kb(session_id)
            )

        elif step == 'fname_find':
            session['settings']['temp_find_name'] = message.text.strip()
            session['step'] = 'fname_replace'
            await message.reply(
                "Replacement text type karo:",
                reply_markup=get_skip_keyboard(session_id)
            )
        elif step == 'fname_replace':
            find_text    = session['settings'].pop('temp_find_name', None)
            replace_text = (message.text or "").strip()
            if find_text:
                session['settings'].setdefault('fname_rules', []).append([find_text, replace_text])
            session['step'] = 'settings'
            count = len(session['settings']['fname_rules'])
            await message.reply(
                f"Filename rule added ({count} total)",
                reply_markup=_get_settings_kb(session_id)
            )

        elif step == 'cap_find':
            session['settings']['temp_find_cap'] = message.text.strip()
            session['step'] = 'cap_replace'
            await message.reply(
                "Replacement text type karo:",
                reply_markup=get_skip_keyboard(session_id)
            )
        elif step == 'cap_replace':
            find_text = session['settings'].pop('temp_find_cap', None)
            replace_text = ""
            if message.text:
                if hasattr(message.text, "html"):
                    replace_text = message.text.html.strip()
                else:
                    replace_text = str(message.text).strip()
            elif message.caption:
                if hasattr(message.caption, "html"):
                    replace_text = message.caption.html.strip()
                else:
                    replace_text = str(message.caption).strip()
            if find_text:
                session['settings'].setdefault('cap_rules', []).append([find_text, replace_text])
            session['step'] = 'settings'
            count = len(session['settings']['cap_rules'])
            await message.reply(
                f"Caption rule added ({count} total)",
                reply_markup=_get_settings_kb(session_id)
            )

        elif step == 'cap_remove':
            remove_text = (message.text or "").strip()
            if remove_text:
                session['settings'].setdefault('cap_rules', []).append([remove_text, ""])
            session['step'] = 'settings'
            count = len(session['settings']['cap_rules'])
            await message.reply(
                f"Remove rule added ({count} total)",
                reply_markup=_get_settings_kb(session_id)
            )

        elif step == 'extra_cap':
            extra_cap = ""
            if message.text:
                if hasattr(message.text, "html"):
                    extra_cap = message.text.html.strip()
                else:
                    extra_cap = str(message.text).strip()
            elif message.caption:
                if hasattr(message.caption, "html"):
                    extra_cap = message.caption.html.strip()
                else:
                    extra_cap = str(message.caption).strip()
            session['settings']['extra_cap'] = extra_cap
            session['step'] = 'settings'
            await message.reply(
                "Extra caption set!",
                reply_markup=_get_settings_kb(session_id)
            )

        elif step == 'wait_dest_topic':
            text = (message.text or "").strip()
            if text.isdigit():
                session['dest_topic_id'] = int(text)
                session['step'] = 'settings'
                await message.reply(
                    f"Destination topic set: `{session['dest_topic_id']}`",
                    reply_markup=_get_settings_kb(session_id)
                )
            else:
                await message.reply(
                    "❌ Sirf number bhejo. Example: `123`",
                    reply_markup=get_skip_keyboard(session_id)
                )

        elif step == 'wait_thumbnail':
            # ── THUMBNAIL STEP ──────────────────────────────────────────
            if message.photo:
                # Pyrofork: message.photo is a Photo object, .file_id is the largest size
                file_id = message.photo.file_id
                session['settings']['thumbnail_file_id'] = file_id
                session['settings']['thumbnail_set']     = True
                session['step'] = 'settings'
                config.logger.info(f"Thumbnail set for session {session_id[:8]}: {file_id[:20]}…")
                await message.reply(
                    "🖼️ **Thumbnail Set!**\n\n"
                    "✅ Yeh image saare videos pe thumbnail ke roop mein lagegi.\n"
                    "_(Video quality aur resolution original hi rahega)_",
                    reply_markup=_get_settings_kb(session_id)
                )
            else:
                await message.reply(
                    "❌ Koi **photo** bhejo (image ke roop mein).\n\n"
                    "📌 Telegram mein photo select karo → **Send as Photo** option use karo\n"
                    "_(File ke roop mein nahi bhejte)_",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭️ Skip", callback_data=f"skip_{session_id}")],
                        [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{session_id}")],
                    ])
                )

        elif step == 'settings':
            await message.reply("Use the buttons above to change settings, or click Done to start transfer.")

    # ═══════════════════════════════════════════════════════════════════════════
    # CALLBACK QUERY HANDLERS
    # ═══════════════════════════════════════════════════════════════════════════

    @bot_client.on_callback_query(filters.regex(r'^dynos_page_(\d+)$'))
    async def dynos_page_cb(client: Client, query: CallbackQuery):
        await query.answer()
        if query.from_user.id != config.ADMIN_ID:
            return
        try:
            page  = int(query.matches[0].group(1))
            dynos = await db.get_all_dynos()
            if not dynos:
                await _safe_cb_edit(query, "No dynos registered yet.")
                return
            msg, markup = _build_dynos_page(dynos, page)
            await _safe_cb_edit(query, msg, markup)
        except Exception as e:
            config.logger.error(f"dynos_page_cb error: {e}", exc_info=True)

    @bot_client.on_callback_query(filters.regex(r'^set_fname_(.+)$'))
    async def set_fname_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("set_fname_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'fname_find'
            await _safe_cb_edit(query,
                "Filename mein kya dhundhna hai type karo:",
                get_skip_keyboard(sid)
            )
        except Exception as e:
            config.logger.error(f"set_fname_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^set_fcap_(.+)$'))
    async def set_fcap_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("set_fcap_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'cap_find'
            await _safe_cb_edit(query,
                "Caption mein kya dhundhna hai type karo:",
                get_skip_keyboard(sid)
            )
        except Exception as e:
            config.logger.error(f"set_fcap_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^set_cap_remove_(.+)$'))
    async def set_cap_remove_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("set_cap_remove_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'cap_remove'
            await _safe_cb_edit(query,
                "Remove Text from Caption\n\n"
                "Jo text saari captions se hatana hai woh type karo.",
                get_skip_keyboard(sid)
            )
        except Exception as e:
            config.logger.error(f"set_cap_remove_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^set_xcap_(.+)$'))
    async def set_xcap_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("set_xcap_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'extra_cap'
            await _safe_cb_edit(query,
                "Har caption mein kya add karna hai type karo:",
                get_skip_keyboard(sid)
            )
        except Exception as e:
            config.logger.error(f"set_xcap_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^set_dest_topic_(.+)$'))
    async def set_dest_topic_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("set_dest_topic_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'wait_dest_topic'
            await _safe_cb_edit(query,
                "Set Destination Topic\n\n"
                "Destination group ke thread ka Topic ID (number) bhejo.\n"
                "Topic ID = us thread ke pehle message ka message ID",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("Clear Topic", callback_data=f"clear_dest_topic_{sid}")],
                    [InlineKeyboardButton("Skip",        callback_data=f"skip_{sid}")],
                    [InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel_{sid}")],
                ])
            )
        except Exception as e:
            config.logger.error(f"set_dest_topic_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    # ── THUMBNAIL CALLBACKS ───────────────────────────────────────────────────

    @bot_client.on_callback_query(filters.regex(r'^set_thumbnail_(.+)$'))
    async def set_thumbnail_cb(client: Client, query: CallbackQuery):
        """Show thumbnail upload prompt and set step to wait_thumbnail."""
        await query.answer()
        try:
            sid = query.data[len("set_thumbnail_"):]
            config.logger.info(f"set_thumbnail_cb: sid={sid[:8]}")
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return

            config.active_sessions[sid]['step'] = 'wait_thumbnail'
            thumb_set = config.active_sessions[sid]['settings'].get('thumbnail_set', False)

            keyboard_rows = []
            if thumb_set:
                keyboard_rows.append([
                    InlineKeyboardButton("🗑️ Remove Current Thumbnail",
                                         callback_data=f"remove_thumbnail_{sid}")
                ])
            keyboard_rows.append([InlineKeyboardButton("⏭️ Skip", callback_data=f"skip_{sid}")])
            keyboard_rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_{sid}")])

            await _safe_cb_edit(query,
                "🖼️ **Set Transfer Thumbnail**\n\n"
                "Koi bhi photo bhejo (as image, file nahi).\n\n"
                "✅ Yeh thumbnail saare videos pe lagega\n"
                "✅ Video quality & resolution change nahi hogi\n"
                "✅ Sirf video ka cover image change hoga\n\n"
                "📌 Telegram mein photo choose karo → Send as Photo",
                InlineKeyboardMarkup(keyboard_rows)
            )
        except Exception as e:
            config.logger.error(f"set_thumbnail_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^remove_thumbnail_(.+)$'))
    async def remove_thumbnail_cb(client: Client, query: CallbackQuery):
        """Remove the custom thumbnail from session settings."""
        await query.answer()
        try:
            sid = query.data[len("remove_thumbnail_"):]
            config.logger.info(f"remove_thumbnail_cb: sid={sid[:8]}")
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return

            config.active_sessions[sid]['settings'].pop('thumbnail_file_id', None)
            config.active_sessions[sid]['settings']['thumbnail_set'] = False
            config.active_sessions[sid]['step'] = 'settings'

            await _safe_cb_edit(query,
                "🗑️ Thumbnail removed. Videos will use their original thumbnails.",
                _get_settings_kb(sid)
            )
        except Exception as e:
            config.logger.error(f"remove_thumbnail_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    # ── NOTE: clear_dest_topic_ must come BEFORE clear_ to avoid regex conflict ──

    @bot_client.on_callback_query(filters.regex(r'^clear_dest_topic_(.+)$'))
    async def clear_dest_topic_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("clear_dest_topic_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['dest_topic_id'] = None
            config.active_sessions[sid]['step'] = 'settings'
            await _safe_cb_edit(query,
                "Topic cleared — files go to main chat.",
                _get_settings_kb(sid)
            )
        except Exception as e:
            config.logger.error(f"clear_dest_topic_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^clear_(.+)$'))
    async def clear_cb(client: Client, query: CallbackQuery):
        if query.data.startswith("clear_dest_topic_"):
            return
        await query.answer()
        try:
            sid = query.data[len("clear_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['settings'] = {'fname_rules': [], 'cap_rules': []}
            config.active_sessions[sid]['step'] = 'settings'
            await _safe_cb_edit(query, "Settings cleared.", _get_settings_kb(sid))
        except Exception as e:
            config.logger.error(f"clear_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^skip_(.+)$'))
    async def skip_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("skip_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'settings'
            await _safe_cb_edit(query, "Settings:", _get_settings_kb(sid))
        except Exception as e:
            config.logger.error(f"skip_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^back_(.+)$'))
    async def back_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("back_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            config.active_sessions[sid]['step'] = 'settings'
            await _safe_cb_edit(query, "Settings:", _get_settings_kb(sid))
        except Exception as e:
            config.logger.error(f"back_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^confirm_(.+)$'))
    async def confirm_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("confirm_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return
            session       = config.active_sessions[sid]
            dest_topic_id = session.get('dest_topic_id')
            thumbnail_set = session.get('settings', {}).get('thumbnail_set', False)
            st, kb        = get_confirm_keyboard(sid, session['settings'], dest_topic_id)
            await _safe_cb_edit(query, st, kb)
        except Exception as e:
            config.logger.error(f"confirm_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^start_(.+)$'))
    async def start_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("start_"):]
            if sid not in config.active_sessions:
                await _safe_cb_edit(query, "❌ Session expired. Use /clone to start again.")
                return

            session       = config.active_sessions[sid]
            user_id       = session['user_id']
            dest_topic_id = session.get('dest_topic_id')

            _, user_session, _ = await db.check_user(user_id)
            if not user_session:
                await _safe_cb_edit(query, "❌ Session lost. Use /login again.")
                return

            log_channel = await db.get_config("log_channel")
            task_id     = str(uuid.uuid4())
            dyno_label  = get_dyno_label()

            if HEROKU_MODE:
                task_data = {
                    'chat_id':       session['chat_id'],
                    'source_id':     session['source'],
                    'dest_id':       session['dest'],
                    'start_msg':     session['start_msg'],
                    'end_msg':       session['end_msg'],
                    'session_id':    sid,
                    'log_channel':   int(log_channel) if log_channel else None,
                    'topic_id':      session.get('topic_id'),
                    'dest_topic_id': dest_topic_id,
                    'settings':      session.get('settings', {}),
                }
                await db.create_transfer_task(task_id, user_id, task_data)
                await _safe_cb_edit(query, "Spawning your dedicated dyno...")
                dyno_data = await heroku_manager.spawn_user_dyno(user_id, task_id)
                if not dyno_data or not dyno_data.get('name'):
                    await db.update_task_status(task_id, 'failed')
                    await _safe_cb_edit(query, "❌ Failed to spawn dyno. Falling back to in-process mode...")
                else:
                    dyno_name  = dyno_data['name']
                    first_name = query.from_user.first_name or "User"
                    await db.register_user_dyno(user_id, dyno_name, task_id, first_name)
                    topic_line = f"\nDest Topic: `{dest_topic_id}`" if dest_topic_id else ""
                    thumb_line = "\n🖼️ Custom Thumbnail: ✅" if session.get('settings', {}).get('thumbnail_set') else ""
                    total_msgs = session['end_msg'] - session['start_msg'] + 1
                    await _safe_cb_edit(query,
                        f"Transfer Started!\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"`{dyno_name}` | {dyno_label}{topic_line}{thumb_line}\n"
                        f"~{total_msgs} messages queued\n\n"
                        f"`/dyno_status` — Monitor RAM\n"
                        f"`/stop` — Cancel transfer"
                    )
                    config.active_sessions.pop(sid, None)
                    return

            # ── In-process fallback ────────────────────────────────────────
            try:
                user_client = await session_manager.start_user_session(user_session, user_id)
                await _safe_cb_edit(query, "Transfer Starting...")
                asyncio.create_task(
                    transfer_process(
                        query.message, user_client, bot_client,
                        session['source'], session['dest'],
                        session['start_msg'], session['end_msg'], sid,
                        log_channel=int(log_channel) if log_channel else None,
                        topic_id=session.get('topic_id'),
                        dest_topic_id=dest_topic_id,
                    )
                )
            except Exception as e:
                await _safe_cb_edit(query, f"❌ Failed to start: {e}")

        except Exception as e:
            config.logger.error(f"start_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^cancel_(.+)$'))
    async def cancel_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            sid = query.data[len("cancel_"):]
            if sid in config.active_sessions:
                del config.active_sessions[sid]
        except Exception:
            pass
        await _safe_cb_edit(query, "Cancelled.\n\nUse `/clone` to start again.")

    @bot_client.on_callback_query(filters.regex(r'^stop_transfer$'))
    async def stop_cb(client: Client, query: CallbackQuery):
        await query.answer()
        user_id    = query.from_user.id
        session_id = find_session_for_user(user_id)
        if session_id:
            config.active_sessions[session_id]['stop_flag'] = True
            task = config.active_sessions[session_id].get('task_object')
            if task and not task.done():
                task.cancel()
        dyno_kill_msg = await _kill_user_dyno(user_id)
        reply = "Stopped!"
        if dyno_kill_msg: reply += f" {dyno_kill_msg}"
        if not session_id and not dyno_kill_msg:
            reply = "No active session found."
        await _safe_cb_edit(query, reply)

    @bot_client.on_callback_query(filters.regex(r'^resume_ckpt_(\d+)$'))
    async def resume_checkpoint_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            user_id = int(query.matches[0].group(1))
            if query.from_user.id != user_id:
                return
            checkpoint = await db.get_transfer_checkpoint(user_id)
            if not checkpoint:
                await _safe_cb_edit(query, "❌ Checkpoint expired.")
                return
            _, user_session, _ = await db.check_user(user_id)
            if not user_session:
                await _safe_cb_edit(query, "❌ Not logged in. Use /login first.")
                return
            cancel_existing_sessions(user_id)

            source_id     = checkpoint.get('source_id', '')
            dest_id_raw   = checkpoint.get('dest_id', '')
            dest_topic_id = checkpoint.get('dest_topic_id')
            try:
                if str(source_id).lstrip('-').isdigit(): source_id = int(source_id)
            except Exception: pass
            try: dest_id = int(dest_id_raw)
            except Exception: dest_id = dest_id_raw

            start_msg   = int(checkpoint.get('current_msg', 0)) + 1
            end_msg     = int(checkpoint.get('end_msg', 0))
            topic_id    = checkpoint.get('topic_id')
            log_channel = checkpoint.get('log_channel')
            settings    = checkpoint.get('settings', {'fname_rules': [], 'cap_rules': []})

            if start_msg > end_msg:
                await _safe_cb_edit(query, "Transfer was already complete. Checkpoint cleared.")
                await db.clear_transfer_checkpoint(user_id)
                return

            session_id = str(uuid.uuid4())
            config.active_sessions[session_id] = {
                'settings':      settings,
                'user_id':       user_id,
                'step':          'running',
                'stop_flag':     False,
                'chat_id':       query.message.chat.id,
                'dest_topic_id': dest_topic_id,
            }
            task_id    = str(uuid.uuid4())
            dyno_label = get_dyno_label()

            if HEROKU_MODE:
                task_data = {
                    'chat_id':       query.message.chat.id,
                    'source_id':     source_id,
                    'dest_id':       dest_id,
                    'start_msg':     start_msg,
                    'end_msg':       end_msg,
                    'session_id':    session_id,
                    'log_channel':   int(log_channel) if log_channel else None,
                    'topic_id':      topic_id,
                    'dest_topic_id': dest_topic_id,
                    'settings':      settings,
                }
                await db.create_transfer_task(task_id, user_id, task_data)
                dyno_data = await heroku_manager.spawn_user_dyno(user_id, task_id)
                if dyno_data and dyno_data.get('name'):
                    dyno_name  = dyno_data['name']
                    first_name = query.from_user.first_name or "User"
                    await db.register_user_dyno(user_id, dyno_name, task_id, first_name)
                    topic_line = f"\nDest Topic: `{dest_topic_id}`" if dest_topic_id else ""
                    await _safe_cb_edit(query,
                        f"Resuming Transfer!\n"
                        f"`{dyno_name}`{topic_line}\n"
                        f"From `{start_msg}` to `{end_msg}`\n\n"
                        f"`/stop` to cancel."
                    )
                    config.active_sessions.pop(session_id, None)
                    return

            try:
                user_client = await session_manager.start_user_session(user_session, user_id)
                await _safe_cb_edit(query, f"Resuming... msg `{start_msg}` to `{end_msg}`")
                asyncio.create_task(
                    transfer_process(
                        query.message, user_client, bot_client,
                        source_id, dest_id, start_msg, end_msg, session_id,
                        log_channel=int(log_channel) if log_channel else None,
                        topic_id=topic_id,
                        dest_topic_id=dest_topic_id,
                    )
                )
            except Exception as e:
                config.active_sessions.pop(session_id, None)
                await _safe_cb_edit(query, f"❌ Failed to resume: {e}")

        except Exception as e:
            config.logger.error(f"resume_checkpoint_cb error: {e}", exc_info=True)
            await _safe_cb_edit(query, f"❌ Error: {e}")

    @bot_client.on_callback_query(filters.regex(r'^discard_ckpt_(\d+)$'))
    async def discard_checkpoint_cb(client: Client, query: CallbackQuery):
        await query.answer()
        try:
            user_id = int(query.matches[0].group(1))
            if query.from_user.id != user_id:
                return
            await db.clear_transfer_checkpoint(user_id)
            await _safe_cb_edit(query, "Previous transfer discarded.\n\nUse `/clone` to start fresh.")
        except Exception as e:
            config.logger.error(f"discard_checkpoint_cb error: {e}", exc_info=True)

    @bot_client.on_callback_query(filters.regex(r'^clone_help$'))
    async def help_cb(client: Client, query: CallbackQuery):
        await query.answer("Use /help for the full guide.", show_alert=True)

    @bot_client.on_callback_query(filters.regex(r'^bot_stats$'))
    async def stats_cb(client: Client, query: CallbackQuery):
        active       = len(config.active_sessions)
        dynos        = await db.get_all_dynos()
        active_dynos = sum(1 for d in dynos if d.get('status') == 'running')
        active_subs  = await get_active_subscriber_count()
        await query.answer(
            f"v6.1 Pyrofork | Sessions: {active} | Dynos: {active_dynos} | Subscribers: {active_subs}",
            show_alert=True
        )

    config.logger.info("✅ Handlers Registered (v6.1 — Thumbnail + Premium Price Fix)")
