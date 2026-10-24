"""Reuse one in-memory MTProto authorization for Drive and web transfers.

No polling, disk session files, environment changes or database credentials.
Each transfer closes its own connection; the reusable authorization lives only
for this process. Serialize initial authorization to avoid Telegram login floods.
"""

import asyncio
import hashlib
import math
import os
import threading
import time

_LOCK = threading.Lock()
_SESSIONS = {}
_COOLDOWNS = {}


class TelegramTransferCooldown(ValueError):
    def __init__(self, seconds):
        self.seconds = max(1, math.ceil(seconds))
        super().__init__("Telegram file-transfer authorization is temporarily rate limited.")

    def user_message(self):
        minutes = math.ceil(self.seconds / 60)
        return f"טלגרם הגבילה זמנית חיבורים להעברת קבצים. נסה שוב בעוד כ־{minutes} דקות."


async def close_client(client):
    if getattr(client, "is_connected", False):
        if getattr(client, "is_initialized", True):
            await client.stop()
        else:
            await client.disconnect()


async def connected_client(bot_token, label):
    from pyrogram import Client
    key = hashlib.sha256(bot_token.encode()).hexdigest()
    # Nonblocking acquisition is cancellable; no executor thread can acquire
    # an abandoned lock after a browser disconnects.
    while not _LOCK.acquire(blocking=False):
        await asyncio.sleep(.05)
    client = None
    try:
        remaining = _COOLDOWNS.get(key, 0) - time.monotonic()
        if remaining > 0:
            raise TelegramTransferCooldown(remaining)
        for attempt in range(2):
            cached = _SESSIONS.get(key)
            client = Client(
                label, api_id=int(os.environ["TELEGRAM_API_ID"]),
                api_hash=os.environ["TELEGRAM_API_HASH"], bot_token=bot_token,
                session_string=cached, in_memory=True,
                no_updates=True, workers=1, max_concurrent_transmissions=1,
            )
            try:
                await client.start()
                if not client.me.is_bot or client.me.id != int(bot_token.split(":", 1)[0]):
                    raise ValueError("Unexpected Telegram file-transfer identity.")
                _SESSIONS[key] = await client.export_session_string()
                return client
            except Exception as exc:
                if attempt or not cached or type(exc).__name__ not in {
                    "AuthKeyUnregistered", "AuthKeyDuplicated", "SessionRevoked", "SessionExpired",
                }:
                    raise
                # Refresh only this bot's invalid in-memory transfer session.
                _SESSIONS.pop(key, None)
                try:
                    await close_client(client)
                except Exception:
                    pass
                client = None
    except BaseException as exc:
        cooldown = None
        if type(exc).__name__ in {"FloodWait", "FloodWaitError"}:
            cooldown = TelegramTransferCooldown(getattr(exc, "value", getattr(exc, "seconds", 3)))
            _COOLDOWNS[key] = time.monotonic() + cooldown.seconds
        if client is not None:
            try:
                await close_client(client)
            except Exception:
                pass
        if cooldown:
            raise cooldown from None
        raise
    finally:
        _LOCK.release()
