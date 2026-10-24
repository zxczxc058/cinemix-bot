"""Short-window group flood protection for approved groups."""

import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from telebot import types
from telebot.handler_backends import BaseMiddleware, CancelUpdate

from allowed_chats import chat_is_active
from bot_storage import is_admin
from group_management import GroupManagementMiddleware, delete_later
from restriction_engine import PERMISSIONS


class GroupAntiSpamMiddleware(BaseMiddleware):
    LIMIT = 3
    WINDOW_SECONDS = 5.0
    MUTE_SECONDS = 15 * 60
    WARNING_SECONDS = 10 * 60

    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.update_sensitive = True
        self.update_types = ["message"]
        self._lock = threading.RLock()
        self._messages = defaultdict(deque)
        self._muted_until = {}
        self._warned_until = {}

    def _is_group_admin(self, chat_id, user_id, now):
        try:
            member = self.bot.get_chat_member(chat_id, user_id)
            result = getattr(member, "status", None) in {"administrator", "creator"}
        except Exception as exc:
            # Fail open: do not punish someone if Telegram cannot confirm their role.
            logging.warning("Could not check flood-sender group role (%s).", type(exc).__name__)
            return True
        # Role changes take effect immediately, including promotion and demotion.
        return result

    def _delete(self, chat_id, message_id):
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception as exc:
            logging.warning("Could not remove a flood message (%s).", type(exc).__name__)

    def pre_process_message(self, message, data):
        chat = getattr(message, "chat", None)
        user = getattr(message, "from_user", None)
        if (
            chat is None or chat.type not in {"group", "supergroup"}
            or not chat_is_active(self.bot, chat.id)
            or user is None or getattr(user, "is_bot", False)
            or getattr(message, "sender_chat", None) is not None
            or is_admin(user.id)
            or GroupManagementMiddleware._is_service_message(message)
        ):
            return None

        now = time.monotonic()
        if self._is_group_admin(chat.id, user.id, now):
            return None

        key = (chat.id, user.id)
        with self._lock:
            # Keep only recent buckets to prevent long-lived groups/users consuming memory.
            for old_key, messages in list(self._messages.items()):
                if not messages or now - messages[-1][0] > 60:
                    self._messages.pop(old_key, None)
            for old_key, expiry in list(self._muted_until.items()):
                if expiry <= now:
                    self._muted_until.pop(old_key, None)
            for old_key, expiry in list(self._warned_until.items()):
                if expiry <= now:
                    self._warned_until.pop(old_key, None)
            expiry = self._muted_until.get(key)
            if expiry is not None and expiry > now:
                flood = [(now, message.message_id)]
                already_muted = True
            else:
                already_muted = False
                bucket = self._messages[key]
                while bucket and now - bucket[0][0] > self.WINDOW_SECONDS:
                    bucket.popleft()
                bucket.append((now, message.message_id))
                if len(bucket) <= self.LIMIT:
                    return None
                flood = list(bucket)
                bucket.clear()
                first_warning = key not in self._warned_until
                self._warned_until[key] = now + self.WARNING_SECONDS
                if not first_warning:
                    # Reserve the enforcement while Telegram is processing the mute.
                    self._muted_until[key] = now + self.MUTE_SECONDS

        if already_muted:
            self._delete(chat.id, message.message_id)
            return CancelUpdate()

        for _, message_id in flood:
            self._delete(chat.id, message_id)

        if first_warning:
            try:
                sent = self.bot.send_message(
                    chat.id,
                    f"⚠️ משתמש {user.id}: זוהתה הצפה. מותרות עד 3 הודעות ב־5 שניות. "
                    "הצפה חוזרת תגרור השתקה ל־15 דקות.",
                    disable_web_page_preview=True,
                )
                delete_later(self.bot, chat.id, sent.message_id, 5)
            except Exception:
                logging.exception("Could not send the first group flood warning.")
            return CancelUpdate()

        mute_succeeded = False
        try:
            permissions = {permission: False for permission in PERMISSIONS}
            self.bot.restrict_chat_member(
                chat.id,
                user.id,
                types.ChatPermissions(**permissions),
                until_date=datetime.fromtimestamp(
                    time.time() + self.MUTE_SECONDS, timezone.utc,
                ),
                use_independent_chat_permissions=True,
            )
            mute_succeeded = True
            with self._lock:
                self._muted_until[key] = now + self.MUTE_SECONDS
        except Exception as exc:
            with self._lock:
                self._muted_until.pop(key, None)
            logging.warning("Could not mute a group flood sender (%s).", type(exc).__name__)

        notice = (
            "🚫 זוהתה הצפה: הודעות המשתמש נמחקו והוא הושתק ל־15 דקות."
            if mute_succeeded else
            "⚠️ זוהתה הצפה וההודעות נמחקו, אך לא ניתן להשתיק את המשתמש. בדקו הרשאות ניהול."
        )
        try:
            sent = self.bot.send_message(chat.id, notice, disable_web_page_preview=True)
            delete_later(self.bot, chat.id, sent.message_id, 5)
        except Exception as exc:
            logging.warning("Could not send the group flood notice (%s).", type(exc).__name__)
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        """Complete the class-middleware contract; no post-processing is needed."""
        return None
