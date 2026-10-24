"""One persisted admin menu per chat, temporary notices, and input cleanup."""

import json
import logging
import threading
import functools
import html
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace

from telebot.handler_backends import BaseMiddleware

from admin_menus import build_menu
from bot_storage import is_admin
from group_management import delete_later
from system_features import write_json
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

UI_STATE_FILE = Path(__file__).resolve().with_name("admin_ui_state.json")
_feedback = ContextVar("private_admin_feedback", default=None)


def collect_private_feedback(chat_id, text):
    state = _feedback.get()
    if state is None or state["chat_id"] != chat_id:
        return False
    state["notices"].append(str(text))
    plain = html.unescape(re.sub(r"<[^>]+>", "", str(text))).strip()
    if plain.startswith(("לא ניתן", "❌", "⚠️", "שגיאה", "אין הרשאה")):
        from admin_edit_flow import block_cleanup
        block_cleanup()
    return True


def suppress_input_feedback():
    """Keep input-driven previews limited to the buttons the preview requires."""
    state = _feedback.get()
    if state is not None and state["call"] is None:
        state["suppress_alert"] = True


def callback_feedback(handler):
    @functools.wraps(handler)
    def wrapped(self, call):
        with self.bot.feedback(call=call):
            from admin_edit_flow import track_edit
            with track_edit(self, call=call):
                return handler(self, call)
    return wrapped


def input_feedback(handler):
    @functools.wraps(handler)
    def wrapped(self, message):
        with self.bot.feedback(message=message):
            from admin_edit_flow import track_edit
            with track_edit(self, message=message):
                return handler(self, message)
    return wrapped


