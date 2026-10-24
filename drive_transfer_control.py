"""Private, uploader-owned cancellation; never stored in bot configuration."""
import secrets
import threading

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

_LOCK = threading.Lock()
_JOBS = {}
_CURRENT = threading.local()


class UploadCancelled(Exception):
    pass


class DriveJob:
    def __init__(self, uid):
        self.uid = uid
        self.token = secrets.token_urlsafe(9)
        self.event = threading.Event()
        self.previous = getattr(_CURRENT, "job", None)
        _CURRENT.job = self
        with _LOCK:
            _JOBS[self.token] = self

    def markup(self):
        return InlineKeyboardMarkup().row(InlineKeyboardButton(
            "❌ ביטול העלאה", callback_data="adm:enhance_drive_cancel:" + self.token,
        ))

    def close(self):
        with _LOCK:
            _JOBS.pop(self.token, None)
        _CURRENT.job = self.previous


def cancel_upload(uid, token):
    with _LOCK:
        job = _JOBS.get(token)
        if job is None or job.uid != uid:
            return False
        job.event.set()
        return True


def check_cancelled():
    job = getattr(_CURRENT, "job", None)
    if job is not None and job.event.is_set():
        raise UploadCancelled()


def cancel_user_uploads(uid):
    with _LOCK:
        for job in _JOBS.values():
            if job.uid == uid:
                job.event.set()


def current_upload_user_id():
    job = getattr(_CURRENT, "job", None)
    return job.uid if job is not None else None
