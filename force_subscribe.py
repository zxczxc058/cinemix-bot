"""Persistent channel-subscription gate and private admin controls."""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from pathlib import Path

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import format_telegram_text, is_admin
from system_features import write_json

FORCE_SUBSCRIBE_FILE = Path(__file__).resolve().with_name("force_subscribe_settings.json")
DEFAULT_PROMPT = "🔒 כדי להשתמש בבוט, צריך להירשם לערוץ ואז ללחוץ על בדיקת הרשמה."
DEFAULT_FORCE_SUBSCRIBE = {
    "enabled": False,
    "channel_ref": None,
    "channel_id": None,
    "channel_title": "",
    "join_url": None,
    "prompt_text": DEFAULT_PROMPT,
    "prompts": {},
}
_LOCK = threading.RLock()
_CHANNEL_USERNAME = re.compile(r"@[A-Za-z0-9_]{5,32}\Z")
_CHANNEL_ID = re.compile(r"-100\d+\Z")


def _validated(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("מבנה הגדרות חובת ההרשמה אינו תקין.")
    result = copy.deepcopy(DEFAULT_FORCE_SUBSCRIBE)
    result.update(data)
    if type(result["enabled"]) is not bool:
        raise ValueError("מצב חובת ההרשמה אינו תקין.")
    if result["channel_ref"] is not None and not isinstance(result["channel_ref"], str):
        raise ValueError("מזהה הערוץ אינו תקין.")
    channel_id = result["channel_id"]
    if channel_id is not None and (type(channel_id) is not int or channel_id >= 0):
        raise ValueError("מזהה הערוץ חייב להיות מזהה שלילי.")
    if not isinstance(result["channel_title"], str):
        raise ValueError("שם הערוץ אינו תקין.")
    join_url = result["join_url"]
    if join_url is not None and (
        not isinstance(join_url, str)
        or not (join_url.startswith("https://t.me/") or join_url.startswith("http://t.me/"))
    ):
        raise ValueError("קישור ההצטרפות אינו תקין.")
    prompt = result["prompt_text"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 3500:
        raise ValueError("הודעת הדרישה חייבת להכיל עד 3,500 תווים ואסור שתהיה ריקה.")
    prompts = result["prompts"]
    if not isinstance(prompts, dict):
        raise ValueError("רשימת הודעות ההרשמה אינה תקינה.")
    for user_id, record in prompts.items():
        if (
            not isinstance(user_id, str) or not user_id.isdigit()
            or not isinstance(record, dict)
            or type(record.get("chat_id")) is not int or record["chat_id"] == 0
            or type(record.get("message_id")) is not int or record["message_id"] <= 0
        ):
            raise ValueError("רשומת הודעת הרשמה אינה תקינה.")
    return result


def load_force_subscribe(path=FORCE_SUBSCRIBE_FILE) -> dict:
    path = Path(path)
    with _LOCK:
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(path))
        except FileNotFoundError:
            data = copy.deepcopy(DEFAULT_FORCE_SUBSCRIBE)
            write_json(path, data)
        except json.JSONDecodeError as exc:
            raise ValueError("קובץ חובת ההרשמה אינו JSON תקין.") from exc
        return _validated(data)


def save_force_subscribe(data: dict, path=FORCE_SUBSCRIBE_FILE) -> dict:
    path = Path(path)
    validated = _validated(data)
    with _LOCK:
        write_json(path, validated)
    return validated


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _gate_keyboard(settings: dict) -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    if settings.get("join_url"):
        keyboard.add(InlineKeyboardButton("📢 הצטרפות לערוץ", url=settings["join_url"]))
    keyboard.add(InlineKeyboardButton("🔄 בדוק הרשמה", callback_data="force:check"))
    return keyboard


def _render_gate_prompt(settings: dict) -> tuple[str, str | None]:
    return format_telegram_text(settings["prompt_text"])


def _delete_prompt(bot, chat_id: int, message_id: int) -> None:
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logging.info(
            "Force-subscribe prompt cleanup failed (chat=%s, message=%s, error=%s).",
            chat_id, message_id, type(exc).__name__,
        )


def clear_force_prompts(bot, path=FORCE_SUBSCRIBE_FILE) -> None:
    """Remove stored prompts and best-effort delete their bot-created messages."""
    settings = load_force_subscribe(path)
    prompts = list(settings["prompts"].values())
    settings["prompts"] = {}
    save_force_subscribe(settings, path)
    for record in prompts:
        _delete_prompt(bot, record["chat_id"], record["message_id"])


def refresh_force_prompts(bot, path=FORCE_SUBSCRIBE_FILE) -> None:
    """Update outstanding prompt text and join links after admin changes."""
    settings = load_force_subscribe(path)
    if not settings["enabled"]:
        clear_force_prompts(bot, path)
        return
    rendered, parse_mode = _render_gate_prompt(settings)
    stale = []
    for user_id, record in settings["prompts"].items():
        try:
            bot.edit_message_text(
                rendered,
                chat_id=record["chat_id"],
                message_id=record["message_id"],
                reply_markup=_gate_keyboard(settings),
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.info(
                "Force-subscribe prompt refresh failed (user=%s, error=%s).",
                user_id, type(exc).__name__,
            )
            stale.append((user_id, record))
    if stale:
        settings = load_force_subscribe(path)
        for user_id, record in stale:
            settings["prompts"].pop(user_id, None)
        save_force_subscribe(settings, path)
        for _, record in stale:
            _delete_prompt(bot, record["chat_id"], record["message_id"])


class ForceSubscribeAdmin:
    """Admin-dashboard view and text-input flow for force-subscribe settings."""

    INPUT_ACTIONS = {"force_subscribe_channel", "force_subscribe_prompt"}

    def __init__(self, dashboard, settings_file=FORCE_SUBSCRIBE_FILE):
        self.d = dashboard
        self.path = Path(settings_file)

    def menu(self, chat_id: int) -> None:
        settings = load_force_subscribe(self.path)
        status = "🟢 פעילה" if settings["enabled"] else "🔴 כבויה"
        if settings["channel_id"] is None:
            channel = "לא הוגדר"
        else:
            label = settings["channel_title"] or str(settings["channel_ref"])
            channel = f"{label} ({settings['channel_id']})"
        preview = " ".join(settings["prompt_text"].split())
        if len(preview) > 180:
            preview = preview[:177] + "..."
        self.d._send_submenu(
            chat_id,
            "🔒 ניהול חובת הרשמה\n\n"
            f"מצב: {status}\n"
            f"ערוץ נדרש: {channel}\n"
            f"הודעת דרישה: {preview}",
            [
                (
                    "🟢 כבה חובת הרשמה" if settings["enabled"] else "🔴 הפעל חובת הרשמה",
                    "adm:force_toggle",
                ),
                ("📢 הגדר ערוץ", "adm:force_set_channel"),
                ("✏️ עריכת הודעת דרישה", "adm:force_set_prompt"),
            ],
            back_action="adm:bot_settings",
            back_label="🔙 חזרה להגדרות ומצבי בוט",
        )

    def _send_input_prompt(self, chat_id: int, text: str) -> None:
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            InlineKeyboardButton(
                "🔙 חזרה לניהול חובת הרשמה",
                callback_data="adm:force_subscribe",
            )
        )
        self.d.bot.send_message(chat_id, text, reply_markup=keyboard)

    def handle_callback(self, action, call) -> bool:
        actions = {
            "force_subscribe", "force_toggle", "force_set_channel", "force_set_prompt",
        }
        if action not in actions:
            return False
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        chat = getattr(message, "chat", None)
        if not is_admin(actor) or chat is None:
            return True
        if getattr(chat, "type", "private") != "private" or chat.id != actor:
            self.d.bot.answer_callback_query(
                call.id, text="ניהול חובת הרשמה זמין בפרטי בלבד.", show_alert=True,
            )
            return True
        try:
            if action == "force_subscribe":
                self.d.cancel_pending(actor)
                self.menu(chat.id)
            elif action == "force_toggle":
                settings = load_force_subscribe(self.path)
                if not settings["enabled"] and (
                    settings["channel_id"] is None or not settings["join_url"]
                ):
                    self.d.bot.answer_callback_query(
                        call.id,
                        text="צריך להגדיר ערוץ וקישור הצטרפות לפני הפעלת חובת הרשמה.",
                        show_alert=True,
                    )
                    return True
                settings["enabled"] = not settings["enabled"]
                save_force_subscribe(settings, self.path)
                if not settings["enabled"]:
                    clear_force_prompts(self.d.raw_bot, self.path)
                self.d.cancel_pending(actor)
                self.menu(chat.id)
                self.d.bot.answer_callback_query(
                    call.id,
                    text=("חובת ההרשמה הופעלה." if settings["enabled"] else "חובת ההרשמה כובתה."),
                )
            elif action == "force_set_channel":
                self.d.cancel_pending(actor)
                self.d.pending[actor] = {"action": "force_subscribe_channel"}
                self.d.pending_chat_id = chat.id
                self._send_input_prompt(
                    chat.id,
                    "שלח שם ערוץ ציבורי בפורמט @channel_username או מזהה ערוץ פרטי בפורמט ‎-100…‎. "
                    "הבוט צריך להיות מנהל בערוץ כדי לבדוק הרשמה; בערוץ פרטי נדרשת גם הרשאת יצירת קישור. "
                    "לביטול שלח „ביטול”.",
                )
            elif action == "force_set_prompt":
                self.d.cancel_pending(actor)
                self.d.pending[actor] = {"action": "force_subscribe_prompt"}
                self.d.pending_chat_id = chat.id
                self._send_input_prompt(
                    chat.id,
                    "שלח את הודעת הדרישה שתוצג למי שעדיין לא נרשם (עד 3,500 תווים). "
                    "אפשר להשתמש בעיצוב HTML או Markdown. לביטול שלח „ביטול”.",
                )
        except (OSError, ValueError) as exc:
            self.d.bot.answer_callback_query(
                call.id, text=f"לא ניתן לעדכן את ההגדרה: {exc}", show_alert=True,
            )
        return True

    def handle_cancel(self, action, user_id: int, chat_id: int) -> bool:
        if action not in self.INPUT_ACTIONS:
            return False
        self.d.cancel_pending(user_id)
        self.menu(chat_id)
        self.d.bot.answer_callback_query(None, text="הפעולה בוטלה.")
        return True

    def _configure_channel(self, value: str) -> dict:
        ref = value.strip()
        if not (_CHANNEL_USERNAME.fullmatch(ref) or _CHANNEL_ID.fullmatch(ref)):
            raise ValueError("שלח @channel_username או מזהה שלילי בפורמט -100….")
        lookup = ref
        try:
            chat = self.d.raw_bot.get_chat(lookup)
        except Exception as exc:
            raise ValueError(
                "לא ניתן לגשת לערוץ. בדוק את שם המשתמש/המזהה וודא שהבוט צורף לערוץ."
            ) from exc
        chat_id = _field(chat, "id")
        if type(chat_id) is not int or chat_id >= 0 or _field(chat, "type") != "channel":
            raise ValueError("היעד שנשלח אינו ערוץ טלגרם.")
        try:
            bot_user_id = _field(self.d.raw_bot.get_me(), "id")
            bot_membership = self.d.raw_bot.get_chat_member(
                chat_id=chat_id,
                user_id=bot_user_id,
            )
        except Exception as exc:
            raise ValueError(
                "לא ניתן לאמת את הרשאות הבוט בערוץ. הוסף את הבוט כמנהל ונסה שוב."
            ) from exc
        if _field(bot_membership, "status") not in {"administrator", "creator"}:
            raise ValueError("הוסף את הבוט כמנהל הערוץ כדי ש-Telegram יאפשר בדיקת הרשמות אמינה.")

        username = _field(chat, "username")
        title = _field(chat, "title") or username or ref
        if username:
            join_url = f"https://t.me/{username}"
        else:
            join_url = _field(chat, "invite_link")
            if not join_url:
                try:
                    invite = self.d.raw_bot.create_chat_invite_link(
                        chat_id=chat_id,
                        name="Bot subscription",
                    )
                except Exception as exc:
                    raise ValueError(
                        "לא ניתן ליצור קישור לערוץ הפרטי. ודא שהבוט מנהל הערוץ "
                        "עם הרשאת הזמנת משתמשים."
                    ) from exc
                join_url = _field(invite, "invite_link")
            if not join_url:
                raise ValueError("לא התקבל קישור הצטרפות מהערוץ.")

        settings = load_force_subscribe(self.path)
        settings.update(
            channel_ref=ref,
            channel_id=chat_id,
            channel_title=str(title),
            join_url=join_url,
        )
        save_force_subscribe(settings, self.path)
        refresh_force_prompts(self.d.raw_bot, self.path)
        return settings

    def handle_input(self, action, reply_text: str, text: str, chat_id: int, user_id: int) -> bool:
        if action not in self.INPUT_ACTIONS:
            return False
        try:
            if action == "force_subscribe_channel":
                self._configure_channel(text)
                notice = "✅ ערוץ חובת ההרשמה עודכן."
            else:
                value = reply_text.strip()
                if not value or len(value) > 3500:
                    raise ValueError("ההודעה חייבת להכיל 1–3,500 תווים.")
                settings = load_force_subscribe(self.path)
                settings["prompt_text"] = value
                save_force_subscribe(settings, self.path)
                refresh_force_prompts(self.d.raw_bot, self.path)
                notice = "✅ הודעת הדרישה עודכנה."
        except (OSError, ValueError) as exc:
            self.d.bot.answer_callback_query(None, text=f"❌ לא ניתן לשמור: {exc}")
            return True
        self.d.cancel_pending(user_id)
        self.menu(chat_id)
        self.d.bot.answer_callback_query(None, text=notice)
        return True


class ForceSubscribeMiddleware(BaseMiddleware):
    """Blocks non-admin activity until membership in the configured channel is verified."""

    def __init__(self, bot, settings_file=FORCE_SUBSCRIBE_FILE, commands_file=None):
        super().__init__()
        self.bot = bot
        self.settings_file = Path(settings_file)
        self.commands_file = Path(commands_file) if commands_file else Path(__file__).with_name("commands.json")
        self._lock = threading.RLock()
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]

    @staticmethod
    def _is_member_record(record) -> bool:
        status = _field(record, "status")
        if status in {"member", "administrator", "creator"}:
            return True
        return status == "restricted" and bool(_field(record, "is_member", False))

    def _subscribed(self, user_id: int, settings: dict) -> bool:
        channel_id = settings.get("channel_id")
        if channel_id is None:
            return False
        try:
            return self._is_member_record(
                self.bot.get_chat_member(chat_id=channel_id, user_id=user_id),
            )
        except Exception as exc:
            logging.warning(
                "Force-subscribe membership check failed (user=%s, error=%s).",
                user_id, type(exc).__name__,
            )
            return False

    def _settings_or_block(self, chat_id, user_id):
        try:
            return load_force_subscribe(self.settings_file)
        except (OSError, ValueError) as exc:
            logging.exception("Could not read force-subscribe settings.")
            try:
                self.bot.send_message(chat_id, "🔒 הגדרות ההרשמה אינן זמינות כרגע. נסו שוב מאוחר יותר.")
            except Exception:
                pass
            return None

    def _prompt_record(self, user_id):
        try:
            return load_force_subscribe(self.settings_file)["prompts"].get(str(user_id))
        except (OSError, ValueError):
            return None

    def _remove_prompt_record(self, user_id, record=None):
        with self._lock:
            try:
                settings = load_force_subscribe(self.settings_file)
            except (OSError, ValueError):
                return None
            current = settings["prompts"].get(str(user_id))
            if record is not None and current != record:
                return current
            current = settings["prompts"].pop(str(user_id), None)
            save_force_subscribe(settings, self.settings_file)
        return current

    def _send_prompt(self, chat_id: int, user_id: int, settings: dict, *, force=False) -> None:
        with self._lock:
            existing = self._prompt_record(user_id)
            if existing is not None:
                if existing["chat_id"] == chat_id and not force:
                    return
                old = self._remove_prompt_record(user_id, existing)
                if old:
                    _delete_prompt(self.bot, old["chat_id"], old["message_id"])
            rendered, parse_mode = _render_gate_prompt(settings)
            try:
                sent = self.bot.send_message(
                    chat_id,
                    rendered,
                    reply_markup=_gate_keyboard(settings),
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                logging.warning(
                    "Could not send force-subscribe prompt (user=%s, error=%s).",
                    user_id, type(exc).__name__,
                )
                return
            latest = load_force_subscribe(self.settings_file)
            if not latest["enabled"]:
                _delete_prompt(self.bot, chat_id, sent.message_id)
                return
            latest["prompts"][str(user_id)] = {
                "chat_id": int(chat_id),
                "message_id": int(sent.message_id),
            }
            save_force_subscribe(latest, self.settings_file)

    def _send_private_menu(self, user_id: int) -> bool:
        from bot_storage import get_start_reply
        from management_config import resolve_reply
        from media_catalog import build_private_start_keyboard

        try:
            reply = get_start_reply(self.commands_file)
        except Exception:
            reply = None
        buttons = ()
        buttons_enabled = True
        if reply is None:
            text = "ברוכים הבאים!"
        else:
            text = reply.text
            buttons = reply.buttons
            buttons_enabled = reply.buttons_enabled
            try:
                reply = resolve_reply(reply, self.commands_file.parent / "config.json")
                text, buttons = reply.text, reply.buttons
                buttons_enabled = reply.buttons_enabled
            except (OSError, ValueError):
                # Keep the standard bot entry points available if an optional
                # start-message link has not been configured.
                text = "ברוכים הבאים! בחרו פעולה מהתפריט."
                buttons = ()
        rendered, parse_mode = format_telegram_text(f"✅ ההרשמה אומתה!\n\n{text}")
        try:
            self.bot.send_message(
                user_id,
                rendered,
                parse_mode=parse_mode,
                    reply_markup=build_private_start_keyboard(
                        buttons, enabled=buttons_enabled,
                    ),
                disable_web_page_preview=True,
            )
            from reply_keyboard import apply_user_keyboard
            apply_user_keyboard(self.bot, user_id)
            return True
        except Exception as exc:
            logging.info(
                "Could not send the private menu after subscription (user=%s, error=%s).",
                user_id, type(exc).__name__,
            )
            return False

    def _send_group_success(self, chat_id: int) -> None:
        keyboard = InlineKeyboardMarkup(row_width=1)
        try:
            username = getattr(self.bot.get_me(), "username", None)
        except Exception:
            username = None
        if username:
            keyboard.add(
                InlineKeyboardButton(
                    "📩 פתיחת תפריט פרטי",
                    url=f"https://t.me/{username}?start=force_subscribed",
                )
            )
        self.bot.send_message(
            chat_id,
            "✅ ההרשמה אומתה! פתחו את הצ׳אט הפרטי עם הבוט כדי להשתמש בתפריט.",
            reply_markup=keyboard if keyboard.keyboard else None,
            disable_web_page_preview=True,
        )

    def _complete(self, user_id: int, source_chat_id: int, source_message_id=None) -> None:
        record = self._remove_prompt_record(user_id)
        if record:
            _delete_prompt(self.bot, record["chat_id"], record["message_id"])
        elif source_message_id is not None:
            _delete_prompt(self.bot, source_chat_id, source_message_id)
        if source_chat_id > 0 and source_chat_id == user_id:
            self._send_private_menu(user_id)
        elif not self._send_private_menu(user_id):
            self._send_group_success(source_chat_id)

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        if user is None or getattr(user, "is_bot", False) or is_admin(user.id):
            return None
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        text = (getattr(message, "text", None) or "").strip()
        command = text.split(maxsplit=1)[0].partition("@")[0].casefold() if text else ""
        if command == "/start":
            # Clear an old subscription prompt, then allow the normal welcome
            # handler to run. Subscription remains required on later updates.
            record = self._remove_prompt_record(user.id)
            if record:
                _delete_prompt(self.bot, record["chat_id"], record["message_id"])
            return None
        code_pending = getattr(self, "awaiting_access_code", None)
        if (
            getattr(message.chat, "type", None) == "private"
            and callable(code_pending) and code_pending(message)
        ):
            # Let the isolated credential handler finish the requested code
            # attempt. Membership still gates subsequent content/actions.
            return None
        settings = self._settings_or_block(chat_id, user.id)
        if settings is None:
            return CancelUpdate()
        if not settings["enabled"]:
            return None
        if self._subscribed(user.id, settings):
            record = self._remove_prompt_record(user.id)
            if record:
                _delete_prompt(self.bot, record["chat_id"], record["message_id"])
                self._complete_after_message(user.id, chat_id)
                return CancelUpdate()
            return None
        if command == "/cancel":
            record = self._remove_prompt_record(user.id)
            if record:
                _delete_prompt(self.bot, record["chat_id"], record["message_id"])
            self.bot.send_message(
                chat_id,
                "✅ הפעולה בוטלה. שלח /start כדי להציג שוב את דרישת ההרשמה.",
            )
            return CancelUpdate()
        self._send_prompt(chat_id, user.id, settings)
        return CancelUpdate()

    def _complete_after_message(self, user_id: int, source_chat_id: int) -> None:
        if source_chat_id > 0 and source_chat_id == user_id:
            self._send_private_menu(user_id)
        elif not self._send_private_menu(user_id):
            self._send_group_success(source_chat_id)

    def pre_process_callback_query(self, call, data):
        user = getattr(call, "from_user", None)
        if user is None or is_admin(user.id) or (call.data or "") == "force:check":
            return None
        message = getattr(call, "message", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        settings = self._settings_or_block(chat_id or user.id, user.id)
        if settings is None:
            self.bot.answer_callback_query(
                call.id, text="ההרשמה נדרשת כרגע.", show_alert=True,
            )
            return CancelUpdate()
        if not settings["enabled"]:
            return None
        if self._subscribed(user.id, settings):
            record = self._remove_prompt_record(user.id)
            if record:
                _delete_prompt(self.bot, record["chat_id"], record["message_id"])
                self.bot.answer_callback_query(call.id, text="✅ ההרשמה אומתה!")
                self._complete_after_message(user.id, chat_id or user.id)
                return CancelUpdate()
            return None
        if chat_id is not None:
            self._send_prompt(chat_id, user.id, settings)
        self.bot.answer_callback_query(
            call.id, text="🔒 צריך להירשם לערוץ כדי להשתמש בבוט.", show_alert=True,
        )
        return CancelUpdate()

    # Class-based pyTelegramBotAPI middleware runs a post-process hook after
    # each update. These no-op hooks are required even though this middleware
    # only needs to gate updates before handlers run.
    def post_process_message(self, message, data, exception):
        return None

    def post_process_callback_query(self, call, data, exception):
        return None

    def register_handlers(self) -> None:
        self.bot.callback_query_handler(
            func=lambda call: (call.data or "") == "force:check",
        )(self.handle_check_callback)

    def handle_check_callback(self, call) -> None:
        user = getattr(call, "from_user", None)
        message = getattr(call, "message", None)
        if user is None or message is None:
            self.bot.answer_callback_query(call.id, text="הבקשה אינה תקינה.", show_alert=True)
            return
        if is_admin(user.id):
            self.bot.answer_callback_query(call.id, text="למנהלים אין צורך בהרשמה.")
            return
        try:
            settings = load_force_subscribe(self.settings_file)
        except (OSError, ValueError):
            self.bot.answer_callback_query(
                call.id, text="לא ניתן לבדוק את ההרשמה כרגע.", show_alert=True,
            )
            return
        record = settings["prompts"].get(str(user.id))
        if (
            record is None
            or record["chat_id"] != message.chat.id
            or record["message_id"] != message.message_id
        ):
            self.bot.answer_callback_query(
                call.id, text="ההודעה התיישנה. שלחו הודעה חדשה לבוט כדי לנסות שוב.",
                show_alert=True,
            )
            return
        if not settings["enabled"]:
            self._remove_prompt_record(user.id, record)
            _delete_prompt(self.bot, message.chat.id, message.message_id)
            self.bot.answer_callback_query(call.id, text="חובת ההרשמה כבויה.")
            return
        if not self._subscribed(user.id, settings):
            self.bot.answer_callback_query(
                call.id,
                text="עדיין לא זוהתה הרשמה לערוץ. הצטרפו ולחצו שוב.",
                show_alert=True,
            )
            return
        self.bot.answer_callback_query(call.id, text="✅ ההרשמה אומתה!")
        self._complete(user.id, message.chat.id, message.message_id)
