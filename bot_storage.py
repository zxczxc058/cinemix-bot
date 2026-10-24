"""JSON-backed bot settings, keyword replies, and Telegram user registration."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import telebot
from telebot.handler_backends import BaseMiddleware
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from admin_roles import OWNER_ID

ADMIN_USER_ID = OWNER_ID
COMMANDS_FILE = Path(__file__).resolve().with_name("commands.json")
USERS_FILE = Path(__file__).resolve().with_name("users.json")
GROUPS_FILE = Path(__file__).resolve().with_name("groups.json")
FALLBACK_RESPONSE = "לא מצאתי תשובה מתאימה. אפשר לנסות מילת מפתח אחרת?"
WELCOME_MESSAGE = "היי ברוכים הבאים\nרשמו את שם הסרט/הסדרה שאתם מחפשים"

DEFAULT_BOT_DATA = {
    "responses": {
        "חילוץ תחת אש": {
            "text": (
                "חילוץ תחת אש [2025]\n"
                "https://drive.google.com/file/d/1I8B77vGw5zPkFUhiaZPMBvJLrB8QJSK5/view?usp=drivesdk"
            ),
            "buttons": [],
        },
        "פשוט לנשום": {
            "text": (
                "פשוט לנשום [2025] ↙️\n"
                "https://drive.google.com/file/d/1X03AQB02tXTtO0_Bskqq-i5KjalHIWuP/view?usp=drivesdk"
            ),
            "buttons": [],
        },
        "הכוכב הבא": {
            "text": "הכוכב הבא עונה 12",
            "buttons": [
                {
                    "text": "למעבר לסדרה לחצו כאן",
                    "url": "https://drive.google.com/drive/folders/1AiWYf-21SNUGOh4gXSJmXxTxWMn8PkKC",
                }
            ],
        },
        "היי": {"text": "היי! איך אפשר לעזור?", "buttons": []},
        "מחיר": {
            "text": "כדי לברר מחיר, כתבו איזה מוצר או שירות מעניין אתכם.",
            "buttons": [],
        },
        "שעות": {
            "text": "שעות הפעילות עדיין לא הוגדרו. אפשר לעדכן אותן מתפריט הניהול.",
            "buttons": [],
        },
    },
    "start": {"text": WELCOME_MESSAGE, "buttons": [], "buttons_enabled": True},
    "fallback": {"text": FALLBACK_RESPONSE},
}

_COMMANDS_LOCK = threading.RLock()
_USERS_LOCK = threading.Lock()
_GROUPS_LOCK = threading.Lock()


@dataclass(frozen=True)
class InlineButtonSpec:
    """Configuration for a Telegram inline URL button."""

    text: str
    url: str


@dataclass(frozen=True)
class BotReply:
    """A reply with optional inline URL buttons."""

    text: str
    buttons: tuple[InlineButtonSpec, ...] = ()
    report_id: str | None = None
    buttons_enabled: bool = True
    action_type: str = "reply"


def is_admin(user_id: int | None) -> bool:
    from admin_roles import is_authorized_admin
    return is_authorized_admin(user_id)


def _validate_buttons(buttons: object, context: str) -> None:
    from management_config import is_link_input
    if not isinstance(buttons, list):
        raise ValueError(f"{context} buttons must be a list.")
    for button in buttons:
        if not isinstance(button, dict):
            raise ValueError(f"{context} has an invalid button.")
        label = button.get("text")
        url = button.get("url")
        parsed_url = urlparse(url) if isinstance(url, str) else None
        if not isinstance(label, str) or not label.strip() or len(label) > 64:
            raise ValueError(f"{context} has a button with an invalid label.")
        if not is_link_input(url):
            raise ValueError(f"{context} has a button with an invalid URL.")


def _validate_bot_data(data: object) -> None:
    if not isinstance(data, dict):
        raise ValueError("commands.json must contain a JSON object.")
    responses = data.get("responses")
    if not isinstance(responses, dict):
        raise ValueError("commands.json must contain a responses object.")
    for keyword, response in responses.items():
        if not isinstance(keyword, str) or not keyword.strip():
            raise ValueError("commands.json contains an invalid keyword.")
        if not isinstance(response, dict) or not isinstance(
            response.get("text"), str
        ):
            raise ValueError(f"Response for {keyword!r} must contain text.")
        _validate_buttons(response.get("buttons"), f"Response for {keyword!r}")
        if type(response.get("enabled", True)) is not bool:
            raise ValueError(f"Response for {keyword!r} has an invalid status.")
        if response.get("action_type", "reply") not in {"reply", "vip_purchase"}:
            raise ValueError(f"Response for {keyword!r} has an invalid action type.")
        if type(response.get("report_enabled", False)) is not bool:
            raise ValueError("Invalid reporting configuration.")
        if response.get("report_enabled") and not re.fullmatch(r"[a-f0-9]{16}", response.get("report_id", "")):
            raise ValueError("Invalid report button identifier.")

    start = data.get("start")
    if not isinstance(start, dict) or not isinstance(start.get("text"), str):
        raise ValueError("commands.json must contain a start message with text.")
    _validate_buttons(start.get("buttons"), "The start message")
    if type(start.get("buttons_enabled", True)) is not bool:
        raise ValueError("The /start buttons_enabled setting must be a boolean.")
    fallback = data.get("fallback", {"text": FALLBACK_RESPONSE})
    if (
        not isinstance(fallback, dict)
        or not isinstance(fallback.get("text"), str)
        or not fallback["text"].strip()
    ):
        raise ValueError("commands.json must contain a fallback message with text.")


def load_bot_data(path: str | Path = COMMANDS_FILE) -> dict:
    """Read and validate the current responses and /start settings."""
    commands_path = Path(path)
    with _COMMANDS_LOCK:
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(commands_path))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Bot settings file not found: {commands_path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {commands_path}") from exc
        _validate_bot_data(data)
        data.setdefault("fallback", {"text": FALLBACK_RESPONSE})
        data["start"].setdefault("buttons_enabled", True)
        return data


def _write_bot_data(path: Path, data: dict) -> None:
    _validate_bot_data(data)
    from persistent_json import write_json_document
    write_json_document(path, data)

    from admin_edit_flow import record_commit
    record_commit(path)


def save_bot_data(data: dict, path: str | Path = COMMANDS_FILE) -> None:
    """Validate and atomically save all bot replies and start settings."""
    with _COMMANDS_LOCK:
        _write_bot_data(Path(path), data)


def add_command(
    keyword: str,
    text: str,
    path: str | Path = COMMANDS_FILE,
    buttons: list[dict[str, str]] | None = None,
    report_enabled: bool = False,
    action_type: str = "reply",
    display_title: str | None = None,
) -> bool:
    """Add a new keyword reply without replacing an existing keyword."""
    normalized_keyword = keyword.strip()
    if not normalized_keyword or len(normalized_keyword) > 128:
        raise ValueError("Use a non-empty trigger of at most 128 characters.")
    if normalized_keyword.startswith("/") and not re.fullmatch(
        r"/[A-Za-z0-9_]{1,32}", normalized_keyword
    ):
        raise ValueError("Slash commands must look like /mycommand.")
    if not text.strip():
        raise ValueError("The reply cannot be empty.")
    if display_title is not None and (not display_title.strip() or len(display_title) > 128):
        raise ValueError("שם תצוגה חייב להכיל 1–128 תווים.")

    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        if any(
            saved_keyword.casefold() == normalized_keyword.casefold()
            for saved_keyword in data["responses"]
        ):
            return False
        button_data = buttons if buttons is not None else []
        _validate_buttons(button_data, f"Response for {normalized_keyword!r}")
        data["responses"][normalized_keyword] = {
            "text": text,
            "buttons": button_data,
            "enabled": True,
            "action_type": action_type,
            **({"command_trigger": normalized_keyword, "display_title": display_title.strip()}
               if display_title is not None else {}),
        }
        if report_enabled:
            import secrets
            data["responses"][normalized_keyword].update(report_enabled=True, report_id=secrets.token_hex(8))
        _write_bot_data(Path(path), data)
        return True


def set_command_display_title(keyword, title, path=COMMANDS_FILE):
    """Edit presentation without changing trigger, response, buttons or status."""
    if not title.strip() or len(title) > 128:
        raise ValueError("שם תצוגה חייב להכיל 1–128 תווים.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        response = data["responses"].get(keyword)
        if response is None:
            raise ValueError("הפקודה כבר אינה קיימת.")
        response.update(command_trigger=keyword, display_title=title.strip())
        _write_bot_data(Path(path), data)


def update_command(
    keyword: str,
    text: str,
    path: str | Path = COMMANDS_FILE,
    buttons: list[dict[str, str]] | None = None,
) -> bool:
    """Update a reply, optionally replacing its inline buttons."""
    if not text.strip():
        raise ValueError("The reply cannot be empty.")

    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        response = data["responses"].get(keyword)
        if response is None:
            return False
        response["text"] = text
        if buttons is not None:
            _validate_buttons(buttons, f"Response for {keyword!r}")
            response["buttons"] = buttons
        _write_bot_data(Path(path), data)
        return True


def delete_command(keyword: str, path: str | Path = COMMANDS_FILE) -> bool:
    """Delete one keyword reply."""
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        if keyword not in data["responses"]:
            return False
        del data["responses"][keyword]
        _write_bot_data(Path(path), data)
        return True


def set_command_action_type(keyword: str, action_type: str, path=COMMANDS_FILE) -> bool:
    """Change only a command's action, preserving its text and buttons."""
    if action_type not in {"reply", "vip_purchase"}:
        raise ValueError("Unknown command action.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        response = data["responses"].get(keyword)
        if response is None:
            return False
        response["action_type"] = action_type
        _write_bot_data(Path(path), data)
        return True


def rename_command(keyword: str, new_keyword: str, path: str | Path = COMMANDS_FILE) -> bool:
    """Rename a command trigger without losing its response or buttons."""
    new_keyword = new_keyword.strip()
    if not new_keyword or len(new_keyword) > 128:
        raise ValueError("Use a non-empty trigger of at most 128 characters.")
    if new_keyword.startswith("/") and not re.fullmatch(r"/[A-Za-z0-9_]{1,32}", new_keyword):
        raise ValueError("Slash commands must look like /mycommand.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        if keyword not in data["responses"]:
            return False
        if any(existing.casefold() == new_keyword.casefold() for existing in data["responses"]):
            raise ValueError("A command with that trigger already exists.")
        data["responses"][new_keyword] = data["responses"].pop(keyword)
        if "command_trigger" in data["responses"][new_keyword]:
            data["responses"][new_keyword]["command_trigger"] = new_keyword
        _write_bot_data(Path(path), data)
        return True


def set_command_enabled(keyword: str, enabled: bool, path: str | Path = COMMANDS_FILE) -> bool:
    if type(enabled) is not bool:
        raise ValueError("Command status must be boolean.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        response = data["responses"].get(keyword)
        if response is None:
            return False
        response["enabled"] = enabled
        _write_bot_data(Path(path), data)
        return True


def update_start_text(text: str, path: str | Path = COMMANDS_FILE) -> None:
    if not text.strip():
        raise ValueError("The start message cannot be empty.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        data["start"]["text"] = text
        _write_bot_data(Path(path), data)


def update_fallback_response(text: str, path: str | Path = COMMANDS_FILE) -> None:
    if not text.strip() or len(text) > 4096:
        raise ValueError("The fallback response cannot be empty.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        data["fallback"] = {"text": text}
        _write_bot_data(Path(path), data)
    from management_config import _LOCK, load_config, save_json
    config_path = Path(path).parent / "config.json"
    if config_path.exists():
        with _LOCK:
            config = load_config(config_path)
            config["default_response"] = text
            config["unknown_cmd_response"] = text
            config["default_response_text"] = text
            config["unknown_command_text"] = text
            save_json(config_path, config)


def update_start_buttons(
    buttons: list[dict[str, str]],
    path: str | Path = COMMANDS_FILE,
) -> None:
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        data["start"]["buttons"] = buttons
        _write_bot_data(Path(path), data)


def set_start_buttons_enabled(
    enabled: bool,
    path: str | Path = COMMANDS_FILE,
) -> None:
    if type(enabled) is not bool:
        raise ValueError("The /start button setting must be true or false.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        data["start"]["buttons_enabled"] = enabled
        _write_bot_data(Path(path), data)


def update_start_settings(
    text: str,
    buttons: list[dict[str, str]],
    path: str | Path = COMMANDS_FILE,
) -> None:
    """Commit the complete /start edit only after the admin finishes it."""
    if not text.strip():
        raise ValueError("The start message cannot be empty.")
    with _COMMANDS_LOCK:
        data = load_bot_data(path)
        data["start"] = {
            "text": text,
            "buttons": buttons,
            "buttons_enabled": data["start"].get("buttons_enabled", True),
        }
        _write_bot_data(Path(path), data)


def _reply_from_data(reply_data: dict) -> BotReply:
    return BotReply(
        text=reply_data["text"],
        report_id=reply_data.get("report_id") if reply_data.get("report_enabled") else None,
        buttons=tuple(
            InlineButtonSpec(text=button["text"], url=button["url"])
            for button in reply_data["buttons"]
        ),
        buttons_enabled=reply_data.get("buttons_enabled", True),
        action_type=reply_data.get("action_type", "reply"),
    )


def match_reply(text: str, path: str | Path = COMMANDS_FILE) -> BotReply | None:
    """Return a response only when the trimmed input equals a trigger exactly."""
    normalized_text = text.strip()
    if re.fullmatch(r"/[A-Za-z0-9_]{1,32}@[A-Za-z0-9_]{1,64}", normalized_text):
        normalized_text = normalized_text.split("@", 1)[0]
    data = load_bot_data(path)
    for keyword, response in data["responses"].items():
        if keyword == normalized_text and response.get("enabled", True):
            if response.get("display_title"):
                title = response["display_title"]
                body = response["text"]
                response = {**response, "text": (
                    body.replace("{DISPLAY_TITLE}", title) if "{DISPLAY_TITLE}" in body
                    else title + "\n" + body
                )}
            return _reply_from_data(response)
    return None


def has_partial_reply_match(text: str, path: str | Path = COMMANDS_FILE) -> bool:
    """Identify non-exact text that contains a configured trigger."""
    candidate = text.strip().casefold()
    if not candidate:
        return False
    return any(
        response.get("enabled", True)
        and keyword.casefold() != candidate
        and keyword.casefold() in candidate
        for keyword, response in load_bot_data(path)["responses"].items()
    )


def get_reply(text: str, path: str | Path = COMMANDS_FILE) -> BotReply:
    """Match against the latest commands.json contents on every message."""
    return match_reply(text, path) or BotReply(text=get_fallback_response(path))


def get_response(text: str, path: str | Path = COMMANDS_FILE) -> str:
    return get_reply(text, path).text


def get_start_reply(path: str | Path = COMMANDS_FILE) -> BotReply:
    return _reply_from_data(load_bot_data(path)["start"])


def get_fallback_response(path: str | Path = COMMANDS_FILE) -> str:
    from management_config import load_config
    config = Path(path).parent / "config.json"
    return load_config(config)["default_response_text"] if config.exists() else load_bot_data(path)["fallback"]["text"]


def format_telegram_text(text: str) -> tuple[str, str | None]:
    from safe_formatting import format_text
    return format_text(text)


def build_inline_keyboard(
    buttons: tuple[InlineButtonSpec, ...],
    report_id: str | None = None,
    *,
    report_contact_url: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Render every stored URL button as a Telegram InlineKeyboardButton."""
    if not buttons and not report_id:
        return None
    keyboard = InlineKeyboardMarkup()
    for button in buttons:
        keyboard.add(
            InlineKeyboardButton(text=button.text, url=button.url)
        )
    if report_id:
        report_button = InlineKeyboardButton(
            "⚠️ הקישור לא תקין? לחצו לדיווח",
            callback_data=f"report:open:{report_id}",
        )
        if not buttons and not report_contact_url:
            return InlineKeyboardMarkup([[report_button]])
        keyboard.row(report_button)
        if report_contact_url:
            keyboard.add(InlineKeyboardButton("פנייה ישירה למנהל", url=report_contact_url))
    return keyboard


def load_user_ids(path: str | Path = USERS_FILE) -> set[int]:
    """Load and validate the unique Telegram user IDs from JSON."""
    users_path = Path(path)
    try:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(users_path))
    except FileNotFoundError:
        return set()
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {users_path}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("user_ids"), list):
        raise ValueError(f"{users_path} must contain an object with a user_ids list.")
    user_ids = data["user_ids"]
    if any(type(user_id) is not int or user_id <= 0 for user_id in user_ids):
        raise ValueError(f"{users_path} contains an invalid Telegram user ID.")
    records = data.get("moderation", {})
    if not isinstance(records, dict):
        raise ValueError("Invalid user moderation data.")
    for uid, record in records.items():
        if int(uid) <= 0 or not isinstance(record, dict) or type(record.get("banned")) is not bool:
            raise ValueError("Invalid user moderation record.")
        warnings = record.get("warnings")
        if not isinstance(warnings, list) or any(
            not isinstance(warning, dict) or not isinstance(warning.get("reason"), str)
            for warning in warnings
        ):
            raise ValueError("Invalid warning history.")
        count = record.get("warning_count", len(warnings))
        if type(count) is not int or count < 0:
            raise ValueError("Invalid warning count.")
    return set(user_ids)


def _write_user_ids(path: Path, user_ids: set[int]) -> None:
    from persistent_json import read_json_text, write_json_document
    existing = json.loads(read_json_text(path)) if path.exists() else {}
    existing["user_ids"] = sorted(user_ids)
    write_json_document(path, existing)


def record_user_id(user_id: int, path: str | Path = USERS_FILE) -> bool:
    """Persist a user ID once; return True only when it is newly added."""
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Telegram user ID must be a positive integer.")

    users_path = Path(path)
    with _USERS_LOCK:
        user_ids = load_user_ids(users_path)
        if user_id in user_ids:
            return False
        user_ids.add(user_id)
        _write_user_ids(users_path, user_ids)
        return True


def load_moderation_records(path: str | Path = USERS_FILE) -> dict:
    with _USERS_LOCK:
        load_user_ids(path)
        users_path = Path(path)
        if not users_path.exists():
            return {}
        from persistent_json import read_json_text
        return json.loads(read_json_text(users_path)).get("moderation", {})


def update_user_moderation(
    user_id, action, reason="", chat_id=None, path=USERS_FILE,
    max_warnings=3, user_name=None,
):
    from datetime import datetime, timezone
    if type(user_id) is not int or user_id <= 0 or is_admin(user_id):
        raise ValueError("לא ניתן להעניש את המנהל או מזהה לא תקין.")
    if action not in {"warn", "ban", "unban"}:
        raise ValueError("Unknown moderation action.")
    if type(max_warnings) is not int or not 1 <= max_warnings <= 20:
        raise ValueError("כמות האזהרות חייבת להיות מספר שלם בין 1 ל־20.")
    if action != "unban" and (not reason.strip() or len(reason) > 1000):
        raise ValueError("יש לציין סיבה באורך עד 1000 תווים.")
    users_path = Path(path)
    with _USERS_LOCK:
        ids = load_user_ids(users_path)
        from persistent_json import read_json_text
        data = json.loads(read_json_text(users_path)) if users_path.exists() else {"user_ids": []}
        record = data.setdefault("moderation", {}).setdefault(str(user_id), {
            "warnings": [], "banned": False, "ban_reason": "", "warning_count": 0,
        })
        record.setdefault("warning_count", len(record["warnings"]))
        if action == "warn":
            if record["banned"]:
                raise ValueError("המשתמש כבר חסום. שחרר אותו לפני מתן אזהרה חדשה.")
            import secrets
            record["warnings"].append({
                "warning_id": secrets.token_hex(8),
                "reason": reason, "chat_id": chat_id,
                "at": datetime.now(timezone.utc).isoformat(),
                "user_name": str(user_name or "")[:200],
            })
            record["warning_count"] += 1
            if record["warning_count"] >= max_warnings:
                record.update(banned=True, ban_reason=reason, ban_source="warnings")
        elif action == "ban":
            record.update(banned=True, ban_reason=reason, ban_source="manual")
        else:
            revoked_at = datetime.now(timezone.utc).isoformat()
            for warning in record["warnings"]:
                if not warning.get("revoked"):
                    warning.update(revoked=True, revoked_at=revoked_at)
            record.update(banned=False, warning_count=0, ban_reason="", ban_source="")
        from management_config import save_json
        data["user_ids"] = sorted(ids)
        save_json(users_path, data)
        return record.copy()


def revoke_user_warning(
    user_id: int,
    warning_id: str,
    *,
    max_warnings: int = 3,
    actor_id: int | None = None,
    expected_chat_id: int | None = None,
    path: str | Path = USERS_FILE,
):
    """Revoke one exact warning once, retaining it in history for audit."""
    from datetime import datetime, timezone

    if type(user_id) is not int or user_id <= 0 or is_admin(user_id):
        raise ValueError("לא ניתן לבטל אזהרה למנהל או למזהה לא תקין.")
    if type(actor_id) is not int or actor_id <= 0:
        raise ValueError("מזהה המנהל אינו תקין.")
    if not isinstance(warning_id, str) or not re.fullmatch(r"[a-f0-9]{16}", warning_id):
        raise ValueError("מזהה האזהרה אינו תקין.")
    if type(max_warnings) is not int or not 1 <= max_warnings <= 20:
        raise ValueError("כמות האזהרות חייבת להיות מספר שלם בין 1 ל־20.")

    users_path = Path(path)
    with _USERS_LOCK:
        ids = load_user_ids(users_path)
        from persistent_json import read_json_text
        data = json.loads(read_json_text(users_path)) if users_path.exists() else {"user_ids": []}
        record = data.get("moderation", {}).get(str(user_id))
        if not isinstance(record, dict):
            raise ValueError("לא נמצאה אזהרה פעילה לביטול.")
        warning = next(
            (
                entry for entry in reversed(record.get("warnings", []))
                if isinstance(entry, dict)
                and entry.get("warning_id") == warning_id
                and not entry.get("revoked")
                and (
                    expected_chat_id is None
                    or entry.get("chat_id") == expected_chat_id
                )
            ),
            None,
        )
        count = record.get("warning_count", 0)
        if warning is None or type(count) is not int or count <= 0:
            raise ValueError("האזהרה כבר טופלה או שאין אזהרות פעילות.")

        now = datetime.now(timezone.utc).isoformat()
        warning.update(revoked=True, revoked_at=now, revoked_by=actor_id)
        record["warning_count"] = count - 1
        ban_lifted = bool(
            record.get("banned")
            and record.get("ban_source") == "warnings"
            and record["warning_count"] < max_warnings
        )
        if ban_lifted:
            record.update(banned=False, ban_reason="", ban_source="")
        data["user_ids"] = sorted(ids)
        from management_config import save_json
        save_json(users_path, data)
        result = record.copy()
        result["ban_lifted"] = ban_lifted
        return result, dict(warning)


def load_group_ids(path: str | Path = GROUPS_FILE) -> set[int]:
    """Load and validate Telegram group and supergroup IDs."""
    groups_path = Path(path)
    try:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(groups_path))
    except FileNotFoundError:
        return set()
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {groups_path}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("group_ids"), list):
        raise ValueError(f"{groups_path} must contain an object with a group_ids list.")
    group_ids = data["group_ids"]
    if any(type(group_id) is not int or group_id >= 0 for group_id in group_ids):
        raise ValueError(f"{groups_path} contains an invalid Telegram group ID.")
    return set(group_ids)


def _write_group_ids(path: Path, group_ids: set[int]) -> None:
    from persistent_json import write_json_document
    write_json_document(path, {"group_ids": sorted(group_ids)})


def record_group_id(group_id: int, path: str | Path = GROUPS_FILE) -> bool:
    """Persist a group or supergroup ID once."""
    if type(group_id) is not int or group_id >= 0:
        raise ValueError("Telegram group ID must be a negative integer.")

    groups_path = Path(path)
    with _GROUPS_LOCK:
        group_ids = load_group_ids(groups_path)
        if group_id in group_ids:
            return False
        group_ids.add(group_id)
        _write_group_ids(groups_path, group_ids)
        return True


def remove_group_id(group_id: int, path: str | Path = GROUPS_FILE) -> bool:
    """Remove a group the bot has left or been removed from."""
    groups_path = Path(path)
    with _GROUPS_LOCK:
        group_ids = load_group_ids(groups_path)
        if group_id not in group_ids:
            return False
        group_ids.remove(group_id)
        _write_group_ids(groups_path, group_ids)
        return True


def update_group_membership(
    update: telebot.types.ChatMemberUpdated,
    path: str | Path = GROUPS_FILE,
) -> None:
    """Track the bot's own membership changes, not other members' status."""
    if update.chat.type not in {"group", "supergroup"}:
        return
    membership = update.new_chat_member
    active = membership.status in {"member", "administrator", "creator"}
    if membership.status == "restricted":
        active = bool(getattr(membership, "is_member", False))
    if active:
        record_group_id(update.chat.id, path)
    else:
        remove_group_id(update.chat.id, path)


def get_user_count_reply(
    user_id: int | None,
    path: str | Path = USERS_FILE,
) -> str:
    """Return the user total only to the configured administrator."""
    if not is_admin(user_id):
        return FALLBACK_RESPONSE
    return f"סך הכל משתמשים בבוט: {len(load_user_ids(path))}"


class UserRegistrationMiddleware(BaseMiddleware):
    """Record private users and groups where the bot receives messages."""

    def __init__(
        self,
        users_file: str | Path = USERS_FILE,
        groups_file: str | Path = GROUPS_FILE,
    ) -> None:
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message"]
        self.users_file = Path(users_file)
        self.groups_file = Path(groups_file)

    def pre_process_message(
        self,
        message: telebot.types.Message,
        data: dict[str, object],
    ) -> None:
        chat = message.chat
        if chat.type == "private":
            user = message.from_user
            if user is not None and not user.is_bot:
                record_user_id(user.id, self.users_file)
        elif chat.type in {"group", "supergroup"}:
            migrated_to = getattr(message, "migrate_to_chat_id", None)
            migrated_from = getattr(message, "migrate_from_chat_id", None)
            if migrated_to:
                remove_group_id(chat.id, self.groups_file)
                record_group_id(migrated_to, self.groups_file)
            else:
                if migrated_from:
                    remove_group_id(migrated_from, self.groups_file)
                record_group_id(chat.id, self.groups_file)

    def post_process_message(
        self,
        message: telebot.types.Message,
        data: dict[str, object],
        exception: Exception | None,
    ) -> None:
        return None
