"""Independent Telegram-backed web links. No external upload or media catalogue changes."""

from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit

from persistent_json import read_json_text, write_json_document
from telegram_large_files import MAX_FILE_SIZE, CLOUD_DOWNLOAD_LIMIT, client_configured
from telegram_web_source import telegram_range
from telegram_transfer_session import TelegramTransferCooldown
from web_link_progress import WebLinkProgress

STREAM_FILE = Path(__file__).resolve().with_name("web_streams.json")
_LOCK = threading.RLock()
_PREPARATIONS = threading.BoundedSemaphore(2)
_VIEWERS = threading.BoundedSemaphore(3)
_BOT = None
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
SAFE_VIDEO_TYPES = {"video/mp4", "video/webm", "video/ogg", "video/quicktime", "video/x-matroska"}


def configure_stream_bot(bot):
    """Called only after the active service verifies its Telegram identity."""
    global _BOT
    _BOT = bot


def load_streams(path=STREAM_FILE):
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("links"), dict):
        raise ValueError("Invalid web stream registry.")
    return data


def base_url(path=STREAM_FILE):
    value = load_streams(path).get("public_base_url", "").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Web streaming public URL is not configured.")
    return value


def find_stream(token, path=STREAM_FILE):
    if not TOKEN_PATTERN.fullmatch(token):
        return None
    return load_streams(path)["links"].get(hashlib.sha256(token.encode()).hexdigest())


def byte_range(header, size):
    """Single HTTP ranges, including suffixes. Reject multi-range/invalid requests."""
    if header is None:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match or not any(match.groups()):
        raise ValueError("Invalid range.")
    first, last = match.groups()
    if not first:
        count = int(last)
        if count <= 0:
            raise ValueError("Invalid suffix.")
        return max(0, size - count), size - 1, 206
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("Unsatisfiable range.")
    return start, end, 206


@contextmanager
def source(bot, record, start, end):
    with telegram_range(bot, record, start, end) as chunks:
        yield chunks


def preparing_text(sent, total):
    from upload_progress import transfer_size
    ratio = min(100, sent * 100 / total)
    # A large file's first MiB is real progress, not another apparent 0%.
    percent = f"{ratio:.2f}" if 0 < ratio < 1 else str(int(ratio))
    filled = int(ratio) // 10
    return (f"⏳ מכין קישור צפייה מטלגרם... {percent}% "
            f"[{'█' * filled}{'░' * (10 - filled)}]\n"
            f"({transfer_size(sent)} / {transfer_size(total)})")


class WebStreams:
    def __init__(self, bot, path=STREAM_FILE):
        self.bot, self.path = bot, Path(path)

    def start(self, uid, file, source_message=None):
        size = getattr(file, "file_size", None)
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_FILE_SIZE:
            raise ValueError("שלח קובץ בגודל ידוע, עד 4GB.")
        if size > CLOUD_DOWNLOAD_LIMIT and not client_configured():
            raise ValueError("קובץ גדול דורש TELEGRAM_API_ID ו-TELEGRAM_API_HASH ב-Secrets.")
        base_url(self.path)
        if not _PREPARATIONS.acquire(blocking=False):
            raise ValueError("שתי הכנות קישור פעילות. נסה שוב לאחר סיומן.")
        try:
            threading.Thread(target=self._job, args=(uid, file, source_message), daemon=True, name="web-link-prepare").start()
        except Exception:
            _PREPARATIONS.release()
            raise

    def _job(self, uid, file, source_message=None):
        status = None
        progress = None
        try:
            record = {
                "file_id": file.file_id, "size": file.file_size,
                "name": (getattr(file, "file_name", None) or "video.mp4")[:255],
                "mime": getattr(file, "mime_type", None) or "application/octet-stream",
                "owner_id": uid, "created_at": int(time.time()),
                "bot_id": int(self.bot.token.split(":", 1)[0]),
            }
            if source_message is not None:
                record["source_chat_id"] = source_message.chat.id
                record["source_message_id"] = source_message.message_id
            status = self.bot.send_message(uid, preparing_text(0, record["size"]))
            progress = WebLinkProgress(self.bot, uid, status.message_id, record["size"], preparing_text)
            progress.start()
            sent = 0
            # Check real file accessibility before publishing a durable link.
            # This is a bounded verification stream, NOT a second cloud upload.
            with source(self.bot, record, 0, record["size"] - 1) as chunks:
                for chunk in chunks:
                    sent += len(chunk)
                    if sent > record["size"]:
                        raise ValueError("Unexpected file length.")
                    progress.progress_callback(sent, record["size"])
            if sent != record["size"]:
                raise ValueError("Incomplete verification.")
            progress.close()
            shown_bytes, wait_until = progress.shown_bytes, progress.wait_until
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            with _LOCK:
                data = load_streams(self.path)
                data["links"][digest] = record
                write_json_document(self.path, data)
            link = f"{base_url(self.path)}/watch/{token}"
            if wait_until:
                time.sleep(max(0, wait_until - time.monotonic()))
            if shown_bytes != record["size"]:
                # Retry the actual completion edit after a FloodWait, rather than
                # sleeping and silently replacing an unchanged 0% with the URL.
                for attempt in range(3):
                    try:
                        self.bot.edit_message_text(preparing_text(sent, record["size"]),
                                                   chat_id=uid, message_id=status.message_id, parse_mode=None)
                        logging.info("Progress updated: %s %s", sent, record["size"])
                        break
                    except Exception as exc:
                        description = getattr(exc, "description", "")
                        if isinstance(description, str) and "message is not modified" in description.lower():
                            break
                        from drive_uploads import telegram_retry_after
                        delay = telegram_retry_after(exc)
                        if not delay or attempt == 2:
                            logging.warning("Web preparation completion edit failed (%s).", type(exc).__name__)
                            break
                        time.sleep(delay)
            try:
                self.bot.edit_message_text(link, chat_id=uid, message_id=status.message_id,
                                           disable_web_page_preview=True)
            except Exception as exc:
                from drive_uploads import telegram_retry_after
                delay = telegram_retry_after(exc)
                if delay:
                    time.sleep(delay)
                self.bot.send_message(uid, link, disable_web_page_preview=True)
                try:
                    self.bot.delete_message(uid, status.message_id)
                except Exception:
                    pass
        except Exception as exc:
            if progress is not None:
                progress.close(cancel=True)
            logging.warning("Web stream preparation failed (%s).", type(exc).__name__)
            text = "לא ניתן להכין קישור כרגע. בדוק שהקובץ נגיש בטלגרם ונסה שוב."
            if isinstance(exc, TelegramTransferCooldown):
                text = exc.user_message()
            try:
                if status:
                    self.bot.edit_message_text(text, chat_id=uid, message_id=status.message_id)
                else:
                    self.bot.send_message(uid, text)
            except Exception:
                pass
        finally:
            _PREPARATIONS.release()


