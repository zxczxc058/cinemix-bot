"""Restart-safe night locking with persisted original group permissions."""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from telebot.types import ChatPermissions
from telebot.handler_backends import BaseMiddleware

from bot_storage import ADMIN_USER_ID, GROUPS_FILE, format_telegram_text, load_group_ids
from group_settings import GROUP_SETTINGS_FILE, load_group_settings
from system_features import TIME_ZONE, write_json
from group_management import delete_later

NIGHT_STATE_FILE = Path(__file__).resolve().with_name("night_lock_state.json")
SEND_FLAGS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews",
)


def is_closed_time(now, close_time, open_time):
    minutes = now.hour * 60 + now.minute
    close = int(close_time[:2]) * 60 + int(close_time[3:])
    opened = int(open_time[:2]) * 60 + int(open_time[3:])
    if close > opened:
        return minutes >= close or minutes < opened
    return close <= minutes < opened


def load_night_state(path=NIGHT_STATE_FILE):
    from persistent_json import read_json_text
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("Invalid night-lock state.")
    for group_id, record in data["groups"].items():
        if int(group_id) >= 0 or not isinstance(record, dict):
            raise ValueError("Invalid stored group lock.")
        if record.get("phase") not in {"closing", "locked", "opened"}:
            raise ValueError("Invalid night-lock phase.")
        permissions = record.get("permissions")
        if (
            not isinstance(permissions, dict) or not permissions
            or any(not key.startswith("can_") or type(value) is not bool
                   for key, value in permissions.items())
        ):
            raise ValueError("Invalid original group permissions.")
        if type(record.get("close_announced")) is not bool:
            raise ValueError("Invalid night announcement state.")
    return data


class NightLockManager:
    def __init__(self, bot, settings_file=GROUP_SETTINGS_FILE,
                 groups_file=GROUPS_FILE, state_file=NIGHT_STATE_FILE):
        self.bot = bot
        self.settings_file = settings_file
        self.groups_file = groups_file
        self.state_file = state_file
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._last_error_notice = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="night-lock", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self):
        self._wake.set()

    def migrate_group(self, old_id, new_id):
        with self._lock:
            state = load_night_state(self.state_file)
            record = state["groups"].pop(str(old_id), None)
            if record is not None:
                if record["phase"] == "locked":
                    record["phase"] = "closing"
                state["groups"][str(new_id)] = record
                self._commit(state)
        self.wake()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                logging.warning("Night-lock scheduler failed (%s).", type(exc).__name__)
                self._report_failures(1)
            self._wake.wait(30)
            self._wake.clear()

    def tick(self, now=None):
        now = now or datetime.now(TIME_ZONE)
        if now.tzinfo is not None:
            now = now.astimezone(TIME_ZONE)
        with self._lock:
            settings = load_group_settings(self.settings_file)["night_lock"]
            state = load_night_state(self.state_file)
            active_groups = load_group_ids(self.groups_file)
            closed = settings["enabled"] and is_closed_time(
                now, settings["close_time"], settings["open_time"]
            )
            failures = 0
            targets = active_groups | {int(group_id) for group_id in state["groups"]}
            for group_id in sorted(targets):
                from allowed_chats import chat_is_active
                if not chat_is_active(self.bot, group_id):
                    continue
                try:
                    self._reconcile(
                        group_id, closed and group_id in active_groups, settings, state
                    )
                except Exception as exc:
                    failures += 1
                    logging.warning("Group night-lock operation failed (%s).", type(exc).__name__)
            if failures:
                self._report_failures(failures)
            return failures

    def _commit(self, state):
        write_json(self.state_file, state)

    def _set_permissions(self, group_id, permissions):
        result = self.bot.set_chat_permissions(
            group_id, ChatPermissions(**permissions),
            use_independent_chat_permissions=True,
        )
        if result is False:
            raise RuntimeError("Telegram rejected group permissions.")

    def _announce(self, group_id, text):
        text, mode = format_telegram_text(text)
        self.bot.send_message(group_id, text, parse_mode=mode)

    def _reconcile(self, group_id, closed, settings, state):
        key = str(group_id)
        record = state["groups"].get(key)
        # Opening is committed before its announcement. A failed announcement
        # must never cause a second restore or lose the original snapshot.
        if record and record["phase"] == "opened":
            self._announce(group_id, settings["open_message"])
            del state["groups"][key]
            self._commit(state)
            record = None
        if closed:
            if record is None:
                chat = self.bot.get_chat(group_id)
                if chat.permissions is None:
                    raise RuntimeError("Cannot safely snapshot group permissions.")
                permissions = {
                    name: value for name, value in chat.permissions.to_dict().items()
                    if name.startswith("can_") and type(value) is bool
                    and name != "can_send_media_messages"
                }
                if not permissions:
                    raise RuntimeError("Group permission snapshot is empty.")
                record = {
                    "permissions": permissions, "phase": "closing", "close_announced": False,
                }
                state["groups"][key] = record
                # Save before the external operation, so a restart can restore.
                self._commit(state)
            if record["phase"] == "closing":
                self._set_permissions(group_id, {**record["permissions"], **dict.fromkeys(SEND_FLAGS, False)})
                record["phase"] = "locked"
                self._commit(state)
            if not record["close_announced"]:
                self._announce(group_id, settings["close_message"])
                record["close_announced"] = True
                self._commit(state)
        elif record:
            self._set_permissions(group_id, record["permissions"])
            record["phase"] = "opened"
            self._commit(state)
            self._announce(group_id, settings["open_message"])
            del state["groups"][key]
            self._commit(state)

    def _report_failures(self, count):
        now = time.monotonic()
        if self._last_error_notice is not None and now - self._last_error_notice < 600:
            return
        try:
            sent = self.bot.send_message(
                ADMIN_USER_ID,
                f"מצב לילה: פעולה נכשלה ב־{count} קבוצות או ברכיב התזמון. "
                "ודא שהבוט מנהל עם הרשאת הגבלת משתמשים. ההרשאות המקוריות נשמרות "
                "והבוט ינסה שוב אוטומטית.",
            )
            delete_later(self.bot, ADMIN_USER_ID, sent.message_id, 5.0)
        except Exception as exc:
            logging.warning("Night-lock admin notice failed (%s).", type(exc).__name__)
        else:
            self._last_error_notice = now


class NightMigrationMiddleware(BaseMiddleware):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager
        self.update_sensitive = True
        self.update_types = ["message"]

    def pre_process_message(self, message, data):
        to_id = getattr(message, "migrate_to_chat_id", None)
        from_id = getattr(message, "migrate_from_chat_id", None)
        if to_id:
            self.manager.migrate_group(message.chat.id, to_id)
        elif from_id:
            self.manager.migrate_group(from_id, message.chat.id)

    def post_process_message(self, message, data, exception):
        return None
