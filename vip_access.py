"""Separate VIP access-code and member storage, independent of standard access."""

from __future__ import annotations

import copy
import hashlib
import hmac
import logging
import secrets
import threading
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import format_telegram_text, is_admin
from admin_menus import VIP_MENU, VIP_ACCESS_CODE_MENU
from broadcast_presets import payload_from_message, validate_payload
from management_config import is_link_input, normalize_button_link, save_json
from media_storage import MediaRepository, media_token
from system_messages import default_message


VIP_SETTINGS_FILE = Path(__file__).resolve().with_name("vip_settings.json")
MAX_FOLLOWUP_BUTTONS = 8
DEFAULT_VIP_SETTINGS = {
    "code_salt": None,
    "code_hash": None,
    "code_casefold": False,
    "vip_code_enabled": True,
    "vip_users": [],
    "pending_users": [],
    "response_text": default_message("vip_code_success"),
    "followup_message": None,
    "prompt_button_url": None,
    "content_prompt_text": (
        "🔒 תוכן זה קיים בבוט וזמין למשתמשי VIP בלבד!\n"
        "לקבלת פרטים על מנוי VIP ולמעבר לתמיכה, לחץ על הכפתור למטה. 👇"
    ),
    "contact_confirmation_text": (
        "הבקשה נשלחה לצוות התמיכה ✅ ניצור איתך קשר בהקדם!"
    ),
    "contact_popup_text": "הבקשה נשלחה לצוות התמיכה ✅ ניצור איתך קשר בהקדם!",
}
VIP_ONLY_NOTICE = "⭐ זמין רק למשתמשי VIP!"


def vip_restriction_text(text):
    """Keep editable guidance while making the entitlement boundary explicit."""
    return text if VIP_ONLY_NOTICE in text else VIP_ONLY_NOTICE + ("\n" + text if text else "")


_LOCK = threading.RLock()
_LEGACY_CONTENT_PROMPT = (
    "🔒 תוכן זה זמין למשתמשי VIP בלבד! לקבלת פרטים על מנוי VIP "
    "ולמעבר לתמיכה, לחץ על הכפתור למטה."
)
_PBKDF2_ITERATIONS = 210_000


def validate_followup_payload(payload: dict) -> dict:
    normalized = validate_payload(payload)
    buttons = payload.get("buttons", [])
    if not isinstance(buttons, list) or len(buttons) > MAX_FOLLOWUP_BUTTONS:
        raise ValueError(f"הודעת ההמשך יכולה לכלול עד {MAX_FOLLOWUP_BUTTONS} כפתורים.")
    clean_buttons = []
    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError("כפתור בהודעת ההמשך אינו תקין.")
        label = button.get("text")
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 64:
            raise ValueError("טקסט הכפתור חייב להכיל 1–64 תווים.")
        clean_buttons.append({
            "text": label.strip(),
            "url": normalize_button_link(button.get("url")),
        })
    normalized["buttons"] = clean_buttons
    return normalized


def _followup_keyboard(buttons):
    if not buttons:
        return None
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(*[
        InlineKeyboardButton(button["text"], url=button["url"])
        for button in buttons
    ])
    return keyboard


