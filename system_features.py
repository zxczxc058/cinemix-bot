"""Persisted maintenance mode and anonymous hourly request counters."""

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import format_telegram_text, is_admin, load_bot_data

SYSTEM_SETTINGS_FILE = Path(__file__).resolve().with_name("system_settings.json")
ACTIVITY_FILE = Path(__file__).resolve().with_name("activity.json")
TIME_ZONE = ZoneInfo("Asia/Jerusalem")
DEFAULT_SYSTEM_SETTINGS = {
    "maintenance_enabled": False,
    "maintenance_message": "🛠️ הבוט כרגע בעבודות תחזוקה וסדר, נחזור לפעילות בהקדם!",
    "anti_forward_enabled": False,
}
_SETTINGS_LOCK = threading.RLock()
_ACTIVITY_LOCK = threading.RLock()


def write_json(path, data):
    from persistent_json import write_json_document
    write_json_document(path, data)

    from admin_edit_flow import record_commit
    record_commit(path)


def load_system_settings(path=SYSTEM_SETTINGS_FILE):
    with _SETTINGS_LOCK:
        path = Path(path)
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(path))
        except FileNotFoundError:
            data = dict(DEFAULT_SYSTEM_SETTINGS)
            write_json(path, data)
            return data
        if not isinstance(data, dict):
            raise ValueError("Invalid maintenance settings.")
        changed = False
        for key, default in DEFAULT_SYSTEM_SETTINGS.items():
            if key not in data:
                data[key] = default
                changed = True
        # Migrate the previous built-in maintenance copy to the requested text.
        if data["maintenance_message"] == "הבוט בתחזוקה קלה, נשוב מיד!":
            data["maintenance_message"] = DEFAULT_SYSTEM_SETTINGS["maintenance_message"]
            changed = True
        if (
            type(data.get("maintenance_enabled")) is not bool
            or not isinstance(data.get("maintenance_message"), str)
            or not data["maintenance_message"].strip()
            or len(data["maintenance_message"]) > 3000
            or type(data.get("anti_forward_enabled")) is not bool
        ):
            raise ValueError("Invalid maintenance settings.")
        if changed:
            write_json(path, data)
        return data


def toggle_maintenance(path=SYSTEM_SETTINGS_FILE):
    with _SETTINGS_LOCK:
        data = load_system_settings(path)
        data["maintenance_enabled"] = not data["maintenance_enabled"]
        write_json(path, data)
        return data["maintenance_enabled"]


def toggle_anti_forward(path=SYSTEM_SETTINGS_FILE):
    with _SETTINGS_LOCK:
        data = load_system_settings(path)
        data["anti_forward_enabled"] = not data["anti_forward_enabled"]
        write_json(path, data)
        return data["anti_forward_enabled"]


def update_maintenance_message(text, path=SYSTEM_SETTINGS_FILE):
    if not text.strip() or len(text) > 3000:
        raise ValueError("ההודעה חייבת להכיל בין 1 ל־3000 תווים.")
    with _SETTINGS_LOCK:
        data = load_system_settings(path)
        data["maintenance_message"] = text
        write_json(path, data)


def load_activity(path=ACTIVITY_FILE):
    with _ACTIVITY_LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise ValueError("Invalid hourly activity data.")
        sent = data.get("sent_today", {})
        if not isinstance(sent, dict):
            raise ValueError("Invalid daily outbound activity data.")
        for date, count in sent.items():
            datetime.strptime(date, "%Y-%m-%d")
            if type(count) is not int or count < 0:
                raise ValueError("Invalid daily outbound activity count.")
        for date, counts in data["days"].items():
            datetime.strptime(date, "%Y-%m-%d")
            if (
                not isinstance(counts, list) or len(counts) != 24
                or any(type(count) is not int or count < 0 for count in counts)
            ):
                raise ValueError("Invalid hourly activity counters.")
        return data


def record_sent_message(count=1, path=ACTIVITY_FILE, now=None):
    """Persist successful outbound Telegram messages by Israel calendar day."""
    if type(count) is not int or count < 1:
        raise ValueError("Sent message count must be a positive integer.")
    now = now or datetime.now(TIME_ZONE)
    if now.tzinfo is not None:
        now = now.astimezone(TIME_ZONE)
    day = now.strftime("%Y-%m-%d")
    with _ACTIVITY_LOCK:
        data = load_activity(path)
        sent = data.setdefault("sent_today", {})
        sent[day] = sent.get(day, 0) + count
        # Keep a bounded daily history for compact JSON storage.
        for previous_day in sorted(sent)[:-30]:
            del sent[previous_day]
        write_json(path, data)
        return sent[day]


