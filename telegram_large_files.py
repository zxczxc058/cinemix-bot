"""Download-only MTProto adapter. Never registers handlers or changes Bot API polling."""

import asyncio
from contextlib import contextmanager
from telegram_transfer_session import connected_client, close_client, TelegramTransferCooldown
import os
import queue
import threading
from drive_upload_errors import DriveUploadError

MAX_FILE_SIZE = 4 * 1024**3
CLOUD_DOWNLOAD_LIMIT = 20 * 1024**2
# Google resumable uploads require 256KiB-aligned chunks. Larger bounded
# chunks reduce HTTP round trips substantially for multi-gigabyte transfers.
CHUNK_SIZE = 16 * 1024**2


def client_configured():
    return bool(os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH"))


@contextmanager
def client_chunks(bot_token, file_id):
    """Backpressure bounds the download queue to two transfer-sized chunks."""
    if not client_configured():
        raise ValueError("להעלאה גדולה יש להגדיר TELEGRAM_API_ID ו-TELEGRAM_API_HASH ב-Secrets של הפרויקט, לא בצ'אט.")
    chunks = queue.Queue(maxsize=2)
    closed = threading.Event()
    running = {}

    def push(value):
        while not closed.is_set():
            try:
                chunks.put(value, timeout=.25)
                return
            except queue.Full:
                pass

    async def produce():
        client = await connected_client(bot_token, "cinemix-drive-download")
        try:
            async for part in client.stream_media(file_id):
                if closed.is_set():
                    return
                if not isinstance(part, bytes) or len(part) > CHUNK_SIZE:
                    raise ValueError("Unexpected download chunk.")
                await asyncio.to_thread(push, ("data", part))
        finally:
            await close_client(client)

    async def run():
        running["loop"] = asyncio.get_running_loop()
        running["task"] = asyncio.current_task()
        try:
            await produce()
            await asyncio.to_thread(push, ("end", None))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Never pass credential-bearing exceptions across the worker boundary.
            push(("error", exc if isinstance(exc, TelegramTransferCooldown) else None))

    worker = threading.Thread(target=lambda: asyncio.run(run()), daemon=True, name="telegram-file-client")
    def consume():
        # Authorize/download only after Drive has accepted the upload session.
        # A Google login failure must not trigger unnecessary Telegram logins.
        worker.start()
        while True:
            try:
                kind, value = chunks.get(timeout=180)
            except queue.Empty:
                raise DriveUploadError("telegram_media") from None
            if kind == "end":
                return
            if kind == "error":
                if isinstance(value, TelegramTransferCooldown):
                    raise value
                raise DriveUploadError("telegram_media")
            yield value
    try:
        yield consume()
    finally:
        closed.set()
        loop, task = running.get("loop"), running.get("task")
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        if worker.ident is not None:
            worker.join(timeout=10)
