"""Persistent /start inline buttons. Never create built-in welcome buttons."""

import copy
from uuid import uuid4
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from management_config import CONFIG_FILE, _LOCK, load_config, save_json, resolve_reply, normalize_button_link
from bot_storage import BotReply, InlineButtonSpec

KEY = "start_inline_buttons"


def custom_only(buttons):
    # These reserved IDs identify records auto-seeded by the retired keyboard.
    # Admin-created buttons have unique IDs; customized URL links are preserved.
    return [b for b in buttons if not (
        b.get("id") in {"media", "code", "faq", "support"} and b.get("callback_data")
    )]


def validate_button(button):
    text = button.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 64:
        raise ValueError("כיתוב הכפתור חייב להכיל 1–64 תווים.")
    if type(button.get("enabled", True)) is not bool:
        raise ValueError("מצב הכפתור חייב להיות מופעל או כבוי.")
    if bool(button.get("url")) == bool(button.get("callback_data")):
        raise ValueError("יש להגדיר קישור או callback אחד בלבד.")
    if button.get("callback_data"):
        value = button["callback_data"]
        if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 64:
            raise ValueError("callback חייב להכיל 1–64 בתים.")
    else:
        value = button["url"]
        if not isinstance(value, str):
            raise ValueError("קישור אינו תקין.")
        if value.startswith("@"):
            normalize_button_link(value)
        elif not (value.startswith("{") and value.endswith("}")):
            parsed = urlparse(value)
            if parsed.scheme not in {"https", "http", "tg"} or not parsed.netloc:
                raise ValueError("שלח URL תקין או @username.")
    return button


def load_buttons(path=CONFIG_FILE, legacy=(), *, persist=False, key=KEY):
    with _LOCK:
        config = load_config(path)
        if key in config:
            buttons = copy.deepcopy(config[key])
            if not isinstance(buttons, list):
                raise ValueError("רשימת כפתורי הפתיחה אינה תקינה.")
            cleaned = custom_only(buttons)
            if cleaned != buttons:
                buttons = cleaned
                config[key] = buttons
                save_json(path, config)
        else:
            # Only retain custom URL buttons explicitly configured previously.
            buttons = load_buttons(path, legacy) if key != KEY else [
                {"id": uuid4().hex[:12], "text": b.text, "url": b.url,
                 "enabled": True, "legacy": True}
                for b in legacy
            ]
            if persist:
                config[key] = buttons
                save_json(path, config)
        if not isinstance(buttons, list) or len(buttons) > 40:
            raise ValueError("ניתן להגדיר עד 40 כפתורי פתיחה.")
        for button in buttons:
            validate_button(button)
        return buttons


def save_buttons(buttons, path=CONFIG_FILE, *, key=KEY):
    if not isinstance(buttons, list) or len(buttons) > 40:
        raise ValueError("ניתן להגדיר עד 40 כפתורי פתיחה.")
    clean = custom_only(copy.deepcopy(buttons))
    for button in clean:
        validate_button(button)
    with _LOCK:
        config = load_config(path)
        config[key] = clean
        save_json(path, config)


def replace_legacy_urls(buttons, path):
    """Keep the older welcome-text wizard compatible with the new editor."""
    with _LOCK:
        config = load_config(path)
        if KEY not in config:
            return
        current = [b for b in config[KEY] if not b.get("legacy")]
        current.extend(
            {"id": uuid4().hex[:12], "text": b["text"], "url": b["url"],
             "enabled": True, "legacy": True}
            for b in buttons
        )
        save_buttons(current, path)


def build_keyboard(legacy=(), *, enabled=True, path=CONFIG_FILE, key=KEY):
    if not enabled:
        return None
    markup = InlineKeyboardMarkup(row_width=1)
    for button in load_buttons(path, legacy, key=key):
        if not button.get("enabled", True):
            continue
        if button.get("url"):
            resolved = resolve_reply(
                BotReply("", buttons=(InlineButtonSpec(button["text"], button["url"]),)), path,
            )
            url = resolved.buttons[0].url
            if url.startswith("@"):
                url = normalize_button_link(url)
            markup.add(InlineKeyboardButton(button["text"], url=url))
        else:
            markup.add(InlineKeyboardButton(button["text"], callback_data=button["callback_data"]))
    return markup if markup.keyboard else None
