"""Persistent user-facing reply keyboard settings."""

from __future__ import annotations

import json
import threading
import copy
import functools
import inspect
import logging
from pathlib import Path

from telebot.handler_backends import BaseMiddleware
from telebot.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

REPLY_KEYBOARD_FILE = Path(__file__).resolve().with_name("reply_keyboard.json")
DEFAULT_SETTINGS = {
    "enabled": False, "buttons_per_row": 2, "row_count": 2, "buttons": [],
    "shown_to": [],
}
_LOCK = threading.RLock()


def load_settings(path=REPLY_KEYBOARD_FILE) -> dict:
    with _LOCK:
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(path))
        except FileNotFoundError:
            return copy.deepcopy(DEFAULT_SETTINGS)
        if not isinstance(data, dict):
            raise ValueError("Reply keyboard settings must be a JSON object.")
        result = {**DEFAULT_SETTINGS, **data}
        if type(result["enabled"]) is not bool:
            raise ValueError("Reply keyboard enabled must be boolean.")
        if type(result["buttons_per_row"]) is not int or not 1 <= result["buttons_per_row"] <= 4:
            raise ValueError("Buttons per row must be between 1 and 4.")
        if type(result["row_count"]) is not int or not 1 <= result["row_count"] <= 8:
            raise ValueError("Reply keyboard rows must be between 1 and 8.")
        if not isinstance(result["buttons"], list):
            raise ValueError("Reply keyboard buttons must be a list.")
        if not isinstance(result["shown_to"], list) or any(
            not isinstance(user_id, int) for user_id in result["shown_to"]
        ):
            raise ValueError("Reply keyboard delivery records must be a list of user IDs.")
        for button in result["buttons"]:
            if not isinstance(button, dict):
                raise ValueError("A reply keyboard button is invalid.")
            text = button.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > 64:
                raise ValueError("Reply keyboard button text must be 1–64 characters.")
        if len(result["buttons"]) > 40:
            raise ValueError("The reply keyboard can contain at most 40 buttons.")
        return result


