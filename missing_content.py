"""Persistent missing-media requests and administrator actions."""

from __future__ import annotations

import json
import html
import logging
import secrets
import threading
import time
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import format_telegram_text, is_admin
from management_config import _LOCK, load_config, save_json
from media_analytics import MEDIA_ANALYTICS_FILE, MediaAnalytics
from user_notification_card import user_card, broken_link_text
from ticket_status_delivery import DELIVERY_ERROR, send_status_notification, status_feedback


PRIVATE_ACK = "⚠️ התוכן שביקשת לא נמצא כרגע במאגר. בקשתך הועברה לצוות הניהול."
PRIVATE_DELIVERY_FAILURE = (
    "⚠️ התוכן לא נמצא כרגע במאגר, ולא הצלחתי להעביר את הבקשה לצוות. "
    "נסה שוב מאוחר יותר."
)
MAX_REQUEST_TEXT = 3000
MAX_STORED_TITLE = 500


class MissingContentRequests:
    def __init__(
        self,
        bot,
        engine,
        storage_file: str | Path | None = None,
        analytics: MediaAnalytics | None = None,
        vip_store=None,
    ):
        self.bot, self.engine = bot, engine
        self.config_file = Path(engine.path)
        self.storage_file = Path(
            storage_file
            or self.config_file.with_name("missing_content_requests.json")
        )
        self.analytics = analytics or MediaAnalytics(
            self.config_file.with_name(MEDIA_ANALYTICS_FILE.name),
        )
        if vip_store is None:
            from vip_access import VIPSettingsStore
            vip_store = VIPSettingsStore(
                self.config_file.with_name("vip_settings.json"),
            )
        self.vip_store = vip_store
        self.live_chat = None
        self.lock = threading.RLock()
        self._resolving = set()
        self._load()

    def _load(self) -> dict:
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(self.storage_file))
        except FileNotFoundError:
            return {"requests": {}}
        if not isinstance(data, dict) or not isinstance(data.get("requests"), dict):
            raise ValueError("Missing-content request file has an invalid format.")
        return data

    def _save(self, data: dict) -> None:
        with _LOCK:
            save_json(self.storage_file, data)

    @staticmethod
    def is_content_query(text: str | None) -> bool:
        value = (text or "").strip()
        if not value:
            return False
        # A bare web link is a general inbox item, not a missing title/name.
        return not value.lower().startswith(("https://", "http://", "www."))

    def _keyboard(self, uid: int, token: str) -> InlineKeyboardMarkup:
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.row(InlineKeyboardButton(
            "💬 פתח שיחה עם המשתמש", url=f"tg://user?id={uid}",
        ))
        keyboard.row(
            InlineKeyboardButton("✅ הועלה", callback_data=f"missing_content:uploaded:{token}"),
            InlineKeyboardButton("❌ לא זמין", callback_data=f"missing_content:unavailable:{token}"),
        )
        keyboard.row(
            InlineKeyboardButton("🔊 השתק משתמש", callback_data=f"mod:mute:{uid}"),
            InlineKeyboardButton("🚫 חסום משתמש", callback_data=f"mod:ban:{uid}"),
        )
        return keyboard

    @staticmethod
    def _user_name(user) -> str:
        return " ".join(filter(None, [
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        ])) or "ללא שם"

    def _is_vip(self, user_id: int) -> bool:
        try:
            return self.vip_store.is_member(user_id)
        except Exception as exc:
            logging.warning("VIP request classification failed (%s).", type(exc).__name__)
            return False

    def _report_text(self, message, content: str) -> str:
        user = message.from_user
        is_vip = self._is_vip(user.id)
        user_field = (
            f"👤 משתמש: ⭐ {self._user_name(user)} (ID: {user.id}) "
            "- סטטוס: VIP · ⭐ מנוי VIP ⭐"
            if is_vip
            else f"👤 משתמש: {self._user_name(user)} (ID: {user.id})"
        )
        lines = [
            "⭐ בקשת תוכן חדשה - VIP! ⭐" if is_vip else "🔍 בקשת תוכן חדשה!",
        ]
        chat = message.chat
        if getattr(chat, "type", None) in {"group", "supergroup"}:
            group_name = getattr(chat, "title", None) or "קבוצה"
            lines.append(f"👥 קבוצה: {group_name} (ID: {chat.id})")
        lines.append(f"🎬 תוכן מבוקש: {content[:MAX_REQUEST_TEXT]}")
        return user_card(
            user.id, self._user_name(user), getattr(user, "username", None),
            "\n".join(lines),
            is_vip=is_vip,
        )

    def submit(self, message) -> bool:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        content = (getattr(message, "text", None) or "").strip()
        if (
            user is None or getattr(user, "is_bot", False)
            or chat is None or getattr(chat, "type", None) != "private"
            or not self.is_content_query(content)
        ):
            return False

        try:
            config = load_config(self.config_file)
            from admin_notifications import resolve_user_management_chat

            target = resolve_user_management_chat(config_file=self.config_file)
        except Exception as exc:
            logging.error(
                "Missing-content admin destination could not be resolved "
                "(error=%s).",
                type(exc).__name__,
            )
            return False
        if target is None:
            logging.error(
                "Missing-content request was not sent: no admin group is configured."
            )
            return False
        if self.live_chat is not None:
            try:
                delivered = self.live_chat.submit_callback_request(
                    user.id,
                    self._user_name(user),
                    getattr(user, "username", None),
                    context=content[:MAX_REQUEST_TEXT],
                    category="בקשת תוכן",
                    target=target,
                    notification_type="missing_content",
                    source_message=message if chat.type == "private" else None,
                )
            except Exception as exc:
                logging.error("Missing-content live-chat enqueue failed (%s).", type(exc).__name__)
                return False
            if delivered:
                try:
                    self.analytics.record_missing_content_request(content)
                except Exception as exc:
                    logging.error(
                        "Missing-content analytics could not be saved (%s).",
                        type(exc).__name__,
                    )
            return delivered

        token = secrets.token_hex(8)
        request = {
            "uid": user.id,
            "title": content[:MAX_STORED_TITLE],
            "admin_chat_id": target,
            "message_id": None,
            "status": "pending",
            "created_at": time.time(),
            "event_type": "missing_content",
            "is_vip": self._is_vip(user.id),
        }
        with self.lock:
            data = self._load()
            data["requests"][token] = request
            # Retain unresolved requests; discard only old, already-resolved items.
            cutoff = time.time() - 90 * 86400
            data["requests"] = {
                key: value for key, value in data["requests"].items()
                if value.get("status") == "pending"
                or value.get("created_at", 0) >= cutoff
            }
            try:
                self._save(data)
            except Exception as exc:
                logging.error("Missing-content request could not be saved (%s).", type(exc).__name__)
                return False

        try:
            sent = self.bot.send_message(
                target,
                self._report_text(message, content),
                parse_mode="HTML",
                reply_markup=self._keyboard(user.id, token),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.error("Missing-content request delivery failed (%s).", type(exc).__name__)
            with self.lock:
                data = self._load()
                data["requests"].pop(token, None)
                self._save(data)
            return False

        with self.lock:
            data = self._load()
            stored = data["requests"].get(token)
            if stored is not None:
                stored["message_id"] = getattr(sent, "message_id", None)
                self._save(data)
        try:
            self.engine.track(target, sent.message_id, user.id, self._keyboard(user.id, token))
        except Exception as exc:
            logging.warning("Missing-content moderation route could not be tracked (%s).", type(exc).__name__)
        try:
            self.analytics.record_missing_content_request(content)
        except Exception as exc:
            logging.error(
                "Missing-content analytics could not be saved (%s).",
                type(exc).__name__,
            )
        return True

    def submit_link_error(self, user, content: str) -> bool:
        """Persist and notify an actionable Drive/media link-error report."""
        user_id = getattr(user, "id", None)
        if type(user_id) is not int or user_id <= 0:
            return False
        try:
            config = load_config(self.config_file)
            from admin_notifications import resolve_user_management_chat

            target = resolve_user_management_chat(config_file=self.config_file)
        except Exception as exc:
            logging.error(
                "Link-error admin destination could not be resolved "
                "(error=%s).",
                type(exc).__name__,
            )
            return False
        if target is None:
            logging.error(
                "Link-error report was not sent: no admin group is configured."
            )
            return False
        is_vip = self._is_vip(user_id)
        name = self._user_name(user)
        if self.live_chat is not None:
            try:
                return self.live_chat.submit_callback_request(
                    user_id,
                    name,
                    getattr(user, "username", None),
                    context=content[:MAX_STORED_TITLE],
                    category="דיווח תקלה בקישור",
                    target=target,
                    notification_type="link_error",
                )
            except Exception as exc:
                logging.error("Media link-error live-chat enqueue failed (%s).", type(exc).__name__)
                return False
        body = (
            "⭐ 🚨 דיווח על תקלה בקישור דרייב/מדיה - מנוי VIP! ⭐\n"
            f"- תוכן: {content}\n"
            f"- משתמש: ⭐ {name} (ID: {user_id}) "
            "- סטטוס: VIP · ⭐ מנוי VIP ⭐"
            if is_vip
            else "🚨 דיווח על תקלה בקישור דרייב/מדיה!\n"
            f"- תוכן: {content}\n"
            f"- משתמש: {name} (ID: {user_id})"
        )
        body = user_card(
            user_id, name, getattr(user, "username", None),
            broken_link_text(user_id, name, getattr(user, "username", None), content, is_vip),
            is_vip=is_vip,
        )
        token = secrets.token_hex(8)
        record = {
            "uid": user_id,
            "title": content[:MAX_STORED_TITLE],
            "admin_chat_id": target,
            "message_id": None,
            "status": "pending",
            "created_at": time.time(),
            "event_type": "link_error",
            "is_vip": is_vip,
        }
        with self.lock:
            data = self._load()
            data["requests"][token] = record
            self._save(data)
        try:
            sent = self.bot.send_message(
                target,
                body,
                parse_mode="HTML",
                reply_markup=self._keyboard(user_id, token),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.error("Media link-error notification failed (%s).", type(exc).__name__)
            with self.lock:
                data = self._load()
                data["requests"].pop(token, None)
                self._save(data)
            return False
        with self.lock:
            data = self._load()
            saved = data["requests"].get(token)
            if saved is not None:
                saved["message_id"] = getattr(sent, "message_id", None)
                self._save(data)
        try:
            self.engine.track(
                target, sent.message_id, user_id, self._keyboard(user_id, token),
            )
        except Exception as exc:
            logging.warning("Link-report moderation route could not be tracked (%s).", type(exc).__name__)
        return True

    def register_external_action(self, user_id: int, title: str, target: int, *, event_type: str):
        """Create durable action state for an admin inbox message copied elsewhere."""
        if (
            type(user_id) is not int or user_id <= 0
            or type(target) is not int or target >= 0
            or event_type not in {"admin_inbox", "vip_inquiry"}
        ):
            raise ValueError("הודעת הניהול אינה תקינה.")
        token = secrets.token_hex(8)
        with self.lock:
            data = self._load()
            data["requests"][token] = {
                "uid": user_id,
                "title": title[:MAX_STORED_TITLE],
                "admin_chat_id": target,
                "message_id": None,
                "status": "pending",
                "created_at": time.time(),
                "event_type": event_type,
                "is_vip": self._is_vip(user_id),
            }
            self._save(data)
        return token

    def bind_external_action(self, token: str, message_id: int) -> None:
        with self.lock:
            data = self._load()
            record = data["requests"].get(token)
            if record is not None and record.get("event_type") in {
                "admin_inbox", "vip_inquiry",
            }:
                record["message_id"] = message_id
                self._save(data)

    def discard_external_action(self, token: str) -> None:
        with self.lock:
            data = self._load()
            record = data["requests"].get(token)
            if record is not None and record.get("event_type") in {
                "admin_inbox", "vip_inquiry",
            }:
                data["requests"].pop(token, None)
                self._save(data)

    def _callback_feedback(self, call, text, *, show_alert=False):
        if not getattr(call, "_ticket_answered", False):
            self.bot.answer_callback_query(call.id, text=text, show_alert=show_alert)
            call._ticket_answered = True
            return
        # Telegram allows one answer: render the result on the existing card
        # rather than reusing an already-answered query or adding group spam.
        message = call.message
        options = {
            "chat_id": message.chat.id, "message_id": message.message_id,
            "parse_mode": "HTML", "reply_markup": getattr(message, "reply_markup", None),
        }
        try:
            if getattr(message, "text", None):
                body = html.escape(message.text[:3500]) + "\n\n" + html.escape(text)
                self.bot.edit_message_text(body, disable_web_page_preview=True, **options)
            elif getattr(message, "caption", None):
                body = html.escape(message.caption[:800]) + "\n\n" + html.escape(text)
                self.bot.edit_message_caption(caption=body, **options)
        except Exception as exc:
            logging.warning("Ticket result display failed (%s).", type(exc).__name__)

    def handle_callback(self, call) -> bool:
        call._ticket_answered = False
        try:
            return self._handle_callback(call)
        except Exception:
            logging.exception("Management ticket action failed.")
            try:
                self._callback_feedback(
                    call, "לא ניתן לבצע את הפעולה כרגע. נסה שוב.", show_alert=True,
                )
            except Exception:
                logging.warning("Could not acknowledge the failed ticket action.")
            return True

    def _handle_callback(self, call) -> bool:
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        if message is None:
            self._callback_feedback(call, "הבקשה אינה תקינה.", show_alert=True)
            return True
        if not is_admin(actor):
            self._callback_feedback(call, "אין הרשאה.", show_alert=True)
            return True
        try:
            prefix, action, token = (call.data or "").split(":")
            if prefix != "missing_content" or action not in {"uploaded", "unavailable"}:
                raise ValueError
        except ValueError:
            self._callback_feedback(call, "הפעולה אינה זמינה.", show_alert=True)
            return True
        # Keep the callback available for the actual success/failure alert.
        # Validate against the durable card's actual destination, not one
        # legacy config alias (or a subsequently changed destination).
        target = message.chat.id

        with self.lock:
            data = self._load()
            request = data["requests"].get(token)
            if (
                request is None
                or request.get("admin_chat_id") != target
                or (
                    request.get("message_id") is not None
                    and request["message_id"] != message.message_id
                )
            ):
                self._callback_feedback(call, "בקשת התוכן לא נמצאה.", show_alert=True)
                return True
            if token in self._resolving:
                self._callback_feedback(call, "העדכון למשתמש נמצא בשליחה.", show_alert=True)
                return True
            if request.get("status") not in {"pending", "processing"}:
                self._callback_feedback(call, "הבקשה כבר טופלה.", show_alert=True)
                return True
            # In-process guard only: never persist completion or a claim before
            # Telegram delivery, and allow abandoned claims from older builds.
            self._resolving.add(token)
            uid, title = request["uid"], request["title"]

        resolved_status = "uploaded" if action == "uploaded" else "unavailable"
        try:
            try:
                send_status_notification(
                    self.bot, self.config_file, uid, resolved_status, title,
                )
            except Exception as exc:
                logging.warning(
                    "Missing-content response could not reach the user (%s; code=%s).",
                    type(exc).__name__, getattr(exc, "error_code", None),
                )
                status_feedback(self.bot, call, DELIVERY_ERROR, error=True)
                return True
            with self.lock:
                data = self._load()
                current = data["requests"].get(token)
                if current is not None:
                    current["status"] = resolved_status
                    current["resolved_by"] = actor
                    current["resolved_at"] = time.time()
                    current.pop("processing_at", None)
                    self._save(data)
        finally:
            with self.lock:
                self._resolving.discard(token)
        try:
            markup = getattr(message, "reply_markup", None) or self._keyboard(uid, token)
            self.engine.track(
                target, message.message_id, uid, markup,
            )
            message.reply_markup = markup
        except Exception as exc:
            logging.warning("Resolved content request keyboard could not be refreshed (%s).", type(exc).__name__)
        status_feedback(
            self.bot, call, "✅ התוכן הועלה — העדכון נשלח למשתמש." if action == "uploaded"
            else "✅ סומן כלא זמין — העדכון נשלח למשתמש.",
        )
        return True
