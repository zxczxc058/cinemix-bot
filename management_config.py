"""Configured destinations and explicit, dynamically resolved public links."""

import json
import copy
import re
import threading
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse
from admin_roles import OWNER_ID

CONFIG_FILE = Path(__file__).resolve().with_name("config.json")
DEFAULT_MISSING_CONTENT_MESSAGES = {
    "uploaded": "🎉 התוכן שביקשת ({CONTENT_NAME}) הועלה בהצלחה לבוט! מוזמנים לחפש אותו.",
    "unavailable": "ℹ️ התוכן שביקשת ({CONTENT_NAME}) אינו זמין כרגע במאגר.",
}
DEFAULT_CONFIG = {
    "main_group_id": None, "main_group_link": None,
    "channel_id": None, "channel_link": None,
    "bot_link": None, "automatic_backup": True,
    "admin_ids": [OWNER_ID],
    "default_response": "לא מצאתי תשובה מתאימה. אפשר לנסות מילת מפתח אחרת?",
    "unknown_cmd_response": None,
    "default_response_text": None,
    "unknown_command_text": "📩 פנייתך הועברה בהצלחה לצוות הניהול, נשיב לך בהקדם!",
    "unauthorized_message": (
        "🔒 הבוט מוגן בקוד גישה. אנא הזן את קוד הגישה כדי להתחיל.\n"
        "לקבלת קוד הגישה 👇"
    ),
    "unauthorized_buttons": [],
    "access_prompt_button_url": None,
    "admin_chat_id": None, "ADMIN_CHAT_ID": None, "ADMIN_GROUP_ID": None,
    "LOG_CHAT_ID": None,
    "LOG_CHANNEL_ID": None,
    "ADMIN_COMMANDS_CHAT_ID": None,
    "admin_input_drafts": {},
    "system_messages": {},
    "invalid_code_message": None,
    "allowed_chats": [],
    "managed_chats": {},
    "group_commands": {},
    "report_contact_url": None,
    "link_error_popup_text": "תודה על הדיווח, נעדכן בקרוב! 🙏",
    "missing_content_messages": DEFAULT_MISSING_CONTENT_MESSAGES,
    "banned_users": {}, "muted_users": {},
    "link_check_enabled": True, "link_check_interval_seconds": 21600,
}
LINK_TOKENS = {"{main_group_link}", "{channel_link}", "{support_link}"}
_LOCK = threading.RLock()


def save_json(path, data):
    from persistent_json import write_json_document
    write_json_document(path, data)

    from admin_edit_flow import record_commit
    record_commit(path)


def is_link_input(value):
    if not isinstance(value, str):
        return False
    if value in LINK_TOKENS:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_button_link(value):
    """Convert a Telegram username or HTTP(S) URL into a URL-button target."""
    if not isinstance(value, str):
        raise ValueError("שלח שם משתמש בטלגרם או קישור HTTP/HTTPS.")
    target = value.strip()
    username = target[1:] if target.startswith("@") else target
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}"
    if is_link_input(target):
        return target
    raise ValueError("שלח שם משתמש בטלגרם, למשל @username, או קישור HTTP/HTTPS.")


