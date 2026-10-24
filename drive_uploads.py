"""Bounded streaming between Telegram and Google Drive's managed OAuth proxy."""

import base64
import json
import logging
from drive_upload_errors import DriveUploadError
from drive_resumable import upload_chunk
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import urlsplit
import requests
import selectors
import io
from telebot.types import InlineKeyboardMarkup
from drive_transfer_control import DriveJob, UploadCancelled, check_cancelled
from management_config import _LOCK, load_config, save_json
from telegram_large_files import MAX_FILE_SIZE, CLOUD_DOWNLOAD_LIMIT, CHUNK_SIZE, client_chunks, client_configured
from telegram_transfer_session import TelegramTransferCooldown

BRIDGE = Path(__file__).resolve().parent / "artifacts/api-server/google-drive-bridge.cjs"
_SLOTS = threading.BoundedSemaphore(2)


def telegram_retry_after(exc):
    """Read Bot API 429 and MTProto FloodWait without logging API URLs."""
    result = getattr(exc, "result_json", None)
    if isinstance(result, dict) and result.get("error_code") == 429:
        value = result.get("parameters", {}).get("retry_after", 3)
    elif type(exc).__name__ in {"FloodWait", "FloodWaitError"}:
        value = getattr(exc, "value", getattr(exc, "seconds", 3))
    else:
        return 0
    try:
        return max(1, float(value))
    except (ValueError, TypeError):
        return 3