def _headers(handler, code, length, content_type, extra=()):
    handler.send_response(code)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
    for key, value in extra:
        handler.send_header(key, value)
    handler.end_headers()


def _error(handler, code, head=False, extra=()):
    body = {403: b"Access denied.", 404: b"Link unavailable.", 503: b"Stream temporarily unavailable.",
            416: b"Requested range unavailable.", 502: b"Telegram file unavailable."}[code]
    _headers(handler, code, len(body), "text/plain; charset=utf-8", extra)
    if not head:
        handler.wfile.write(body)


def video_mime(record):
    return record["mime"] if record["mime"] in SAFE_VIDEO_TYPES else {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
        ".ogv": "video/ogg", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    }.get(Path(record["name"]).suffix.lower(), "application/octet-stream")


def handle_web_request(handler, *, head=False):
    """Return False for unrelated paths so existing health behavior is untouched."""
    path = urlsplit(handler.path).path
    if not (path.startswith("/watch/") or path.startswith("/stream/")):
        return False
    token = path.rsplit("/", 1)[-1]
    if path not in {f"/watch/{token}", f"/stream/{token}"}:
        _error(handler, 404, head)
        return True
    try:
        record = find_stream(token)
    except Exception:
        _error(handler, 503, head)
        return True
    if not record:
        _error(handler, 404, head)
        return True
    if path.startswith("/watch/"):
        from web_player import render_watch_page
        video_url = f"../stream/{token}"
        nonce = secrets.token_urlsafe(18)
        body = render_watch_page(record["name"], video_url, nonce, video_mime(record))
        _headers(handler, 200, len(body), "text/html; charset=utf-8", [
            ("Content-Security-Policy", f"default-src 'none'; media-src 'self'; "
             "script-src 'none'; "
             f"style-src 'nonce-{nonce}'; style-src-elem 'nonce-{nonce}'; style-src-attr 'none'; "
             "img-src 'self' data:; connect-src 'self'; "
             "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"),
        ])
        if not head:
            handler.wfile.write(body)
        return True
    return serve_record(handler, record, head=head)


def serve_record(handler, record, *, head=False):
    """Shared bounded byte-range response; existing public links stay unchanged."""
    size = record["size"]
    try:
        start, end, code = byte_range(handler.headers.get("Range"), size)
    except ValueError:
        _error(handler, 416, head, [("Content-Range", f"bytes */{size}"), ("Accept-Ranges", "bytes")])
        return True
    mime = video_mime(record)
    headers = [("Accept-Ranges", "bytes"),
               ("X-Accel-Buffering", "no"),
               ("Content-Disposition", f"inline; filename*=UTF-8''{quote(record['name'], safe='')}")]
    if code == 206:
        headers.append(("Content-Range", f"bytes {start}-{end}/{size}"))
    if head:
        _headers(handler, code, end - start + 1, mime, headers)
        return True
    bot = _BOT
    if bot is None or record["bot_id"] != int(bot.token.split(":", 1)[0]):
        _error(handler, 503)
        return True
    if not _VIEWERS.acquire(blocking=False):
        _error(handler, 503, extra=[("Retry-After", "5")])
        return True
    began = False
    try:
        with source(bot, record, start, end) as chunks:
            iterator = iter(chunks)
            first = next(iterator)
            handler.connection.settimeout(30)
            _headers(handler, code, end - start + 1, mime, headers)
            began = True
            remaining = end - start + 1
            def write(part):
                nonlocal remaining
                if not part or len(part) > remaining:
                    raise ValueError("Invalid stream length.")
                handler.wfile.write(part)
                handler.wfile.flush()
                remaining -= len(part)
            write(first)
            for part in iterator:
                write(part)
            if remaining:
                raise ValueError("Incomplete stream.")
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        handler.close_connection = True
    except Exception as exc:
        logging.warning("Web stream request failed (%s).", type(exc).__name__)
        if not began:
            if isinstance(exc, TelegramTransferCooldown):
                _error(handler, 503, extra=[("Retry-After", str(exc.seconds))])
            else:
                _error(handler, 502)
        handler.close_connection = True
    finally:
        _VIEWERS.release()
    return True
