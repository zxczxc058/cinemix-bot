"""Validated, atomic JSON settings shared by all managed groups."""

import json
import threading
from pathlib import Path

from management_config import normalize_button_link

GROUP_SETTINGS_FILE = Path(__file__).resolve().with_name("group_settings.json")
WELCOME_DELETE_MODES = {"after_5_minutes", "delete_previous", "none"}
WELCOME_FIELDS = {
    "welcome_text", "welcome_enabled", "welcome_delete_mode",
    "welcome_button_enabled", "welcome_button_text", "welcome_button_url",
}
DEFAULT_GROUP_SETTINGS = {
    "welcome_text": "ברוכים הבאים {user_mention} לקבוצת {group_name}!",
    "welcome_enabled": True,
    "welcome_delete_mode": "after_5_minutes",
    "welcome_button_enabled": False,
    "welcome_button_text": "📜 חוקי הקבוצה",
    "welcome_button_url": None,
    "welcome_by_chat": {},
    "last_welcome_messages": {},
    "delete_links": True,
    "delete_service_messages": False,
    "blacklist": [],
    "link_warning_text": "{user_mention}, קישורים אינם מורשים בקבוצה! אזהרה {warnings_count}/{max_warnings}.",
    "command_rejection_text": "⛔ אין לך הרשאה לבצע פקודה זו!",
    "command_rejection_notices": {},
    "max_warnings": 3,
    "night_lock": {
        "enabled": False,
        "close_time": "23:00",
        "open_time": "07:00",
        "close_message": "לילה טוב! הקבוצה סגורה להודעות עד הבוקר.",
        "open_message": "בוקר טוב! הקבוצה פתוחה שוב להודעות.",
    },
}
_LOCK = threading.RLock()