class VIPSettingsStore:
    """Validated JSON storage for a single VIP code and authorized users."""

    def __init__(self, path: str | Path = VIP_SETTINGS_FILE):
        self.path = Path(path)

    def load(self) -> dict:
        import json

        with _LOCK:
            try:
                from persistent_json import read_json_text
                data = json.loads(read_json_text(self.path))
            except FileNotFoundError:
                data = copy.deepcopy(DEFAULT_VIP_SETTINGS)
                save_json(self.path, data)
            except json.JSONDecodeError as exc:
                raise ValueError("קובץ הגדרות ה-VIP פגום.") from exc
            if not isinstance(data, dict):
                raise ValueError("קובץ הגדרות ה-VIP חייב להכיל אובייקט JSON.")
            normalized = copy.deepcopy(DEFAULT_VIP_SETTINGS)
            normalized.update(data)
            if normalized["content_prompt_text"] == _LEGACY_CONTENT_PROMPT:
                normalized["content_prompt_text"] = DEFAULT_VIP_SETTINGS[
                    "content_prompt_text"
                ]
            for flag in ("vip_code_enabled", "code_casefold"):
                if normalized[flag] is None:
                    normalized[flag] = DEFAULT_VIP_SETTINGS[flag]
            if type(normalized["vip_code_enabled"]) is not bool:
                raise ValueError("מצב קוד ה-VIP חייב להיות מופעל או כבוי.")
            if type(normalized["code_casefold"]) is not bool:
                raise ValueError("הגדרת השוואת קוד ה-VIP אינה תקינה.")
            for field in ("vip_users", "pending_users"):
                values = normalized[field]
                if not isinstance(values, list) or any(
                    type(user_id) is not int or user_id <= 0 for user_id in values
                ):
                    raise ValueError(f"רשימת {field} בהגדרות ה-VIP אינה תקינה.")
                normalized[field] = list(dict.fromkeys(values))
            if normalized["code_salt"] is not None and not isinstance(normalized["code_salt"], str):
                raise ValueError("מלח קוד ה-VIP אינו תקין.")
            if normalized["code_hash"] is not None and not isinstance(normalized["code_hash"], str):
                raise ValueError("גיבוב קוד ה-VIP אינו תקין.")
            if not isinstance(normalized["response_text"], str):
                raise ValueError("הודעת התגובה ל-VIP חייבת להיות טקסט.")
            if not isinstance(normalized["content_prompt_text"], str):
                raise ValueError("הודעת דרישת תוכן VIP חייבת להיות טקסט.")
            if not isinstance(normalized["contact_confirmation_text"], str):
                raise ValueError("הודעת אישור פנייה ל-VIP חייבת להיות טקסט.")
            if (
                not isinstance(normalized["contact_popup_text"], str)
                or not normalized["contact_popup_text"].strip()
                or len(normalized["contact_popup_text"]) > 200
            ):
                raise ValueError("הודעת החלונית לבירור VIP חייבת להכיל 1–200 תווים.")
            if normalized["followup_message"] is not None:
                try:
                    normalized["followup_message"] = validate_followup_payload(
                        normalized["followup_message"],
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("הודעת ההמשך ל-VIP אינה תקינה.") from exc
            link = normalized["prompt_button_url"]
            if link is not None and (
                not isinstance(link, str) or not is_link_input(link)
            ):
                raise ValueError("קישור כפתור קוד ה-VIP אינו תקין.")
            if normalized != data:
                save_json(self.path, normalized)
            return normalized

    def _save(self, state: dict) -> None:
        with _LOCK:
            save_json(self.path, state)

    def snapshot(self) -> dict:
        return copy.deepcopy(self.load())

    def configured(self) -> bool:
        state = self.load()
        return bool(state["code_salt"] and state["code_hash"])

    def set_code(self, code: str) -> None:
        value = code.strip()
        if not 4 <= len(value) <= 128:
            raise ValueError("קוד VIP חייב להכיל בין 4 ל-128 תווים.")
        value = value.casefold()
        if not 4 <= len(value) <= 128:
            raise ValueError("קוד VIP חייב להכיל בין 4 ל-128 תווים.")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256", value.encode("utf-8"), salt, _PBKDF2_ITERATIONS,
        ).hex()
        state = self.load()
        state["code_salt"] = salt.hex()
        state["code_hash"] = digest
        state["code_casefold"] = True
        self._save(state)

    def delete_code(self) -> None:
        state = self.load()
        state["code_salt"] = None
        state["code_hash"] = None
        state["pending_users"] = []
        self._save(state)

    def toggle_code_enabled(self) -> bool:
        state = self.load()
        state["vip_code_enabled"] = not state["vip_code_enabled"]
        self._save(state)
        return state["vip_code_enabled"]

    def set_response(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("הודעת התגובה אינה יכולה להיות ריקה.")
        if len(text) > 4096:
            raise ValueError("הודעת התגובה ארוכה מהמותר בטלגרם.")
        state = self.load()
        state["response_text"] = text
        self._save(state)

    def set_content_prompt_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("הודעת דרישת תוכן VIP אינה יכולה להיות ריקה.")
        if len(text) > 4096:
            raise ValueError("הודעת דרישת תוכן VIP ארוכה מהמותר בטלגרם.")
        state = self.load()
        state["content_prompt_text"] = text
        self._save(state)

    def set_contact_confirmation_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("הודעת אישור פנייה ל-VIP אינה יכולה להיות ריקה.")
        if len(text) > 4096:
            raise ValueError("הודעת אישור פנייה ל-VIP ארוכה מהמותר בטלגרם.")
        state = self.load()
        state["contact_confirmation_text"] = text
        self._save(state)

    def set_contact_popup_text(self, text: str) -> None:
        value = text.strip()
        if not value or len(value) > 200:
            raise ValueError("הודעת החלונית לבירור VIP חייבת להכיל 1–200 תווים.")
        state = self.load()
        state["contact_popup_text"] = value
        self._save(state)

    def set_followup_message(self, payload: dict | None) -> None:
        normalized = None if payload is None else validate_followup_payload(payload)
        state = self.load()
        state["followup_message"] = normalized
        self._save(state)

    def set_prompt_button_url(self, value: str) -> str:
        link = normalize_button_link(value)
        state = self.load()
        state["prompt_button_url"] = link
        self._save(state)
        return link

    def is_vip(self, user_id: int | None) -> bool:
        return isinstance(user_id, int) and (
            is_admin(user_id) or self.is_member(user_id)
        )

    def is_member(self, user_id: int | None) -> bool:
        """Return persisted VIP membership, excluding admin-only bypass access."""
        if type(user_id) is not int:
            return False
        state = self.load()
        if user_id not in state["vip_users"]:
            return False
        record = state.get("vip_membership", {}).get(str(user_id), {})
        if record.get("is_vip") is False:
            return False
        expires = record.get("expires_at")
        if expires is not None:
            import time
            return isinstance(expires, (int, float)) and expires > time.time()
        return True

    def add_pending(self, user_id: int) -> None:
        state = self.load()
        if user_id not in state["pending_users"]:
            state["pending_users"].append(user_id)
            self._save(state)

    def remove_pending(self, user_id: int) -> None:
        state = self.load()
        if user_id in state["pending_users"]:
            state["pending_users"].remove(user_id)
            self._save(state)

    def clear_pending(self, user_id: int | None = None) -> int:
        """Clear unfinished VIP-code entry states, preserving VIP grants."""
        state = self.load()
        old = state["pending_users"]
        if user_id is None:
            state["pending_users"] = []
        else:
            state["pending_users"] = [uid for uid in old if uid != user_id]
        removed = len(old) - len(state["pending_users"])
        if removed:
            self._save(state)
        return removed

    def remove_vip(self, user_id: int) -> bool:
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("מזהה המשתמש אינו תקין.")
        with _LOCK:
            state = self.load()
            removed = user_id in state["vip_users"]
            state["vip_users"] = [uid for uid in state["vip_users"] if uid != user_id]
            records = state.setdefault("vip_membership", {})
            records[str(user_id)] = {"is_vip": False, "expires_at": None}
            state["pending_users"] = [uid for uid in state["pending_users"] if uid != user_id]
            # Subscriptions cannot remain active after entitlement is revoked.
            state.setdefault("experience", {}).setdefault("subscriptions", {}).pop(str(user_id), None)
            self._save(state)
            return removed

    def has_pending(self, user_id: int) -> bool:
        return user_id in self.load()["pending_users"]

    def verify_and_grant(self, user_id: int, code: str) -> bool:
        state = self.load()
        if not state["vip_code_enabled"]:
            return False
        salt_hex, expected = state["code_salt"], state["code_hash"]
        if not salt_hex or not expected:
            return False
        candidate = code.strip()
        if not 4 <= len(candidate) <= 128:
            return False
        if state["code_casefold"]:
            candidate = candidate.casefold()
        if not 4 <= len(candidate) <= 128:
            return False
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            logging.error("Invalid VIP code salt in %s.", self.path.name)
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", candidate.encode("utf-8"), salt, _PBKDF2_ITERATIONS,
        ).hex()
        if not hmac.compare_digest(actual, expected):
            return False
        if not state["code_casefold"]:
            # Migrate legacy exact-case hashes only after a successful check.
            normalized = candidate.casefold()
            if 4 <= len(normalized) <= 128:
                new_salt = secrets.token_bytes(16)
                new_digest = hashlib.pbkdf2_hmac(
                    "sha256", normalized.encode("utf-8"), new_salt,
                    _PBKDF2_ITERATIONS,
                ).hex()
                state.update(
                    code_salt=new_salt.hex(),
                    code_hash=new_digest,
                    code_casefold=True,
                )
        if user_id not in state["vip_users"]:
            state["vip_users"].append(user_id)
        state.setdefault("vip_membership", {})[str(user_id)] = {
            "is_vip": True, "expires_at": None,
        }
        if user_id in state["pending_users"]:
            state["pending_users"].remove(user_id)
        self._save(state)
        return True


