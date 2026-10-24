"""Persistent user-facing messages and adapters to the existing welcome editor."""

import json
from pathlib import Path

from bot_storage import COMMANDS_FILE, load_bot_data, update_start_text, format_telegram_text
from management_config import CONFIG_FILE, _LOCK, load_config, save_json

MESSAGES = {
    "welcome": ("👋 הודעת פתיחה /start", None),
    "first_welcome": ("👋 הודעת כניסה ראשונה", None),
    "returning_welcome": ("🔄 הודעת משתמש חוזר", None),
    "request_received": ("📩 הפנייה התקבלה", None),
    "support_prompt": ("💬 הודעת כתיבת פנייה לתמיכה", None),
    "chat_closed": ("🛑 הודעת סיום שיחה", None),
    "delivery_error": ("⚠️ שגיאת העברת פנייה", None),
    "media_unavailable": ("🎬 תוכן אינו זמין", None),
    "file_unavailable": ("📎 קובץ אינו זמין", None),
    "drive_unavailable": ("📁 הודעת דרייב מוקפצת", None),
    "invalid_code": ("✏️ הודעת קוד שגוי", None),
    "access_prompt": ("🔒 הודעת דרישת קוד גישה", None),
    "standard_code_success": ("✅ הודעת אישור קוד רגיל", None),
    "vip_code_success": ("⭐ הודעת אישור קוד VIP", None),
}

def default_message(key):
    """All user-facing defaults are data, never Python response literals."""
    defaults = json.loads(
        Path(__file__).with_name("system_message_defaults.json").read_text(encoding="utf-8")
    )
    return defaults[key]


def message_text(key, config_file=CONFIG_FILE, commands_file=COMMANDS_FILE, *, access_file=None):
    if key not in MESSAGES:
        raise ValueError("הודעת מערכת לא מוכרת.")
    if key == "welcome":
        return load_bot_data(commands_file)["start"]["text"]
    if key == "vip_code_success":
        from vip_access import VIPSettingsStore
        return VIPSettingsStore(Path(config_file).with_name("vip_settings.json")).snapshot()["response_text"]
    try:
        config = load_config(config_file)
    except FileNotFoundError:
        config = {}
    if key == "invalid_code":
        saved = config.get("invalid_code_message")
        return saved if isinstance(saved, str) and saved.strip() else ""
    saved = config.get("system_messages", {}).get(key)
    if isinstance(saved, str) and saved.strip():
        return saved
    if key == "first_welcome":
        return message_text("standard_code_success", config_file, commands_file, access_file=access_file)
    if key == "returning_welcome":
        return load_bot_data(commands_file)["start"]["text"]
    if key == "request_received":
        return config.get("unknown_command_text") or default_message(key)
    if key == "access_prompt":
        return config.get("unauthorized_message") or default_message(key)
    if key == "standard_code_success":
        from access_control import load_access
        try:
            welcome = load_access(access_file or Path(config_file).with_name("access_settings.json")).get("standard_welcome_text")
            if welcome:
                return welcome
        except FileNotFoundError:
            pass
    return default_message(key)


def update_message(key, text, config_file=CONFIG_FILE, commands_file=COMMANDS_FILE):
    if key not in MESSAGES:
        raise ValueError("הודעת מערכת לא מוכרת.")
    limit = 200 if key == "drive_unavailable" else 3500
    if not isinstance(text, str) or not text.strip() or len(text) > limit:
        raise ValueError(f"שלח טקסט שאינו ריק, עד {limit} תווים.")
    if key == "welcome":
        update_start_text(text, commands_file)
        return
    if key == "vip_code_success":
        from vip_access import VIPSettingsStore
        VIPSettingsStore(Path(config_file).with_name("vip_settings.json")).set_response(text)
        return
    with _LOCK:
        config = load_config(config_file)
        config.setdefault("system_messages", {})[key] = text
        if key == "invalid_code":
            config["invalid_code_message"] = text
        if key == "request_received":
            config["unknown_command_text"] = text
        if key == "access_prompt":
            config["unauthorized_message"] = text
        save_json(config_file, config)


def send_system_message(bot, chat_id, key, config_file=CONFIG_FILE):
    text, mode = format_telegram_text(message_text(key, config_file))
    return bot.send_message(
        chat_id, text, parse_mode=mode, disable_web_page_preview=True,
    )