def load_group_settings(path=GROUP_SETTINGS_FILE, chat_id=None) -> dict:
    with _LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        if not isinstance(data, dict):
            raise ValueError("Group settings must be a JSON object.")
        text = data.get("welcome_text")
        if not isinstance(text, str) or not text.strip() or len(text) > 3000:
            raise ValueError("Welcome text must contain 1–3000 characters.")
        if type(data.get("delete_links")) is not bool:
            raise ValueError("delete_links must be a boolean.")
        data.setdefault("welcome_enabled", True)
        if type(data["welcome_enabled"]) is not bool:
            raise ValueError("welcome_enabled must be a boolean.")
        data.setdefault("welcome_delete_mode", DEFAULT_GROUP_SETTINGS["welcome_delete_mode"])
        if data["welcome_delete_mode"] not in WELCOME_DELETE_MODES:
            raise ValueError("Invalid welcome deletion mode.")
        data.setdefault("welcome_button_enabled", DEFAULT_GROUP_SETTINGS["welcome_button_enabled"])
        if type(data["welcome_button_enabled"]) is not bool:
            raise ValueError("welcome_button_enabled must be a boolean.")
        data.setdefault("welcome_button_text", DEFAULT_GROUP_SETTINGS["welcome_button_text"])
        button_text = data["welcome_button_text"]
        if not isinstance(button_text, str) or not button_text.strip() or len(button_text) > 64:
            raise ValueError("Welcome button text must contain 1–64 characters.")
        data.setdefault("welcome_button_url", None)
        if data["welcome_button_url"] is not None:
            data["welcome_button_url"] = normalize_button_link(data["welcome_button_url"])
        data.setdefault("last_welcome_messages", {})
        if not isinstance(data["last_welcome_messages"], dict):
            raise ValueError("last_welcome_messages must be an object.")
        if any(
            not str(chat_id).lstrip("-").isdigit()
            or type(message_id) is not int
            or message_id < 1
            for chat_id, message_id in data["last_welcome_messages"].items()
        ):
            raise ValueError("Invalid saved welcome message reference.")
        data.setdefault("welcome_by_chat", {})
        if not isinstance(data["welcome_by_chat"], dict):
            raise ValueError("welcome_by_chat must be an object.")
        for group_id, overrides in data["welcome_by_chat"].items():
            if (
                not isinstance(group_id, str)
                or not group_id.startswith("-")
                or not group_id[1:].isdigit()
                or int(group_id) >= 0
                or str(int(group_id)) != group_id
                or not isinstance(overrides, dict)
                or set(overrides) - WELCOME_FIELDS
            ):
                raise ValueError("Invalid per-group welcome settings.")
            if "welcome_text" in overrides and (
                not isinstance(overrides["welcome_text"], str)
                or not overrides["welcome_text"].strip()
                or len(overrides["welcome_text"]) > 3000
            ):
                raise ValueError("Invalid per-group welcome text.")
            if "welcome_enabled" in overrides and type(overrides["welcome_enabled"]) is not bool:
                raise ValueError("Invalid per-group welcome toggle.")
            if (
                "welcome_delete_mode" in overrides
                and overrides["welcome_delete_mode"] not in WELCOME_DELETE_MODES
            ):
                raise ValueError("Invalid per-group welcome deletion mode.")
            if "welcome_button_enabled" in overrides and type(overrides["welcome_button_enabled"]) is not bool:
                raise ValueError("Invalid per-group welcome button toggle.")
            if "welcome_button_text" in overrides and (
                not isinstance(overrides["welcome_button_text"], str)
                or not overrides["welcome_button_text"].strip()
                or len(overrides["welcome_button_text"]) > 64
            ):
                raise ValueError("Invalid per-group welcome button text.")
            if overrides.get("welcome_button_url") is not None:
                overrides["welcome_button_url"] = normalize_button_link(
                    overrides["welcome_button_url"],
                )
        data.setdefault("delete_service_messages", DEFAULT_GROUP_SETTINGS["delete_service_messages"])
        if type(data["delete_service_messages"]) is not bool:
            raise ValueError("delete_service_messages must be a boolean.")
        data.setdefault("link_warning_text", DEFAULT_GROUP_SETTINGS["link_warning_text"])
        warning_text = data["link_warning_text"]
        if not isinstance(warning_text, str) or not warning_text.strip() or len(warning_text) > 3000:
            raise ValueError("Link warning text must contain 1–3000 characters.")
        data.setdefault(
            "command_rejection_text",
            DEFAULT_GROUP_SETTINGS["command_rejection_text"],
        )
        rejection_text = data["command_rejection_text"]
        if (
            not isinstance(rejection_text, str)
            or not rejection_text.strip()
            or len(rejection_text) > 3000
        ):
            raise ValueError("Command rejection text must contain 1–3000 characters.")
        data.setdefault("command_rejection_notices", {})
        rejection_notices = data["command_rejection_notices"]
        if not isinstance(rejection_notices, dict):
            raise ValueError("command_rejection_notices must be an object.")
        for group_id, user_ids in rejection_notices.items():
            if (
                not isinstance(group_id, str)
                or not group_id.startswith("-")
                or not group_id[1:].isdigit()
                or int(group_id) >= 0
                or not isinstance(user_ids, list)
                or any(type(user_id) is not int or user_id == 0 for user_id in user_ids)
            ):
                raise ValueError("Invalid command rejection notice records.")
        data.setdefault("max_warnings", DEFAULT_GROUP_SETTINGS["max_warnings"])
        data.setdefault("blacklist", [])
        if (
            not isinstance(data["blacklist"], list)
            or len(data["blacklist"]) > 200
            or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 500
                   for entry in data["blacklist"])
        ):
            raise ValueError("Blacklist must contain at most 200 nonempty entries of up to 500 characters.")
        validate_max_warnings(data["max_warnings"])
        data.setdefault("night_lock", DEFAULT_GROUP_SETTINGS["night_lock"].copy())
        validate_night_settings(data["night_lock"])
        if chat_id is not None:
            if type(chat_id) is not int or chat_id >= 0:
                raise ValueError("A group chat ID must be a negative integer.")
            data.update(data["welcome_by_chat"].get(str(chat_id), {}))
        return data


def _save(data: dict, path) -> None:
    from persistent_json import write_json_document
    write_json_document(path, data)

    from admin_edit_flow import record_commit
    record_commit(path)


def blacklist_key(entry: str) -> str:
    """Stable callback reference; stale buttons must never delete another entry."""
    import hashlib
    return hashlib.sha256(entry.casefold().encode("utf-8")).hexdigest()[:20]


def add_blacklist_entry(entry: str, path=GROUP_SETTINGS_FILE) -> None:
    entry = entry.strip()
    if not entry or len(entry) > 500:
        raise ValueError("שלח מילה, ביטוי או קישור באורך 1–500 תווים.")
    with _LOCK:
        settings = load_group_settings(path)
        if any(item.casefold() == entry.casefold() for item in settings["blacklist"]):
            raise ValueError("הפריט כבר קיים ברשימה השחורה.")
        if len(settings["blacklist"]) >= 200:
            raise ValueError("הרשימה יכולה להכיל עד 200 פריטים.")
        settings["blacklist"].append(entry)
        _save(settings, path)