def sent_messages_today(path=ACTIVITY_FILE, now=None):
    now = now or datetime.now(TIME_ZONE)
    if now.tzinfo is not None:
        now = now.astimezone(TIME_ZONE)
    return load_activity(path).get("sent_today", {}).get(now.strftime("%Y-%m-%d"), 0)


def record_activity(timestamp=None, path=ACTIVITY_FILE):
    now = (
        datetime.fromtimestamp(timestamp, TIME_ZONE)
        if timestamp is not None else datetime.now(TIME_ZONE)
    )
    with _ACTIVITY_LOCK:
        data = load_activity(path)
        counts = data["days"].setdefault(now.strftime("%Y-%m-%d"), [0] * 24)
        counts[now.hour] += 1
        write_json(path, data)


def hourly_activity_report(path=ACTIVITY_FILE):
    data = load_activity(path)
    days = data["days"]
    if not days:
        return "פעילות לפי שעות — שעון ישראל\nעדיין אין פניות מתועדות. הנתונים נאספים מעכשיו."
    totals = [sum(counts[hour] for counts in days.values()) for hour in range(24)]
    ranked = sorted(range(24), key=lambda hour: (-totals[hour], hour))
    dates = sorted(days)
    lines = [
        "פעילות לפי שעות — שעון ישראל",
        f"תקופה: {dates[0]} עד {dates[-1]}",
        f"סך הפניות: {sum(totals)}",
        "שעות העומס:",
    ]
    for hour in ranked[:3]:
        if totals[hour]:
            lines.append(f"• {hour:02d}:00–{(hour + 1) % 24:02d}:00 — {totals[hour]} פניות")
    peak_date, peak_hour, peak_count = max(
        ((date, hour, count)
         for date, counts in days.items() for hour, count in enumerate(counts)),
        key=lambda item: item[2],
    )
    lines.append(f"שיא בשעה אחת: {peak_count} פניות ב־{peak_date}, {peak_hour:02d}:00.")
    lines.append("נספרות פניות פרטיות ופקודות/מילות מפתח בקבוצות, ללא הודעות המנהל.")
    return "\n".join(lines)


class ActivityMaintenanceMiddleware(BaseMiddleware):
    def __init__(self, bot, settings_file=SYSTEM_SETTINGS_FILE, activity_file=ACTIVITY_FILE,
                 commands_file=None):
        super().__init__()
        self.bot = bot
        self.settings_file = settings_file
        self.activity_file = activity_file
        self.commands_file = commands_file
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]

    def pre_process_message(self, message, data):
        text = (getattr(message, "text", None) or "").strip()
        command = text.split(maxsplit=1)[0].partition("@")[0].casefold() if text else ""
        if message.chat.type == "private" and command == "/start":
            # /start always opens the welcome; maintenance still gates later actions.
            return None
        user = getattr(message, "from_user", None)
        if user is None or getattr(user, "is_bot", False) or is_admin(user.id):
            return None
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        private = message.chat.type == "private"
        group = message.chat.type in {"group", "supergroup"}
        if not (private or group):
            return None
        addressed = private
        if group and text:
            bot_data = load_bot_data(self.commands_file) if self.commands_file else load_bot_data()
            addressed = text.startswith("/") or any(
                keyword.casefold() in text.casefold() for keyword in bot_data["responses"]
            )
        if addressed and text:
            record_activity(getattr(message, "date", None), self.activity_file)
        settings = load_system_settings(self.settings_file)
        if not settings["maintenance_enabled"]:
            return None
        if addressed:
            notice, mode = format_telegram_text(settings["maintenance_message"])
            self.bot.send_message(message.chat.id, notice, parse_mode=mode)
        # Service messages still reach their dedicated handlers; ordinary group
        # conversation does not get a maintenance notice on every message.
        if private or text:
            return CancelUpdate()
        return None

    def pre_process_callback_query(self, call, data):
        if is_admin(call.from_user.id):
            return None
        settings = load_system_settings(self.settings_file)
        if settings["maintenance_enabled"]:
            text = re.sub(r"<[^>]*>", "", settings["maintenance_message"])
            self.bot.answer_callback_query(call.id, text=text[:200])
            return CancelUpdate()
        return None

    def post_process_message(self, message, data, exception):
        return None

    def post_process_callback_query(self, call, data, exception):
        return None
