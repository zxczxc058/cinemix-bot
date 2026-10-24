"""Relay unmatched private requests without bypassing auth or admin workflows."""

import logging
from html import escape
from pathlib import Path

from bot_storage import (
    COMMANDS_FILE, build_inline_keyboard, format_telegram_text,
    match_reply, is_admin,
)
from management_config import load_config, resolve_reply
from management_config import save_json
from group_management import delete_later
from media_analytics import MEDIA_ANALYTICS_FILE, MediaAnalytics
from missing_content import MissingContentRequests, PRIVATE_ACK, PRIVATE_DELIVERY_FAILURE
from live_chat import LiveChatQueue
from telebot.handler_backends import BaseMiddleware
from admin_notifications import controls_fallback_allowed

MEDIA_TYPES = [
    "photo", "document", "audio", "voice", "video", "video_note", "animation",
    "sticker", "contact", "location", "venue", "poll", "dice",
]


class PrivateInbox:
    def __init__(self, bot, engine, commands_file=COMMANDS_FILE, analytics=None):
        self.bot, self.engine, self.commands_file = bot, engine, commands_file
        self.admin_registry = None
        self.live_chat = None
        analytics_path = Path(engine.path).with_name(MEDIA_ANALYTICS_FILE.name)
        self.analytics = analytics or MediaAnalytics(analytics_path)
        self.missing_content = MissingContentRequests(
            bot, engine, analytics=self.analytics,
        )
        # Legacy installations without the shared live-chat service use the
        # same durable scheduler, with their existing headers/action controls.
        self._fallback_delivery = LiveChatQueue(
            bot, config_file=engine.path,
            state_file=Path(engine.path).with_name("private_inbox_delivery.json"),
            request_delivery=self._deliver_fallback,
            delivery_allowed=lambda: self.live_chat is None or not self.live_chat.load().get("active_session"),
        )

    def relay(self, message):
        if hasattr(message, "admin_delivery_result"):
            return message.admin_delivery_result
        user = getattr(message, "from_user", None)
        if message.chat.type != "private" or user is None or user.is_bot:
            return None
        config = load_config(self.engine.path)
        from admin_notifications import (
            resolve_user_management_chat,
        )
        target = resolve_user_management_chat(
            config.get("LOG_CHAT_ID"), config_file=self.engine.path,
        )
        if target is None:
            logging.error("Private inbox delivery skipped: no admin/log destination configured.")
            return False
        content = (getattr(message, "text", None) or getattr(message, "caption", None) or "").lstrip()
        category = "פקודה לא מוכרת" if content.startswith("/") else "פנייה כללית"
        if self.live_chat is not None:
            return self.live_chat.receive_message(
                message, category=category, target=target,
            )
        accepted = self._fallback_delivery.receive_message(
            message, category=category, target=target,
        )
        if any(
            record.get("delivery_pending") and record.get("delivery_retry", {}).get("blocked") is False
            for record in self._fallback_delivery.load()["requests"].values()
        ):
            self._fallback_delivery.start()
        return accepted

    def _deliver_fallback(self, state, record):
        """Checkpoint each successful Telegram stage before attempting another."""
        target = record["admin_chat_id"]
        user_id = record["user_id"]
        category = record["category"]
        delivered = set(record["delivered_source_ids"])
        for source in record["source_messages"]:
            if source["message_id"] in delivered:
                continue
            actions = record.setdefault("source_actions", {})
            source_key = str(source["message_id"])
            action_token = actions.get(source_key)
            if action_token is None:
                action_token = self.missing_content.register_external_action(
                    user_id, record.get("summary") or category,
                    target, event_type="admin_inbox",
                )
                actions[source_key] = action_token
                self._fallback_delivery._save(state)
            markup = self.missing_content._keyboard(user_id, action_token)
            tracked_markup = markup
            from user_notification_card import user_card
            payload = user_card(
                user_id, record.get("name"), record.get("username"),
                source.get("summary") or record.get("summary") or category,
                is_vip=self.missing_content.vip_store.is_member(user_id),
            )
            payload += f"\n📁 קטגוריה: {escape(str(category))}"
            if source.get("is_text"):
                from types import SimpleNamespace
                sent = SimpleNamespace(message_id=self._fallback_delivery._send_text_source(
                    state, record, source, markup,
                ))
            else:
                payload = user_card(
                    user_id, record.get("name"), record.get("username"),
                    str(source.get("summary") or record.get("summary") or "קובץ מדיה")[:300],
                    is_vip=self.missing_content.vip_store.is_member(user_id),
                )
                payload += f"\n📁 קטגוריה: {escape(str(category))}"
                send = lambda **kwargs: self.bot.copy_message(
                    target, source["chat_id"], source["message_id"],
                    caption=payload, parse_mode="HTML", **kwargs,
                )
                try:
                    sent = send(reply_markup=markup)
                except Exception as exc:
                    if not controls_fallback_allowed(exc):
                        raise
                    logging.error("Admin inbox copy with buttons failed (%s); retrying without buttons.", type(exc).__name__)
                    sent = send()
                    tracked_markup = None
            if not isinstance(record.get("header_message_id"), int):
                record["header_message_id"] = sent.message_id
            delivered.add(source["message_id"])
            record["delivered_source_ids"] = sorted(delivered)
            self._fallback_delivery._save(state)
            if tracked_markup is None:
                self.missing_content.discard_external_action(action_token)
            else:
                self.missing_content.bind_external_action(action_token, sent.message_id)
            try:
                self.engine.track(target, sent.message_id, user_id, tracked_markup)
            except Exception as exc:
                logging.warning("Delivered inbox controls could not be tracked (%s).", type(exc).__name__)

    def handle_message(self, message):
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        reply = match_reply(text, self.commands_file)
        if reply is not None:
            # Active editor inputs are handled by their earlier state handlers.
            # Merely having an admin menu open must not suppress normal replies.
            try:
                reply = resolve_reply(reply, self.engine.path)
            except ValueError:
                self.bot.reply_to(message, "התוכן אינו זמין: קישור היעד טרם הוגדר. פנה למנהל.")
                return
            body, mode = format_telegram_text(reply.text)
            self.bot.reply_to(
                message, body, parse_mode=mode,
                reply_markup=build_inline_keyboard(
                    reply.buttons, reply.report_id,
                    report_contact_url=load_config(self.engine.path)["report_contact_url"],
                ),
            )
            return
        sender = getattr(message, "from_user", None)
        if message.chat.type in {"group", "supergroup"}:
            # Group searches without an exact match are never support tickets.
            return
        if message.chat.type != "private":
            return
        if hasattr(message, "admin_delivery_result"):
            # The private delivery middleware already copied and acknowledged
            # this message. Do not submit it again through another handler.
            return

        if (
            sender is not None and not getattr(sender, "is_bot", False)
            and not is_admin(sender.id)
            and not text.strip().startswith("/")
            and self.missing_content.is_content_query(getattr(message, "text", None))
        ):
            delivered = self.missing_content.submit(message)
            self.bot.reply_to(
                message,
                PRIVATE_ACK if delivered else PRIVATE_DELIVERY_FAILURE,
            )
            return

        forwarded = self.relay(message)
        body, mode = format_telegram_text(
            load_config(self.engine.path)["unknown_command_text"]
            if forwarded else PRIVATE_DELIVERY_FAILURE
        )
        self.bot.reply_to(message, body, parse_mode=mode)
        if sender is not None and is_admin(sender.id):
            # Menu/form consumers run first. Unmatched administrator requests
            # follow the private inbox flow, then their input is cleaned.
            self._delete_admin_input(message)

    def forward_and_confirm(self, message, *, known_content=False):
        """Route an ordinary private message once, before command consumers."""
        if message.chat.type != "private":
            return False
        if hasattr(message, "admin_delivery_result"):
            return message.admin_delivery_result
        text = getattr(message, "text", None) or ""
        try:
            if (
                not known_content and match_reply(text, self.commands_file) is None
                and not text.strip().startswith("/")
                and self.missing_content.is_content_query(text)
            ):
                if self.live_chat is not None:
                    accepted = self.live_chat.receive_message(
                        message, category="בקשת תוכן",
                        notification_type="missing_content",
                    )
                    if accepted and not getattr(message, "live_chat_delivery", False):
                        try:
                            self.analytics.record_missing_content_request(text.strip())
                        except Exception:
                            logging.exception("Could not record the accepted content request's analytics.")
                else:
                    accepted = self.missing_content.submit(message)
            else:
                accepted = self.relay(message)
        except Exception:
            logging.exception("Private message could not be accepted for management delivery.")
            accepted = False
        message.admin_delivery_result = bool(accepted)
        if getattr(message, "live_chat_delivery", False):
            return bool(accepted)
        from system_messages import send_system_message
        send_system_message(
            self.bot, message.chat.id,
            "request_received" if accepted else "delivery_error", self.engine.path,
        )
        return bool(accepted)

    def _delete_admin_input(self, message):
        protection = getattr(self.bot, "private_admin_protection", None)
        try:
            if protection is not None:
                deleted = protection.delete_private_admin_input(message)
            else:
                deleted = self.bot.delete_message(message.chat.id, message.message_id)
            if deleted is False:
                logging.warning("Unrouted administrator input was not deleted.")
        except Exception as exc:
            logging.warning("Unrouted administrator input cleanup failed (%s).", type(exc).__name__)

    def register_media_handler(self):
        # Register after support and admin input consumers. Authorization and
        # moderation middleware runs first, including for non-text messages.
        self.bot.message_handler(func=lambda message: message.chat.type == "private", content_types=MEDIA_TYPES)(
            self.handle_message
        )

    def register_missing_content_callback(self):
        self.bot.callback_query_handler(
            func=lambda call: bool(call.data) and call.data.startswith("missing_content:")
        )(self.handle_missing_content_callback)

    def handle_missing_content_callback(self, call):
        return self.missing_content.handle_callback(call)

    def handle_admin_reply(self, message):
        """Route replies using durable bot-created message IDs, never quoted IDs."""
        if self.live_chat is not None and self.live_chat.handle_admin_reply(message):
            return True
        user = getattr(message, "from_user", None)
        quoted = getattr(message, "reply_to_message", None)
        config = load_config(self.engine.path)
        if (
            user is None or user.id not in config["admin_ids"] or quoted is None
            or message.chat.type not in {"group", "supergroup"}
            or message.chat.id != config["ADMIN_CHAT_ID"]
        ):
            return False
        with self.engine.operation_lock:
            record = self.engine.events["messages"].get(
                f"{message.chat.id}:{quoted.message_id}"
            )
            if record is None:
                return False
            delivered = self.engine.events.setdefault("delivered_replies", {})
            key = f"{message.chat.id}:{message.message_id}"
            if key in delivered:
                return True
            try:
                self.bot.copy_message(record["uid"], message.chat.id, message.message_id)
                delivered[key] = record["uid"]
                save_json(self.engine.events_file, self.engine.events)
            except Exception as exc:
                logging.error("Admin inbox reply delivery failed (%s).", type(exc).__name__)
                notice = "⚠️ התשובה לא נשלחה. ייתכן שהמשתמש חסם את הבוט; נסה שוב."
            else:
                notice = "✉️ התשובה נשלחה בהצלחה למשתמש!"
        try:
            sent = self.bot.send_message(message.chat.id, notice)
            delete_later(self.bot, message.chat.id, sent.message_id, 5)
        except Exception as exc:
            logging.warning("Admin inbox reply confirmation failed (%s).", type(exc).__name__)
        return True

    def relay_support_request(self, message, support_chat_id):
        """Mirror unmatched explicit support requests, without same-chat duplicates."""
        if load_config(self.engine.path)["admin_chat_id"] == support_chat_id and self.live_chat is None:
            return None
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        if match_reply(text, self.commands_file) is not None:
            return None
        return self.relay(message)


