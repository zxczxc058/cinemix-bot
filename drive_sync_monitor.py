"""Non-blocking, additive recurring Drive movie/season/episode discovery."""
import logging
import threading

from admin_roles import admin_ids
from bot_storage import ADMIN_USER_ID, is_admin
from drive_catalog_sync import (
    _jobs_lock, _jobs, _cancel_events, folder_reference, sync_drive_content, save_scan_media,
)
from media_storage import MEDIA_DB_FILE, MediaRepository


class DriveSyncMonitor:
    def __init__(self, drive, regular=None, vip=None, interval=60):
        self.drive = drive
        self.regular = regular or MediaRepository()
        self.vip = vip or MediaRepository(MEDIA_DB_FILE.with_name("vip_media_db.json"))
        self.interval = interval
        self.stopped = threading.Event()
        self._active = None
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="drive-catalog-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self._active is not None:
            self._active.set()

    def sync_account(self, user_id):
        if not is_admin(user_id) or self.stopped.is_set():
            return
        if not self.drive.oauth_operation("status", user_id).get("connected"):
            return
        with _jobs_lock:
            if user_id in _jobs:
                return
            event = threading.Event()
            _jobs.add(user_id)
            _cancel_events[user_id] = event
            self._active = event
        try:
            roots = {("root", None)}
            # Shared season folders may not be under My Drive. Keep following
            # explicit links, including links replaced by a Telegram edit.
            for repository in (self.regular, self.vip):
                for item in repository.list_media():
                    if item.get("drive_account_id") not in (None, user_id):
                        continue
                    values = [item.get("drive_url"), *item.get("drive_folder_urls", [])]
                    values += [file.get("drive_url") for file in item.get("telegram_files", [])]
                    for value in values:
                        if isinstance(value, str) and "/folders/" in value:
                            try:
                                roots.add(folder_reference(value))
                            except ValueError:
                                pass
            seen_folders = set()
            for folder_id, resource_key in sorted(roots, key=lambda entry: entry[0] != "root"):
                if event.is_set() or self.stopped.is_set():
                    break
                if folder_id in seen_folders:
                    continue
                try:
                    files = sync_drive_content(self.drive, user_id, folder_id, resource_key, event)
                    if event.is_set() or self.stopped.is_set():
                        break
                    seen_folders.update(file["folder_id"] for file in files)
                    result = save_scan_media(files, self.regular, self.vip, user_id=user_id)
                    logging.info("Automatic Drive catalogue sync completed: %s files, %s added, %s linked.",
                                 result["scanned"], result["added"], result["linked"])
                except Exception as exc:
                    # A denied shared folder must not erase other imports or
                    # terminate monitoring of future uploads.
                    logging.warning("Automatic Drive subtree sync failed (%s).", type(exc).__name__)
        finally:
            with _jobs_lock:
                _jobs.discard(user_id)
                _cancel_events.pop(user_id, None)
                self._active = None

    def _loop(self):
        while not self.stopped.is_set():
            for user_id in {ADMIN_USER_ID, *admin_ids()}:
                if self.stopped.is_set():
                    break
                try:
                    self.sync_account(user_id)
                except Exception as exc:
                    logging.warning("Automatic Drive account sync unavailable (%s).", type(exc).__name__)
            self.stopped.wait(self.interval)