def save_settings(settings: dict, path=REPLY_KEYBOARD_FILE) -> None:
    if not isinstance(settings, dict):
        raise ValueError("Reply keyboard settings must be a JSON object.")
    candidate = {**DEFAULT_SETTINGS, **settings}
    if type(candidate["enabled"]) is not bool:
        raise ValueError("Reply keyboard enabled must be boolean.")
    if type(candidate["buttons_per_row"]) is not int or not 1 <= candidate["buttons_per_row"] <= 4:
        raise ValueError("Buttons per row must be between 1 and 4.")
    if type(candidate["row_count"]) is not int or not 1 <= candidate["row_count"] <= 8:
        raise ValueError("Reply keyboard rows must be between 1 and 8.")
    if not isinstance(candidate["buttons"], list):
        raise ValueError("Reply keyboard buttons must be a list.")
    if not isinstance(candidate["shown_to"], list) or any(
        not isinstance(user_id, int) for user_id in candidate["shown_to"]
    ):
        raise ValueError("Reply keyboard delivery records must be a list of user IDs.")
    for button in candidate["buttons"]:
        text = button.get("text") if isinstance(button, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 64:
            raise ValueError("Reply keyboard button text must be 1–64 characters.")
        if "response" in button and (
            not isinstance(button["response"], str) or not button["response"].strip()
            or len(button["response"]) > 4096
        ):
            raise ValueError("Reply keyboard response must be 1–4096 characters.")
    if len(candidate["buttons"]) > 40:
        raise ValueError("The reply keyboard can contain at most 40 buttons.")
    target = Path(path)
    with _LOCK:
        from persistent_json import write_json_document
        write_json_document(target, candidate)


def build_reply_keyboard(settings=None, path=REPLY_KEYBOARD_FILE):
    settings = settings or load_settings(path)
    if not settings["enabled"] or not settings["buttons"]:
        return None
    keyboard = ReplyKeyboardMarkup(
        resize_keyboard=True,
        is_persistent=False,
        row_width=settings["buttons_per_row"],
        selective=False,
    )
    width = settings["buttons_per_row"]
    buttons = [KeyboardButton(item["text"]) for item in settings["buttons"]]
    row_count = min(
        len(buttons),
        max(settings["row_count"], (len(buttons) + width - 1) // width),
    )
    offset = 0
    for row_index in range(row_count):
        remaining = len(buttons) - offset
        rows_left = row_count - row_index
        this_row_count = (remaining + rows_left - 1) // rows_left
        keyboard.row(*buttons[offset:offset + this_row_count])
        offset += this_row_count
    return keyboard


def get_button_trigger(text: str, path=REPLY_KEYBOARD_FILE):
    """Match a configured keyboard label exactly, not arbitrary free text."""
    settings = load_settings(path)
    if not settings["enabled"]:
        return None
    for button in settings["buttons"]:
        if text.strip() == button["text"].strip():
            trigger = button.get("trigger", button.get("command", button["text"]))
            if isinstance(trigger, str) and trigger.strip():
                return trigger.strip()
    return None


def remove_reply_keyboard():
    return ReplyKeyboardRemove()


def send_button_response(bot, message, path=REPLY_KEYBOARD_FILE):
    from bot_storage import format_telegram_text
    settings = load_settings(path)
    if not settings["enabled"]:
        return False
    for button in settings["buttons"]:
        if (message.text or "").strip() == button["text"] and "response" in button:
            body, mode = format_telegram_text(button["response"])
            bot.reply_to(message, body, parse_mode=mode, disable_web_page_preview=True)
            return True
    return False


def apply_user_keyboard(bot, chat_id: int, *, force: bool = True) -> None:
    """Compatibility hook: never send a standalone keyboard status message."""
    install_response_keyboard(bot)


def install_response_keyboard(bot):
    """Attach the configured keyboard to real private responses, not dummy text.

    Explicit markup (especially inline menus) is never replaced. Telegram
    cannot update a reply keyboard silently or combine it with inline markup.
    """
    if getattr(bot, "_response_keyboard_installed", False) is True:
        return
    bot._response_keyboard_installed = True
    for name in (
        "send_message", "send_photo", "send_video", "send_document",
        "send_audio", "send_voice", "send_animation",
    ):
        method = getattr(bot, name, None)
        if not callable(method):
            continue
        signature = inspect.signature(method)

        def wrap(original, call_signature):
            @functools.wraps(original)
            def send(*args, **kwargs):
                bound = call_signature.bind_partial(*args, **kwargs)
                chat_id = bound.arguments.get("chat_id")
                extra = bound.arguments.get("kwargs", {})
                markup = bound.arguments.get("reply_markup", extra.get("reply_markup"))
                if isinstance(chat_id, int) and chat_id > 0 and markup is None:
                    try:
                        settings = load_settings()
                        from vip_experience import keyboard_for_user
                        markup = keyboard_for_user(chat_id, settings) or remove_reply_keyboard()
                        if "reply_markup" in call_signature.parameters:
                            bound.arguments["reply_markup"] = markup
                        else:
                            bound.arguments.setdefault("kwargs", {})["reply_markup"] = markup
                    except Exception:
                        logging.exception("Could not attach reply keyboard to response.")
                return original(*bound.args, **bound.kwargs)
            return send

        setattr(bot, name, wrap(method, signature))


class ReplyKeyboardMiddleware(BaseMiddleware):
    """Deliver keyboard updates once when private users next interact."""

    update_sensitive = True
    update_types = ["message"]

    def __init__(self, bot):
        super().__init__()
        self._bot = bot
        install_response_keyboard(bot)

    def pre_process_message(self, message, data):
        return None

    def post_process_message(self, message, data, exception=None):
        return None
