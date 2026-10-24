"""Independent persistent first-entry and returning-user welcome profiles."""

import logging
from pathlib import Path

from telebot.types import ReplyKeyboardRemove

from bot_storage import BotReply, format_telegram_text, get_start_reply
from management_config import _LOCK, load_config, save_json
from reply_keyboard import build_reply_keyboard, load_settings
from start_keyboard import KEY, build_keyboard
from system_messages import message_text

PROFILES = {
    "before": "פתיחה (לפני הזנת קוד)",
    "first": "פתיחה (לאחר אימות קוד)",
    "returning": "פתיחה למשתמש מאומת חוזר",
}


def before_code_keyboard(config_file, *, ignore_enabled=False):
    """Use explicit pre-code buttons, preserving legacy prompt settings only
    until this separate keyboard is configured by an administrator."""
    from bot_storage import InlineButtonSpec, build_inline_keyboard
    config = load_config(config_file)
    if not ignore_enabled and not config.get("start_before_inline_enabled", True):
        return None
    if button_key("before") in config:
        return build_keyboard(path=config_file, key=button_key("before"))
    url = config.get("access_prompt_button_url")
    buttons = [
        InlineButtonSpec(**button)
        for button in config.get("unauthorized_buttons", [])
        if not (url and button.get("text", "").strip() in {"לחצו כאן", "לחץ כאן"})
    ]
    if url:
        buttons.append(InlineButtonSpec(text="לחצו כאן", url=url))
    return build_inline_keyboard(tuple(buttons))


def button_key(profile):
    if profile not in PROFILES:
        raise ValueError("Unknown welcome profile.")
    return f"start_{profile}_buttons"


def was_welcomed(user_id, config_file):
    return user_id in load_config(config_file).get("welcome_seen_users", [])


def mark_welcomed(user_id, config_file):
    with _LOCK:
        config = load_config(config_file)
        users = config.setdefault("welcome_seen_users", [])
        if user_id not in users:
            users.append(user_id)
            save_json(config_file, config)


def send_welcome(bot, chat_id, profile, config_file, commands_file, *,
                 fallback_text=None, preview=False, legacy_inline=None,
                 use_custom_text=True, inherit_inline=True):
    """One real welcome message; apply reply and inline keyboards separately."""
    config_file = Path(config_file)
    config = load_config(config_file)
    if profile == "before":
        text, mode = format_telegram_text(message_text("access_prompt", config_file, commands_file) or "\u2063")
        return bot.send_message(
            chat_id, text, parse_mode=mode,
            reply_markup=before_code_keyboard(config_file),
            disable_web_page_preview=True,
        )
    key = button_key(profile)
    custom = config.get("system_messages", {}).get(f"{profile}_welcome")
    body = custom if use_custom_text and isinstance(custom, str) and custom.strip() else fallback_text
    if body is None:
        body = message_text(f"{profile}_welcome", config_file, commands_file)
    try:
        original = get_start_reply(commands_file)
    except FileNotFoundError:
        # A standalone VIP store can still deliver its configured welcome.
        original = BotReply(body)
    inline_enabled = config.get(
        f"start_{profile}_inline_enabled",
        True if profile == "first" else original.buttons_enabled,
    )
    # The post-code message owns its buttons. Never inherit a pre-code,
    # global /start or legacy support keyboard when none was configured.
    if (profile == "first" or not inherit_inline) and key not in config:
        inline = None
    elif key not in config and KEY not in config and legacy_inline is not None:
        inline = legacy_inline if inline_enabled else None
    else:
        inline = build_keyboard(original.buttons, path=config_file, key=key, enabled=inline_enabled)
    settings = load_settings(config_file.with_name("reply_keyboard.json"))
    enabled = config.get(f"start_{profile}_main_keyboard_enabled", True)
    reply = build_reply_keyboard(settings) if enabled else None
    text, mode = format_telegram_text(body)
    # Telegram accepts only one markup per send. Set the persistent bottom
    # keyboard with the welcome, then attach inline controls to that same
    # message. No dummy text or duplicate welcome is needed.
    remove = not enabled or (chat_id in settings["shown_to"] and reply is None)
    markup = reply or (ReplyKeyboardRemove() if remove else inline)
    sent = bot.send_message(
        chat_id, text, parse_mode=mode, reply_markup=markup,
        disable_web_page_preview=True,
    )
    if inline is not None and (reply is not None or remove):
        try:
            bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=sent.message_id, reply_markup=inline,
            )
        except Exception:
            logging.exception("Could not attach configured %s welcome buttons.", profile)
    if not preview:
        mark_welcomed(chat_id, config_file)
    return sent
