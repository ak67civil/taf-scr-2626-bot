"""
database.py  –  MongoDB helpers
Includes original user/config/checkpoint functions PLUS
new dyno-tracking, task-queue, and subscription plan functions.
"""

import motor.motor_asyncio
import time
import os
import logging
import config

logger = logging.getLogger(__name__)

mongo_client = None
db = None


# ── INIT ─────────────────────────────────────────────────────────────────────

async def init_db():
    global mongo_client, db
    uri = config.MONGO_URI
    if not uri:
        logger.error("MONGO_URI not found! Database will not work properly.")
        return

    try:
        mongo_client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        db = mongo_client.get_default_database('telegram_bot_db')
        logger.info(f"💾 Connected to MongoDB: {db.name}")

        # Core indexes
        await db.users.create_index("user_id", unique=True)
        await db.config.create_index("key", unique=True)
        await db.checkpoints.create_index("user_id", unique=True)

        # Dyno + Task indexes
        await db.dynos.create_index("user_id", unique=True)
        await db.tasks.create_index("task_id", unique=True)
        await db.tasks.create_index("user_id")

        # Plan indexes
        await db.plans.create_index("id", unique=True)

        # Local payment claims index (backup for single-bot mode)
        await db.payment_claims.create_index("utr", unique=True)
        await db.payment_claims.create_index("msg_id", unique=True)

        logger.info("✅ MongoDB indexes created")
    except Exception as e:
        logger.error(f"MongoDB Connection Failed: {e}", exc_info=True)
        db = None

    # Also init shared payments DB
    try:
        from payments_db import init_shared_payments_db
        await init_shared_payments_db()
    except Exception as e:
        logger.warning(f"Shared payments DB init skipped: {e}")


# ── USERS ─────────────────────────────────────────────────────────────────────

async def add_user(user_id, phone=None, session_string=None, validity_duration=0):
    if db is None:
        logger.error("DB not initialized in add_user"); return
    expiry = time.time() + validity_duration if validity_duration else 0
    now = time.time()
    await db.users.update_one(
        {'user_id': user_id},
        {'$set': {
            'phone': phone,
            'session_string': session_string,
            'validity_expiry': expiry,
            'joined_date': now,
            'is_admin': 0
        }},
        upsert=True
    )


async def update_user_name(user_id, first_name):
    if db is None: return
    await db.users.update_one(
        {'user_id': user_id},
        {'$set': {'first_name': first_name},
         '$setOnInsert': {'joined_date': time.time(), 'validity_expiry': 0}},
        upsert=True
    )


async def update_user_session(user_id, session_string, phone):
    if db is None:
        logger.error("DB not initialized in update_user_session"); return
    user = await db.users.find_one({'user_id': user_id})
    if user:
        await db.users.update_one(
            {'user_id': user_id},
            {'$set': {'session_string': session_string, 'phone': phone}}
        )
    else:
        await db.users.insert_one({
            'user_id': user_id,
            'phone': phone,
            'session_string': session_string,
            'validity_expiry': 0,
            'joined_date': time.time(),
            'is_admin': 0,
            'first_name': 'User'
        })


async def update_validity(user_id, duration):
    """Extend/set validity. duration in seconds."""
    if db is None:
        logger.error("DB not initialized in update_validity"); return 0
    user = await db.users.find_one({'user_id': user_id})
    now = time.time()
    current_expiry = user.get('validity_expiry', 0) if user else 0
    new_expiry = (current_expiry if current_expiry > now else now) + duration
    await db.users.update_one(
        {'user_id': user_id},
        {'$set': {'validity_expiry': new_expiry, 'is_admin': 0},
         '$setOnInsert': {'joined_date': now, 'first_name': 'User'}},
        upsert=True
    )
    return new_expiry


async def check_user(user_id):
    """Returns (is_valid, session_string, phone)."""
    if db is None:
        logger.warning("⚠️ Database not initialized when checking user!")
        return False, None, None
    try:
        user = await db.users.find_one({'user_id': user_id})
    except Exception as e:
        logger.error(f"Error checking user {user_id}: {e}")
        return False, None, None

    if not user:
        return False, None, None

    expiry  = user.get('validity_expiry', 0)
    session = user.get('session_string')
    phone   = user.get('phone')
    return expiry > time.time(), session, phone


async def revoke_user(user_id):
    if db is None: return
    await db.users.update_one(
        {'user_id': user_id},
        {'$set': {'validity_expiry': 0, 'session_string': None}}
    )


async def get_all_users():
    """Return list of (user_id, expiry, phone, first_name)."""
    if db is None: return []
    try:
        cursor = db.users.find({})
        users  = []
        async for doc in cursor:
            users.append((
                doc.get('user_id'),
                doc.get('validity_expiry', 0),
                doc.get('phone'),
                doc.get('first_name', 'User')
            ))
        return users
    except Exception as e:
        logger.error(f"Error getting all users: {e}")
        return []


async def get_all_session_strings():
    """
    Return list of dicts with user info + session_string for all users
    who have a non-null session_string saved in DB.
    """
    if db is None:
        logger.error("DB not initialized in get_all_session_strings")
        return []
    try:
        cursor = db.users.find({'session_string': {'$ne': None, '$exists': True}})
        result = []
        async for doc in cursor:
            session = doc.get('session_string')
            if session:
                result.append({
                    'user_id':         doc.get('user_id'),
                    'first_name':      doc.get('first_name', 'User'),
                    'phone':           doc.get('phone', 'N/A'),
                    'validity_expiry': doc.get('validity_expiry', 0),
                    'session_string':  session,
                })
        return result
    except Exception as e:
        logger.error(f"Error in get_all_session_strings: {e}")
        return []


