"""Persistent, atomic daily search quotas; authentication and support are separate."""

import copy
import math
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import is_admin
from management_config import CONFIG_FILE, _LOCK, load_config, save_json

KEY = "daily_command_limiter"
DEFAULTS = {
    "enabled": False,
    "limit": 20,
    "timezone": "UTC",
    "exceeded_message": "הגעת למגבלת הפקודות היומית.\nכמה זמן נותר: {hours} שעות ו-{minutes} דקות.",
    "usage": {},
}

def is_private_daily_request(message, trigger=None):
    """Authentication/navigation are not usage; support text/media are."""
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return getattr(message, "content_type", None) in {
            "photo", "video", "document", "audio", "voice", "animation",
            "video_note", "sticker", "contact", "location",
        }
    command = (trigger or text).split(maxsplit=1)[0].partition("@")[0].lower()
    return command not in {
        "/start", "/cancel", "/help", "/admin", "/users", "/vip",
        "/code", "/support", "/report",
    }


class DailyCommandLimiter:
    def __init__(self, config_file=CONFIG_FILE, *, vip_check=None, clock=None):
        self.path = config_file
        self.vip_check = vip_check or (lambda uid: False)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _settings(self, config):
        data = {**copy.deepcopy(DEFAULTS), **config.get(KEY, {})}
        if (
            type(data["enabled"]) is not bool or type(data["limit"]) is not int
            or not 1 <= data["limit"] <= 100000
            or data["timezone"] not in {"UTC", "Asia/Jerusalem"}
            or not isinstance(data["exceeded_message"], str)
            or not data["exceeded_message"].strip() or len(data["exceeded_message"]) > 3500
            or not isinstance(data["usage"], dict)
        ):
            raise ValueError("הגדרות הגבלת פקודות אינן תקינות.")
        return data

    def settings(self):
        with _LOCK:
            return self._settings(load_config(self.path))

    def update(self, **changes):
        if not changes.keys() <= {"enabled", "limit", "timezone", "exceeded_message"}:
            raise ValueError("הגדרה לא מוכרת.")
        with _LOCK:
            config = load_config(self.path)
            data = self._settings(config)
            data.update(changes)
            config[KEY] = self._settings({KEY: data})
            save_json(self.path, config)

    def reset_user(self, uid):
        if type(uid) is not int or not 0 < uid < 2**52:
            raise ValueError("שלח מזהה משתמש מספרי חיובי.")
        with _LOCK:
            config = load_config(self.path)
            data = self._settings(config)
            data["usage"].pop(str(uid), None)
            config[KEY] = data
            save_json(self.path, config)

    def _rejection(self, settings, now):
        next_day = datetime.combine(
            now.date() + timedelta(days=1), time.min, ZoneInfo(settings["timezone"]),
        )
        minutes = max(1, math.ceil((next_day.timestamp() - now.timestamp()) / 60))
        hours, minutes = divmod(minutes, 60)
        remaining = f"{hours} שעות ו-{minutes} דקות"
        template = settings["exceeded_message"]
        text = template.replace("{hours}", str(hours)).replace("{minutes}", str(minutes))
        text = text.replace("{time_remaining}", remaining).replace("{remaining_time}", remaining)
        if not any(variable in template for variable in ("{hours}", "{minutes}", "{time_remaining}", "{remaining_time}")):
            text += f"\nכמה זמן נותר: {remaining}"
        return text

    def consume(self, uid):
        if is_admin(uid) or self.vip_check(uid):
            return None  # Exempt users are neither limited nor counted.
        with _LOCK:
            config = load_config(self.path)
            settings = self._settings(config)
            if not settings["enabled"]:
                return None
            now = self.clock().astimezone(ZoneInfo(settings["timezone"]))
            day = now.date().isoformat()
            record = settings["usage"].get(str(uid), {})
            count = record.get("count", 0) if record.get("date") == day else 0
            if type(count) is not int or count < 0:
                raise ValueError("מונה פקודות לא תקין.")
            if count >= settings["limit"]:
                return self._rejection(settings, now)
            settings["usage"][str(uid)] = {"date": day, "count": count + 1}
            config[KEY] = settings
            save_json(self.path, config)
        return None


class DailyLimiterMiddleware(BaseMiddleware):
    def __init__(self, bot, limiter, *, eligible, skip=None, catalog_prefixes=(), request_callbacks=()):
        super().__init__()
        self.bot, self.limiter, self.eligible = bot, limiter, eligible
        self.skip = skip or (lambda message: False)
        self.catalog_prefixes = set(catalog_prefixes)
        self.request_callbacks = set(request_callbacks)
        self.update_types = ["message", "callback_query"]
        self.update_sensitive = True

    def pre_process_message(self, message, data):
        sender = getattr(message, "from_user", None)
        if (
            sender is None or getattr(sender, "is_bot", False)
            or getattr(message, "daily_quota_checked", False)
            or self.skip(message) or not self.eligible(message)
        ):
            return None
        message.daily_quota_checked = True
        rejection = self.limiter.consume(sender.id)
        if rejection:
            self.bot.send_message(message.chat.id, rejection, disable_web_page_preview=True)
            return CancelUpdate()
        return None

    def pre_process_callback_query(self, call, data):
        parts = (call.data or "").split(":")
        if (
            len(parts) != 3
            or not (
                (parts[0] in self.catalog_prefixes and parts[1] == "item")
                or (parts[0], parts[1]) in self.request_callbacks
            )
            or call.from_user is None or call.message is None
        ):
            return None
        rejection = self.limiter.consume(call.from_user.id)
        if rejection:
            self.bot.answer_callback_query(call.id, text=rejection[:200], show_alert=True)
            return CancelUpdate()
        return None

    def post_process_message(self, message, data, exception):
        pass

    def post_process_callback_query(self, call, data, exception):
        pass
