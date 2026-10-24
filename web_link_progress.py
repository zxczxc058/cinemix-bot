"""Web-link-only progress edits; Telegram API latency never blocks file reads."""

import logging
import threading
import time

EDIT_INTERVAL = 3
BYTE_INTERVAL = 5 * 1024**2


class WebLinkProgress:
    def __init__(self, bot, uid, message_id, total, formatter):
        self.bot, self.uid, self.message_id = bot, uid, message_id
        self.total, self.formatter = total, formatter
        self.shown_bytes = None
        self.wait_until = 0
        self._sent = 0
        self._closing = self._cancelled = False
        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._run, daemon=True, name="web-link-progress")

    def start(self):
        self._thread.start()

    def update(self, sent):
        with self._condition:
            self._sent = sent
            self._condition.notify()

    def progress_callback(self, current, total):
        """File-reader callback; the editor thread makes the timed Telegram call."""
        if total != self.total or not 0 <= current <= total:
            raise ValueError("Invalid progress byte counts.")
        self.update(current)

    def close(self, *, cancel=False):
        with self._condition:
            self._closing = True
            self._cancelled = cancel
            self._condition.notify()
        # Finish an in-flight edit before a link/error replaces this message.
        if self._thread.ident is not None:
            self._thread.join()

    def _run(self):
        started = last_edit = time.monotonic()
        last_bytes = 0
        while True:
            with self._condition:
                if self._cancelled:
                    return
                now = time.monotonic()
                sent, closing = self._sent, self._closing
                due = closing or now - last_edit >= EDIT_INTERVAL or sent - last_bytes >= BYTE_INTERVAL
                if now < self.wait_until or not due:
                    if closing:
                        return
                    self._condition.wait(timeout=.25)
                    continue
            last_edit, last_bytes = now, sent
            text = self.formatter(sent, self.total)
            if not sent and not closing:
                text += f"\nממתין לנתונים מטלגרם · חלפו {int(now - started)} שניות"
            elif self.shown_bytes == sent and not closing:
                continue
            try:
                self.bot.edit_message_text(text, chat_id=self.uid,
                                           message_id=self.message_id, parse_mode=None)
                self.shown_bytes = sent
                logging.info("Progress updated: %s %s", sent, self.total)
            except Exception as exc:
                description = getattr(exc, "description", "")
                if isinstance(description, str) and "message is not modified" in description.lower():
                    self.shown_bytes = sent
                else:
                    from drive_uploads import telegram_retry_after
                    self.wait_until = time.monotonic() + (telegram_retry_after(exc) or 3)
                    logging.warning("Web progress edit deferred (%s).", type(exc).__name__)
            if closing:
                return
