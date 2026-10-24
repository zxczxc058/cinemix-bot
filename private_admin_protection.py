"""Fail-closed retention, with explicit enforcement-reason cleanup."""

import functools
import threading

from telebot.handler_backends import BaseMiddleware
from bot_storage import is_admin


def _reason_message_id(chat_id, message, allow_photo=False):
    """Accept only verified administrator input in their own private chat."""
    if message is None:
        return None
    chat = getattr(message, "chat", None)
    sender = getattr(message, "from_user", None)
    mid = getattr(message, "message_id", None)
    if (
        chat and getattr(chat, "type", None) == "private" and getattr(chat, "id", None) == chat_id
        and sender and getattr(sender, "id", None) == chat_id and getattr(sender, "is_bot", None) is False
        and is_admin(chat_id) and type(mid) is int and mid > 0
        and (
            isinstance(getattr(message, "text", None), str) and message.text.strip()
            or allow_photo and any(
                getattr(message, kind, None)
                for kind in ("photo", "video", "document", "animation", "audio", "voice")
            )
        )
    ):
        return mid
    return None


def _delete_each(bot, chat_id, message_ids):
    for mid in message_ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception as exc:
            import logging
            logging.warning("Enforcement cleanup failed (%s).", type(exc).__name__)


def send_enforcement_confirmation(bot, chat_id, text, reason_message=None, **kwargs):
    """Expire only explicitly identified bot-authored enforcement receipts."""
    from admin_ui import collect_private_feedback
    if collect_private_feedback(chat_id, text):
        from admin_edit_flow import private_edit_active
        if private_edit_active(chat_id):
            return None
        protection = getattr(bot, "private_admin_protection", None)
        if isinstance(protection, PrivateAdminProtection):
            protection.expire_enforcement_reason(chat_id, reason_message)
        else:
            reason_id = _reason_message_id(chat_id, reason_message)
            if reason_id:
                from confirmation_cleanup import schedule_confirmation_cleanup
                schedule_confirmation_cleanup(lambda: _delete_each(bot, chat_id, [reason_id]), 5)
        from types import SimpleNamespace
        return SimpleNamespace(message_id=None)
    sent = bot.send_message(chat_id, text, **kwargs)
    protection = getattr(bot, "private_admin_protection", None)
    if isinstance(protection, PrivateAdminProtection):
        protection.expire_enforcement_confirmation(chat_id, sent.message_id, reason_message)
    else:
        from confirmation_cleanup import schedule_confirmation_cleanup
        reason_id = _reason_message_id(chat_id, reason_message)
        ids = [reason_id, sent.message_id] if reason_id else [sent.message_id]
        schedule_confirmation_cleanup(lambda: _delete_each(bot, chat_id, ids), 5)
    return sent


# Compatibility for existing unban/unmute callers.
send_release_confirmation = send_enforcement_confirmation


def send_broadcast_completion(bot, chat_id, text, source_message=None):
    """Clean only the broadcast payload and its private completion receipt."""
    from admin_ui import AdminChatUI, collect_private_feedback
    if isinstance(bot, AdminChatUI) and chat_id > 0 and is_admin(chat_id):
        if not collect_private_feedback(chat_id, text):
            with bot._lock:
                bot._offer_alert(chat_id, text, True)
        from admin_edit_flow import private_edit_active
        editor_will_clean = bool(source_message is not None and getattr(source_message, "text", None)
                                 and private_edit_active(chat_id))
        if source_message is not None and not editor_will_clean and not getattr(source_message, "private_admin_input_cleaned", False):
            protection = getattr(bot.raw, "private_admin_protection", None)
            if isinstance(protection, PrivateAdminProtection):
                protection.expire_broadcast_input(chat_id, source_message)
            else:
                source_id = _reason_message_id(chat_id, source_message, allow_photo=True)
                if source_id is not None:
                    def delete_source():
                        try:
                            bot.raw.delete_message(chat_id, source_id)
                        except Exception:
                            pass
                    from confirmation_cleanup import schedule_confirmation_cleanup
                    schedule_confirmation_cleanup(delete_source, 5)
        return None
    sent = bot.send_message(chat_id, text)
    protection = getattr(bot, "private_admin_protection", None)
    if isinstance(protection, PrivateAdminProtection):
        protection.expire_broadcast_completion(chat_id, sent.message_id, source_message)
    else:
        from confirmation_cleanup import schedule_confirmation_cleanup
        source_id = _reason_message_id(chat_id, source_message, allow_photo=True)
        ids = [source_id, sent.message_id] if source_id else [sent.message_id]
        schedule_confirmation_cleanup(lambda: _delete_each(bot, chat_id, ids), 5)
    return sent


