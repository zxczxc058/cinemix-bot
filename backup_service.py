"""Allowlisted JSON backups, weekly deduplication, and partial-upload resumption."""

import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from bot_storage import ADMIN_USER_ID
from group_management import delete_later
from management_config import load_config, save_json
from support_storage import load_support_settings
from system_features import TIME_ZONE

BACKUP_STATE_FILE = Path(__file__).resolve().with_name("backup_state.json")
DATA_FILES = (
    "config.json", "users.json", "commands.json", "groups.json", "group_settings.json",
    "system_settings.json", "activity.json", "access_settings.json",
    "admin_ui_state.json", "night_lock_state.json", "support_settings.json",
    "support_threads.json", "support_tickets.json", "tickets.json",
    "moderation_routes.json", "tasks.json", "backup_state.json",
    "reports.json", "command_drafts.json", "media_db.json",
    "moderation_events.json", "link_health.json",
)


def load_backup_state(path=BACKUP_STATE_FILE):
    from persistent_json import read_json_text
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or "last_success_week" not in data or "active" not in data:
        raise ValueError("Invalid backup state.")
    last = data["last_success_week"]
    if last is not None and not isinstance(last, str):
        raise ValueError("Invalid backup period.")
    active = data["active"]
    if active is not None:
        if (
            not isinstance(active, dict) or not isinstance(active.get("week"), str)
            or not isinstance(active.get("sent_files"), list)
            or any(name not in DATA_FILES for name in active["sent_files"])
            or not isinstance(active.get("retry_after"), (int, float))
        ):
            raise ValueError("Invalid in-progress backup.")
    return data


