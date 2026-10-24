"""Single support-group configuration and persistent relay message routing."""

import json
import threading
from pathlib import Path

from system_features import write_json

SUPPORT_SETTINGS_FILE = Path(__file__).resolve().with_name("support_settings.json")
SUPPORT_THREADS_FILE = Path(__file__).resolve().with_name("support_threads.json")
SUPPORT_LOCK = threading.RLock()
DEFAULT_REMINDER_MINUTES = 1440
MAX_REMINDER_MINUTES = 43200


def load_support_settings(path=SUPPORT_SETTINGS_FILE):
    with SUPPORT_LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        group = data.get("support_chat_id") if isinstance(data, dict) else None
        if (
            not isinstance(data, dict)
            or (group is not None and (type(group) is not int or group >= 0))
            or not isinstance(data.get("confirmation_text"), str)
            or not data["confirmation_text"].strip() or len(data["confirmation_text"]) > 3000
        ):
            raise ValueError("Invalid support settings.")
        enabled = data.setdefault("reminders_enabled", False)
        minutes = data.setdefault("reminder_minutes", DEFAULT_REMINDER_MINUTES)
        if type(enabled) is not bool or type(minutes) is not int or not 1 <= minutes <= MAX_REMINDER_MINUTES:
            raise ValueError("Invalid support reminder settings.")
        return data


def set_support_chat(chat_id, path=SUPPORT_SETTINGS_FILE):
    if type(chat_id) is not int or chat_id >= 0:
        raise ValueError("יש להגדיר את התמיכה מתוך קבוצה בלבד.")
    with SUPPORT_LOCK:
        data = load_support_settings(path)
        data["support_chat_id"] = chat_id
        write_json(path, data)


def update_support_confirmation(text, path=SUPPORT_SETTINGS_FILE):
    if not text.strip() or len(text) > 3000:
        raise ValueError("ההודעה חייבת להכיל בין 1 ל־3000 תווים.")
    with SUPPORT_LOCK:
        data = load_support_settings(path)
        data["confirmation_text"] = text
        write_json(path, data)
        # The legacy support editor and the system-message editor control the
        # same acknowledgement, so neither can leave a stale active override.
        from system_messages import update_message
        update_message("request_received", text, Path(path).with_name("config.json"))


def update_support_reminders(*, enabled=None, minutes=None, path=SUPPORT_SETTINGS_FILE):
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("מצב התזכורות אינו תקין.")
    if minutes is not None and (type(minutes) is not int or not 1 <= minutes <= MAX_REMINDER_MINUTES):
        raise ValueError(f"יש להזין מספר דקות בין 1 ל־{MAX_REMINDER_MINUTES}.")
    with SUPPORT_LOCK:
        data = load_support_settings(path)
        if enabled is not None:
            data["reminders_enabled"] = enabled
        if minutes is not None:
            data["reminder_minutes"] = minutes
        write_json(path, data)


def load_support_threads(path=SUPPORT_THREADS_FILE):
    with SUPPORT_LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        if (
            not isinstance(data, dict) or not isinstance(data.get("messages"), dict)
            or not isinstance(data.get("pending_users"), list)
            or any(type(uid) is not int or uid <= 0 for uid in data["pending_users"])
        ):
            raise ValueError("Invalid support relay state.")
        for key, uid in data["messages"].items():
            group, message_id = key.split(":")
            if int(group) >= 0 or int(message_id) <= 0 or type(uid) is not int or uid <= 0:
                raise ValueError("Invalid support relay route.")
        replies = data.setdefault("delivered_replies", {})
        if not isinstance(replies, dict):
            raise ValueError("Invalid delivered support replies.")
        for key, uid in replies.items():
            group, message_id = key.split(":")
            if int(group) >= 0 or int(message_id) <= 0 or type(uid) is not int or uid <= 0:
                raise ValueError("Invalid delivered support reply.")
        requests = data.setdefault("requests", {})
        if not isinstance(requests, dict):
            raise ValueError("Invalid support requests.")
        for key, request in requests.items():
            if (
                key not in data["messages"] or not isinstance(request, dict)
                or request.get("status") not in ("open", "handled")
            ):
                raise ValueError("Invalid support request status.")
            # Legacy headers lack a known creation time: never invent their age.
            created = request.get("created_at")
            if "created_at" in request and (type(created) is not int or created <= 0):
                raise ValueError("Invalid support request creation time.")
            if type(request.get("reminder_sent", False)) is not bool:
                raise ValueError("Invalid support reminder state.")
        return data


def clear_pending_support_users(user_id=None, path=SUPPORT_THREADS_FILE):
    """Clear temporary 'waiting for the next support message' state only."""
    with SUPPORT_LOCK:
        data = load_support_threads(path)
        old = data["pending_users"]
        if user_id is None:
            data["pending_users"] = []
        else:
            data["pending_users"] = [uid for uid in old if uid != user_id]
        removed = len(old) - len(data["pending_users"])
        if removed:
            write_json(path, data)
        return removed


def track_legacy_header(chat_id, message_id, settings_path=SUPPORT_SETTINGS_FILE,
                        threads_path=SUPPORT_THREADS_FILE):
    """Track only the explicitly selected existing route, never infer media links."""
    if type(chat_id) is not int or type(message_id) is not int or message_id <= 0:
        raise ValueError("מזהה הודעת הכותרת אינו תקין.")
    with SUPPORT_LOCK:
        group = load_support_settings(settings_path)["support_chat_id"]
        if group is None or chat_id != group:
            raise ValueError("יש לצרף פנייה מתוך קבוצת התמיכה הנוכחית בלבד.")
        state = load_support_threads(threads_path)
        key = f"{chat_id}:{message_id}"
        if key not in state["messages"]:
            raise ValueError("להודעה זו אין מסלול מענה שמור; לא ניתן לצרפה למעקב.")
        created = key not in state["requests"]
        if created:
            # No inferred creation time or relationships, and no private content.
            state["requests"][key] = {"status": "open"}
            write_json(threads_path, state)
        return created, state["requests"][key]["status"]


def open_support_requests(settings_path=SUPPORT_SETTINGS_FILE, threads_path=SUPPORT_THREADS_FILE):
    """Return header IDs only, scoped to the current support group."""
    with SUPPORT_LOCK:
        group = load_support_settings(settings_path)["support_chat_id"]
        state = load_support_threads(threads_path)
        return [
            int(key.split(":")[1]) for key, request in state["requests"].items()
            if key.split(":")[0] == str(group) and request["status"] == "open"
        ]
