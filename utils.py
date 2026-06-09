import os
import mimetypes
import re
import unicodedata

# NOTE: No Telethon imports. Pyrogram message attributes are checked directly
# via boolean attribute access (message.poll, message.location, etc.)


def human_readable_size(size):
    """Convert bytes to human readable format."""
    if not size:
        return "0B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f}{unit}"
        size /= 1024.0
    return f"{size:.2f}TB"


def time_formatter(seconds):
    """Convert seconds to formatted time string."""
    if seconds is None or seconds < 0:
        return "..."
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes   = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


# ── UNICODE-AWARE REPLACEMENT ──────────────────────────────────────────────────

def smart_replace(original_text, find_str, replace_str):
    """
    Replace text while handling fancy Unicode fonts (bold, italic, math, etc.).

    Telegram captions often use Unicode math chars like 𝙀𝙭𝙩𝙧𝙖𝙘𝙩𝙚𝙙 𝐊𝐔𝐍𝐀𝐋.
    Normal str.replace() fails because code points differ from ASCII.
    NFKC normalization maps each fancy Unicode letter to its ASCII equivalent.
    """
    if not find_str or original_text is None:
        return original_text or ""

    # Fast path: exact match
    if find_str in original_text:
        return original_text.replace(find_str, replace_str)

    norm_original = unicodedata.normalize('NFKC', original_text)
    norm_find     = unicodedata.normalize('NFKC', find_str)

    if norm_find not in norm_original:
        return original_text

    norm_to_orig_start = []
    norm_to_orig_end   = []

    for orig_i, orig_char in enumerate(original_text):
        norm_chars = unicodedata.normalize('NFKC', orig_char)
        for _ in norm_chars:
            norm_to_orig_start.append(orig_i)
            norm_to_orig_end.append(orig_i + 1)

    norm_to_orig_start.append(len(original_text))
    norm_to_orig_end.append(len(original_text))

    result        = []
    norm_i        = 0
    prev_orig_end = 0
    find_len      = len(norm_find)
    norm_len      = len(norm_original)

    while norm_i <= norm_len - find_len:
        if norm_original[norm_i:norm_i + find_len] == norm_find:
            orig_start = norm_to_orig_start[norm_i]
            orig_end   = norm_to_orig_end[norm_i + find_len - 1]
            result.append(original_text[prev_orig_end:orig_start])
            result.append(replace_str)
            prev_orig_end = orig_end
            norm_i       += find_len
        else:
            norm_i += 1

    result.append(original_text[prev_orig_end:])
    return ''.join(result)


# ── LINK PARSING ──────────────────────────────────────────────────────────────

def extract_link_info(link):
    """
    Extract (source_identifier, message_id, topic_id) from a Telegram link.
    topic_id is None for non-topic messages.

    Supported formats:
      Private regular:  t.me/c/CHATID/MSGID         → (-100CHATID, MSGID, None)
      Private topic:    t.me/c/CHATID/TOPICID/MSGID  → (-100CHATID, MSGID, TOPICID)
      Public regular:   t.me/USERNAME/MSGID           → ('username', MSGID, None)
      Public topic:     t.me/USERNAME/TOPICID/MSGID   → ('username', MSGID, TOPICID)
    """
    if not link:
        return None, None, None

    link = link.strip()

    # Private forum topic: t.me/c/CHATID/TOPICID/MSGID
    private_topic = re.search(r't\.me/c/(\d+)/(\d+)/(\d+)', link)
    if private_topic:
        topic_id = int(private_topic.group(2))
        msg_id   = int(private_topic.group(3))
        return int(f"-100{private_topic.group(1)}"), msg_id, topic_id

    # Private regular: t.me/c/CHATID/MSGID
    private_regular = re.search(r't\.me/c/(\d+)/(\d+)', link)
    if private_regular:
        return int(f"-100{private_regular.group(1)}"), int(private_regular.group(2)), None

    # Public forum topic: t.me/USERNAME/TOPICID/MSGID
    public_topic = re.search(r't\.me/([a-zA-Z0-9_]+)/(\d+)/(\d+)', link)
    if public_topic:
        username = public_topic.group(1)
        if username.lower() != 'c':
            return username, int(public_topic.group(3)), int(public_topic.group(2))

    # Public regular: t.me/USERNAME/MSGID
    public_regular = re.search(r't\.me/([a-zA-Z0-9_]+)/(\d+)', link)
    if public_regular:
        username = public_regular.group(1)
        if username.lower() != 'c':
            return username, int(public_regular.group(2)), None

    return None, None, None


# ── PYROGRAM MEDIA HELPERS ────────────────────────────────────────────────────

def is_special_media(message) -> bool:
    """
    Return True if the message contains media that cannot be downloaded as a file.

    Pyrogram does not have Telethon's typed MediaWebPage etc. We check
    presence of high-level attributes directly on the message object.
    """
    if not message.media:
        return False
    return bool(
        message.poll     or
        message.venue    or
        message.location or
        message.contact  or
        message.dice     or
        message.game     or
        message.invoice
    )


