"""Explicit private purchase inquiries, isolated from access-code/fallback input."""

import logging
import threading

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import match_reply, format_telegram_text
from reply_keyboard import get_button_trigger
from system_messages import send_system_message


class PurchaseInquiryMiddleware(BaseMiddleware):
    def __init__(self, inbox, *, clear_code_state=None, cancel_support=None,
                 keyboard_file=None, allow_inquiry=None):
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message"]
        self.inbox = inbox
        self.clear_code_state = clear_code_state
        self.cancel_support = cancel_support
        self.keyboard_file = keyboard_file
        self.allow_inquiry = allow_inquiry
        self.pending = {}
        self._lock = threading.RLock()

    def cancel_user(self, user_id):
        with self._lock:
            self.pending.pop(user_id, None)

    def handle_callback(self, call, store, *, context="מנוי VIP"):
        user = getattr(call, "from_user", None)
        message = getattr(call, "message", None)
        if (user is None or message is None or message.chat.type != "private"
                or message.chat.id != user.id):
            self.inbox.bot.answer_callback_query(call.id)
            return
        settings = store.snapshot()
        try:
            self.inbox.bot.answer_callback_query(
                call.id, text=settings["contact_popup_text"][:200], show_alert=True,
            )
        except Exception:
            logging.exception("Purchase button popup could not be acknowledged.")
        if self.allow_inquiry and not self.allow_inquiry(user.id):
            return
        if self.clear_code_state:
            self.clear_code_state(user.id, message.chat.id)
        if self.cancel_support:
            self.cancel_support(user.id, message.chat.id)
        with self._lock:
            self.pending[user.id] = {
                "state": "awaiting_vip_purchase_inquiry_message",
                "command": context,
            }
        try:
            live = self.inbox.live_chat or self.inbox._fallback_delivery
            name = " ".join(filter(None, [
                getattr(user, "first_name", ""), getattr(user, "last_name", ""),
            ])) or "ללא שם"
            accepted = live.submit_callback_request(
                user.id, name, getattr(user, "username", None), context=context,
                category="רכישת מנוי VIP", notification_type="vip_purchase",
            )
            if not accepted:
                logging.error("Purchase button request has no available management destination.")
            body, mode = format_telegram_text(settings["contact_confirmation_text"])
            self.inbox.bot.send_message(
                message.chat.id, body, parse_mode=mode, disable_web_page_preview=True,
            )
        except Exception:
            logging.exception("Purchase button request could not be delivered.")

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        if (getattr(message.chat, "type", None) != "private"
                or user is None or getattr(user, "is_bot", False)):
            return
        text = (getattr(message, "text", None) or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold() if text else ""
        if command in {"/start", "/cancel"} or text == "ביטול":
            self.cancel_user(user.id)
            return
        if self.allow_inquiry and not self.allow_inquiry(user.id):
            # Existing maintenance handlers retain their normal response.
            return
        with self._lock:
            state = self.pending.get(user.id)
        if state:
            try:
                live = self.inbox.live_chat or self.inbox._fallback_delivery
                accepted = live.receive_message(
                    message, category="רכישת מנוי VIP",
                    notification_type="vip_purchase",
                )
                if accepted:
                    self.cancel_user(user.id)
                if not getattr(message, "live_chat_delivery", False):
                    send_system_message(
                        self.inbox.bot, message.chat.id,
                        "request_received" if accepted else "delivery_error",
                        self.inbox.engine.path,
                    )
            except Exception:
                # Retain the input state for retry; never treat inquiry input
                # as a code or silently fall through to a second ticket path.
                logging.exception("VIP purchase inquiry could not be delivered.")
            return CancelUpdate()
        live = self.inbox.live_chat
        if live is not None and live.is_active_user(user.id):
            # Buyers need not possess an access code to talk to the admin who
            # accepted their inquiry. Existing live relay stays card/receipt-free.
            self.inbox.forward_and_confirm(message)
            return CancelUpdate()
        trigger = (get_button_trigger(text, self.keyboard_file)
                   if self.keyboard_file else get_button_trigger(text))
        reply = match_reply(trigger or text, self.inbox.commands_file)
        if reply is None or reply.action_type != "vip_purchase":
            return
        if self.clear_code_state:
            self.clear_code_state(user.id, message.chat.id)
        if self.cancel_support:
            self.cancel_support(user.id, message.chat.id)
        with self._lock:
            self.pending[user.id] = {
                "state": "awaiting_vip_purchase_inquiry_message",
                "command": trigger or text,
            }
        body, mode = format_telegram_text(reply.text)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🛒 פרטים על רכישת מנוי VIP", callback_data="vip:purchase:membership",
            ),
        ]])
        try:
            self.inbox.bot.send_message(
                message.chat.id, body, parse_mode=mode,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        except Exception:
            logging.exception("Could not send the configured purchase inquiry prompt.")
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        pass
