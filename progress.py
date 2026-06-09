"""
progress.py  –  Throttled progress callbacks for Pyrogram download/upload.

WHY THROTTLING IS CRITICAL:
  Pyrogram fires progress callbacks on every ~512 KB network chunk.
  For a 500 MB file at 10 MB/s → ~500 callbacks in 50 seconds.
  Without throttling → 500 message.edit calls → instant FloodWait → bot stuck.
  With 8-second throttle → ~6 edits in 50 seconds → safe for 30+ concurrent users.

DESIGN:
  TransferProgress tracks one file's complete lifecycle: download phase + upload phase.
  Both phases share the same status message and use separate last_update timers so
  the download bar never bleeds into the upload bar timing.

  KEY RULE: No manual edit calls should accompany these callbacks.
  The 8-second interval IS the update rate. If the interval hasn't elapsed since the
  last download tick and upload just started, the message stays on the last download
  state — that is intentional. User sees whatever is happening at each 8s tick.

  _safe_edit() uses asyncio.create_task() so the edit never blocks the download/upload
  coroutine. A failed edit is silently swallowed — non-fatal.

  msg_id field: shown in the progress bar so users know which message is being processed.
"""

import asyncio
import math
import time

import config
from utils import human_readable_size, time_formatter


class TransferProgress:
    """
    Progress tracker for a single file's download + upload phases.

    Usage:
        tracker = TransferProgress(file_name, file_size, status_msg, session_data, msg_id=msg.id)

        # During download:
        await user_client.download_media(
            message,
            file_name=path,
            progress=tracker.download_cb,
        )

        # Reset before upload:
        tracker.reset_for_upload()

        # During upload:
        await bot_client.send_document(
            ...,
            progress=tracker.upload_cb,
        )
    """

    def __init__(
        self,
        file_name:    str,
        file_size:    int,
        status_msg,           # Pyrogram Message object with edit_text()
        session_data: dict,
        msg_id:       int = 0,
    ):
        self.file_name    = file_name
        self.file_size    = file_size or 0
        self.status_msg   = status_msg
        self.session_data = session_data
        self.msg_id       = msg_id

        self._start_time   = time.time()
        self._last_dl      = 0.0   # last download edit timestamp
        self._last_ul      = 0.0   # last upload edit timestamp
        self._phase        = "download"

    # ── PUBLIC API ────────────────────────────────────────────────────────────

    async def download_cb(self, current: int, total: int):
        """
        Pass to download_media(progress=tracker.download_cb).
        Pyrogram calls this with (bytes_downloaded, total_bytes).
        Only fires an edit if DOWNLOAD_PROGRESS_INTERVAL has elapsed.
        """
        now = time.time()
        if now - self._last_dl < config.DOWNLOAD_PROGRESS_INTERVAL:
            return
        self._last_dl = now
        self._phase   = "download"
        text = self._build_text(current, total)
        asyncio.create_task(self._safe_edit(text))

    async def upload_cb(self, current: int, total: int):
        """
        Pass to send_document/send_video/etc(progress=tracker.upload_cb).
        Pyrogram calls this with (bytes_uploaded, total_bytes).
        Only fires an edit if UPLOAD_PROGRESS_INTERVAL has elapsed.

        NOTE: If upload starts shortly after download, the UPLOAD_PROGRESS_INTERVAL
        timer means the first upload edit will fire 8s after upload BEGINS — not
        immediately. This is intentional: prevents message flicker when a file
        downloads quickly and immediately starts uploading.
        """
        now = time.time()
        if now - self._last_ul < config.UPLOAD_PROGRESS_INTERVAL:
            return
        self._last_ul = now
        self._phase   = "upload"
        text = self._build_text(current, total)
        asyncio.create_task(self._safe_edit(text))

    def reset_for_upload(self):
        """
        Call this after download completes, before starting upload.
        Resets the upload timer so it starts fresh for the upload phase.
        Does NOT immediately edit the message — let the first upload_cb tick do that.
        """
        self._start_time = time.time()
        self._last_ul    = 0.0
        self._phase      = "upload"

    # ── INTERNAL ──────────────────────────────────────────────────────────────

    def _ram_bar(self) -> str:
        ram_used  = self.session_data.get('ram_used',  0)
        ram_total = self.session_data.get('ram_total', 0)
        if not ram_total:
            return ""
        pct      = ram_used / ram_total * 100
        filled   = min(10, int(pct / 10))
        bar      = "█" * filled + "░" * (10 - filled)
        icon     = "✅" if pct < 60 else ("⚠️" if pct < 80 else "🔴")
        used_mb  = ram_used  / (1024 * 1024)
        total_mb = ram_total / (1024 * 1024)
        return f"🧠 `{bar}` `{used_mb:.0f}/{total_mb:.0f}MB` {icon}\n"

    def _build_text(self, current: int, total: int) -> str:
        total   = total or self.file_size or 1
        elapsed = time.time() - self._start_time
        speed   = current / elapsed if elapsed > 0 else 0
        eta     = (total - current) / speed if speed > 0 else 0
        pct     = current * 100 / total
        filled  = math.floor(pct / 10)
        bar     = "█" * filled + "░" * (10 - filled)

        if self._phase == "download":
            icon  = "📥"
            label = "Downloading"
        else:
            icon  = "📤"
            label = "Uploading"

        # Truncate filename to fit neatly
        name = self.file_name or "file"
        name_display = (name[:30] + "…") if len(name) > 30 else name

        msg_line = f"🔢 Msg `#{self.msg_id}`\n" if self.msg_id else ""
        ram_line  = self._ram_bar()

        return (
            f"{icon} **{label}**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{ram_line}"
            f"{msg_line}"
            f"📂 `{name_display}`\n"
            f"`{bar}` **{pct:.1f}%**\n"
            f"⚡ `{human_readable_size(speed)}/s`  ┃  ETA `{time_formatter(eta)}`\n"
            f"💾 `{human_readable_size(current)}` / `{human_readable_size(total)}`"
        )

    async def _safe_edit(self, text: str):
        try:
            await self.status_msg.edit_text(text)
        except Exception:
            pass   # non-fatal — transfer continues regardless