def get_media_file_size(message) -> int:
    """Return file size in bytes for any downloadable Pyrogram media type."""
    for attr in ('document', 'video', 'audio', 'voice', 'video_note', 'animation', 'sticker'):
        obj = getattr(message, attr, None)
        if obj:
            return getattr(obj, 'file_size', 0) or 0
    # Photo: Pyrogram gives list of PhotoSize — pick largest
    if message.photo:
        return getattr(message.photo, 'file_size', 0) or 0
    return 0


def get_target_info(message):
    """
    Smart format detection for Pyrogram messages.
    Returns (filename, mime_type, is_video_mode).

    Pyrogram message structure (relevant attributes):
      message.photo      → Photo  (inline image — NOT sent as document)
      message.document   → Document (generic file)
      message.video      → Video
      message.audio      → Audio
      message.voice      → Voice
      message.video_note → VideoNote (round video)
      message.animation  → Animation (GIF)
      message.sticker    → Sticker
    """
    if is_special_media(message):
        return None, None, False

    # ── Photo (inline, not sent as document) ──────────────────────────────────
    if message.photo and not message.document:
        return f"Image_{message.id}.jpg", "image/jpeg", False

    # ── Sticker ───────────────────────────────────────────────────────────────
    if message.sticker:
        s    = message.sticker
        mime = getattr(s, 'mime_type', '') or 'image/webp'
        ext  = '.webp' if 'webp' in mime else ('.tgs' if 'tgs' in mime else '.webm')
        return f"Sticker_{message.id}{ext}", mime, False

    # ── Video Note (round video) ───────────────────────────────────────────────
    if message.video_note:
        return f"VideoNote_{message.id}.mp4", "video/mp4", True

    # ── Animation (GIF) ───────────────────────────────────────────────────────
    if message.animation:
        a    = message.animation
        mime = getattr(a, 'mime_type', '') or 'video/mp4'
        return f"Animation_{message.id}.mp4", mime, False

    # ── Voice ─────────────────────────────────────────────────────────────────
    if message.voice:
        return f"Voice_{message.id}.ogg", "audio/ogg", False

    # ── Determine the primary media object ────────────────────────────────────
    media_obj = message.video or message.audio or message.document
    if not media_obj:
        return None, None, False

    mime          = getattr(media_obj, 'mime_type', '') or ''
    original_name = getattr(media_obj, 'file_name', '') or f"File_{message.id}"
    base_name     = os.path.splitext(original_name)[0]

    # ── Video ──────────────────────────────────────────────────────────────────
    if message.video or 'video' in mime or original_name.lower().endswith(
        ('.mkv', '.avi', '.webm', '.mov', '.flv', '.wmv', '.m4v', '.3gp', '.ts')
    ):
        return base_name + ".mp4", "video/mp4", True

    # ── Audio ──────────────────────────────────────────────────────────────────
    if message.audio or 'audio' in mime:
        ext = os.path.splitext(original_name)[1] or '.mp3'
        return base_name + ext, mime or 'audio/mpeg', False

    # ── Image document ─────────────────────────────────────────────────────────
    if 'image' in mime:
        ext = os.path.splitext(original_name)[1] or '.jpg'
        return base_name + ext, mime, False

    # ── PDF ───────────────────────────────────────────────────────────────────
    if 'pdf' in mime or original_name.lower().endswith('.pdf'):
        return base_name + ".pdf", "application/pdf", False

    # ── Fallback: keep original name and mime ─────────────────────────────────
    return original_name, mime or "application/octet-stream", False


# ── MANIPULATION HELPERS ──────────────────────────────────────────────────────

def apply_filename_manipulations(filename, settings):
    """Apply find/replace rules on a filename."""
    if not settings:
        return filename

    if 'find_name' in settings and 'replace_name' in settings:
        filename = smart_replace(filename, settings['find_name'], settings['replace_name'])

    for find_str, replace_str in settings.get('fname_rules', []):
        if find_str:
            filename = smart_replace(filename, find_str, replace_str)

    return filename


def apply_caption_manipulations(message, settings):
    """Apply caption find/replace/remove/append rules, preserving HTML entities/links."""
    text_obj = message.text or message.caption

    # Try to get HTML formatted text to preserve embedded links
    if text_obj:
        if hasattr(text_obj, "html"):
            original_caption = text_obj.html
        else:
            original_caption = str(text_obj)
    else:
        original_caption = ""

    if not settings:
        return original_caption

    caption = original_caption

    if 'find_cap' in settings and 'replace_cap' in settings:
        caption = smart_replace(caption, settings['find_cap'], settings['replace_cap'])

    for find_str, replace_str in settings.get('cap_rules', []):
        if find_str:
            caption = smart_replace(caption, find_str, replace_str)

    if settings.get('extra_cap'):
        caption = f"{caption}\n\n{settings['extra_cap']}" if caption else settings['extra_cap']

    return caption


def sanitize_filename(filename):
    """Remove characters that are invalid in filenames."""
    for char in '<>:"/\\|?*':
        filename = filename.replace(char, '_')
    return filename