def send_vip_entry_messages(bot, store: VIPSettingsStore, chat_id: int, *, include_followup=True):
    """Send the configured VIP welcome, then its optional custom follow-up."""
    from bot_storage import format_telegram_text

    state = store.snapshot()
    if not include_followup:
        # Standalone VIP integrations need only their own store. The full bot
        # uses welcome profiles when its shared configuration is available.
        from management_config import load_config
        config_file = store.path.with_name("config.json")
        try:
            load_config(config_file)
        except FileNotFoundError:
            pass
        else:
            from welcome_profiles import send_welcome
            return send_welcome(
                bot, chat_id, "first", config_file,
                store.path.with_name("commands.json"), fallback_text=state["response_text"],
                use_custom_text=False, inherit_inline=False,
            )
    welcome, welcome_mode = format_telegram_text(state["response_text"])
    sent = bot.send_message(
        chat_id,
        welcome,
        parse_mode=welcome_mode,
        disable_web_page_preview=True,
    )

    if not include_followup:
        return sent
    payload = state["followup_message"]
    if payload is None:
        return sent
    try:
        text, parse_mode = format_telegram_text(payload["text"])
        markup = _followup_keyboard(payload.get("buttons", []))
        send_options = {"parse_mode": parse_mode}
        if markup is not None:
            send_options["reply_markup"] = markup
        if payload["kind"] == "text":
            send_options["disable_web_page_preview"] = True
            bot.send_message(
                chat_id,
                text,
                **send_options,
            )
        else:
            sender = getattr(bot, f"send_{payload['kind']}")
            sender(
                chat_id,
                payload["file_id"],
                caption=text or None,
                **send_options,
            )
    except Exception as exc:
        # The user has already received the welcome and access is already
        # granted; a failed optional second message must not undo redemption.
        logging.warning("VIP follow-up delivery failed (%s).", type(exc).__name__)
    return sent


