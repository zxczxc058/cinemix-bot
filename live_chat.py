"""Persistent one-at-a-time admin chat sessions with a queued request inbox."""

from __future__ import annotations

import copy
import html
import json
import logging
import re
import secrets
import threading
import time
from uuid import uuid4
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_notifications import (
    resolve_user_management_chat, delivery_retry_delay, controls_fallback_allowed,
    delivery_error_code,
)
from bot_storage import is_admin
from management_config import CONFIG_FILE, load_config, save_json
from support_storage import SUPPORT_SETTINGS_FILE, load_support_settings
from user_notification_card import user_card, broken_link_text


LIVE_CHAT_FILE = Path(__file__).resolve().with_name("live_chat_state.json")
_EMPTY_STATE = {
    "active_session": None,
    "queue": [],
    "requests": {},
    "message_routes": {},
    "delivered_replies": {},
}
_LOCK = threading.RLock()


class LiveChatQueue:
    """Relay live admin replies safely and queue other users' requests."""

    def __init__(
        self,
        bot,
        config_file=CONFIG_FILE,
        state_file=LIVE_CHAT_FILE,
        support_settings_file=SUPPORT_SETTINGS_FILE,
        moderation_markup=None,
        vip_store=None,
        request_delivery=None,
        delivery_allowed=None,
    ):
        self.bot = bot
        self.config_file = Path(config_file)
        self.state_file = Path(state_file)
        self.support_settings_file = Path(support_settings_file)
        self.moderation_markup = moderation_markup
        self.request_delivery = request_delivery
        self.delivery_allowed = delivery_allowed
        self._stop = threading.Event()
        self._worker = None
        self._status_in_flight = set()
        if vip_store is None:
            from vip_access import VIPSettingsStore

            vip_store = VIPSettingsStore(
                self.config_file.with_name("vip_settings.json"),
            )
        self.vip_store = vip_store

    def load(self) -> dict:
        with _LOCK:
            try:
                from persistent_json import read_json_text
                data = json.loads(read_json_text(self.state_file))
            except FileNotFoundError:
                data = copy.deepcopy(_EMPTY_STATE)
                save_json(self.state_file, data)
            except json.JSONDecodeError as exc:
                raise ValueError("קובץ תור השיחות פגום.") from exc
            if not isinstance(data, dict):
                raise ValueError("קובץ תור השיחות חייב להכיל אובייקט JSON.")
            state = copy.deepcopy(_EMPTY_STATE)
            state.update(data)
            if (
                not isinstance(state["queue"], list)
                or not isinstance(state["requests"], dict)
                or not isinstance(state["message_routes"], dict)
                or not isinstance(state["delivered_replies"], dict)
            ):
                raise ValueError("מבנה קובץ תור השיחות אינו תקין.")
            if state["active_session"] is not None and not isinstance(state["active_session"], dict):
                raise ValueError("השיחה הפעילה בקובץ אינה תקינה.")
            return state

    def _save(self, state: dict) -> None:
        with _LOCK:
            save_json(self.state_file, state)

    def update_destination(self, target):
        """Move undelivered work and the live relay, retaining old card controls."""
        if type(target) is not int or target >= 0:
            raise ValueError("Management destination must be a negative chat ID.")
        with _LOCK:
            state = self.load()
            active = state.get("active_session")
            if active:
                active.setdefault("control_chat_id", active["admin_chat_id"])
                active["admin_chat_id"] = target
            for record in state["requests"].values():
                is_live = active and (
                    record["token"] == active["token"]
                    or record.get("active_token") == active["token"]
                )
                if record.get("admin_chat_id") == target:
                    continue
                if is_live:
                    record.setdefault("ticket_chat_id", record["admin_chat_id"])
                    record["admin_chat_id"] = target
                    record.pop("delivery_retry", None)
                elif record.get("status") == "queued" or record.get("delivery_pending"):
                    record["admin_chat_id"] = target
                    record.pop("ticket_chat_id", None)
                    record["header_message_id"] = None
                    record["ticket_message_ids"] = []
                    record["source_text_parts"] = {}
                    record["source_text_last_ids"] = {}
                    record.pop("delivery_retry", None)
            self._save(state)

    def start(self):
        """Resume persisted work; no Telegram polling or user notices here."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._retry_loop, name="admin-delivery-retries", daemon=True,
        )
        self._worker.start()

    def stop(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=5)

    def _retry_loop(self):
        while not self._stop.is_set():
            try:
                self.retry_pending()
            except Exception as exc:
                logging.error("Admin retry worker failed (error=%s).", type(exc).__name__)
            self._stop.wait(1)

    def _due(self, state, record, now):
        retry = record.get("delivery_retry", {})
        return (
            not retry.get("blocked")
            and now >= retry.get("next_attempt_at", 0)
            and now >= state.get("delivery_not_before", 0)
        )

    def retry_pending(self, *, now=None):
        """Retry independently so one broken request cannot block the inbox."""
        now = time.time() if now is None else now
        with _LOCK:
            if self.delivery_allowed is not None and not self.delivery_allowed():
                return
            state = self.load()
            active = state.get("active_session")
            for record in list(state["requests"].values()):
                pending = record.get("delivery_pending")
                if pending is None:
                    if record.get("status") not in {"waiting", "queued"}:
                        continue
                    # Recover waiting work persisted by older bot versions.
                    pending = not isinstance(record.get("header_message_id"), int) or any(
                        source.get("message_id") not in record.get("delivered_source_ids", [])
                        for source in record.get("source_messages", [])
                        if isinstance(source.get("message_id"), int)
                    )
                if not pending or not self._due(state, record, now):
                    continue
                if active and record.get("user_id") != active.get("user_id"):
                    continue
                if self._deliver_request(state, record) and not record.get("delivery_pending"):
                    token = record["token"]
                    if token in state["queue"]:
                        state["queue"].remove(token)
                        self._save(state)

    def retry_blocked(self, token):
        """Explicit operator recovery after correcting destination/permissions."""
        with _LOCK:
            state = self.load()
            record = state["requests"].get(token)
            if record is None or not record.get("delivery_retry", {}).get("blocked"):
                return False
            target = self._target()
            if target is None:
                return False
            if target != record["admin_chat_id"]:
                # Confirmations belong to the old destination only.
                record["admin_chat_id"] = target
                record["header_message_id"] = None
                record["delivered_source_ids"] = []
            record.pop("delivery_retry", None)
            record["delivery_pending"] = True
            self._save(state)
            return True

    def _delivery_failed(self, state, record, exc):
        attempts = record.get("delivery_retry", {}).get("attempts", 0) + 1
        delay = delivery_retry_delay(exc, attempts)
        record["delivery_pending"] = True
        record["delivery_retry"] = {
            "attempts": attempts,
            "blocked": delay is None,
            "next_attempt_at": time.time() + delay if delay is not None else None,
            "error_type": type(exc).__name__,
            "error_code": delivery_error_code(exc),
        }
        if delivery_error_code(exc) == 429:
            state["delivery_not_before"] = record["delivery_retry"]["next_attempt_at"]
        self._save(state)
        logging.error(
            "Admin request delivery %s (error=%s, code=%s, description=%s).%s",
            "requires correction" if delay is None else "scheduled for retry",
            type(exc).__name__, delivery_error_code(exc),
            str(getattr(exc, "description", ""))[:250],
            " Check the configured destination and bot permissions." if delay is None else "",
        )
        return delay is not None

    def _target(self, fallback=None) -> int | None:
        try:
            config = load_config(self.config_file)
            target = config.get("LOG_CHANNEL_ID")
            if target is not None:
                if type(target) is not int or target >= 0:
                    raise ValueError("LOG_CHANNEL_ID must be a negative Telegram chat ID.")
                return target
            target = config.get("ADMIN_CHAT_ID")
            # Keep legacy destinations only as a fallback when no admin group
            # was explicitly configured.
            if target is None:
                target = config.get("LOG_CHAT_ID") or fallback
            if target is None:
                target = load_support_settings(
                    self.support_settings_file,
                )["support_chat_id"]
            return resolve_user_management_chat(target, config_file=self.config_file)
        except FileNotFoundError:
            if fallback is None:
                return None
            try:
                return resolve_user_management_chat(fallback, config_file=self.config_file)
            except Exception as exc:
                logging.error(
                    "Live-chat admin destination is invalid (error=%s).",
                    type(exc).__name__,
                )
                return None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logging.error(
                "Live-chat admin destination could not be resolved (error=%s).",
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _user_fields(user) -> tuple[int, str, str]:
        user_id = getattr(user, "id", None)
        name = " ".join(filter(None, [
            getattr(user, "first_name", ""),
            getattr(user, "last_name", ""),
        ])) or "ללא שם"
        username = getattr(user, "username", None) or "לא מוגדר"
        return user_id, name, username

    def _moderation_keyboard(self, user_id: int):
        if not callable(self.moderation_markup):
            return None
        try:
            return self.moderation_markup(user_id)
        except Exception as exc:
            logging.warning(
                "Could not build live-chat moderation controls (%s).",
                type(exc).__name__,
            )
            return None

    def _is_vip_member(self, user_id: int) -> bool:
        try:
            return self.vip_store.is_member(user_id)
        except Exception as exc:
            logging.warning("VIP live-chat classification failed (%s).", type(exc).__name__)
            return False

    @staticmethod
    def _accept_keyboard(token: str):
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("💬 פתיחת שיחה בלייב", callback_data=f"live:accept:{token}"))
        return keyboard

    @staticmethod
    def _close_keyboard(token: str):
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("🛑 סגירת לייב", callback_data=f"live:close:{token}"))
        return keyboard

    @staticmethod
    def _request_keyboard(token: str, user_id: int, mode: str = "waiting"):
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.row(InlineKeyboardButton(
            "💬 פתח שיחה עם המשתמש", url=f"tg://user?id={user_id}",
        ))
        keyboard.row(
            InlineKeyboardButton(
                "✅ הועלה", callback_data=f"live:resolve:uploaded:{token}",
            ),
            InlineKeyboardButton(
                "❌ לא זמין", callback_data=f"live:resolve:unavailable:{token}",
            ),
        )
        keyboard.row(
            InlineKeyboardButton(
                "🔊 השתק משתמש", callback_data=f"mod:mute:{user_id}",
            ),
            InlineKeyboardButton(
                "🚫 חסום משתמש", callback_data=f"mod:ban:{user_id}",
            ),
        )
        if mode in {"waiting", "closed"}:
            keyboard.add(InlineKeyboardButton(
                "💬 פתיחת שיחה בלייב", callback_data=f"live:accept:{token}",
            ))
        elif mode == "active":
            keyboard.add(InlineKeyboardButton(
                "🛑 סגירת לייב", callback_data=f"live:close:{token}",
            ))
        return keyboard

    def _route_key(self, chat_id: int, message_id: int) -> str:
        return f"{chat_id}:{message_id}"

    @staticmethod
    def _new_request(
        user_id: int,
        name: str,
        username: str,
        category: str,
        summary: str,
        target: int,
        source=None,
    ) -> dict:
        return {
            "token": secrets.token_hex(8),
            "user_id": user_id,
            "name": name[:180],
            "username": username[:100],
            "category": category[:120],
            "summary": summary[:1000],
            "notification_type": None,
            "admin_chat_id": target,
            "created_at": int(time.time()),
            "status": "waiting",
            "header_message_id": None,
            "source_messages": [source] if source else [],
            "delivered_source_ids": [],
            "delivery_pending": True,
        }

    def receive_message(
        self, message, *, category="פנייה כללית", target=None,
        notification_type=None,
    ) -> bool:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        if (
            user is None or getattr(user, "is_bot", False) or chat is None
            or getattr(chat, "type", None) != "private"
        ):
            return False
        user_id, name, username = self._user_fields(user)
        if not isinstance(user_id, int):
            return False
        destination = self._target(fallback=target)
        if destination is None:
            return False
        source = {
            "chat_id": chat.id,
            "message_id": message.message_id,
        }
        content = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        summary = content
        source["summary"] = summary
        source["is_text"] = isinstance(getattr(message, "text", None), str)
        with _LOCK:
            state = self.load()
            active = state.get("active_session")
            message.live_chat_delivery = bool(active and active.get("user_id") == user_id)
            for previous in state["requests"].values():
                if any(
                    old.get("chat_id") == source["chat_id"]
                    and old.get("message_id") == source["message_id"]
                    for old in previous.get("source_messages", [])
                ):
                    return not previous.get("delivery_retry", {}).get("blocked", False)
            if active and active.get("user_id") == user_id:
                delivered = self._deliver_active_message(
                    state, active, user_id, name, source, username=username,
                )
                if delivered:
                    self._active_receipt(user_id)
                return delivered
            if active:
                record = self._queued_request(
                    state, user_id, name, username, category, summary, destination,
                )
                if source not in record["source_messages"]:
                    record["source_messages"].append(source)
                if notification_type:
                    record["notification_type"] = notification_type
                self._save(state)
                return True
            record = self._waiting_request(
                state, user_id, name, username, category, summary, destination,
            )
            if source not in record["source_messages"]:
                record["source_messages"].append(source)
                record["delivery_pending"] = True
            if notification_type:
                record["notification_type"] = notification_type
            self._save(state)
            if not self._due(state, record, time.time()):
                return not record.get("delivery_retry", {}).get("blocked", False)
            return self._deliver_request(state, record)

    def submit_callback_request(
        self, user_id: int, name: str, username: str | None, *,
        context: str, category="פנייה לתמיכה", target=None,
        notification_type: str | None = None,
        source_message=None,
    ) -> bool:
        if notification_type not in {
            None, "vip_inquiry", "missing_content", "link_error", "vip_purchase",
        }:
            raise ValueError("סוג הודעת הבקשה אינו נתמך.")
        destination = self._target(fallback=target)
        if destination is None or type(user_id) is not int or user_id <= 0:
            return False
        source = None
        if source_message is not None:
            if source_message.chat.type != "private" or source_message.from_user.id != user_id:
                return False
            source = {
                "chat_id": source_message.chat.id,
                "message_id": source_message.message_id,
                "summary": (getattr(source_message, "text", None)
                            or getattr(source_message, "caption", None) or ""),
                "is_text": isinstance(getattr(source_message, "text", None), str),
            }
        with _LOCK:
            state = self.load()
            if source is not None:
                for previous in state["requests"].values():
                    if source in previous.get("source_messages", []):
                        return not previous.get("delivery_retry", {}).get("blocked", False)
            active = state.get("active_session")
            if active and active.get("user_id") == user_id:
                if source is not None:
                    return self._deliver_active_message(
                        state, active, user_id, name, source, username=username or "",
                    )
                request_text = {
                    "chat_id": user_id,
                    "message_id": None,
                    "summary": f"{category}: {context}"[:1000],
                    "event_id": uuid4().hex,
                }
                return self._deliver_active_message(
                    state, active, user_id, name, request_text,
                )
            if active:
                record = self._new_request(
                    user_id, name, username or "לא מוגדר",
                    category, context, destination,
                )
                record["status"] = "queued"
                state["requests"][record["token"]] = record
                state["queue"].append(record["token"])
                record["notification_type"] = notification_type
                record["summary"] = context[:1000]
                record["is_vip"] = self._is_vip_member(user_id)
                if source is not None and source not in record["source_messages"]:
                    record["source_messages"].append(source)
                    record["delivery_pending"] = True
                self._save(state)
                return True
            record = self._new_request(
                user_id, name, username or "לא מוגדר",
                category, context, destination,
            )
            state["requests"][record["token"]] = record
            record["summary"] = context[:1000]
            record["notification_type"] = notification_type
            record["is_vip"] = self._is_vip_member(user_id)
            if source is not None and source not in record["source_messages"]:
                record["source_messages"].append(source)
            record["delivery_pending"] = True
            self._save(state)
            if not self._due(state, record, time.time()):
                return not record.get("delivery_retry", {}).get("blocked", False)
            return self._deliver_request(state, record)

    @staticmethod
    def _queued_request(
        state, user_id, name, username, category, summary, target,
    ) -> dict:
        for token in state["queue"]:
            record = state["requests"].get(token)
            if (
                record and record.get("user_id") == user_id
                and record.get("admin_chat_id") == target
            ):
                record["updated_at"] = int(time.time())
                if summary and not record.get("summary"):
                    record["summary"] = summary[:1000]
                return record
        record = LiveChatQueue._new_request(
            user_id, name, username, category, summary, target,
        )
        state["requests"][record["token"]] = record
        state["queue"].append(record["token"])
        return record

    @staticmethod
    def _waiting_request(
        state, user_id, name, username, category, summary, target,
    ) -> dict:
        for record in state["requests"].values():
            if (
                record.get("user_id") == user_id
                and record.get("admin_chat_id") == target
                and record.get("status") == "waiting"
                and not record.get("delivery_retry", {}).get("blocked")
            ):
                if summary and not record.get("summary"):
                    record["summary"] = summary[:1000]
                return record
        record = LiveChatQueue._new_request(
            user_id, name, username, category, summary, target,
        )
        state["requests"][record["token"]] = record
        return record

    def _request_header_text(self, record: dict) -> str:
        summary = record.get("summary") or "קובץ מדיה"
        if record.get("notification_type") == "link_error":
            summary = broken_link_text(
                record["user_id"], record.get("name"), record.get("username"),
                summary, self._is_vip_member(record["user_id"]),
            )
            return user_card(
                record["user_id"], record.get("name"),
                record.get("username"), summary,
                is_vip=self._is_vip_member(record["user_id"]),
            )
        body = self._request_header_body(record).strip()
        return (
            user_card(record["user_id"], record.get("name"),
                      record.get("username"), summary,
                      is_vip=self._is_vip_member(record["user_id"]))
            + "\n" + body
        )

    def _request_user_card(self, record: dict) -> str:
        return user_card(
            record["user_id"], record.get("name"), record.get("username"),
            is_vip=self._is_vip_member(record["user_id"]),
        )

    def _request_header_body(self, record: dict) -> str:
        summary = record.get("summary") or "המשתמש שלח הודעה או מדיה."
        is_vip = self._is_vip_member(record["user_id"])
        user_field = ""
        notification_type = record.get("notification_type")
        if notification_type == "vip_purchase":
            header = "🛒 פרטים על רכישת מנוי VIP"
            if is_vip:
                header = f"⭐ {header} - מנוי VIP! ⭐"
            return f"<b>{header}</b>\n{user_field}"
        if notification_type == "vip_inquiry":
            header = (
                "⭐ 👑 בירור על מנוי VIP! ⭐"
                if is_vip
                else "👑 בירור על מנוי VIP!"
            )
            return (
                f"<b>{header}</b>\n"
                f"- תוכן מבוקש: {html.escape(str(summary))}\n"
                f"{user_field}"
            )
        if notification_type == "missing_content":
            header = (
                "⭐ בקשת תוכן חדשה - VIP! ⭐"
                if is_vip
                else "🔍 בקשת תוכן חדשה!"
            )
            return (
                f"<b>{header}</b>\n"
                f"🎬 תוכן מבוקש: {html.escape(str(summary))}\n"
                f"{user_field}"
            )
        if notification_type == "link_error":
            header = (
                "⭐ 🚨 דיווח על תקלה בקישור דרייב/מדיה - מנוי VIP! ⭐"
                if is_vip
                else "🚨 דיווח על תקלה בקישור דרייב/מדיה!"
            )
            return (
                f"<b>{header}</b>\n"
                f"- תוכן: {html.escape(str(summary))}\n"
                f"{user_field}"
            )
        header = (
            "⭐ בקשת תוכן חדשה - VIP! ⭐"
            if is_vip
            else "📩 בקשת תמיכה חדשה"
        )
        return (
            f"<b>{header}</b>\n"
            f"{user_field}\n"
            f"📁 <b>קטגוריה:</b> {html.escape(str(record.get('category', 'פנייה כללית')))}"
        )

    def _deliver_request(self, state: dict, record: dict) -> bool:
        active = state.get("active_session")
        if active:
            if record.get("user_id") != active.get("user_id"):
                record["status"] = "queued"
                record["delivery_pending"] = True
                if record["token"] not in state["queue"]:
                    state["queue"].append(record["token"])
                self._save(state)
                return True
            record["active_token"] = active["token"]
            record["admin_chat_id"] = active["admin_chat_id"]
            if not record.get("source_messages") and record.get("summary"):
                record["source_messages"] = [{
                    "chat_id": record["user_id"], "message_id": None,
                    "summary": record["summary"], "event_id": uuid4().hex,
                }]
            self._save(state)
        target = record.get("admin_chat_id")
        if not isinstance(target, int):
            return False
        token = record["token"]
        if record.get("active_token"):
            return self._deliver_live_sources(state, record)
        try:
            if self.request_delivery is not None:
                self.request_delivery(state, record)
                record["delivery_pending"] = False
                record.pop("delivery_retry", None)
                self._save(state)
                return True
            if not isinstance(record.get("header_message_id"), int):
                active_token = record.get("active_token")
                if active_token:
                    identity = html.escape(record["name"])
                    vip = self._is_vip_member(record["user_id"])
                    text = (
                        "⭐ 💬 <b>הודעה חדשה בשיחה הפעילה - מנוי VIP!</b>\n"
                        if vip else "💬 <b>הודעה חדשה בשיחה הפעילה</b>\n"
                    )
                    text += (
                        f"👤 משתמש: {'⭐ ' if vip else ''}{identity} "
                        f"(ID: <code>{record['user_id']}</code>)"
                        + (" - סטטוס: VIP · ⭐ מנוי VIP ⭐" if vip else "")
                    )
                    username = str(record.get("username") or "לא מוגדר")
                    if username != "לא מוגדר":
                        username = "@" + username.lstrip("@")
                    text += f"\n👤 שם משתמש: {html.escape(username)}"
                    if record.get("summary"):
                        text += f"\n📝 {html.escape(record['summary'])}"
                    keyboard = self._request_keyboard(
                        active_token, record["user_id"], "active",
                    )
                    sources = record.get("source_messages", [])
                    display_summary = record["summary"]
                    if sources and sources[0].get("summary"):
                        display_summary = sources[0]["summary"][:1400]
                    text = (
                        user_card(record["user_id"], record["name"],
                                  record.get("username"), display_summary) + "\n"
                        + text.split("\n", 1)[0]
                    )
                else:
                    display = dict(record)
                    sources = record.get("source_messages", [])
                    if sources and sources[0].get("summary"):
                        display["summary"] = sources[0]["summary"][:1400]
                    text = self._request_header_text(display)
                    keyboard = self._request_keyboard(token, record["user_id"], "waiting")
                header = self.bot.send_message(
                    target,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=keyboard,
                )
                record["header_message_id"] = header.message_id
                record.setdefault("ticket_message_ids", []).append(header.message_id)
                sources = record.get("source_messages", [])
                if sources and sources[0].get("is_text"):
                    record["header_source_id"] = sources[0]["message_id"]
                    source_key = str(sources[0]["message_id"])
                    record.setdefault("source_text_parts", {})[source_key] = 1
                    record.setdefault("source_text_last_ids", {})[source_key] = header.message_id
                state["message_routes"][self._route_key(target, header.message_id)] = {
                    "user_id": record["user_id"],
                    "request_token": active_token or token,
                }
                self._save(state)
                engine = getattr(self.bot, "restriction_service", None)
                if engine is not None:
                    try:
                        engine.track(
                            target, header.message_id, record["user_id"],
                            keyboard,
                        )
                    except Exception as exc:
                        logging.warning(
                            "Live-chat action controls could not be tracked (%s).",
                            type(exc).__name__,
                        )
            delivered = set(record.get("delivered_source_ids", []))
            for source in record.get("source_messages", []):
                source_id = source.get("message_id")
                if not isinstance(source_id, int) or source_id in delivered:
                    continue
                # Copies must not depend on an older header still existing.
                # Each original message has its own durable reply route and
                # complete action controls.
                markup = self._request_keyboard(
                    record.get("active_token") or token,
                    record["user_id"],
                    "active" if record.get("active_token") else "closed",
                )
                options = {"reply_markup": markup}
                if (source.get("is_text") and source_id == record.get("header_source_id")
                        and len(source.get("summary") or "") <= 1400):
                    delivered.add(source_id)
                    record["delivered_source_ids"] = sorted(delivered)
                    self._save(state)
                    continue
                if source.get("is_text"):
                    from types import SimpleNamespace
                    copied = SimpleNamespace(message_id=self._send_text_source(
                        state, record, source, markup,
                    ))
                else:
                    options["caption"] = user_card(
                        record["user_id"], record.get("name"),
                        record.get("username"),
                        str(source.get("summary") or "קובץ מדיה")[:300],
                        is_vip=self._is_vip_member(record["user_id"]),
                    )
                    options["parse_mode"] = "HTML"
                    send = lambda **kwargs: self.bot.copy_message(
                        target, source["chat_id"], source_id, **kwargs,
                    )
                    try:
                        copied = send(**options)
                    except Exception as exc:
                        if not controls_fallback_allowed(exc):
                            raise
                        logging.warning(
                            "Live-chat copy with controls failed (%s); retrying without controls.",
                            type(exc).__name__,
                        )
                        options.pop("reply_markup", None)
                        copied = send(**options)
                delivered.add(source_id)
                if not source.get("is_text"):
                    record.setdefault("ticket_message_ids", []).append(copied.message_id)
                record["delivered_source_ids"] = sorted(delivered)
                state["message_routes"][self._route_key(target, copied.message_id)] = {
                    "user_id": record["user_id"],
                    "request_token": record.get("active_token") or token,
                }
                self._save(state)
                engine = getattr(self.bot, "restriction_service", None)
                if engine is not None:
                    try:
                        engine.track(target, copied.message_id, record["user_id"], markup)
                    except Exception as exc:
                        logging.warning(
                            "Live-chat copy controls could not be tracked (%s).",
                            type(exc).__name__,
                        )
            if record.get("status") == "queued":
                record["status"] = "waiting"
            record["delivery_pending"] = False
            record.pop("delivery_retry", None)
            self._save(state)
            return True
        except Exception as exc:
            return self._delivery_failed(state, record, exc)

    def _send_text_source(self, state, record, source, markup):
        """Send complete text in bounded cards, checkpointing each delivered part."""
        key = str(source["message_id"])
        text = str(source.get("summary") or record.get("summary") or "הודעה")
        parts = [text[index:index + 1400] for index in range(0, len(text), 1400)]
        progress = record.setdefault("source_text_parts", {})
        last_ids = record.setdefault("source_text_last_ids", {})
        target = record["admin_chat_id"]
        for index in range(progress.get(key, 0), len(parts)):
            payload = user_card(
                record["user_id"], record.get("name"), record.get("username"), parts[index],
                is_vip=self._is_vip_member(record["user_id"]),
            )
            options = dict(parse_mode="HTML", disable_web_page_preview=True,
                           reply_markup=markup)
            try:
                sent = self.bot.send_message(target, payload, **options)
            except Exception as exc:
                if not controls_fallback_allowed(exc):
                    raise
                logging.warning("Card controls failed; retrying without buttons (%s).",
                                type(exc).__name__)
                options.pop("reply_markup")
                sent = self.bot.send_message(target, payload, **options)
            progress[key] = index + 1
            last_ids[key] = sent.message_id
            record.setdefault("ticket_message_ids", []).append(sent.message_id)
            state["message_routes"][self._route_key(target, sent.message_id)] = {
                "user_id": record["user_id"],
                "request_token": record.get("active_token") or record["token"],
            }
            self._save(state)
            engine = getattr(self.bot, "restriction_service", None)
            if engine is not None:
                try:
                    engine.track(target, sent.message_id, record["user_id"],
                                 options.get("reply_markup"))
                except Exception as exc:
                    logging.warning("Delivered card controls could not be tracked (%s).",
                                    type(exc).__name__)
        return last_ids[key]

    def is_active_user(self, user_id):
        with _LOCK:
            active = self.load().get("active_session")
            return bool(active and active.get("user_id") == user_id)

    def user_status(self, user_id):
        return "Active Chat" if self.is_active_user(user_id) else "Inactive"

    def _deliver_live_sources(self, state, record):
        """Copy originals, preserving media/captions, without cards or receipts."""
        delivered = set(record.get("delivered_source_ids", []))
        events = set(record.get("delivered_event_ids", []))
        try:
            for source in record.get("source_messages", []):
                source_id = source.get("message_id")
                if source_id is None and source.get("event_id"):
                    event_id = source["event_id"]
                    if event_id in events:
                        continue
                    sent = self.bot.send_message(
                        record["admin_chat_id"], source.get("summary") or "הודעה",
                        reply_markup=self._close_keyboard(record["active_token"]),
                    )
                    state["message_routes"][self._route_key(
                        record["admin_chat_id"], sent.message_id,
                    )] = {"user_id": record["user_id"],
                          "request_token": record["active_token"]}
                    events.add(event_id)
                    record["delivered_event_ids"] = sorted(events)
                    self._save(state)
                    continue
                if not isinstance(source_id, int) or source_id in delivered:
                    continue
                copied = self.bot.copy_message(
                    record["admin_chat_id"], source["chat_id"], source_id,
                    reply_markup=self._close_keyboard(record["active_token"]),
                )
                state["message_routes"][self._route_key(
                    record["admin_chat_id"], copied.message_id,
                )] = {"user_id": record["user_id"],
                      "request_token": record["active_token"]}
                delivered.add(source_id)
                record["delivered_source_ids"] = sorted(delivered)
                self._save(state)
            record["delivery_pending"] = False
            record.pop("delivery_retry", None)
            self._save(state)
            return True
        except Exception as exc:
            return self._delivery_failed(state, record, exc)

    def _deliver_active_message(
        self, state: dict, active: dict, user_id: int, name: str, source: dict,
        username: str = "",
    ) -> bool:
        target = active.get("admin_chat_id")
        if not isinstance(target, int):
            return False
        record = state["requests"].get(active["token"])
        if record is None:
            return False
        if source not in record["source_messages"]:
            record["source_messages"].append(source)
        record["active_token"] = active["token"]
        record["delivery_pending"] = True
        self._save(state)
        if not self._due(state, record, time.time()):
            return True
        return self._deliver_request(state, record)

    def _active_receipt(self, user_id):
        """A short, private receipt; never change ticket or live-relay state."""
        try:
            sent = self.bot.send_message(
                user_id, "📩 פנייתך הועברה בהצלחה לצווות הניהול, נשיב לך בהקדם!",
            )
            def remove():
                try:
                    self.bot.delete_message(chat_id=user_id, message_id=sent.message_id)
                except Exception:
                    pass
            timer = threading.Timer(5, remove)
            timer.daemon = True
            timer.start()
        except Exception:
            logging.exception("Could not send private live-chat receipt.")

    def register_handlers(self) -> None:
        self.bot.callback_query_handler(
            func=lambda call: bool(call.data) and call.data.startswith("live:"),
        )(self.handle_callback)

    def _set_request_controls(self, state, record, mode, clicked_id, clicked_markup=None):
        """Update ticket cards only; leave plain live-chat messages clean."""
        ids = set(record.get("ticket_message_ids", []))
        # Before any live relay, the mapped messages are all ticket cards.
        # Recover these IDs for waiting tickets from older builds, but do not
        # guess card identities after a legacy record has relayed live messages.
        if not record.get("active_token"):
            prefix = f"{record.get('ticket_chat_id', record['admin_chat_id'])}:"
            for key, route in state["message_routes"].items():
                if (key.startswith(prefix) and key[len(prefix):].isdigit()
                        and route.get("request_token") == record["token"]
                        and route.get("user_id") == record["user_id"]):
                    ids.add(int(key[len(prefix):]))
        ids.update((record.get("header_message_id"), clicked_id))
        ids.discard(None)
        record["ticket_message_ids"] = sorted(ids)
        self._save(state)
        chat_id = record.get("ticket_chat_id", record["admin_chat_id"])
        clicked_updated = False
        for message_id in sorted(ids, key=lambda mid: (mid != clicked_id, mid)):
            base = clicked_markup if message_id == clicked_id else None
            base = base or getattr(self.bot, "markup_cache", {}).get((chat_id, message_id))
            engine = getattr(self.bot, "restriction_service", None)
            if base is None and engine is not None:
                saved = engine.events.get("messages", {}).get(
                    self._route_key(chat_id, message_id), {},
                ).get("markup")
                if saved:
                    base = InlineKeyboardMarkup.de_json(saved)
            markup = self._request_keyboard(record["token"], record["user_id"], mode)
            if base is not None:
                updated = InlineKeyboardMarkup.de_json(base.to_dict())
                live_button = markup.keyboard[-1][0]
                found = False
                for row in updated.keyboard:
                    for index, button in enumerate(row):
                        if button.callback_data in {
                            f"live:accept:{record['token']}", f"live:close:{record['token']}",
                        }:
                            row[index] = live_button
                            found = True
                if not found:
                    updated.row(live_button)
                required = {b.callback_data: b for row in markup.keyboard for b in row
                            if b.callback_data and not b.callback_data.startswith("live:accept:")
                            and not b.callback_data.startswith("live:close:")}
                present = {b.callback_data for row in updated.keyboard for b in row}
                for data, button in required.items():
                    # Preserve dynamic unban/unmute variants instead of adding duplicates.
                    if data.startswith("mod:") and any(
                        p and p.startswith("mod:") and p.rsplit(":", 1)[-1] == str(record["user_id"])
                        and (("ban" in p) == ("ban" in data)) for p in present
                    ):
                        continue
                    if data not in present:
                        updated.row(button)
                if not any(b.url == f"tg://user?id={record['user_id']}"
                           for row in updated.keyboard for b in row):
                    updated.keyboard.insert(0, markup.keyboard[0])
                markup = updated
            try:
                self.bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id,
                    reply_markup=markup,
                )
                if message_id == clicked_id:
                    clicked_updated = True
                if engine is not None:
                    engine.track(chat_id, message_id, record["user_id"], markup)
            except Exception as exc:
                if "message is not modified" in str(getattr(exc, "description", "")).lower():
                    if message_id == clicked_id:
                        clicked_updated = True
                else:
                    self._log_error("keyboard edit", exc)
                    if message_id == clicked_id:
                        # Repair the control the admin actually clicked without
                        # waiting for edits of historical/deleted cards.
                        return False
        return clicked_updated

    @staticmethod
    def _log_error(operation, exc):
        # Never log transport URLs (which can contain the bot token).
        description = re.sub(
            r"(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}", "[redacted]",
            str(getattr(exc, "description", "")),
        )[:250]
        logging.warning("Live-chat %s failed (%s; code=%s; description=%s).",
                        operation, type(exc).__name__,
                        getattr(exc, "error_code", None), description)

    def _repair_controls(self, state, record, mode, clicked_id, markup=None):
        if self._set_request_controls(state, record, mode, clicked_id, markup):
            previous = record.get("replacement_control")
            if previous and previous.get("message_id") == clicked_id:
                previous["mode"] = mode
                self._save(state)
            return
        target = self._target() or record["admin_chat_id"]
        previous = record.get("replacement_control", {})
        if (previous.get("chat_id") == target and previous.get("mode") == mode
                and previous.get("message_id") != clicked_id):
            return  # Do not create duplicate panels from repeated stale-card clicks.
        keyboard = self._request_keyboard(record["token"], record["user_id"], mode)
        if self.moderation_markup is not None:
            keyboard = self.moderation_markup(record["user_id"], keyboard)
        sent = self.bot.send_message(
            target, f"💬 ניהול שיחה עם משתמש {record['user_id']}",
            reply_markup=keyboard,
        )
        record.update(
            ticket_chat_id=target, header_message_id=sent.message_id,
            ticket_message_ids=[sent.message_id],
            replacement_control={"chat_id": target, "message_id": sent.message_id, "mode": mode},
        )
        state["message_routes"][self._route_key(target, sent.message_id)] = {
            "user_id": record["user_id"], "request_token": record["token"],
        }
        active = state.get("active_session")
        if active and active["token"] == record["token"]:
            active.update(control_chat_id=target, request_header_id=sent.message_id)
        self._save(state)
        engine = getattr(self.bot, "restriction_service", None)
        if engine is not None:
            engine.track(target, sent.message_id, record["user_id"], keyboard)

    def handle_callback(self, call) -> None:
        """Registered live: callback boundary; never fail silently."""
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        try:
            authorized = isinstance(actor, int) and is_admin(actor)
        except Exception as exc:
            self._log_error("callback authorization", exc)
            authorized = False
        chat_type = getattr(getattr(message, "chat", None), "type", None)
        parts = (call.data or "").split(":")
        if (
            authorized and chat_type in {"group", "supergroup"}
            and len(parts) == 4 and parts[:2] == ["live", "resolve"]
            and parts[2] in {"uploaded", "unavailable"}
        ):
            # These callbacks need one result popup, particularly on failure.
            # Live open/close callbacks retain their immediate acknowledgement.
            try:
                self._handle_callback(call)
            except Exception as exc:
                self._log_error("ticket-status action", exc)
                from ticket_status_delivery import status_feedback
                status_feedback(
                    self.bot, call, "לא ניתן לבצע את הפעולה כרגע. נסה שוב.", error=True,
                )
            return
        if not authorized:
            notice = "הרשאה זו שמורה למנהלים בלבד!"
        elif chat_type not in {"group", "supergroup"}:
            notice = "שיחת לייב דורשת קבוצת ניהול או סופר-קבוצה, ולא ערוץ."
        else:
            notice = "בקשת הלייב התקבלה, מעבד את הפעולה…"
        try:
            self.bot.answer_callback_query(
                call.id, text=notice, show_alert=not authorized or chat_type not in {"group", "supergroup"},
            )
        except Exception as exc:
            self._log_error("callback acknowledgement", exc)
        if not authorized or chat_type not in {"group", "supergroup"}:
            return
        try:
            self._handle_callback(call)
        except Exception as exc:
            self._log_error("callback action", exc)
            self._admin_notice(message.chat.id, "❌ הפעולה לא הושלמה. בדקו את הרשאות הבוט ואת יומן השגיאות.")

    def _handle_callback(self, call) -> None:
        parts = (call.data or "").split(":")
        message = getattr(call, "message", None)
        actor = getattr(getattr(call, "from_user", None), "id", None)
        if (
            len(parts) == 4 and parts[1] == "resolve"
            and parts[2] in {"uploaded", "unavailable"}
            and message is not None and isinstance(actor, int)
            and is_admin(actor)
            and getattr(message.chat, "type", None) in {"group", "supergroup"}
        ):
            self._resolve_request_status(
                message, parts[2], parts[3], call=call,
            )
            return
        if (
            len(parts) != 3 or message is None or not isinstance(actor, int)
            or not is_admin(actor)
            or getattr(message.chat, "type", None) not in {"group", "supergroup"}
        ):
            return
        action, token = parts[1], parts[2]
        with _LOCK:
            state = self.load()
            record = state["requests"].get(token)
            if action == "accept":
                active = state.get("active_session")
                route = state["message_routes"].get(
                    self._route_key(message.chat.id, message.message_id),
                )
                belongs = bool(record and message.chat.id == record.get("ticket_chat_id", record.get("admin_chat_id")) and (
                    message.message_id == record.get("header_message_id")
                    or (route and route.get("request_token") == token
                        and route.get("user_id") == record.get("user_id"))
                ))
                if (
                    record is not None
                    and active is not None
                    and active.get("user_id") == record.get("user_id")
                    and belongs
                ):
                    if active.get("token") == token:
                        self._repair_controls(
                            state, record, "active", message.message_id,
                            getattr(message, "reply_markup", None),
                        )
                    self._admin_notice(message.chat.id, "השיחה עם המשתמש הזה כבר פעילה.")
                    return
                if (
                    not belongs or record.get("status") not in {"waiting", "closed"}
                ):
                    self._admin_notice(message.chat.id, "הבקשה כבר אינה זמינה לקבלה.")
                    return
                if active and active.get("token") != token:
                    self._admin_notice(message.chat.id, "יש כבר שיחה פעילה. סגרו אותה לפני קבלת בקשה נוספת.")
                    return
                record["status"] = "active"
                record.pop("closed_at", None)
                record.setdefault("ticket_chat_id", record["admin_chat_id"])
                record["admin_chat_id"] = self._target() or record["admin_chat_id"]
                active = {
                    "token": token,
                    "user_id": record["user_id"],
                    "admin_chat_id": record["admin_chat_id"],
                    "opened_at": int(time.time()),
                    "request_header_id": record["header_message_id"],
                    "control_chat_id": record["ticket_chat_id"],
                }
                state["active_session"] = active
                state.setdefault("user_statuses", {})[str(record["user_id"])] = "Active Chat"
                self._save(state)
                self._admin_notice(message.chat.id, "✅ שיחת הלייב הופעלה! הודעות המשתמש יגיעו ישירות לשיחה.")
                try:
                    self._repair_controls(
                        state, record, "active", message.message_id,
                        getattr(message, "reply_markup", None),
                    )
                except Exception as exc:
                    self._log_error("active controls recovery", exc)
                    self._admin_notice(message.chat.id, "⚠️ השיחה פעילה, אך הכפתור לא עודכן. בדקו הרשאות עריכת הודעות.")
            elif action == "close":
                active = state.get("active_session")
                route = state["message_routes"].get(
                    self._route_key(message.chat.id, message.message_id),
                )
                if (
                    not active or active.get("token") != token
                    or message.chat.id not in {
                        active.get("admin_chat_id"),
                        active.get("control_chat_id", active.get("admin_chat_id")),
                    }
                    or not (
                        (message.chat.id == active.get("control_chat_id", active.get("admin_chat_id"))
                         and message.message_id == active.get("request_header_id"))
                        or (
                            route and route.get("request_token") == token
                            and route.get("user_id") == active.get("user_id")
                        )
                    )
                ):
                    self._admin_notice(message.chat.id, "לא נמצאה שיחה פעילה לסגירה.")
                    return
                state["active_session"] = None
                state.setdefault("user_statuses", {})[str(active["user_id"])] = "Inactive"
                if record:
                    record["status"] = "closed"
                    record["closed_at"] = int(time.time())
                self._save(state)
                if record:
                    card_id = (message.message_id
                               if message.message_id in record.get("ticket_message_ids", [])
                               and message.chat.id == record.get("ticket_chat_id", record["admin_chat_id"])
                               else active["request_header_id"])
                    try:
                        self._repair_controls(
                            state, record, "closed", card_id,
                            getattr(message, "reply_markup", None)
                            if (card_id == message.message_id
                                and message.chat.id == record.get("ticket_chat_id", record["admin_chat_id"]))
                            else None,
                        )
                    except Exception as exc:
                        self._log_error("closed controls recovery", exc)
                try:
                    from system_messages import send_system_message
                    if load_config(self.config_file).get("chat_close_notice_enabled", True):
                        send_system_message(
                            self.bot, active["user_id"], "chat_closed", self.config_file,
                        )
                except Exception as exc:
                    logging.warning("Chat farewell could not be sent (%s).", type(exc).__name__)
                self._flush_queue(state)

    def _resolve_request_status(self, message, result: str, token: str, *, call=None) -> None:
        """Resolve a queued/admin request without disturbing any live session."""
        from ticket_status_delivery import DELIVERY_ERROR, send_status_notification, status_feedback

        def feedback(text, error=False):
            if call is not None:
                status_feedback(self.bot, call, text, error=error)
            else:
                self._admin_notice(message.chat.id, text)

        key = (token, message.chat.id, message.message_id)
        with _LOCK:
            state = self.load()
            record = state["requests"].get(token)
            route = state["message_routes"].get(
                self._route_key(message.chat.id, message.message_id),
            )
            is_header = (
                record is not None
                and record.get("header_message_id") == message.message_id
            )
            is_routed_message = (
                record is not None and route is not None
                and route.get("request_token") == token
                and route.get("user_id") == record.get("user_id")
            )
            if (
                record is None or record.get("admin_chat_id") != message.chat.id
                or not (is_header or is_routed_message)
            ):
                feedback("הבקשה אינה זמינה.", True)
                return
            message_key = str(message.message_id)
            delivery_key = "header" if is_header else message_key
            resolution = (
                record.get("admin_resolution") if is_header
                else record.get("message_resolutions", {}).get(message_key)
            )
            delivered = record.get("delivered_status_updates", {}).get(delivery_key)
            # The old implementation never notified ordinary headers or routed
            # cards. Permit those known false completions to be retried, but
            # preserve idempotence for legacy headers that did notify users.
            legacy_unsent = bool(
                resolution and not delivered and (
                    not is_header
                    or record.get("notification_type") not in {"vip_inquiry", "missing_content"}
                )
            )
            if (
                delivered or (resolution and not legacy_unsent)
            ):
                feedback("הבקשה כבר סומנה כטופלה.", True)
                return
            header_key = (token, "header")
            claim = header_key if is_header else key
            if claim in self._status_in_flight:
                feedback("העדכון למשתמש נמצא בשליחה.", True)
                return
            self._status_in_flight.add(claim)
            uid, title = record["user_id"], record.get("summary") or "קובץ מדיה"
        try:
            try:
                # Every card, including ordinary support and routed media,
                # must notify its owner before any completion is persisted.
                send_status_notification(self.bot, self.config_file, uid, result, title)
            except Exception as exc:
                self._log_error("ticket-status user delivery", exc)
                feedback(DELIVERY_ERROR, True)
                return
            with _LOCK:
                state = self.load()
                record = state["requests"].get(token)
                if record is not None:
                    if is_header:
                        record["admin_resolution"] = result
                    record.setdefault("message_resolutions", {})[message_key] = result
                    record.setdefault("delivered_status_updates", {})[delivery_key] = result
                    actor = (
                        getattr(getattr(call, "from_user", None), "id", None)
                        if call is not None
                        else getattr(getattr(message, "from_user", None), "id", None)
                    )
                    record["resolved_by"] = actor
                    record["resolved_at"] = int(time.time())
                    self._save(state)
        finally:
            with _LOCK:
                self._status_in_flight.discard(claim)
        feedback(
            "✅ התוכן הועלה — העדכון נשלח למשתמש." if result == "uploaded"
            else "✅ סומן כלא זמין — העדכון נשלח למשתמש.",
        )

    def _admin_notice(self, chat_id: int, text: str) -> None:
        try:
            from group_management import delete_later

            sent = self.bot.send_message(chat_id, text)
            delete_later(self.bot, chat_id, sent.message_id, 5)
        except Exception as exc:
            self._log_error("admin notice", exc)

    def _flush_queue(self, state: dict) -> None:
        for token in list(state["queue"]):
            record = state["requests"].get(token)
            if record is None:
                state["queue"].remove(token)
                self._save(state)
                continue
            record["status"] = "waiting"
            self._save(state)
            if not self._due(state, record, time.time()):
                continue
            if not self._deliver_request(state, record) or record.get("delivery_pending"):
                # Keep the undelivered request for a later retry rather than
                # silently dropping it or repeatedly notifying the user.
                record["status"] = "queued"
                self._save(state)
                continue
            state["queue"].remove(token)
            self._save(state)

    def handle_admin_reply(self, message) -> bool:
        user = getattr(message, "from_user", None)
        quoted = getattr(message, "reply_to_message", None)
        if (
            user is None or not is_admin(user.id)
            or getattr(getattr(message, "chat", None), "type", None) not in {"group", "supergroup"}
        ):
            return False
        with _LOCK:
            state = self.load()
            active = state.get("active_session")
            route = state["message_routes"].get(
                self._route_key(message.chat.id, quoted.message_id),
            ) if quoted is not None else None
            if route is None and active and active["admin_chat_id"] == message.chat.id:
                route = {"user_id": active["user_id"], "request_token": active["token"]}
            record = state["requests"].get(route.get("request_token")) if route else None
            if not record or message.chat.id != record.get("admin_chat_id"):
                return False
            recipient = route["user_id"]
            delivery_key = self._route_key(message.chat.id, message.message_id)
            if delivery_key in state["delivered_replies"]:
                return True
            try:
                self.bot.copy_message(
                    recipient, message.chat.id, message.message_id,
                )
                state["delivered_replies"][delivery_key] = recipient
                self._save(state)
            except Exception as exc:
                logging.warning("Live-chat admin reply delivery failed (%s).", type(exc).__name__)
                self._admin_notice(message.chat.id, "⚠️ התשובה לא נשלחה. ייתכן שהמשתמש חסם את הבוט.")
            else:
                if active and active.get("user_id") == recipient:
                    return True
                self._admin_notice(message.chat.id, "✉️ התשובה נשלחה למשתמש!")
            return True