class DriveUploads:
    _queue_lock = threading.RLock()
    _queues = {}
    _workers = set()

    def __init__(self, bot, config_file):
        self.bot, self.path = bot, Path(config_file)

    def enabled(self):
        return load_config(self.path).get("drive_upload_enabled", False)

    def enable(self, value):
        with _LOCK:
            state = load_config(self.path)
            state["drive_upload_enabled"] = bool(value)
            save_json(self.path, state)

    def oauth_operation(self, operation, uid, **parameters):
        try:
            process = subprocess.run(["node", str(BRIDGE)],
                                     input=json.dumps({**parameters, "operation": operation, "uid": uid}) + "\n",
                                     text=True, capture_output=True, timeout=40, check=False)
            result = json.loads(process.stdout)
        except (ValueError, KeyError, OSError, subprocess.SubprocessError):
            raise ValueError("חיבור Google אינו זמין. לחץ על התחברות/החלפת חשבון Google ונסה שוב.") from None
        if process.returncode or result.get("error"):
            if result.get("error_code") == "invalid_client_id":
                raise ValueError("הגדרת GOOGLE_OAUTH_CLIENT_ID אינה תקינה. יש להזין Client ID של Google ולא Client Secret.")
            if result.get("error_code") == "oauth_configuration_incomplete":
                raise ValueError("חסרות הגדרות Google OAuth. בדוק Client ID, Client Secret וכתובת החזרה.")
            if result.get("error_code") == "oauth_redirect_mismatch":
                raise ValueError("כתובת ההחזרה של Google OAuth חייבת להיות https://telegram-faq-bot.replit.app/api/google-drive/oauth/callback.")
            if result.get("error_code") == "google_invalid_client":
                raise ValueError("Google החזיר invalid_client. בדוק ב-Replit Secrets את GOOGLE_OAUTH_CLIENT_ID ואת GOOGLE_OAUTH_CLIENT_SECRET ואת הגדרת לקוח OAuth ב-Google Cloud.")
            raise ValueError("חיבור Google אינו זמין. לחץ על התחברות/החלפת חשבון Google ונסה שוב.")
        return result

    def account(self, uid):
        return self.oauth_operation("about", uid)["user"]

    def auth_link(self, uid):
        # Cancel old transfers before deleting this admin's previous credentials.
        self._cancel_user_transfers(uid)
        return self.oauth_operation("auth_link", uid)["url"]

    def reset_all_oauth(self, uid):
        from bot_storage import ADMIN_USER_ID
        if uid != ADMIN_USER_ID:
            raise PermissionError("Only the owner may reset all Google connections")
        from drive_catalog_sync import _jobs_lock, _jobs, cancel_user_sync
        from drive_transfer_control import cancel_user_uploads
        with _jobs_lock:
            syncing = set(_jobs)
        with self._queue_lock:
            users = set(self._queues) | set(self._workers) | syncing
            for user_id in users:
                cancel_user_uploads(user_id)
                cancel_user_sync(user_id)
                pending = self._queues.get(user_id)
                if pending is not None:
                    while True:
                        try:
                            pending.get_nowait()
                            pending.task_done()
                        except queue.Empty:
                            break
        return self.oauth_operation("reset_all", uid)

    def _cancel_user_transfers(self, uid):
        # Cancel active transfers and clear only this admin's waiting queue.
        from drive_transfer_control import cancel_user_uploads
        from drive_catalog_sync import cancel_user_sync
        cancel_user_sync(uid)
        with self._queue_lock:
            cancel_user_uploads(uid)
            pending = self._queues.get(uid)
            if pending is not None:
                while True:
                    try:
                        pending.get_nowait()
                        pending.task_done()
                    except queue.Empty:
                        break
    def logout(self, uid):
        self._cancel_user_transfers(uid)
        self.oauth_operation("logout", uid)

    def start(self, uid, file):
        if not self.enabled():
            raise ValueError("חיבור העלאות Drive כבוי.")
        size = getattr(file, "file_size", None)
        if isinstance(size, int) and size > MAX_FILE_SIZE:
            raise ValueError("הקובץ גדול ממגבלת 4GB.")
        if isinstance(size, int) and size > CLOUD_DOWNLOAD_LIMIT and not client_configured():
            raise ValueError("לקבצים גדולים יש להגדיר TELEGRAM_API_ID ו-TELEGRAM_API_HASH ב-Secrets של Replit. אין לשלוח אותם לבוט.")
        with self._queue_lock:
            pending = self._queues.setdefault(uid, queue.Queue())
            already_running = uid in self._workers
            pending.put(file)
            if not already_running:
                self._workers.add(uid)
                thread = threading.Thread(
                    target=self._drain_queue,
                    args=(uid, pending),
                    daemon=True,
                    name=f"drive-upload-{uid}",
                )
                try:
                    thread.start()
                except Exception:
                    self._workers.discard(uid)
                    try:
                        pending.get_nowait()
                        pending.task_done()
                    except queue.Empty:
                        pass
                    if pending.empty():
                        self._queues.pop(uid, None)
                    raise
        if already_running:
            try:
                self.bot.send_message(
                    uid,
                    "✅ הקובץ התקבל לתור ההעלאה ויעלה מיד לאחר סיום הקובץ הקודם.",
                )
            except Exception:
                logging.warning("Could not acknowledge a queued Drive file.")

    def _drain_queue(self, uid, pending):
        """Upload one admin's files serially while keeping the queue in memory."""
        while True:
            with self._queue_lock:
                try:
                    file = pending.get_nowait()
                except queue.Empty:
                    if self._queues.get(uid) is pending:
                        self._queues.pop(uid, None)
                    self._workers.discard(uid)
                    return
            try:
                with _SLOTS:
                    self._job(uid, file)
            except Exception:
                logging.exception("A queued Drive upload worker failed.")
            finally:
                pending.task_done()

    def _job(self, uid, file):
        job = DriveJob(uid)
        status = None
        last_progress = 0
        last_bytes = 0
        confirmed = None
        shown_bytes = None
        blocked_until = 0
        try:
            status = self.bot.send_message(
                uid, progress_text(0, getattr(file, "file_size", None) or 0), reply_markup=job.markup(),
            )
            def progress(sent, total, phase="upload"):
                nonlocal last_progress, last_bytes, blocked_until, confirmed, shown_bytes
                check_cancelled()
                if phase == "upload":
                    confirmed = (sent, total)
                now = time.monotonic()
                if now < blocked_until:
                    return
                if sent != total and now - last_progress < 3 and sent - last_bytes < 5 * 1024**2:
                    return
                last_progress = now
                last_bytes = sent
                try:
                    text = progress_text(sent, total)
                    if phase == "download":
                        text = text.replace("מעלה ל-Google Drive", "מוריד מ-Telegram")
                    self.bot.edit_message_text(
                        text, chat_id=uid, message_id=status.message_id, reply_markup=job.markup(),
                    )
                    if phase == "upload":
                        shown_bytes = sent
                except Exception as exc:
                    # Keep streaming, but obey Telegram's server-provided cooldown.
                    blocked_until = now + telegram_retry_after(exc)
            progress.download_progress = lambda sent, total: progress(sent, total, phase="download")
            check_cancelled()
            link = self.upload(file, progress)
            check_cancelled()
            if blocked_until:
                time.sleep(max(0, blocked_until - time.monotonic()))
            check_cancelled()
            if confirmed and confirmed[0] == confirmed[1] and shown_bytes != confirmed[0]:
                try:
                    self.bot.edit_message_text(progress_text(*confirmed), chat_id=uid, message_id=status.message_id)
                except Exception as exc:
                    delay = telegram_retry_after(exc)
                    if delay:
                        time.sleep(delay)
            try:
                self.bot.edit_message_text(link, chat_id=uid, message_id=status.message_id, disable_web_page_preview=True,
                                           reply_markup=InlineKeyboardMarkup())
            except Exception as exc:
                delay = telegram_retry_after(exc)
                if delay:
                    time.sleep(delay)
                    check_cancelled()
                    try:
                        self.bot.edit_message_text(link, chat_id=uid, message_id=status.message_id, disable_web_page_preview=True,
                                                   reply_markup=InlineKeyboardMarkup())
                    except Exception:
                        self._send_completion(uid, status, link)
                else:
                    self._send_completion(uid, status, link)
        except Exception as exc:
            # Network errors can include a Telegram token in their URL.
            logging.warning("Drive upload failed (%s, diagnostic=%s); no catalogue record was changed.",
                            type(exc).__name__, exc.code if isinstance(exc, DriveUploadError) else "transfer_failed")
            try:
                text = "ההעלאה לא הושלמה. בדוק את חיבור Google, חיבור Telegram Client והרשאות הקובץ ונסה שוב."
                if isinstance(exc, UploadCancelled):
                    text = "❌ ההעלאה בוטלה."
                if isinstance(exc, TelegramTransferCooldown):
                    text = exc.user_message()
                if isinstance(exc, DriveUploadError):
                    text = exc.user_message()
                if status:
                    self.bot.edit_message_text(text, chat_id=uid, message_id=status.message_id,
                                               reply_markup=InlineKeyboardMarkup())
                else:
                    self.bot.send_message(uid, text)
            except Exception:
                pass
        finally:
            job.close()

    def _send_completion(self, uid, status, link):
        check_cancelled()
        self.bot.send_message(uid, link, disable_web_page_preview=True)
        try:
            self.bot.delete_message(uid, status.message_id)
        except Exception:
            pass

    def upload(self, file, progress=None, uid=None):
        check_cancelled()
        if uid is None:
            from drive_transfer_control import current_upload_user_id
            uid = current_upload_user_id()
        size = getattr(file, "file_size", None)
        if isinstance(size, int) and size > MAX_FILE_SIZE:
            raise ValueError("File exceeds 4GB.")
        if isinstance(size, int) and size > CLOUD_DOWNLOAD_LIMIT:
            with client_chunks(self.bot.token, file.file_id) as chunks:
                return self._upload_source(file, size, chunks, progress, uid=uid)
        remote = self.bot.get_file(file.file_id)
        total = getattr(remote, "file_size", None) or getattr(file, "file_size", None)
        if type(total) is not int or not 0 < total <= MAX_FILE_SIZE:
            raise ValueError("Missing Telegram file size.")
        if total > CLOUD_DOWNLOAD_LIMIT:
            with client_chunks(self.bot.token, file.file_id) as chunks:
                return self._upload_source(file, total, chunks, progress, uid=uid)
        url = f"https://api.telegram.org/file/bot{self.bot.token}/{remote.file_path}"
        with requests.get(url, stream=True, timeout=(15, 90)) as response:
            response.raise_for_status()
            return self._upload_source(file, total, response.iter_content(chunk_size=CHUNK_SIZE), progress, uid=uid)

    def _upload_source(self, file, total, chunks, progress=None, uid=None):
        # The standalone bridge owns OAuth tokens; Python never obtains Google credentials.
        process = subprocess.Popen(["node", str(BRIDGE)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        session = None
        try:
            # Reuse the TLS connection across resumable chunks instead of
            # constructing a fresh requests session for every 16 MiB PUT.
            session = requests.Session()
            def exchange(payload):
                check_cancelled()
                process.stdin.write(json.dumps(payload) + "\n")
                process.stdin.flush()
                # OAuth/bridge failure must not leave an upload at 0% forever.
                try:
                    descriptor = process.stdout.fileno()
                except (io.UnsupportedOperation, AttributeError):
                    descriptor = None  # In-memory bridge adapters need no OS readiness wait.
                if type(descriptor) is int:
                    deadline = time.monotonic() + 45
                    with selectors.DefaultSelector() as selector:
                        selector.register(descriptor, selectors.EVENT_READ)
                        while not selector.select(timeout=.25):
                            check_cancelled()
                            if time.monotonic() >= deadline:
                                raise ValueError("Drive authorization response timed out.")
                line = process.stdout.readline()
                if not line:
                    raise ValueError("Drive bridge stopped.")
                result = json.loads(line)
                if result.get("error"):
                    raise DriveUploadError(result.get("error_code"))
                return result
            result = exchange({"operation": "upload", "uid": uid, "size": total, "direct": True,
                               "name": getattr(file, "file_name", None) or "video.mp4",
                               "mime": getattr(file, "mime_type", None) or "video/mp4"})
            if not result.get("ready"):
                raise ValueError("Upload session unavailable.")
            upload_url = result.get("upload_url")
            if upload_url:
                parsed = urlsplit(upload_url)
                if (parsed.scheme != "https" or parsed.netloc != "www.googleapis.com"
                        or not parsed.path.startswith("/upload/drive/")):
                    raise ValueError("Unexpected upload endpoint.")
            def send_chunk(part, offset):
                check_cancelled()
                if not upload_url:  # Compatibility with an older bridge during rollout.
                    return exchange({"offset": offset, "data": base64.b64encode(part).decode()})
                return upload_chunk(session, upload_url, part, offset, total,
                                    getattr(file, "mime_type", None) or "video/mp4")
            offset, downloaded, buffer = 0, 0, bytearray()
            for chunk in chunks:
                check_cancelled()
                if len(chunk) > CHUNK_SIZE or offset + len(buffer) + len(chunk) > total:
                    raise ValueError("Invalid source length.")
                buffer.extend(chunk)
                downloaded += len(chunk)
                download_progress = getattr(progress, "download_progress", None)
                if download_progress:
                    download_progress(downloaded, total)
                while len(buffer) >= CHUNK_SIZE:
                    part = bytes(buffer[:CHUNK_SIZE])
                    del buffer[:CHUNK_SIZE]
                    result = send_chunk(part, offset)
                    offset += len(part)
                    if progress:
                        progress(offset, total)
            if buffer:
                result = send_chunk(bytes(buffer), offset)
                offset += len(buffer)
                if progress:
                    progress(offset, total)
            if offset != total:
                raise DriveUploadError("telegram_media")
            if not result.get("link"):
                raise DriveUploadError("google_unavailable")
            return result["link"]
        finally:
            if session is not None:
                session.close()
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()


def progress_text(sent, total):
    from upload_progress import transfer_size
    percentage = min(100, sent * 100 // total) if total else 0
    filled = percentage * 10 // 100
    return (f"⏳ מעלה ל-Google Drive... {percentage}% "
            f"[{'█' * filled}{'░' * (10-filled)}] "
            f"({transfer_size(sent)} / {transfer_size(total)})")