class BackupService:
    def __init__(self, bot, root, support_file, config_file=None, state_file=None, pace=3.1):
        self.bot, self.root = bot, Path(root)
        self.support_file = support_file
        self.config_file = config_file or self.root / "config.json"
        self.state_file = state_file or self.root / "backup_state.json"
        self.pace = pace
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def _snapshots(self):
        snapshots = []
        for name in DATA_FILES:
            path = self.root / name
            if not path.exists():
                continue
            # Refuse symlinks escaping the data directory.
            if path.is_symlink() or path.resolve().parent != self.root.resolve():
                raise ValueError("נתיב גיבוי אינו בתוך תיקיית נתוני הבוט.")
            from persistent_json import read_json_text
            payload = read_json_text(path).encode("utf-8")
            json.loads(payload)
            if len(payload) > 45 * 1024 * 1024:
                raise ValueError(f"קובץ הגיבוי {name} גדול מדי לשליחה.")
            snapshots.append((name, payload))
        if not snapshots:
            raise ValueError("אין קובצי נתונים לגיבוי.")
        return snapshots

    def _target(self):
        from admin_notifications import resolve_admin_chat
        config = load_config(self.config_file)
        target = config.get("LOG_CHAT_ID")
        if type(target) is not int or target >= 0:
            raise ValueError("הגדר קבוצת גיבוי נפרדת תחת LOG_CHAT_ID.")
        management_targets = {
            config.get(key) for key in
            ("LOG_CHANNEL_ID", "ADMIN_CHAT_ID", "ADMIN_GROUP_ID", "admin_chat_id")
            if config.get(key) is not None
        }
        support_target = (
            load_support_settings(self.support_file)["support_chat_id"]
            if self.support_file.exists() else None
        )
        management_targets.add(resolve_admin_chat(
            support_target, config_file=self.config_file,
        ))
        if target in management_targets:
            raise ValueError("קבוצת הגיבוי חייבת להיות נפרדת מקבוצת הניהול.")
        return target

    def _notice(self, chat_id, text):
        sent = self.bot.send_message(chat_id, text)
        delete_later(self.bot, chat_id, sent.message_id, 5)

    def _document(self, target, name, payload):
        if target != self._target():
            raise ValueError("יעד הגיבוי השתנה; לא יישלח קובץ לקבוצת הניהול.")
        with io.BytesIO(payload) as document:
            document.name = name
            self.bot.send_document(target, document, visible_file_name=name)
        if self.pace:
            time.sleep(self.pace)

    def manual_config(self):
        """Send only config.json to the quiet LOG_CHAT_ID destination."""
        if not self._lock.acquire(blocking=False):
            raise ValueError("גיבוי כבר מתבצע. המתן לסיומו.")
        try:
            from persistent_json import read_json_text
            target = self._target()
            path = self.config_file
            if path.is_symlink() or path.resolve().parent != self.root.resolve():
                raise ValueError("נתיב config.json אינו בתוך תיקיית נתוני הבוט.")
            payload = read_json_text(path).encode("utf-8")
            json.loads(payload)
            if len(payload) > 45 * 1024 * 1024:
                raise ValueError("קובץ config.json גדול מדי לשליחה.")
            with io.BytesIO(payload) as document:
                document.name = "config.json"
                self.bot.send_document(target, document, visible_file_name="config.json")
            return target
        finally:
            self._lock.release()

    def manual(self):
        if not self._lock.acquire(blocking=False):
            raise ValueError("גיבוי כבר מתבצע. המתן לסיומו.")
        try:
            target = self._target()
            snapshots = self._snapshots()
        except Exception:
            self._lock.release()
            raise

        def work():
            succeeded = failed = 0
            try:
                for name, payload in snapshots:
                    try:
                        self._document(target, name, payload)
                    except Exception as exc:
                        failed += 1
                        logging.warning("Manual backup upload failed (%s).", type(exc).__name__)
                    else:
                        succeeded += 1
                self._notice(target, f"💾 הגיבוי הסתיים: {succeeded} קבצים נשלחו, {failed} נכשלו.")
            except Exception as exc:
                logging.warning("Manual backup report failed (%s).", type(exc).__name__)
            finally:
                self._lock.release()

        worker = threading.Thread(target=work, name="manual-json-backup", daemon=True)
        worker.start()
        return worker

    def tick(self, now=None):
        now = (now or datetime.now(TIME_ZONE)).astimezone(TIME_ZONE)
        monday = (now - timedelta(days=now.weekday())).replace(hour=10, minute=0, second=0, microsecond=0)
        if now < monday or not load_config(self.config_file)["automatic_backup"]:
            return False
        week = now.strftime("%G-W%V")
        if not self._lock.acquire(blocking=False):
            return False
        try:
            state = load_backup_state(self.state_file)
            if state["last_success_week"] and state["last_success_week"] >= week:
                return False
            try:
                target = self._target()
            except ValueError:
                return False
            active = state["active"]
            if active is None or active["week"] != week:
                active = {"week": week, "sent_files": [], "retry_after": 0}
                state["active"] = active
                save_json(self.state_file, state)
            if active["retry_after"] > now.timestamp():
                return False
            failed = 0
            for name, payload in self._snapshots():
                if name in active["sent_files"]:
                    continue
                try:
                    self._document(target, name, payload)
                except Exception as exc:
                    failed += 1
                    logging.warning("Weekly backup upload failed (%s).", type(exc).__name__)
                else:
                    active["sent_files"].append(name)
                    save_json(self.state_file, state)
            if failed:
                active["retry_after"] = (now + timedelta(minutes=10)).timestamp()
                save_json(self.state_file, state)
                self._notice(ADMIN_USER_ID, f"גיבוי שבועי: {failed} קבצים נכשלו. יבוצע ניסיון חוזר רק לקבצים החסרים.")
                return False
            state.update(last_success_week=week, active=None)
            save_json(self.state_file, state)
            self._notice(target, f"💾 הגיבוי השבועי הושלם: {len(active['sent_files'])} קבצים.")
            return True
        finally:
            self._lock.release()

    def start(self):
        if self._thread:
            return
        def work():
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logging.warning("Weekly backup scheduler failed (%s).", type(exc).__name__)
                self._stop.wait(60)
        self._thread = threading.Thread(target=work, name="weekly-json-backup", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