def load_config(path=CONFIG_FILE):
    with _LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        if not isinstance(data, dict):
            raise ValueError("Invalid management configuration.")
        changed = False
        has_admin_chat_id = "ADMIN_CHAT_ID" in data
        has_admin_group_id = "ADMIN_GROUP_ID" in data
        if "admin_chat_id" not in data and "ADMIN_CHAT_ID" in data:
            data["admin_chat_id"] = data["ADMIN_CHAT_ID"]
            changed = True
        for key in DEFAULT_CONFIG:
            if key not in data:
                value = copy.deepcopy(DEFAULT_CONFIG[key])
                if key == "default_response":
                    commands = Path(path).parent / "commands.json"
                    if commands.exists():
                        value = json.loads(read_json_text(commands))["fallback"]["text"]
                elif key == "unknown_cmd_response":
                    value = data["default_response"]
                elif key == "invalid_code_message":
                    # Preserve existing administrator edits when adding the
                    # explicit access-menu storage field.
                    legacy = data.get("system_messages", {}).get("invalid_code")
                    value = legacy if isinstance(legacy, str) and legacy.strip() else None
                elif key == "default_response_text":
                    value = data["unknown_cmd_response"] or data["default_response"]
                elif key == "unknown_command_text":
                    value = next(
                        (
                            data[name]
                            for name in ("default_response_text", "unknown_cmd_response", "default_response")
                            if isinstance(data.get(name), str) and data[name].strip()
                        ),
                        value,
                    )
                data[key] = value
                changed = True
        if data["unknown_cmd_response"] is None:
            data["unknown_cmd_response"] = data["default_response"]
            changed = True
        if data["default_response_text"] is None:
            data["default_response_text"] = data["unknown_cmd_response"]
            changed = True
        # ADMIN_CHAT_ID is canonical when the key exists, including an
        # explicit null that clears a previous destination. Accept the two
        # historical aliases when ADMIN_CHAT_ID was never present.
        if has_admin_chat_id:
            admin_target = data["ADMIN_CHAT_ID"]
        elif has_admin_group_id:
            admin_target = data["ADMIN_GROUP_ID"]
        else:
            admin_target = data.get("admin_chat_id")
        if isinstance(admin_target, str):
            if not re.fullmatch(r"-[1-9][0-9]*", admin_target.strip()):
                raise ValueError("The admin group ID must be a negative Telegram chat ID.")
            admin_target = int(admin_target.strip())
        if any(
            data[key] != admin_target
            for key in ("ADMIN_CHAT_ID", "ADMIN_GROUP_ID", "admin_chat_id")
        ):
            data["ADMIN_CHAT_ID"] = data["ADMIN_GROUP_ID"] = data["admin_chat_id"] = admin_target
            changed = True
        if not isinstance(data, dict) or type(data.get("automatic_backup")) is not bool:
            raise ValueError("Invalid management configuration.")
        log_target = data["LOG_CHAT_ID"]
        if isinstance(log_target, str):
            if not re.fullmatch(r"-[1-9][0-9]*", log_target.strip()):
                raise ValueError("LOG_CHAT_ID must be a negative group ID.")
            data["LOG_CHAT_ID"] = int(log_target.strip())
            changed = True
        log_channel = data["LOG_CHANNEL_ID"]
        if isinstance(log_channel, str):
            if not re.fullmatch(r"-[1-9][0-9]*", log_channel.strip()):
                raise ValueError("LOG_CHANNEL_ID must be a negative Telegram chat ID.")
            data["LOG_CHANNEL_ID"] = int(log_channel.strip())
            changed = True
        logs_target = data["ADMIN_COMMANDS_CHAT_ID"]
        if isinstance(logs_target, str):
            if not re.fullmatch(r"-[1-9][0-9]*", logs_target.strip()):
                raise ValueError("ADMIN_COMMANDS_CHAT_ID must be a negative Telegram chat ID.")
            data["ADMIN_COMMANDS_CHAT_ID"] = int(logs_target.strip())
            changed = True
        for key in ("main_group_id", "channel_id", "LOG_CHAT_ID", "LOG_CHANNEL_ID", "ADMIN_COMMANDS_CHAT_ID"):
            if data.get(key) is not None and (type(data[key]) is not int or data[key] >= 0):
                raise ValueError("Invalid configured destination.")
        if data["LOG_CHAT_ID"] is not None and abs(data["LOG_CHAT_ID"]) >= 2**52:
            raise ValueError("Invalid backup group ID.")
        for key in ("main_group_link", "channel_link", "bot_link"):
            if data.get(key) is not None and (
                not is_link_input(data[key]) or data[key] in LINK_TOKENS
            ):
                raise ValueError("Invalid configured destination link.")
        ids = data["admin_ids"]
        if not isinstance(ids, list) or OWNER_ID not in ids or any(type(uid) is not int or not 0 < uid < 2**52 for uid in ids):
            raise ValueError("Invalid authorized administrators.")
        if not isinstance(data["default_response"], str) or not data["default_response"].strip() or len(data["default_response"]) > 4096:
            raise ValueError("Invalid default response.")
        if not isinstance(data["unknown_cmd_response"], str) or not data["unknown_cmd_response"].strip() or len(data["unknown_cmd_response"]) > 4096:
            raise ValueError("Invalid unknown command response.")
        if not isinstance(data["default_response_text"], str) or not data["default_response_text"].strip() or len(data["default_response_text"]) > 4096:
            raise ValueError("Invalid default response text.")
        if (
            not isinstance(data["unknown_command_text"], str)
            or not data["unknown_command_text"].strip()
            or len(data["unknown_command_text"]) > 4096
        ):
            raise ValueError("Invalid unknown command text.")
        if (
            not isinstance(data["link_error_popup_text"], str)
            or not data["link_error_popup_text"].strip()
            or len(data["link_error_popup_text"]) > 200
        ):
            raise ValueError("Invalid link-error popup text.")
        messages = data["missing_content_messages"]
        if not isinstance(messages, dict) or any(
            not isinstance(messages.get(key), str)
            or not messages[key].strip()
            or len(messages[key]) > 4096
            for key in DEFAULT_MISSING_CONTENT_MESSAGES
        ):
            raise ValueError("Invalid missing-content response messages.")
        if not isinstance(data["unauthorized_message"], str) or not isinstance(data["unauthorized_buttons"], list):
            raise ValueError("Invalid unauthorized message.")
        prompt_url = data["access_prompt_button_url"]
        if prompt_url is not None and (
            not isinstance(prompt_url, str) or not is_link_input(prompt_url)
        ):
            raise ValueError("Invalid access-code prompt button URL.")
        if not data["unauthorized_message"] and not data["unauthorized_buttons"]:
            raise ValueError("An access message requires text or buttons.")
        for button in data["unauthorized_buttons"]:
            if not isinstance(button, dict) or not isinstance(button.get("text"), str) or not button["text"].strip() or not is_link_input(button.get("url", "")):
                raise ValueError("Invalid unauthorized button.")
        for field in ("banned_users", "muted_users"):
            if not isinstance(data[field], dict):
                raise ValueError("Invalid restriction records.")
            for uid, record in data[field].items():
                if not uid.isdigit() or int(uid) <= 0 or not isinstance(record, dict) or not isinstance(record.get("reason"), str):
                    raise ValueError("Invalid restriction record.")
                if field == "muted_users" and (
                    "until" not in record
                    or (record["until"] is not None and type(record["until"]) not in (int, float))
                ):
                    raise ValueError("Invalid mute expiry.")
        if data["admin_chat_id"] is not None and (type(data["admin_chat_id"]) is not int or data["admin_chat_id"] >= 0):
            raise ValueError("Invalid admin destination.")
        if data["report_contact_url"] is not None and not is_link_input(data["report_contact_url"]):
            raise ValueError("Invalid report contact URL.")
        if type(data["link_check_enabled"]) is not bool or type(data["link_check_interval_seconds"]) is not int or data["link_check_interval_seconds"] < 60:
            raise ValueError("Invalid link checking schedule.")
        if (not isinstance(data["allowed_chats"], list)
                or any(type(cid) is not int or cid >= 0 for cid in data["allowed_chats"])):
            raise ValueError("allowed_chats must contain negative Telegram chat IDs.")
        if not isinstance(data["managed_chats"], dict):
            raise ValueError("managed_chats must be an object.")
        for key, record in data["managed_chats"].items():
            if (not isinstance(key, str) or not key.startswith("-") or not key[1:].isdigit()
                    or int(key) >= 0 or str(int(key)) != key
                    or not isinstance(record, dict)
                    or type(record.get("present")) is not bool
                    or record.get("type") not in {"group", "supergroup", "channel"}
                    or not isinstance(record.get("title"), str)
                    or len(record["title"]) > 200
                    or ("pending" in record and type(record["pending"]) is not bool)):
                raise ValueError("Invalid managed chat record.")
        group_commands = data["group_commands"]
        if not isinstance(group_commands, dict):
            raise ValueError("group_commands must be an object.")
        for chat_id, commands in group_commands.items():
            if (
                not isinstance(chat_id, str) or not chat_id.startswith("-")
                or not chat_id[1:].isdigit() or int(chat_id) >= 0
                or str(int(chat_id)) != chat_id or abs(int(chat_id)) >= 2**52
                or not isinstance(commands, dict) or len(commands) > 50
            ):
                raise ValueError("Invalid per-group command set.")
            for keyword, text in commands.items():
                if (
                    not isinstance(keyword, str) or not keyword.strip()
                    or len(keyword) > 80 or "\n" in keyword or "\r" in keyword
                    or not isinstance(text, str) or not text.strip() or len(text) > 3000
                ):
                    raise ValueError("Invalid per-group command.")
        if changed:
            save_json(path, data)
        return data


