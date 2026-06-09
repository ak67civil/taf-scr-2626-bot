from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_settings_keyboard(
    session_id,
    dest_topic_id=None,
    thumbnail_set: bool = False,
) -> InlineKeyboardMarkup:
    """Main settings keyboard for file manipulation.
    
    thumbnail_set: pass True if user has already set a custom transfer thumbnail.
    """
    topic_label = (
        f"🧵 Dest Topic: {dest_topic_id} ✅"
        if dest_topic_id
        else "🧵 Set Destination Topic"
    )
    thumb_label = (
        "🖼️ Transfer Thumbnail: ✅ (tap to change)"
        if thumbnail_set
        else "🖼️ Set Transfer Thumbnail"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Filename: Find & Replace",  callback_data=f"set_fname_{session_id}")],
        [InlineKeyboardButton("💬 Caption: Find & Replace",   callback_data=f"set_fcap_{session_id}")],
        [InlineKeyboardButton("✂️ Caption: Remove Text",      callback_data=f"set_cap_remove_{session_id}")],
        [InlineKeyboardButton("➕ Add Extra Caption",          callback_data=f"set_xcap_{session_id}")],
        [InlineKeyboardButton(topic_label,                    callback_data=f"set_dest_topic_{session_id}")],
        [InlineKeyboardButton(thumb_label,                    callback_data=f"set_thumbnail_{session_id}")],
        [
            InlineKeyboardButton("✅ Done - Start Transfer",  callback_data=f"confirm_{session_id}"),
            InlineKeyboardButton("❌ Cancel",                 callback_data=f"cancel_{session_id}"),
        ],
    ])


def get_confirm_keyboard(session_id, settings, dest_topic_id=None):
    """
    Build a settings summary text + confirm keyboard.
    Returns (settings_text: str, InlineKeyboardMarkup).
    """
    settings_text = "**Current Settings:**\n\n"

    if settings.get('find_name'):
        settings_text += (
            f"📝 Filename:\n`{settings['find_name']}` → "
            f"`{settings.get('replace_name', '')}`\n\n"
        )

    if settings.get('find_cap'):
        settings_text += (
            f"💬 Caption:\n`{settings['find_cap']}` → "
            f"`{settings.get('replace_cap', '')}`\n\n"
        )

    if settings.get('extra_cap'):
        settings_text += f"➕ Extra Caption:\n`{settings['extra_cap'][:50]}...`\n\n"

    cap_rules = settings.get('cap_rules', [])
    if cap_rules:
        settings_text += f"📋 Caption Rules ({len(cap_rules)}):\n"
        for i, (find_str, replace_str) in enumerate(cap_rules[:5], 1):
            action = "REMOVE" if replace_str == "" else f"→ `{replace_str[:20]}`"
            settings_text += f"  {i}. `{find_str[:20]}` {action}\n"
        if len(cap_rules) > 5:
            settings_text += f"  …and {len(cap_rules) - 5} more\n"
        settings_text += "\n"

    fname_rules = settings.get('fname_rules', [])
    if fname_rules:
        settings_text += f"📋 Filename Rules ({len(fname_rules)}):\n"
        for i, (find_str, replace_str) in enumerate(fname_rules[:3], 1):
            settings_text += f"  {i}. `{find_str[:20]}` → `{replace_str[:20]}`\n"
        settings_text += "\n"

    if dest_topic_id:
        settings_text += f"🧵 Destination Topic ID: `{dest_topic_id}`\n\n"

    if settings.get('thumbnail_set'):
        settings_text += "🖼️ Transfer Thumbnail: **Set ✅** (applied to all videos)\n\n"

    if not any([
        settings.get('find_name'), settings.get('find_cap'),
        settings.get('extra_cap'), cap_rules, fname_rules,
        dest_topic_id, settings.get('thumbnail_set'),
    ]):
        settings_text += "⚠️ No modifications set\n\n"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔙 Back to Settings", callback_data=f"back_{session_id}"),
            InlineKeyboardButton("✅ Confirm & Start",  callback_data=f"start_{session_id}"),
        ],
        [
            InlineKeyboardButton("🗑️ Clear All Settings", callback_data=f"clear_{session_id}"),
            InlineKeyboardButton("❌ Cancel",              callback_data=f"cancel_{session_id}"),
        ],
    ])
    return settings_text, keyboard


def get_skip_keyboard(session_id) -> InlineKeyboardMarkup:
    """Skip option keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Skip",    callback_data=f"skip_{session_id}")],
        [InlineKeyboardButton("❌ Cancel",  callback_data=f"cancel_{session_id}")],
    ])


def get_progress_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown during active transfer."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop Transfer", callback_data="stop_transfer")],
    ])


def get_clone_info_keyboard() -> InlineKeyboardMarkup:
    """Info keyboard shown with /clone command."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("ℹ️ How to use?", callback_data="clone_help")],
        [InlineKeyboardButton("📊 Bot Stats",   callback_data="bot_stats")],
    ])