# ── CONFIG ────────────────────────────────────────────────────────────────────

async def set_config(key, value):
    if db is None: return
    await db.config.update_one(
        {'key': key},
        {'$set': {'value': str(value)}},
        upsert=True
    )


async def get_config(key):
    if db is None: return None
    try:
        doc = await db.config.find_one({'key': key})
        return doc.get('value') if doc else None
    except Exception as e:
        logger.error(f"Error getting config {key}: {e}")
        return None


# ── CHECKPOINTS ───────────────────────────────────────────────────────────────

async def save_transfer_checkpoint(user_id, checkpoint_data):
    if db is None:
        logger.warning("DB not initialized — checkpoint not saved"); return
    try:
        await db.checkpoints.update_one(
            {'user_id': user_id},
            {'$set': {
                'user_id':    user_id,
                'data':       checkpoint_data,
                'updated_at': time.time(),
            }},
            upsert=True
        )
        logger.info(f"✅ Checkpoint saved for user {user_id} at msg {checkpoint_data.get('current_msg')}")
    except Exception as e:
        logger.error(f"Checkpoint save error: {e}")


async def get_transfer_checkpoint(user_id):
    if db is None: return None
    try:
        doc = await db.checkpoints.find_one({'user_id': user_id})
        if not doc:
            return None
        if time.time() - doc.get('updated_at', 0) > 86400:
            await db.checkpoints.delete_one({'user_id': user_id})
            return None
        return doc.get('data')
    except Exception as e:
        logger.error(f"Checkpoint fetch error: {e}")
        return None


async def clear_transfer_checkpoint(user_id):
    if db is None: return
    try:
        await db.checkpoints.delete_one({'user_id': user_id})
        logger.info(f"🗑️ Checkpoint cleared for user {user_id}")
    except Exception as e:
        logger.error(f"Checkpoint clear error: {e}")


# ── DYNO TRACKING ─────────────────────────────────────────────────────────────

async def register_user_dyno(user_id: int, dyno_name: str, task_id: str,
                               first_name: str = "User") -> None:
    if db is None: return
    await db.dynos.update_one(
        {'user_id': user_id},
        {'$set': {
            'user_id':    user_id,
            'first_name': first_name,
            'dyno_name':  dyno_name,
            'task_id':    task_id,
            'started_at': time.time(),
            'ram_used':   0,
            'ram_total':  512 * 1024 * 1024,
            'status':     'running',
            'label':      '',
        }},
        upsert=True
    )


async def update_dyno_ram(user_id: int, ram_used: int, ram_total: int,
                           label: str = "") -> None:
    if db is None: return
    try:
        await db.dynos.update_one(
            {'user_id': user_id},
            {'$set': {
                'ram_used':   ram_used,
                'ram_total':  ram_total,
                'last_ping':  time.time(),
                'label':      label,
            }}
        )
    except Exception as e:
        logger.error(f"update_dyno_ram error: {e}")


async def get_user_dyno(user_id: int) -> dict | None:
    if db is None: return None
    try:
        return await db.dynos.find_one({'user_id': user_id})
    except Exception as e:
        logger.error(f"get_user_dyno error: {e}")
        return None


async def get_all_dynos() -> list:
    if db is None: return []
    try:
        cursor = db.dynos.find({})
        return [doc async for doc in cursor]
    except Exception as e:
        logger.error(f"get_all_dynos error: {e}")
        return []


async def clear_user_dyno(user_id: int) -> None:
    if db is None: return
    try:
        await db.dynos.update_one(
            {'user_id': user_id},
            {'$set': {
                'status':    'stopped',
                'dyno_name': None,
                'task_id':   None,
            }}
        )
    except Exception as e:
        logger.error(f"clear_user_dyno error: {e}")


# ── TASK QUEUE ────────────────────────────────────────────────────────────────

async def create_transfer_task(task_id: str, user_id: int, task_data: dict) -> None:
    if db is None: return
    await db.tasks.insert_one({
        'task_id':    task_id,
        'user_id':    user_id,
        'data':       task_data,
        'status':     'pending',
        'created_at': time.time(),
    })


async def get_transfer_task(task_id: str) -> dict | None:
    if db is None: return None
    return await db.tasks.find_one({'task_id': task_id})


async def update_task_status(task_id: str, status: str) -> None:
    if db is None: return
    await db.tasks.update_one(
        {'task_id': task_id},
        {'$set': {'status': status, 'updated_at': time.time()}}
    )


async def check_stop_signal(task_id: str) -> bool:
    if db is None: return False
    try:
        doc = await db.tasks.find_one({'task_id': task_id})
        return doc is not None and doc.get('status') == 'stop_requested'
    except Exception:
        return False


async def request_task_stop(task_id: str) -> None:
    await update_task_status(task_id, 'stop_requested')


async def request_ram_cleanup(user_id: int) -> None:
    if db is None: return
    await db.dynos.update_one(
        {'user_id': user_id},
        {'$set': {'cleanup_requested': True}}
    )


async def clear_cleanup_flag(user_id: int) -> None:
    if db is None: return
    await db.dynos.update_one(
        {'user_id': user_id},
        {'$set': {'cleanup_requested': False}}
    )


async def is_cleanup_requested(user_id: int) -> bool:
    if db is None: return False
    doc = await db.dynos.find_one({'user_id': user_id})
    return bool(doc and doc.get('cleanup_requested'))