class PrivateAdminProtection(BaseMiddleware):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot
        self.bot_messages = set()
        self.menu_messages = set()
        self.stat_messages = set()
        self.release_confirmations = set()
        self.enforcement_reasons = set()
        self.broadcast_receipts = set()
        self.broadcast_inputs = set()
        self.command_inputs = set()
        self.command_receipts = set()
        self.setting_inputs = set()
        self.lock = threading.RLock()
        self.navigation = threading.local()
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]
        original_delete = bot.delete_message

        @functools.wraps(original_delete)
        def delete_message(chat_id, message_id, *args, **kwargs):
            if type(chat_id) is int and chat_id > 0 and is_admin(chat_id):
                with self.lock:
                    key = (chat_id, message_id)
                    permitted = (
                        getattr(self.navigation, "active", False) and key in self.menu_messages
                    ) or (
                        getattr(self.navigation, "statistics", False) and key in self.stat_messages
                    ) or (
                        getattr(self.navigation, "release_confirmation", False) and key in self.release_confirmations
                    ) or (
                        getattr(self.navigation, "broadcast_receipt", False) and key in self.broadcast_receipts
                    ) or (
                        getattr(self.navigation, "command_receipt", False) and key in self.command_receipts
                    )
                    permitted_reason = (
                        getattr(self.navigation, "enforcement_reason", False)
                        and key in self.enforcement_reasons
                    ) or (
                        getattr(self.navigation, "broadcast_input", False) and key in self.broadcast_inputs
                    ) or (
                        getattr(self.navigation, "command_input", False) and key in self.command_inputs
                    ) or (
                        getattr(self.navigation, "setting_input", False) and key in self.setting_inputs
                    )
                    if not permitted_reason and (not permitted or key not in self.bot_messages):
                        return False
            result = original_delete(chat_id, message_id, *args, **kwargs)
            if result is not False:
                from admin_edit_flow import record_deleted
                record_deleted(chat_id, message_id)
            return result
        bot.delete_message = delete_message
        if hasattr(bot, "delete_messages"):
            original_bulk = bot.delete_messages

            @functools.wraps(original_bulk)
            def delete_messages(chat_id, message_ids, **kwargs):
                if type(chat_id) is int and chat_id > 0 and is_admin(chat_id):
                    if not getattr(self.navigation, "active", False):
                        return False
                    with self.lock:
                        message_ids = [mid for mid in message_ids if (chat_id, mid) in self.bot_messages and (chat_id, mid) in self.menu_messages]
                    if not message_ids:
                        return False
                return original_bulk(chat_id, message_ids, **kwargs)
            bot.delete_messages = delete_messages
        for name in (
            "send_message", "send_photo", "send_document", "send_voice", "send_video",
            "send_audio", "send_sticker", "send_animation", "send_video_note",
            "send_location", "send_contact", "send_poll", "send_venue",
            "send_media_group", "copy_message", "forward_message",
        ):
            self._wrap_send(name)
        bot.private_admin_protection = self

    def mark_menu(self, chat_id, message_id, proof=None):
        with self.lock:
            sender = getattr(proof, "from_user", None)
            chat = getattr(proof, "chat", None)
            if (
                chat and chat.id == chat_id and getattr(proof, "message_id", None) == message_id
                and getattr(sender, "is_bot", False) is True
            ):
                self.bot_messages.add((chat_id, message_id))
            if (chat_id, message_id) in self.bot_messages:
                self.menu_messages.add((chat_id, message_id))

    def delete_menu(self, chat_id, message_id):
        """Only the menu navigator may remove a proven bot-authored message."""
        previous = getattr(self.navigation, "active", False)
        self.navigation.active = True
        try:
            return self.bot.delete_message(chat_id, message_id)
        finally:
            self.navigation.active = previous

    def mark_stat(self, chat_id, message_id):
        with self.lock:
            key = (chat_id, message_id)
            if key in self.bot_messages:
                self.stat_messages.add(key)

    def delete_stat(self, chat_id, message_id):
        previous = getattr(self.navigation, "statistics", False)
        self.navigation.statistics = True
        try:
            return self.bot.delete_message(chat_id, message_id)
        finally:
            self.navigation.statistics = previous

    def _wrap_send(self, name):
        original = getattr(self.bot, name, None)
        if original is None:
            return

        @functools.wraps(original)
        def send(*args, **kwargs):
            result = original(*args, **kwargs)
            chat_id = args[0] if args else kwargs.get("chat_id")
            results = result if isinstance(result, list) else [result]
            if type(chat_id) is int and chat_id > 0 and is_admin(chat_id):
                with self.lock:
                    for message in results:
                        mid = getattr(message, "message_id", None)
                        if type(mid) is int:
                            self.bot_messages.add((chat_id, mid))
            return result
        setattr(self.bot, name, send)

    def expire_enforcement_confirmation(self, chat_id, message_id, reason_message=None):
        """Narrow exception: only a proven bot-created enforcement receipt."""
        reason_id = _reason_message_id(chat_id, reason_message)
        self._expire_pair(chat_id, message_id, reason_id, self.release_confirmations,
                          self.enforcement_reasons, "release_confirmation", "enforcement_reason")

    def delete_command_inputs(self, chat_id, proofs):
        """Called only after a committed command's archive delivery succeeds."""
        ids = {_reason_message_id(chat_id, proof) for proof in proofs} - {None}
        with self.lock:
            ids = {mid for mid in ids if (chat_id, mid) not in self.bot_messages}
            keys = {(chat_id, mid) for mid in ids}
            self.command_inputs.update(keys)
        previous = getattr(self.navigation, "command_input", False)
        self.navigation.command_input = True
        failed = 0
        try:
            for mid in ids:
                try:
                    if self.bot.delete_message(chat_id, mid) is False:
                        failed += 1
                except Exception as exc:
                    failed += 1
                    import logging
                    logging.warning("Archived command input cleanup failed (%s).", type(exc).__name__)
        finally:
            self.navigation.command_input = previous
            with self.lock:
                self.command_inputs.difference_update(keys)
        return failed

    def expire_command_confirmation(self, chat_id, message_id):
        self._expire_pair(chat_id, message_id, None, self.command_receipts,
                          self.command_inputs, "command_receipt", "command_input")

    def delete_setting_inputs(self, chat_id, proofs):
        """Immediate, scoped cleanup only after a setting commit is confirmed."""
        ids = {_reason_message_id(chat_id, proof) for proof in proofs} - {None}
        with self.lock:
            ids = {mid for mid in ids if (chat_id, mid) not in self.bot_messages}
            keys = {(chat_id, mid) for mid in ids}
            self.setting_inputs.update(keys)
        previous = getattr(self.navigation, "setting_input", False)
        self.navigation.setting_input = True
        failed = 0
        try:
            for mid in ids:
                try:
                    if self.bot.delete_message(chat_id, mid) is False:
                        failed += 1
                except Exception:
                    failed += 1
        finally:
            self.navigation.setting_input = previous
            with self.lock:
                self.setting_inputs.difference_update(keys)
        return failed

    def delete_private_admin_input(self, message):
        """Delete only a verified administrator's own private input message."""
        actor = getattr(getattr(message, "from_user", None), "id", None)
        chat = getattr(message, "chat", None)
        message_id = _reason_message_id(actor, message)
        if chat is None or message_id is None:
            return False
        key = (actor, message_id)
        with self.lock:
            if key in self.bot_messages:
                return False
            self.setting_inputs.add(key)
        previous = getattr(self.navigation, "setting_input", False)
        self.navigation.setting_input = True
        try:
            return self.bot.delete_message(actor, message_id) is not False
        finally:
            self.navigation.setting_input = previous
            with self.lock:
                self.setting_inputs.discard(key)

    def expire_enforcement_reason(self, chat_id, reason_message):
        """Popup receipts create no message; retain only verified reason cleanup."""
        reason_id = _reason_message_id(chat_id, reason_message)
        if reason_id is None:
            return
        key = (chat_id, reason_id)
        with self.lock:
            if key in self.bot_messages:
                return
            self.enforcement_reasons.add(key)
        def delete():
            previous = getattr(self.navigation, "enforcement_reason", False)
            self.navigation.enforcement_reason = True
            try:
                _delete_each(self.bot, chat_id, [reason_id])
            finally:
                self.navigation.enforcement_reason = previous
                with self.lock:
                    self.enforcement_reasons.discard(key)
        from confirmation_cleanup import schedule_confirmation_cleanup
        schedule_confirmation_cleanup(delete, 5)

    def expire_broadcast_completion(self, chat_id, message_id, source_message=None):
        source_id = _reason_message_id(chat_id, source_message, allow_photo=True)
        self._expire_pair(chat_id, message_id, source_id, self.broadcast_receipts,
                          self.broadcast_inputs, "broadcast_receipt", "broadcast_input")

    def expire_broadcast_input(self, chat_id, source_message):
        source_id = _reason_message_id(chat_id, source_message, allow_photo=True)
        if source_id is None:
            return
        key = (chat_id, source_id)
        with self.lock:
            self.broadcast_inputs.add(key)

        def delete():
            previous = getattr(self.navigation, "broadcast_input", False)
            self.navigation.broadcast_input = True
            try:
                self.bot.delete_message(chat_id, source_id)
            except Exception:
                pass
            finally:
                self.navigation.broadcast_input = previous
                with self.lock:
                    self.broadcast_inputs.discard(key)

        from confirmation_cleanup import schedule_confirmation_cleanup
        schedule_confirmation_cleanup(delete, 5)

    def _expire_pair(self, chat_id, message_id, reason_id, receipts, inputs, receipt_flag, input_flag):
        with self.lock:
            key = (chat_id, message_id)
            if key not in self.bot_messages:
                return
            receipts.add(key)
            reason_key = (chat_id, reason_id) if reason_id else None
            if reason_key in self.bot_messages:
                reason_key, reason_id = None, None
            if reason_key:
                inputs.add(reason_key)

        def delete():
            previous = getattr(self.navigation, receipt_flag, False)
            previous_reason = getattr(self.navigation, input_flag, False)
            setattr(self.navigation, receipt_flag, True)
            setattr(self.navigation, input_flag, True)
            try:
                ids = [reason_id, message_id] if reason_id else [message_id]
                _delete_each(self.bot, chat_id, ids)
            finally:
                setattr(self.navigation, receipt_flag, previous)
                setattr(self.navigation, input_flag, previous_reason)
                with self.lock:
                    receipts.discard(key)
                    inputs.discard(reason_key)
        from confirmation_cleanup import schedule_confirmation_cleanup
        schedule_confirmation_cleanup(delete, 5)

    expire_release_confirmation = expire_enforcement_confirmation

    def pre_process_message(self, message, data):
        return None

    def pre_process_callback_query(self, call, data):
        message = getattr(call, "message", None)
        sender = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        if chat and chat.type == "private" and is_admin(chat.id) and getattr(sender, "is_bot", False):
            with self.lock:
                self.bot_messages.add((chat.id, message.message_id))

    def post_process_message(self, message, data, exception):
        return None

    def post_process_callback_query(self, call, data, exception):
        return None