class VIPAccess:
    """User-facing VIP code entry and VIP-only-content contact prompts."""

    def __init__(
        self,
        bot,
        store: VIPSettingsStore | None = None,
        media_repository: MediaRepository | None = None,
        live_chat=None,
        request_service=None,
    ):
        self.bot = bot
        self.store = store or VIPSettingsStore()
        self.media_repository = media_repository or MediaRepository(
            VIP_SETTINGS_FILE.with_name("vip_media_db.json"),
        )
        self.purchase_inquiry = None
        self.live_chat = live_chat
        self.request_service = request_service
        self.catalogue = None

    def register_handlers(self) -> None:
        self.bot.callback_query_handler(
            func=lambda call: bool(call.data) and call.data.startswith("vip:"),
        )(self.handle_callback)
        self.bot.message_handler(
            func=self.has_pending_code,
            content_types=["text"],
        )(self.handle_code_input)

    def has_pending_code(self, message) -> bool:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        return bool(
            user and not is_admin(user.id) and chat and chat.type == "private"
            and chat.id == user.id and self.store.has_pending(user.id)
        )

    def show_prompt(self, chat_id: int, item: dict | None = None) -> None:
        item_token = media_token(item["command_name"]) if item else "membership"
        settings = self.store.snapshot()
        content_name = item["title"] if item else "תוכן VIP"
        prompt_text = settings["content_prompt_text"].replace(
            "{CONTENT_NAME}", str(content_name),
        )
        prompt_text = vip_restriction_text(prompt_text)
        keyboard = InlineKeyboardMarkup(row_width=1)
        contact_action = "purchase" if self.purchase_inquiry is not None else "contact"
        keyboard.add(InlineKeyboardButton(
            "לחץ כאן ליצירת קשר",
            callback_data=f"vip:{contact_action}:{item_token}",
        ))
        self.bot.send_message(
            chat_id,
            prompt_text,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    def _requested_item(self, token):
        item = self.media_repository.get_by_token(token)
        if item is None:
            standard = getattr(self, "standard_media_repository", None)
            if standard is not None:
                item = standard.get_by_token(token)
        return item

    def handle_callback(self, call) -> None:
        # Acknowledge immediately; Telegram callbacks are answered only once.
        parts = (call.data or "").split(":")
        action = parts[1] if len(parts) > 1 else ""
        if action == "purchase" and self.purchase_inquiry is not None:
            item = self._requested_item(parts[2]) if len(parts) == 3 and parts[2] != "membership" else None
            self.purchase_inquiry.handle_callback(
                call, self.store, context=item["title"] if item else "מנוי VIP",
            )
            return
        try:
            if action == "contact":
                popup_text = self.store.snapshot()["contact_popup_text"][:200]
                self.bot.answer_callback_query(
                    call.id, text=popup_text, show_alert=True,
                )
            else:
                self.bot.answer_callback_query(call.id)
        except Exception as exc:
            logging.warning(
                "VIP callback acknowledgement failed (%s).",
                type(exc).__name__,
            )
        if len(parts) < 2:
            return
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        if not isinstance(actor, int) or message is None:
            return
        action = parts[1]
        if action in {"enter", "open"} and (
            message.chat.type != "private" or message.chat.id != actor
        ):
            return
        if action == "enter":
            if not self.store.snapshot()["vip_code_enabled"]:
                self.bot.send_message(actor, "🔴 קוד ה-VIP כבוי כרגע.")
                return
            if not self.store.configured():
                self.bot.send_message(actor, "קוד VIP עדיין לא הוגדר. פנו לצוות התמיכה לפרטים.")
                return
            self.store.add_pending(actor)
            settings = self.store.snapshot()
            keyboard = None
            if settings["prompt_button_url"]:
                keyboard = InlineKeyboardMarkup(row_width=1)
                keyboard.add(InlineKeyboardButton(
                    "לחצו כאן",
                    url=settings["prompt_button_url"],
                ))
            self.bot.send_message(
                actor,
                "שלחו את קוד ה-VIP כדי לקבל גישה לתוכן הבלעדי. לביטול, שלחו „ביטול”.",
                reply_markup=keyboard,
            )
        elif action == "open":
            if self.store.is_vip(actor) and self.catalogue is not None:
                self.catalogue._show_user_list(actor, 0)
            else:
                self.show_prompt(actor)
        elif action == "contact" and len(parts) == 3:
            item = None
            if parts[2] != "membership":
                item = self._requested_item(parts[2])
            title = item["title"] if item else "מנוי VIP"
            delivered = False
            token = None
            try:
                user = call.from_user
                name = " ".join(filter(None, [
                    getattr(user, "first_name", ""),
                    getattr(user, "last_name", ""),
                ])) or "ללא שם"
                if self.live_chat is not None:
                    delivered = self.live_chat.submit_callback_request(
                        actor, name, getattr(user, "username", None),
                        context=title, category="בירור VIP",
                        notification_type="vip_inquiry",
                    )
                elif self.request_service is not None:
                    from admin_notifications import resolve_user_management_chat, send_user_management_message
                    from management_config import load_config

                    service = self.request_service
                    config = load_config(service.config_file)
                    target = resolve_user_management_chat(
                        config.get("LOG_CHAT_ID"),
                        config_file=service.config_file,
                    )
                    if target is None:
                        raise ValueError("לא הוגדרה קבוצת ניהול.")
                    is_vip = self.store.is_member(actor)
                    header = (
                        "⭐ 👑 בירור על מנוי VIP! ⭐"
                        if is_vip else "👑 בירור על מנוי VIP!"
                    )
                    user_field = (
                        f"משתמש: ⭐ {name} (ID: {actor}) - סטטוס: VIP · ⭐ מנוי VIP ⭐"
                        if is_vip else f"משתמש: {name} (ID: {actor})"
                    )
                    notification = (
                        f"{header}\n- תוכן מבוקש: {title}\n"
                        f"👤 {user_field}"
                    )
                    token = service.register_external_action(
                        actor, title, target, event_type="vip_inquiry",
                    )
                    sent = send_user_management_message(
                        self.bot,
                        target,
                        notification,
                        config_file=service.config_file,
                        reply_markup=service._keyboard(actor, token),
                        disable_web_page_preview=True,
                    )
                    service.bind_external_action(token, sent.message_id)
                    try:
                        service.engine.track(
                            target, sent.message_id, actor,
                            service._keyboard(actor, token),
                        )
                    except Exception as exc:
                        logging.warning(
                            "VIP inquiry controls could not be tracked (%s).",
                            type(exc).__name__,
                        )
                    delivered = True
                else:
                    raise RuntimeError(
                        "VIP inquiry delivery requires the live-chat or admin request service."
                    )
            except Exception as exc:
                if token is not None and self.request_service is not None:
                    try:
                        self.request_service.discard_external_action(token)
                    except Exception as cleanup_exc:
                        logging.error(
                            "Failed to discard undelivered VIP inquiry (%s).",
                            type(cleanup_exc).__name__,
                        )
                logging.error(
                    "VIP inquiry admin-group delivery failed (error=%s, code=%s).",
                    type(exc).__name__, getattr(exc, "error_code", None),
                )
            if not delivered:
                logging.error(
                    "VIP inquiry was not delivered to the configured admin group."
                )

    def handle_code_input(self, message) -> None:
        if not self.has_pending_code(message):
            return
        user = message.from_user
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
        value = (message.text or "").strip()
        if value in {"/cancel", "ביטול"}:
            self.store.remove_pending(user.id)
            self.bot.send_message(user.id, "הזנת קוד ה-VIP בוטלה.")
            return
        from access_control import ACCESS_FILE, verify_active_code, send_standard_code_success
        access_file = self.store.path.with_name(ACCESS_FILE.name)
        result = verify_active_code(user.id, value, access_file, self.store)
        if result == "standard":
            self.store.remove_pending(user.id)
            send_standard_code_success(self.bot, user.id, access_file)
            return
        if result == "vip":
            send_vip_entry_messages(self.bot, self.store, user.id, include_followup=False)
            return
        from system_messages import message_text
        custom_error = message_text("invalid_code", self.store.path.with_name("config.json"))
        if not custom_error:
            logging.warning("Invalid-code text is not configured; no fallback response sent.")
            return
        text, mode = format_telegram_text(custom_error)
        sent = self.bot.send_message(
            user.id, text, parse_mode=mode, disable_web_page_preview=True,
        )
        from group_management import delete_later
        delete_later(self.bot, user.id, sent.message_id, 5.0)


class VIPAdmin:
    """Admin-only VIP code and response controls in the private dashboard."""

    USER_PAGE_SIZE = 10

    def __init__(self, dashboard, store: VIPSettingsStore | None = None):
        self.dashboard = dashboard
        self.store = store or VIPSettingsStore()

    def menu(self, chat_id: int, notice: str | None = None) -> None:
        state = self.store.snapshot()
        status = "פעיל" if state["vip_code_enabled"] else "כבוי"
        configured = "מוגדר" if state["code_hash"] else "לא הוגדר"
        title = (
            "⭐ הגדרות VIP\n"
            f"🔑 קוד VIP: {configured} · {status}\n"
            f"👥 משתמשי VIP: {len(state['vip_users'])}\n"
            f"🔗 קישור לכפתור קוד VIP: {'מוגדר' if state['prompt_button_url'] else 'לא הוגדר'}\n"
            f"✏️ הודעת אישור: {state['response_text'][:180]}\n"
            f"✉️ הודעת המשך: {'מוגדרת' if state['followup_message'] else 'לא מוגדרת'} · "
            f"כפתורים: {len(state['followup_message'].get('buttons', [])) if state['followup_message'] else 0}"
        )
        if notice:
            title = f"{notice}\n\n{title}"
        toggle_label = "🔴 כבה קוד VIP" if state["vip_code_enabled"] else "🟢 הפעל קוד VIP"
        buttons = [
            (toggle_label, callback) if callback == "adm:vip_code_toggle" else (label, callback)
            for label, callback in VIP_MENU
        ]
        from vip_experience import ExperienceStore
        keyboard_on = ExperienceStore(self.store).snapshot()["keyboard_enabled"]
        buttons = [
            (("🔴 כבה תפריט VIP" if keyboard_on else "🟢 הפעל תפריט VIP"), callback)
            if callback == "adm:experience_keyboard" else (label, callback)
            for label, callback in buttons
        ]
        self.dashboard._send_submenu(chat_id, title, buttons)

    def code_menu(self, chat_id: int, notice: str | None = None) -> None:
        state = self.store.snapshot()
        enabled = state["vip_code_enabled"]
        title = (
            "🔑 קוד גישה VIP\n\n"
            f"קוד: {'מוגדר' if state['code_hash'] else 'לא הוגדר'}\n"
            f"בדיקת קוד VIP: {'פעילה' if enabled else 'כבויה'}\n"
            f"קישור לקבלת קוד: {state['prompt_button_url'] or 'לא הוגדר'}\n\n"
            "הקוד נשמר כגיבוב מאובטח ולא ניתן לשחזר או להציג את הטקסט המקורי.\n"
            "הגדרות אלה נפרדות מקוד הגישה הרגיל."
        )
        if notice:
            title = f"{notice}\n\n{title}"
        buttons = [
            (("🔴 כבה קוד VIP" if enabled else "🟢 הפעל קוד VIP"), callback)
            if callback == "adm:vip_code_toggle" else (label, callback)
            for label, callback in VIP_ACCESS_CODE_MENU
        ]
        self.dashboard._send_submenu(
            chat_id, title, buttons, back_action="adm:vip_menu",
            back_label="🔙 חזרה להגדרות VIP",
        )

    def purchase_menu(self, chat_id: int, notice: str | None = None) -> None:
        state = self.store.snapshot()
        title = (
            "ניהול רכישת VIP\n\n"
            f"הודעת הפנייה:\n{state['contact_confirmation_text']}\n\n"
            f"הודעה מוקפצת:\n{state['contact_popup_text']}"
        )
        self.dashboard._send_submenu(
            chat_id, f"{notice}\n\n{title}" if notice else title,
            [
                ("✏️ עריכת הודעת הפנייה / רכישה", "adm:vip_purchase_prompt_edit"),
                ("✏️ עריכת הודעה מוקפצת לרכישה", "adm:vip_purchase_popup_edit"),
            ],
            back_action="adm:menu", back_label="🏠 חזרה לתפריט ראשי",
        )

    def handle_callback(self, action: str, call) -> bool:
        if action not in {
            "vip_purchase_menu", "vip_purchase_prompt_edit", "vip_purchase_popup_edit",
            "vip_media",
            "vip_access_code", "vip_code_view",
            "vip_menu", "vip_code_toggle", "vip_code_set", "vip_code_delete", "vip_response_edit",
            "vip_prompt_link", "vip_content_prompt_edit", "vip_contact_confirmation_edit",
            "vip_inquiry_popup_edit",
            "vip_followup_edit", "vip_users",
            "vip_user_revoke", "vip_user_revoke_confirm",
            "vip_followup_buttons", "vip_followup_button_add",
            "vip_followup_button_edit", "vip_followup_button_remove",
            "vip_followup_buttons_clear",
        }:
            return False
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        if (
            not isinstance(actor, int) or not is_admin(actor) or message is None
            or message.chat.type != "private" or message.chat.id != actor
        ):
            # AdminDashboard acknowledges callbacks before dispatching them.
            return True
        chat_id = message.chat.id
        if action == "vip_menu":
            self.menu(chat_id)
        elif action == "vip_media":
            if self.dashboard.vip_media_manager is not None:
                self.dashboard.vip_media_manager._show_media_menu(self.dashboard, chat_id)
            else:
                self.menu(chat_id)
        elif action == "vip_purchase_menu":
            self.purchase_menu(chat_id)
        elif action in {"vip_purchase_prompt_edit", "vip_purchase_popup_edit"}:
            popup = action == "vip_purchase_popup_edit"
            self.dashboard.pending[actor] = {
                "action": "vip_purchase_popup" if popup else "vip_purchase_prompt",
            }
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["contact_popup_text" if popup else "contact_confirmation_text"]
            self.dashboard._send_prompt(
                chat_id,
                ("שלח את הודעת החלונית הקופצת לרכישת VIP (עד 200 תווים).\n"
                 if popup else "שלח את הטקסט המדויק שיוצג למשתמש הפונה לרכישת VIP.\n")
                + f"הנוסח הנוכחי:\n{current}\n\nלביטול שלח „ביטול”.",
            )
        elif action in {"vip_access_code", "vip_code_view"}:
            self.dashboard.pending.pop(actor, None)
            self.dashboard.pending_chat_id = None
            self.code_menu(chat_id)
        elif action == "vip_code_toggle":
            enabled = self.store.toggle_code_enabled()
            self.code_menu(
                chat_id,
                "🟢 קוד ה-VIP הופעל." if enabled else "🔴 קוד ה-VIP כובה.",
            )
        elif action == "vip_code_set":
            self.dashboard.pending[actor] = {"action": "vip_code"}
            self.dashboard.pending_chat_id = chat_id
            self.dashboard._send_prompt(
                chat_id,
                "שלח את קוד ה-VIP החדש (4–128 תווים). הקוד יישמר בצורה מוצפנת. "
                "לביטול, שלח „ביטול”.",
            )
        elif action == "vip_code_delete":
            self.store.delete_code()
            self.dashboard.pending.pop(actor, None)
            self.code_menu(chat_id, "✅ קוד ה-VIP נמחק.")
        elif action == "vip_response_edit":
            self.dashboard.pending[actor] = {"action": "vip_response"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["response_text"]
            self.dashboard._send_prompt(
                chat_id,
                "שלח את הודעת התגובה למי שמזין קוד VIP נכון.\n"
                f"הנוסח הנוכחי:\n{current}\n\n"
                "אפשר להשתמש ב-Markdown/HTML. לביטול, שלח „ביטול”.",
            )
        elif action == "vip_followup_edit":
            self.dashboard.pending[actor] = {"action": "vip_followup_edit"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["followup_message"]
            current_label = (
                "לא מוגדרת — קוד VIP ישלח רק את הודעת הפתיחה."
                if current is None
                else f"מוגדרת: {current['kind']} — {current['text'][:160] or '(ללא כיתוב)'}"
            )
            self.dashboard._send_prompt(
                chat_id,
                "שלח הודעת המשך אחת אחרי הודעת הפתיחה: טקסט, תמונה, סרטון, מסמך, "
                "GIF או הודעת אודיו. אפשר לכלול קישורים ועיצוב בהודעת הטקסט/בכיתוב.\n"
                f"המצב הנוכחי: {current_label}\n"
                "את כפתורי הקישור ניתן לנהל בנפרד בכפתור „🔘 ניהול כפתורי הודעת המשך”.\n"
                "שלח „ללא” כדי לכבות ולמחוק את הודעת ההמשך, או „ביטול” כדי לצאת.",
            )
        elif action == "vip_followup_buttons":
            self._followup_buttons_menu(chat_id)
        elif action == "vip_followup_button_add":
            if not self.store.snapshot()["followup_message"]:
                self._followup_buttons_menu(chat_id, "קודם יש להגדיר הודעת המשך.")
                return True
            self.dashboard.pending[actor] = {
                "action": "vip_followup_button_label",
                "button_index": None,
            }
            self.dashboard.pending_chat_id = chat_id
            self.dashboard._send_prompt(
                chat_id,
                "שלח את הכיתוב שיופיע בכפתור (עד 64 תווים).",
            )
        elif action == "vip_followup_button_edit":
            parts = (call.data or "").split(":")
            if len(parts) != 3 or not parts[2].isdigit():
                self._followup_buttons_menu(chat_id, "בחירת הכפתור אינה תקינה.")
                return True
            index = int(parts[2])
            payload = self.store.snapshot()["followup_message"]
            buttons = payload.get("buttons", []) if payload else []
            if not 0 <= index < len(buttons):
                self._followup_buttons_menu(chat_id, "הכפתור כבר אינו קיים.")
                return True
            self.dashboard.pending[actor] = {
                "action": "vip_followup_button_label",
                "button_index": index,
            }
            self.dashboard.pending_chat_id = chat_id
            self.dashboard._send_prompt(
                chat_id,
                f"שלח כיתוב חדש לכפתור הנוכחי „{buttons[index]['text']}” (עד 64 תווים).",
            )
        elif action == "vip_followup_button_remove":
            parts = (call.data or "").split(":")
            if len(parts) != 3 or not parts[2].isdigit():
                self._followup_buttons_menu(chat_id, "בחירת הכפתור אינה תקינה.")
                return True
            index = int(parts[2])
            payload = self.store.snapshot()["followup_message"]
            buttons = payload.get("buttons", []) if payload else []
            if not 0 <= index < len(buttons):
                self._followup_buttons_menu(chat_id, "הכפתור כבר אינו קיים.")
                return True
            buttons.pop(index)
            payload["buttons"] = buttons
            self.store.set_followup_message(payload)
            self._followup_buttons_menu(chat_id, "✅ הכפתור הוסר.")
        elif action == "vip_followup_buttons_clear":
            payload = self.store.snapshot()["followup_message"]
            if payload:
                payload["buttons"] = []
                self.store.set_followup_message(payload)
            self._followup_buttons_menu(chat_id, "✅ כל כפתורי הודעת ההמשך הוסרו.")
        elif action == "vip_users":
            parts = (call.data or "").split(":")
            page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
            self._users_menu(chat_id, page)
        elif action == "vip_user_revoke":
            parts = (call.data or "").split(":")
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
                self.menu(chat_id, "❌ בקשת הסרה לא תקינה.")
                return True
            target_id, page = int(parts[2]), int(parts[3])
            if target_id not in self.store.snapshot()["vip_users"]:
                self._users_menu(chat_id, page, "המשתמש כבר אינו ברשימת ה-VIP.")
                return True
            self.dashboard._send_submenu(
                chat_id,
                f"⚠️ להסיר גישת VIP למשתמש {target_id}?",
                [
                    ("✅ כן, הסר גישת VIP", f"adm:vip_user_revoke_confirm:{target_id}:{page}"),
                    ("🔙 חזרה לרשימת משתמשי VIP", f"adm:vip_users:{page}"),
                ],
                back_action=f"adm:vip_users:{page}",
                back_label="🔙 חזרה לרשימת משתמשי VIP",
            )
        elif action == "vip_user_revoke_confirm":
            parts = (call.data or "").split(":")
            if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
                self.menu(chat_id, "❌ בקשת הסרה לא תקינה.")
                return True
            target_id, page = int(parts[2]), int(parts[3])
            removed = self.store.remove_vip(target_id)
            if removed:
                from vip_experience import refresh_revoked_keyboard
                refresh_revoked_keyboard(self.dashboard.bot, target_id, self.store)
            notice = (
                f"✅ גישת VIP הוסרה מהמשתמש {target_id}."
                if removed else "המשתמש כבר אינו ברשימת ה-VIP."
            )
            self._users_menu(chat_id, page, notice)
        elif action == "vip_prompt_link":
            self.dashboard.pending[actor] = {"action": "vip_prompt_link"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["prompt_button_url"] or "לא הוגדר"
            self.dashboard._send_prompt(
                chat_id,
                "שלח שם משתמש בטלגרם (למשל @username) או קישור HTTP/HTTPS "
                "שיופיע בכפתור „לחצו כאן” בהודעת הזנת קוד ה-VIP.\n"
                f"הקישור הנוכחי: {current}\n"
                "לביטול שלח „ביטול”.",
            )
        elif action == "vip_content_prompt_edit":
            self.dashboard.pending[actor] = {"action": "vip_content_prompt"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["content_prompt_text"]
            self.dashboard._send_prompt(
                chat_id,
                "שלח את ההודעה למשתמש רגיל שמבקש תוכן VIP.\n"
                "אפשר להשתמש במשתנה {CONTENT_NAME} שיוחלף בשם התוכן.\n"
                f"הנוסח הנוכחי:\n{current}\n\nלביטול, שלח „ביטול”.",
            )
        elif action == "vip_contact_confirmation_edit":
            self.dashboard.pending[actor] = {"action": "vip_contact_confirmation"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["contact_confirmation_text"]
            self.dashboard._send_prompt(
                chat_id,
                "שלח את הודעת האישור למשתמש לאחר שליחת בירור VIP.\n"
                f"הנוסח הנוכחי:\n{current}\n\nלביטול, שלח „ביטול”.",
            )
        elif action == "vip_inquiry_popup_edit":
            self.dashboard.pending[actor] = {"action": "vip_inquiry_popup_text"}
            self.dashboard.pending_chat_id = chat_id
            current = self.store.snapshot()["contact_popup_text"]
            self.dashboard._send_prompt(
                chat_id,
                "שלח את הטקסט שיוצג בחלונית הקופצת בעת בירור על מנוי VIP "
                "(עד 200 תווים).\n"
                f"הנוסח הנוכחי:\n{current}\n\nלביטול, שלח „ביטול”.",
            )
        return True

    def _users_menu(self, chat_id: int, page: int = 0, notice: str | None = None) -> None:
        users = sorted(uid for uid in self.store.snapshot()["vip_users"] if self.store.is_member(uid))
        pages = max(1, (len(users) + self.USER_PAGE_SIZE - 1) // self.USER_PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * self.USER_PAGE_SIZE
        selected = users[start:start + self.USER_PAGE_SIZE]
        title = (
            f"👥 משתמשי VIP — {len(users)} משתמשים · עמוד {page + 1}/{pages}"
        )
        if notice:
            title = f"{notice}\n\n{title}"
        buttons = [
            (f"👤 {user_id} — הסר גישה", f"adm:vip_user_revoke:{user_id}:{page}")
            for user_id in selected
        ]
        if not selected:
            title += "\n\nאין משתמשי VIP פעילים."
        if page > 0:
            buttons.append(("⬅️ הקודם", f"adm:vip_users:{page - 1}"))
        if page + 1 < pages:
            buttons.append(("➡️ הבא", f"adm:vip_users:{page + 1}"))
        buttons.append(("🔙 חזרה להגדרות VIP", "adm:vip_menu"))
        self.dashboard._send_submenu(
            chat_id,
            title,
            buttons,
            back_action="adm:vip_menu",
            back_label="🔙 חזרה להגדרות VIP",
        )

    def _followup_buttons_menu(self, chat_id: int, notice: str | None = None) -> None:
        payload = self.store.snapshot()["followup_message"]
        title = "🔘 כפתורי קישור להודעת ההמשך"
        buttons = []
        if notice:
            title = f"{notice}\n\n{title}"
        if payload is None:
            title += "\n\nלא הוגדרה הודעת המשך. יש להגדיר אותה לפני הוספת כפתורים."
            buttons.append(("✉️ עריכת הודעת המשך", "adm:vip_followup_edit"))
        else:
            current = payload.get("buttons", [])
            title += f"\n\nכפתורים מוגדרים: {len(current)}/{MAX_FOLLOWUP_BUTTONS}"
            for index, button in enumerate(current):
                buttons.append((
                    f"✏️ {button['text'][:40]}",
                    f"adm:vip_followup_button_edit:{index}",
                ))
                buttons.append((
                    f"🗑️ הסר: {button['text'][:36]}",
                    f"adm:vip_followup_button_remove:{index}",
                ))
            if len(current) < MAX_FOLLOWUP_BUTTONS:
                buttons.append(("➕ הוספת כפתור קישור", "adm:vip_followup_button_add"))
            if current:
                buttons.append(("🧹 הסרת כל הכפתורים", "adm:vip_followup_buttons_clear"))
        buttons.append(("🔙 חזרה להגדרות VIP", "adm:vip_menu"))
        self.dashboard._send_submenu(
            chat_id,
            title,
            buttons,
            back_action="adm:vip_menu",
            back_label="🔙 חזרה להגדרות VIP",
        )

    def handle_input(
        self, action: str | None, text: str, chat_id: int, actor_id: int,
        message=None,
    ) -> bool:
        if action not in {
            "vip_code", "vip_response", "vip_prompt_link", "vip_followup_edit",
            "vip_purchase_prompt", "vip_purchase_popup",
            "vip_content_prompt", "vip_contact_confirmation",
            "vip_inquiry_popup_text",
            "vip_followup_button_label", "vip_followup_button_url",
        }:
            return False
        try:
            if action == "vip_code":
                self.store.set_code(text)
                notice = "✅ קוד ה-VIP נשמר בצורה מאובטחת."
            elif action == "vip_prompt_link":
                self.store.set_prompt_button_url(text)
                notice = "✅ קישור כפתור קבלת קוד ה-VIP עודכן."
            elif action == "vip_content_prompt":
                self.store.set_content_prompt_text(text)
                notice = "✅ הודעת תוכן VIP למשתמש רגיל עודכנה."
            elif action in {"vip_contact_confirmation", "vip_purchase_prompt"}:
                self.store.set_contact_confirmation_text(text)
                notice = "✅ הודעת אישור פנייה ל-VIP עודכנה."
            elif action in {"vip_inquiry_popup_text", "vip_purchase_popup"}:
                self.store.set_contact_popup_text(text)
                notice = "✅ הודעת החלונית לבירור VIP עודכנה."
            elif action == "vip_followup_edit":
                if text.strip().casefold() in {"ללא", "כבוי", "off", "disable"}:
                    self.store.set_followup_message(None)
                else:
                    payload = (
                        payload_from_message(message, text=text)
                        if message is not None
                        else validate_payload({"kind": "text", "text": text})
                    )
                    previous = self.store.snapshot()["followup_message"]
                    if previous:
                        payload["buttons"] = previous.get("buttons", [])
                    self.store.set_followup_message(payload)
                notice = (
                    "✅ הודעת ההמשך ל-VIP עודכנה."
                    if self.store.snapshot()["followup_message"] is not None
                    else "✅ הודעת ההמשך כובתה ונמחקה."
                )
            elif action == "vip_followup_button_label":
                label = text.strip()
                if not label or len(label) > 64:
                    raise ValueError("כיתוב הכפתור חייב להכיל 1–64 תווים.")
                state = self.dashboard.pending.get(actor_id, {})
                self.dashboard.pending[actor_id] = {
                    "action": "vip_followup_button_url",
                    "button_text": label,
                    "button_index": state.get("button_index"),
                }
                self.dashboard._send_prompt(
                    chat_id,
                    "שלח קישור HTTP/HTTPS או שם משתמש בטלגרם (למשל @username) לכפתור.",
                )
                return True
            elif action == "vip_followup_button_url":
                payload = self.store.snapshot()["followup_message"]
                if payload is None:
                    raise ValueError("הודעת ההמשך הוסרה. הגדר אותה מחדש.")
                buttons = payload.get("buttons", [])
                state = self.dashboard.pending.get(actor_id, {})
                index = state.get("button_index")
                item = {
                    "text": state.get("button_text", ""),
                    "url": normalize_button_link(text),
                }
                if index is None:
                    if len(buttons) >= MAX_FOLLOWUP_BUTTONS:
                        raise ValueError(f"ניתן להגדיר עד {MAX_FOLLOWUP_BUTTONS} כפתורים.")
                    buttons.append(item)
                    notice = "✅ הכפתור נוסף להודעת ההמשך."
                else:
                    if type(index) is not int or not 0 <= index < len(buttons):
                        raise ValueError("הכפתור לעריכה כבר אינו קיים.")
                    buttons[index] = item
                    notice = "✅ כפתור הודעת ההמשך עודכן."
                payload["buttons"] = buttons
                self.store.set_followup_message(payload)
                self.dashboard.pending.pop(actor_id, None)
                self.dashboard.pending_chat_id = None
                self._followup_buttons_menu(chat_id, notice)
                return True
            else:
                self.store.set_response(text)
                notice = "✅ הודעת התגובה ל-VIP עודכנה."
        except (OSError, ValueError) as exc:
            from admin_ui import collect_private_feedback

            collect_private_feedback(chat_id, f"❌ לא ניתן לשמור: {exc}")
            return True
        self.dashboard.pending.pop(actor_id, None)
        self.dashboard.pending_chat_id = None
        if action in {"vip_code", "vip_prompt_link"}:
            self.code_menu(chat_id, notice)
        elif action in {"vip_purchase_prompt", "vip_purchase_popup"}:
            self.purchase_menu(chat_id, notice)
        else:
            self.menu(chat_id, notice)
        return True

    def has_media_input(self, message) -> bool:
        user = getattr(message, "from_user", None)
        state = self.dashboard.pending.get(getattr(user, "id", None), {})
        chat = getattr(message, "chat", None)
        return bool(
            user and is_admin(user.id) and chat and chat.type == "private"
            and chat.id == user.id and self.dashboard.pending_chat_id == chat.id
            and state.get("action") == "vip_followup_edit"
            and any(getattr(message, kind, None) for kind in (
                "photo", "video", "document", "animation", "audio", "voice",
            ))
        )

    def handle_media_input(self, message) -> bool:
        if not self.has_media_input(message):
            return False
        try:
            payload = payload_from_message(message)
            current = self.store.snapshot()["followup_message"]
            if current:
                payload["buttons"] = current.get("buttons", [])
            self.store.set_followup_message(payload)
        except (OSError, ValueError) as exc:
            self.dashboard.bot.send_message(message.chat.id, f"❌ לא ניתן לשמור את הודעת ההמשך: {exc}")
            return True
        actor = message.from_user.id
        self.dashboard.pending.pop(actor, None)
        self.dashboard.pending_chat_id = None
        self.menu(message.chat.id, "✅ הודעת ההמשך ל-VIP עודכנה.")
        return True