class PrivateDeliveryMiddleware(BaseMiddleware):
    """Observe ordinary private input after access gates, not admin/code forms."""

    def __init__(self, inbox, *, awaiting_code=None, known_content=None):
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message"]
        self.inbox = inbox
        self.awaiting_code = awaiting_code
        self.known_content = known_content

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        live = getattr(self.inbox, "live_chat", None)
        chat = getattr(message, "chat", None)
        if (live is not None and getattr(chat, "type", None) in {"group", "supergroup"}
                and getattr(message, "content_type", "text") in {"text", *MEDIA_TYPES}):
            text = (getattr(message, "text", None) or "").strip()
            if not text.startswith("/") and live.handle_admin_reply(message) is True:
                from telebot.handler_backends import CancelUpdate
                return CancelUpdate()
        if (
            getattr(getattr(message, "chat", None), "type", None) != "private"
            or user is None or getattr(user, "is_bot", False) or is_admin(user.id)
        ):
            return
        text = (getattr(message, "text", None) or "").strip()
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        # Reset/code entry is not a support request. Never forward credentials.
        if command in {"/start", "/cancel", "/vip", "/code"} or text == "ביטול":
            return
        if self.awaiting_code is not None and self.awaiting_code(user.id):
            return
        if live is not None and live.is_active_user(user.id) is True:
            self.inbox.forward_and_confirm(message)
            from telebot.handler_backends import CancelUpdate
            return CancelUpdate()
        known = self.known_content is not None and self.known_content(message)
        if known or match_reply(text, self.inbox.commands_file) is not None:
            return
        from reply_keyboard import get_button_trigger
        if get_button_trigger(text) is not None:
            return
        if command.startswith("/"):
            registered = {
                "/" + name.casefold()
                for handler in getattr(self.inbox.bot, "message_handlers", [])
                for name in handler.get("filters", {}).get("commands", [])
            }
            if command in registered:
                return
        self.inbox.forward_and_confirm(message)

    def post_process_message(self, message, data, exception):
        pass