def remove_blacklist_entry(key: str, path=GROUP_SETTINGS_FILE) -> bool:
    with _LOCK:
        settings = load_group_settings(path)
        remaining = [entry for entry in settings["blacklist"] if blacklist_key(entry) != key]
        if len(remaining) == len(settings["blacklist"]):
            return False
        settings["blacklist"] = remaining
        _save(settings, path)
        return True


def _validate_group_chat_id(chat_id):
    if type(chat_id) is not int or chat_id >= 0:
        raise ValueError("A group chat ID must be a negative integer.")


def _welcome_overrides(data, chat_id):
    _validate_group_chat_id(chat_id)
    return data["welcome_by_chat"].setdefault(str(chat_id), {})


def update_group_welcome(text: str, path=GROUP_SETTINGS_FILE, chat_id=None) -> None:
    if not text.strip() or len(text) > 3000:
        raise ValueError("הברכה חייבת להכיל בין 1 ל־3000 תווים.")
    with _LOCK:
        data = load_group_settings(path)
        if chat_id is None:
            data["welcome_text"] = text
        else:
            _welcome_overrides(data, chat_id)["welcome_text"] = text
        _save(data, path)


def toggle_link_filter(path=GROUP_SETTINGS_FILE) -> bool:
    return toggle_group_flag("delete_links", path)


def toggle_group_flag(key: str, path=GROUP_SETTINGS_FILE, chat_id=None) -> bool:
    if key not in {
        "welcome_enabled", "welcome_button_enabled", "delete_links",
        "delete_service_messages",
    }:
        raise ValueError("Invalid group setting.")
    if chat_id is not None and key not in {
        "welcome_enabled", "welcome_button_enabled",
    }:
        raise ValueError("This setting is shared across groups.")
    with _LOCK:
        data = load_group_settings(path, chat_id=chat_id)
        enabled = not data[key]
        if chat_id is None:
            data[key] = enabled
        else:
            raw_data = load_group_settings(path)
            _welcome_overrides(raw_data, chat_id)[key] = enabled
            data = raw_data
        _save(data, path)
        return enabled


def set_group_flag(key: str, enabled: bool, path=GROUP_SETTINGS_FILE, chat_id=None) -> None:
    if key not in {
        "welcome_enabled", "welcome_button_enabled", "delete_links",
        "delete_service_messages",
    } or type(enabled) is not bool:
        raise ValueError("Invalid group setting.")
    if chat_id is not None and key not in {
        "welcome_enabled", "welcome_button_enabled",
    }:
        raise ValueError("This setting is shared across groups.")
    with _LOCK:
        data = load_group_settings(path)
        if chat_id is None:
            data[key] = enabled
        else:
            _welcome_overrides(data, chat_id)[key] = enabled
        _save(data, path)


def set_welcome_delete_mode(mode: str, path=GROUP_SETTINGS_FILE, chat_id=None) -> None:
    if mode not in WELCOME_DELETE_MODES:
        raise ValueError("בחר אפשרות מחיקה תקינה להודעת הברוכים הבאים.")
    with _LOCK:
        data = load_group_settings(path)
        if chat_id is None:
            data["welcome_delete_mode"] = mode
        else:
            _welcome_overrides(data, chat_id)["welcome_delete_mode"] = mode
            if mode == "none":
                data["last_welcome_messages"].pop(str(chat_id), None)
        _save(data, path)


def update_welcome_button(text: str, url: str, path=GROUP_SETTINGS_FILE, chat_id=None) -> None:
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 64:
        raise ValueError("טקסט הכפתור חייב להכיל בין 1 ל־64 תווים.")
    normalized_url = normalize_button_link(url)
    with _LOCK:
        data = load_group_settings(path)
        if chat_id is None:
            data["welcome_button_text"] = text.strip()
            data["welcome_button_url"] = normalized_url
        else:
            overrides = _welcome_overrides(data, chat_id)
            overrides["welcome_button_text"] = text.strip()
            overrides["welcome_button_url"] = normalized_url
        _save(data, path)


def set_last_welcome_message(chat_id: int, message_id: int, path=GROUP_SETTINGS_FILE) -> None:
    if type(chat_id) is not int or type(message_id) is not int or message_id < 1:
        raise ValueError("Invalid welcome message reference.")
    with _LOCK:
        data = load_group_settings(path)
        data["last_welcome_messages"][str(chat_id)] = message_id
        _save(data, path)


