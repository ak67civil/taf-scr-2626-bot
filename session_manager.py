"""
session_manager.py  –  Pyrogram client lifecycle for login and transfer sessions.

KEY DIFFERENCES from Telethon version:
  1. phone_code_hash — Telethon cached this internally. Pyrogram does NOT.
     send_code() returns a SentCode object; its .phone_code_hash MUST be stored
     by the caller (handlers.py LOGIN_STATES) and passed back to sign_in().

  2. OTP format — users may send "1-2-3-4-5" (with dashes, Telegram style).
     We strip dashes inside sign_in() before passing to Pyrogram.

  3. Session string format — Pyrogram's export_session_string() produces a
     different format from Telethon StringSession. Old sessions from MongoDB
     will be rejected; users must /login again after migration.

  4. in_memory=True — temp clients (login flow) never touch disk.
     Transfer clients use session_string= so they also live in memory.

  5. no_updates=True — user clients used for transfer do not need updates.
     This prevents the "competing update stream" bug.
"""

import asyncio
import config
from pyrogram import Client
from pyrogram.errors import (
    SessionExpired, AuthKeyUnregistered, AuthKeyInvalid,
    UserDeactivated, UserDeactivatedBan,
)


class SessionManager:
    def __init__(self):
        # user_id → temp Pyrogram Client (active during login flow only)
        self.temp_clients: dict[int, Client] = {}
        self._semaphore = None

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(999_999)
        return self._semaphore

    # ── LOGIN FLOW ────────────────────────────────────────────────────────────

    async def create_temp_client(self, user_id: int) -> Client:
        """
        Create an in-memory Pyrogram client for the login flow.
        Does NOT start() the client — only connects at MTProto level.
        """
        # If a stale temp client exists, clean it up first
        if user_id in self.temp_clients:
            try:
                await self.temp_clients[user_id].disconnect()
            except Exception:
                pass
            del self.temp_clients[user_id]

        client = Client(
            name=f"login_{user_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True,
        )
        await client.connect()
        self.temp_clients[user_id] = client
        return client

    async def send_code(self, user_id: int, phone: str) -> str:
        """
        Send OTP to the given phone number.
        Returns phone_code_hash — the caller MUST store this in LOGIN_STATES.

        NOTE: This is the critical difference from Telethon. Pyrogram's sign_in()
        requires phone_code_hash explicitly. Telethon cached it internally.
        """
        client    = self.temp_clients[user_id]
        sent_code = await client.send_code(phone)
        return sent_code.phone_code_hash

    async def resend_code(self, user_id: int, phone: str, phone_code_hash: str) -> str:
        """
        Resend OTP (user requested new code).
        Returns new phone_code_hash — replace the stored one.
        """
        client    = self.temp_clients[user_id]
        sent_code = await client.resend_code(phone, phone_code_hash)
        return sent_code.phone_code_hash

    async def sign_in(
        self,
        user_id:         int,
        phone:           str,
        phone_code_hash: str,
        code:            str,
    ) -> str:
        """
        Sign in with OTP. Returns Pyrogram session string on success.
        Raises SessionPasswordNeeded if 2FA is enabled.
        Raises PhoneCodeInvalid / PhoneCodeExpired on bad/expired OTP.

        Strips dashes from code so users can send both "12345" and "1-2-3-4-5".
        """
        client     = self.temp_clients[user_id]
        clean_code = code.replace('-', '').replace(' ', '').strip()
        await client.sign_in(
            phone_number=phone,
            phone_code_hash=phone_code_hash,
            phone_code=clean_code,
        )
        return await client.export_session_string()

    async def check_password(self, user_id: int, password: str) -> str:
        """
        Complete 2FA login. Returns Pyrogram session string on success.
        Raises PasswordHashInvalid on wrong password.
        """
        client = self.temp_clients[user_id]
        await client.check_password(password)
        return await client.export_session_string()

    async def get_temp_client(self, user_id: int):
        return self.temp_clients.get(user_id)

    async def remove_temp_client(self, user_id: int):
        """Disconnect and remove a temporary login client."""
        client = self.temp_clients.pop(user_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ── TRANSFER SESSION ──────────────────────────────────────────────────────

    async def start_user_session(self, session_string: str, user_id: int) -> Client:
        """
        Start a Pyrogram user client for file transfer (semaphore-controlled).

        Validates the session immediately with get_me().
        If session is expired or invalid, raises with a clear message so
        handlers.py can ask the user to /login again.
        """
        await self.semaphore.acquire()
        try:
            client = Client(
                name=f"transfer_{user_id}",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session_string,
                no_updates=True,        # user client never needs updates
                sleep_threshold=0,      # we handle FloodWait ourselves
                in_memory=True,
            )
            await client.start()

            # Validate session is actually working
            me = await client.get_me()
            if not me:
                await client.stop()
                self.semaphore.release()
                raise Exception("Session check returned no user — session invalid.")

            config.logger.info(f"✅ User session started: {me.first_name} (id={me.id})")
            return client

        except (SessionExpired, AuthKeyUnregistered, AuthKeyInvalid):
            self.semaphore.release()
            raise Exception(
                "⚠️ Session expired or invalid.\n"
                "Please use /login to reconnect your account."
            )
        except (UserDeactivated, UserDeactivatedBan):
            self.semaphore.release()
            raise Exception(
                "❌ Your Telegram account has been deactivated or banned."
            )
        except Exception:
            self.semaphore.release()
            raise

    async def stop_user_session(self, client):
        """Stop a transfer user client and release the semaphore slot."""
        if client:
            try:
                await client.stop()
            except Exception:
                pass
        try:
            self.semaphore.release()
        except ValueError:
            pass


# ── Global singleton ──────────────────────────────────────────────────────────
session_manager = SessionManager()
