"""Read byte ranges from Telegram without polling, disk files or public bot tokens."""

import asyncio
from contextlib import contextmanager
import os
import queue
import re
import threading

import requests

from telegram_large_files import CHUNK_SIZE, CLOUD_DOWNLOAD_LIMIT, client_configured
from telegram_transfer_session import connected_client, close_client, TelegramTransferCooldown


@contextmanager
def telegram_range(bot, record, start, end):
    """Yield at most one MiB at a time for an inclusive byte range."""
    if record["size"] <= CLOUD_DOWNLOAD_LIMIT:
        info = bot.get_file(record["file_id"])
        path = info.file_path
        if not path or path.startswith("/") or ".." in path.split("/") or "://" in path:
            raise ValueError("Invalid Telegram file path.")
        # The token is used only by the server, never returned to a viewer.
        with requests.get(
            f"https://api.telegram.org/file/bot{bot.token}/{path}",
            stream=True, timeout=(15, 60), allow_redirects=False,
            headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"},
        ) as response:
            ranged = response.status_code == 206
            if ranged:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match or tuple(map(int, match.groups())) != (start, end, record["size"]):
                    raise ValueError("Invalid Telegram range response.")
            elif response.status_code != 200:
                raise ValueError("Telegram file unavailable.")
            def consume():
                offset = start if ranged else 0
                remaining = end - start + 1
                # Small browser probes must not wait for a full MiB download.
                for part in response.iter_content(64 * 1024):
                    if not part:
                        continue
                    next_offset = offset + len(part)
                    if next_offset > start:
                        data = part[max(0, start - offset):][:remaining]
                        if data:
                            yield data
                            remaining -= len(data)
                    offset = next_offset
                    if not remaining:
                        return
                if remaining:
                    raise ValueError("Incomplete Telegram file.")
            yield consume()
        return

    if not client_configured():
        raise ValueError("Telegram application credentials are required.")
    output = queue.Queue(maxsize=2)
    stopped = threading.Event()
    running = {}

    def put(item):
        while not stopped.is_set():
            try:
                output.put(item, timeout=.25)
                return
            except queue.Full:
                continue

    async def produce():
        client = await connected_client(bot.token, "cinemix-web-stream")
        try:
            file_id = record["file_id"]
            if record.get("source_chat_id") and record.get("source_message_id"):
                # Telegram media references can expire. Refresh from the original
                # admin message when it still exists; never request a user login.
                try:
                    message = await client.get_messages(record["source_chat_id"], record["source_message_id"])
                    media = getattr(message, "video", None) or getattr(message, "document", None)
                    if media:
                        file_id = media.file_id
                except Exception:
                    pass
            first = start // CHUNK_SIZE
            count = end // CHUNK_SIZE - first + 1
            remaining = end - start + 1
            skip = start % CHUNK_SIZE
            async for part in client.stream_media(file_id, offset=first, limit=count):
                if stopped.is_set():
                    return
                if not isinstance(part, bytes) or len(part) > CHUNK_SIZE:
                    raise ValueError("Unexpected Telegram chunk.")
                data = part[skip:][:remaining]
                skip = 0
                if data:
                    await asyncio.to_thread(put, ("data", data))
                    remaining -= len(data)
                if not remaining:
                    return
            if remaining:
                raise ValueError("Incomplete Telegram range.")
        finally:
            await close_client(client)

    async def run():
        running["loop"] = asyncio.get_running_loop()
        running["task"] = asyncio.current_task()
        try:
            await produce()
            await asyncio.to_thread(put, ("end", None))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Third-party exceptions may contain a credential-bearing URL.
            put(("error", exc if isinstance(exc, TelegramTransferCooldown) else None))

    worker = threading.Thread(target=lambda: asyncio.run(run()), daemon=True, name="telegram-web-range")
    worker.start()
    def consume():
        while True:
            try:
                kind, data = output.get(timeout=90)
            except queue.Empty:
                raise ValueError("Telegram range timed out.") from None
            if kind == "end":
                return
            if kind == "error":
                if isinstance(data, TelegramTransferCooldown):
                    raise data
                raise ValueError("Telegram range unavailable.")
            yield data
    try:
        yield consume()
    finally:
        stopped.set()
        loop, task = running.get("loop"), running.get("task")
        if loop and task and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        worker.join(timeout=10)