def load_ui_state(path=UI_STATE_FILE):
    try:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
    except FileNotFoundError:
        data = {"menus": {}}
        write_json(path, data)
    if not isinstance(data, dict) or not isinstance(data.get("menus"), dict):
        raise ValueError("Invalid admin menu state.")
    for chat_id, message_id in data["menus"].items():
        if int(chat_id) == 0 or type(message_id) is not int or message_id <= 0:
            raise ValueError("Invalid stored admin menu.")
    for name in ("alerts", "keyboards"):
        if not isinstance(data.get(name, {}), dict):
            raise ValueError("Invalid stored admin feedback.")
    for chat_id, record in data.get("alerts", {}).items():
        if (
            int(chat_id) <= 0 or not isinstance(record, dict)
            or not isinstance(record.get("nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", record["nonce"])
            or not isinstance(record.get("text"), str) or len(record["text"]) > 200
            or type(record.get("message_id")) is not int or record["message_id"] <= 0
        ):
            raise ValueError("Invalid stored admin alert.")
    return data


class AdminChatUI:
    def __init__(self, bot, state_file=UI_STATE_FILE):
        self.raw = bot
        self.state_file = state_file
        self.state = load_ui_state(state_file)
        self._lock = threading.RLock()
        self._menu_content = {}

    @contextmanager
    def feedback(self, call=None, message=None):
        source = call.message if call is not None else message
        actor = getattr(call if call is not None else message, "from_user", None)
        chat = getattr(source, "chat", None)
        if call is not None:
            state = {
                "chat_id": getattr(chat, "id", None),
                "notices": [],
                "call": call,
                "saved": False,
                "callback_answered": False,
                "private_admin": bool(
                    chat and chat.id > 0 and actor and chat.id == actor.id and is_admin(actor.id)
                ),
            }
            token = _feedback.set(state)
            try:
                yield
            finally:
                _feedback.reset(token)
                if not state["callback_answered"]:
                    text = html.unescape(re.sub(r"<[^>]+>", "", "\n".join(state["notices"])))[:200]
                    try:
                        self.raw.answer_callback_query(
                            call.id, text=text, show_alert=bool(text),
                        )
                    except Exception as exc:
                        logging.warning("Callback acknowledgement failed (%s).", type(exc).__name__)
                elif state["notices"] and state["private_admin"]:
                    self._render_callback_notices(state)
            return
        if not chat or chat.id <= 0 or not actor or chat.id != actor.id or not is_admin(actor.id):
            yield
            return
        state = {"chat_id": chat.id, "notices": [], "call": None, "saved": False}
        token = _feedback.set(state)
        pieces = (getattr(message, "text", None) or "").split(maxsplit=1)
        state["allow_initial_menu"] = bool(pieces and pieces[0].split("@")[0] == "/addcmd")
        try:
            yield
        finally:
            _feedback.reset(token)
            if (state["notices"] or state["saved"]) and not state.get("suppress_alert"):
                # No stale callback reuse, no new message, no inline status text.
                text = "\n".join(state["notices"])
                if state["saved"]:
                    text = state.get("saved_text", "✅ ההגדרה עודכנה ונשמרה בהצלחה!") + "\n" + text
                self._offer_alert(chat.id, text, state["saved"])

    def acknowledge_callback_query(self, callback_query_id, text=None, **kwargs):
        """Answer Telegram's callback before running menu and storage logic."""
        state = _feedback.get()
        if state and state["call"] is not None and state["call"].id == callback_query_id:
            if state["callback_answered"]:
                return True
            state["callback_answered"] = True
        try:
            if text is None:
                self.raw.answer_callback_query(callback_query_id)
            else:
                self.raw.answer_callback_query(callback_query_id, text=text, **kwargs)
            return True
        except Exception as exc:
            logging.warning("Immediate callback acknowledgement failed (%s).", type(exc).__name__)
            return False

    def _render_callback_notices(self, state):
        chat_id = state.get("chat_id")
        if not chat_id:
            return
        key = str(chat_id)
        message_id = self.state.get("menus", {}).get(key)
        if not message_id:
            return

        content = self._menu_content.get(chat_id)
        if content:
            base_text, markup, options = content
            options = dict(options or {})
        else:
            base_text = "תפריט מנהל מערכת"
            markup_data = self.state.get("keyboards", {}).get(key)
            try:
                markup = InlineKeyboardMarkup.de_json(markup_data) if markup_data else build_menu([])
            except (TypeError, ValueError):
                markup = build_menu([])
            options = {}

        from safe_formatting import prepare
        rendered_base, _ = prepare(base_text, options.get("parse_mode"))
        notice = html.escape(
            html.unescape(re.sub(r"<[^>]+>", "", "\n".join(state["notices"])))
        )
        options = {
            name: value for name, value in options.items()
            if name not in {"parse_mode", "entities", "caption_entities"}
        }
        try:
            self.raw.edit_message_text(
                f"{rendered_base}\n\n{notice}", chat_id=chat_id,
                message_id=message_id, reply_markup=markup,
                parse_mode="HTML", **options,
            )
            self._menu_content[chat_id] = (base_text, markup, options)
        except Exception as exc:
            logging.warning("Could not render callback feedback in the current menu (%s).", type(exc).__name__)

    def _offer_alert(self, chat_id, text, saved=False):
        key = str(chat_id)
        current = self.state["menus"].get(key)
        if not current:
            logging.warning("Private edit result has no existing menu; reopen /users.")
            return
        nonce = secrets.token_hex(8)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))[:200]
        record = {"nonce": nonce, "text": text, "message_id": current}
        self.state.setdefault("alerts", {})[key] = record
        content = self._menu_content.get(chat_id)
        markup = content[1] if content else InlineKeyboardMarkup.de_json(
            self.state.get("keyboards", {}).get(key, {"inline_keyboard": [
                [{"text": "חזרה לתפריט ראשי", "callback_data": "adm:menu"}],
            ]})
        )
        grid = InlineKeyboardMarkup(row_width=1)
        for row in markup.keyboard:
            for button in row:
                if not (button.callback_data or "").startswith("adm:saved:"):
                    grid.add(button)
        label = "✅ הצגת אישור" if saved else "ℹ️ הצגת פרטים"
        grid.add(InlineKeyboardButton(label, callback_data=f"adm:saved:{nonce}"))
        self.state.setdefault("keyboards", {})[key] = grid.to_dict()
        self._save()
        try:
            self.raw.edit_message_reply_markup(chat_id=chat_id, message_id=current, reply_markup=grid)
            if content:
                self._menu_content[chat_id] = (content[0], grid, content[2])
        except Exception as exc:
            logging.warning("Could not attach private result button (%s); reopen /users.", type(exc).__name__)

    def show_saved_alert(self, call):
        """Show the saved notice without arbitrary time-based expiration."""
        actor = call.from_user.id
        message = call.message
        record = self.state.get("alerts", {}).get(str(actor))
        nonce = (call.data or "").split(":")[-1]
        if (
            not message or message.chat.id != actor or not is_admin(actor)
            or not record or record["nonce"] != nonce
            or record["message_id"] != getattr(message, "message_id", None)
        ):
            return False
        self.answer_callback_query(call.id, text=record["text"], show_alert=True)
        return True

    def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        state = _feedback.get()
        if callback_query_id is None and state and state["call"] is None:
            if text:
                state["notices"].append(str(text))
            return True
        if state and state["call"] is not None and state["call"].id == callback_query_id:
            if text:
                state["notices"].append(str(text))
            return True
        return self.raw.answer_callback_query(callback_query_id, text=text, **kwargs)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _delete(self, chat_id, message_id):
        try:
            from private_admin_protection import PrivateAdminProtection
            protection = getattr(self.raw, "private_admin_protection", None)
            if isinstance(protection, PrivateAdminProtection):
                protection.delete_menu(chat_id, message_id)
            else:
                self.raw.delete_message(chat_id, message_id)
        except Exception as exc:
            logging.warning("Admin message cleanup failed (%s).", type(exc).__name__)

    def _mark_menu(self, chat_id, message_id, proof=None):
        from private_admin_protection import PrivateAdminProtection
        protection = getattr(self.raw, "private_admin_protection", None)
        if isinstance(protection, PrivateAdminProtection):
            protection.mark_menu(chat_id, message_id, proof)

    def _save(self):
        write_json(self.state_file, self.state)

    def back_destination(self, chat_id):
        return self.state.get("navigation", {}).get(str(chat_id), {}).get("back", "adm:menu")

    def is_back_destination(self, chat_id, data):
        return data == self.back_destination(chat_id)

    def _navigation_markup(self, chat_id, text, markup):
        if chat_id > 0 and is_admin(chat_id):
            from admin_navigation import rewrite_back
            state = _feedback.get()
            call = state["call"] if state else None
            if state and call is None and state.get("navigation_action"):
                call = SimpleNamespace(data=state["navigation_action"])
            return rewrite_back(self.state, chat_id, text, markup, call)
        return markup

    def adopt_menu(self, message):
        """Adopt the clicked admin menu instead of rejecting it as stale."""
        message_id = getattr(message, "message_id", None)
        if message_id is None:
            return True
        chat = getattr(message, "chat", None)
        if chat is None or chat.id <= 0 or not is_admin(chat.id):
            return True
        with self._lock:
            key = str(chat.id)
            current = self.state["menus"].get(key)
            if current is not None and current != message_id:
                self._delete(chat.id, current)
            self.state["menus"][key] = message_id
            self._mark_menu(chat.id, message_id, message)
            markup = getattr(message, "reply_markup", None)
            if markup is not None:
                self.state.setdefault("keyboards", {})[key] = markup.to_dict()
                text = (
                    getattr(message, "html_text", None)
                    if getattr(message, "entities", None) else None
                )
                text = (
                    text or getattr(message, "text", None)
                    or getattr(message, "caption", None)
                    or "תפריט מנהל מערכת"
                )
                options = {"parse_mode": "HTML"} if getattr(message, "entities", None) else {}
                self._menu_content[chat.id] = (text, markup, options)
            self._save()
            return True

    def fresh_menu(self, chat_id, text, reply_markup):
        reply_markup = self._layout(
            chat_id, self._navigation_markup(chat_id, text, reply_markup), text,
        )
        self._menu_content[chat_id] = (text, reply_markup, {})
        with self._lock:
            key = str(chat_id)
            current = self.state["menus"].get(key)
            # An explicit /help, /users or settings request must be visible at
            # the bottom of the chat, rather than silently editing an old menu.
            sent = self.raw.send_message(chat_id, text, reply_markup=reply_markup)
            self.state["menus"][key] = sent.message_id
            self.state.setdefault("keyboards", {})[key] = reply_markup.to_dict()
            self._mark_menu(chat_id, sent.message_id, sent)
            self._save()
            if current:
                self._delete(chat_id, current)
            return sent

    def register_external_menu(self, chat_id, sent, text, reply_markup):
        """Track a bot-authored media preview as the current editable admin menu."""
        if chat_id <= 0 or not is_admin(chat_id):
            return sent
        message_id = getattr(sent, "message_id", None)
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("לא ניתן לזהות את הודעת התצוגה המקדימה.")
        reply_markup = self._layout(chat_id, reply_markup, text)
        key = str(chat_id)
        with self._lock:
            current = self.state["menus"].get(key)
            self._menu_content[chat_id] = (text, reply_markup, {})
            self.state["menus"][key] = message_id
            self.state.setdefault("keyboards", {})[key] = reply_markup.to_dict()
            self._mark_menu(chat_id, message_id, sent)
            self._save()
            if current and current != message_id:
                self._delete(chat_id, current)
        return sent

    def hide_menu(self, chat_id):
        with self._lock:
            current = self.state["menus"].pop(str(chat_id), None)
            if current:
                self._delete(chat_id, current)
                self._save()

    def close_menu(self, chat_id, message_id=None):
        key = str(chat_id)
        with self._lock:
            current = message_id or self.state.get("menus", {}).get(key)
            if current:
                try:
                    self.raw.edit_message_text(
                        "תפריט הניהול נסגר.",
                        chat_id=chat_id,
                        message_id=current,
                        reply_markup=None,
                    )
                except Exception as exc:
                    logging.warning("Could not close the admin menu (%s).", type(exc).__name__)
                    try:
                        self.raw.edit_message_reply_markup(
                            chat_id=chat_id, message_id=current, reply_markup=None,
                        )
                    except Exception:
                        pass
            if self.state.get("menus", {}).get(key) == current:
                self.state["menus"].pop(key, None)
                self.state.get("keyboards", {}).pop(key, None)
                self.state.get("navigation", {}).pop(key, None)
                self._menu_content.pop(chat_id, None)
                self._save()

    def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        if reply_markup is None:
            if collect_private_feedback(chat_id, text):
                return SimpleNamespace(message_id=None)
            sent = self.raw.send_message(chat_id, text, **kwargs)
            delete_later(self.raw, chat_id, sent.message_id, 5.0)
            return sent
        reply_markup = self._layout(
            chat_id, self._navigation_markup(chat_id, text, reply_markup), text,
        )
        state = _feedback.get()
        self._menu_content[chat_id] = (text, reply_markup, kwargs)
        with self._lock:
            current = self.state["menus"].get(str(chat_id))
            if current is None:
                if state and state["call"] is None:
                    if state.get("allow_initial_menu"):
                        return self.fresh_menu(chat_id, text, reply_markup)
                    logging.warning("No private edit menu available; reopen /users.")
                    return SimpleNamespace(message_id=None)
                return self.fresh_menu(chat_id, text, reply_markup)
            try:
                sent = self.raw.edit_message_text(
                    text, chat_id=chat_id, message_id=current,
                    reply_markup=reply_markup, **kwargs,
                )
                self.state.setdefault("keyboards", {})[str(chat_id)] = reply_markup.to_dict()
                self._save()
                return sent
            except Exception as exc:
                description = getattr(exc, "description", "").lower()
                if "message is not modified" in description:
                    return SimpleNamespace(message_id=current)
                if "message to edit not found" in description or "message can't be edited" in description:
                    if state and state["call"] is None:
                        logging.warning("Private edit menu was removed; reopen /users.")
                        return SimpleNamespace(message_id=None)
                    return self.fresh_menu(chat_id, text, reply_markup)
                logging.warning("Admin menu editing failed (%s).", type(exc).__name__)
                self.send_message(chat_id, "לא ניתן לעדכן את התפריט כעת. נסה שוב או שלח /users.")
                return SimpleNamespace(message_id=current)

    def prompt(self, chat_id, text):
        return self.send_message(chat_id, text, reply_markup=build_menu([
            ("ביטול וחזרה לתפריט ראשי", "adm:menu"),
        ]))

    @staticmethod
    def _layout(chat_id, markup, text=None):
        if chat_id > 0 and is_admin(chat_id):
            from telebot.types import InlineKeyboardMarkup
            if isinstance(markup, InlineKeyboardMarkup):
                grid = InlineKeyboardMarkup(row_width=1)
                buttons = [button for row in markup.keyboard for button in row]
                if buttons:
                    grid.add(*buttons, row_width=1)
                return grid
        return markup


class AdminInputCleanupMiddleware(BaseMiddleware):
    def __init__(self, bot, dashboard=None):
        super().__init__()
        self.bot = bot
        self.dashboard = dashboard
        self.update_sensitive = True
        self.update_types = ["message"]

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        if user is None or not is_admin(user.id):
            return None
        text = (getattr(message, "text", None) or "").strip()
        private = message.chat.type == "private"
        if private:
            data["delete_admin_input"] = False
            return None
        command = text.split()[0].split("@")[0] if text else ""
        administrative = command in {"/users", "/cancel", "/set_support_chat"} or text in {"הגדרות", "משתמשים"}
        pending = self.dashboard is not None and self.dashboard.has_pending_input(message)
        if not private and getattr(message, "reply_to_message", None) is not None:
            return None
        data["delete_admin_input"] = administrative or pending
        return None

    def post_process_message(self, message, data, exception):
        user = getattr(message, "from_user", None)
        message_id = getattr(message, "message_id", None)
        if message.chat.type != "private" and data.get("delete_admin_input") and user is not None and is_admin(user.id) and message_id is not None:
            try:
                self.bot.delete_message(message.chat.id, message_id)
            except Exception as exc:
                logging.warning("Admin input cleanup failed (%s).", type(exc).__name__)
