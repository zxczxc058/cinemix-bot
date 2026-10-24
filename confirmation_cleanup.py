"""Run short-lived confirmation cleanup without blocking synchronous TeleBot."""

import asyncio
import logging
import threading

_loop = None
_lock = threading.Lock()


async def delete_after(delete, delay=5):
    await asyncio.sleep(delay)
    try:
        # TeleBot is synchronous: never block the asyncio loop on its HTTP call.
        await asyncio.to_thread(delete)
    except Exception as exc:
        logging.warning("Enforcement confirmation cleanup failed (%s).", type(exc).__name__)


def schedule_confirmation_cleanup(delete, delay=5):
    global _loop
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever,
                name="confirmation-cleanup",
                daemon=True,
            ).start()
        loop = _loop

    def start():
        asyncio.create_task(delete_after(delete, delay))
    loop.call_soon_threadsafe(start)