def clear_last_welcome_message(
    chat_id: int,
    path=GROUP_SETTINGS_FILE,
    expected_message_id=None,
) -> bool:
    _validate_group_chat_id(chat_id)
    with _LOCK:
        data = load_group_settings(path)
        key = str(chat_id)
        current = data["last_welcome_messages"].get(key)
        if current is None or (
            expected_message_id is not None and current != expected_message_id
        ):
            return False
        data["last_welcome_messages"].pop(key, None)
        _save(data, path)
        return True


def update_link_warning(text: str, path=GROUP_SETTINGS_FILE) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise ValueError("הודעת האזהרה חייבת להכיל בין 1 ל־3000 תווים.")
    with _LOCK:
        data = load_group_settings(path)
        data["link_warning_text"] = text
        _save(data, path)


def update_command_rejection_text(text: str, path=GROUP_SETTINGS_FILE) -> None:
    if not isinstance(text, str) or not text.strip() or len(text) > 3000:
        raise ValueError("הודעת הדחייה חייבת להכיל בין 1 ל־3000 תווים.")
    with _LOCK:
        data = load_group_settings(path)
        data["command_rejection_text"] = text
        _save(data, path)


def claim_command_rejection_notice(
    chat_id: int, user_id: int, path=GROUP_SETTINGS_FILE,
) -> bool:
    """Persist whether this group/user has already seen a command rejection."""
    if type(chat_id) is not int or chat_id >= 0:
        raise ValueError("A group chat ID must be a negative integer.")
    if type(user_id) is not int or user_id == 0:
        raise ValueError("A sender ID must be a non-zero integer.")
    with _LOCK:
        data = load_group_settings(path)
        notices = data["command_rejection_notices"]
        group_notices = notices.setdefault(str(chat_id), [])
        if user_id in group_notices:
            return False
        group_notices.append(user_id)
        _save(data, path)
        return True


def release_command_rejection_notice(
    chat_id: int, user_id: int, path=GROUP_SETTINGS_FILE,
) -> None:
    with _LOCK:
        data = load_group_settings(path)
        group_notices = data["command_rejection_notices"].get(str(chat_id), [])
        if user_id not in group_notices:
            return
        group_notices.remove(user_id)
        if not group_notices:
            data["command_rejection_notices"].pop(str(chat_id), None)
        _save(data, path)


def validate_max_warnings(value) -> int:
    if type(value) is not int or not 1 <= value <= 20:
        raise ValueError("כמות האזהרות חייבת להיות מספר שלם בין 1 ל־20.")
    return value


def set_max_warnings(value: int, path=GROUP_SETTINGS_FILE) -> None:
    value = validate_max_warnings(value)
    with _LOCK:
        data = load_group_settings(path)
        data["max_warnings"] = value
        _save(data, path)


def validate_time(value):
    import re
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("שלח שעה תקינה בפורמט HH:MM, למשל 23:00.")
    return value


def validate_night_settings(settings):
    if not isinstance(settings, dict) or type(settings.get("enabled")) is not bool:
        raise ValueError("Invalid night-lock settings.")
    close = validate_time(settings.get("close_time"))
    opened = validate_time(settings.get("open_time"))
    if close == opened:
        raise ValueError("שעת הסגירה ושעת הפתיחה חייבות להיות שונות.")
    for key in ("close_message", "open_message"):
        if not isinstance(settings.get(key), str) or not settings[key].strip() or len(settings[key]) > 3000:
            raise ValueError("הודעת הלילה חייבת להכיל בין 1 ל־3000 תווים.")


def toggle_night_lock(path=GROUP_SETTINGS_FILE):
    with _LOCK:
        data = load_group_settings(path)
        data["night_lock"]["enabled"] = not data["night_lock"]["enabled"]
        _save(data, path)
        return data["night_lock"]["enabled"]


def update_night_hours(close, opened, path=GROUP_SETTINGS_FILE):
    with _LOCK:
        data = load_group_settings(path)
        data["night_lock"].update(close_time=validate_time(close), open_time=validate_time(opened))
        validate_night_settings(data["night_lock"])
        _save(data, path)


def update_night_message(key, text, path=GROUP_SETTINGS_FILE):
    if key not in {"close_message", "open_message"}:
        raise ValueError("Unknown night message.")
    with _LOCK:
        data = load_group_settings(path)
        data["night_lock"][key] = text
        validate_night_settings(data["night_lock"])
        _save(data, path)