def configure_identity(bot, path=CONFIG_FILE):
    username = bot.get_me().username
    if not isinstance(username, str) or not username:
        raise ValueError("Bot username is unavailable.")
    with _LOCK:
        data = load_config(path)
        data["bot_link"] = f"https://t.me/{username}"
        save_json(path, data)


def set_admin_chat(chat_id, path=CONFIG_FILE):
    """Keep the requested public field compatible with existing consumers."""
    if type(chat_id) is not int or chat_id >= 0:
        raise ValueError("יש לבחור ערוץ או קבוצה תקינים.")
    with _LOCK:
        config = load_config(path)
        config["admin_chat_id"] = config["ADMIN_CHAT_ID"] = config["ADMIN_GROUP_ID"] = chat_id
        save_json(path, config)


def configure_destination(bot, chat_id, kind, path=CONFIG_FILE):
    if kind not in {"main_group", "channel"}:
        raise ValueError("Unknown destination.")
    chat = bot.get_chat(chat_id)
    allowed = {"group", "supergroup"} if kind == "main_group" else {"channel"}
    if chat.type not in allowed:
        raise ValueError("סוג היעד אינו מתאים לפקודה.")
    username = getattr(chat, "username", None)
    link = f"https://t.me/{username}" if username else getattr(chat, "invite_link", None)
    if not link:
        # Creating an additional link does not invalidate existing invite links.
        link = bot.create_chat_invite_link(chat.id, name="Telegram bot destination").invite_link
    if not is_link_input(link) or link in LINK_TOKENS:
        raise ValueError("לא ניתן ליצור קישור. נדרשת הרשאת הזמנת משתמשים.")
    with _LOCK:
        data = load_config(path)
        data[f"{kind}_id"] = chat.id
        data[f"{kind}_link"] = link
        save_json(path, data)
    return link


def resolve_text(text, path=CONFIG_FILE):
    if not any(token in text for token in LINK_TOKENS):
        return text
    config = load_config(path)
    links = {
        "{main_group_link}": config["main_group_link"],
        "{channel_link}": config["channel_link"],
        "{support_link}": config["bot_link"],
    }
    for token, value in links.items():
        if token in text:
            if not value:
                raise ValueError(f"הקישור {token} טרם הוגדר. הגדר את היעד לפני השליחה.")
            text = text.replace(token, value)
    return text


def resolve_reply(reply, path=CONFIG_FILE):
    return replace(
        reply, text=resolve_text(reply.text, path),
        buttons=tuple(replace(button, url=resolve_text(button.url, path)) for button in reply.buttons),
    )
