"""Bounded anonymous Drive-link checks with deduplicated, editable alerts."""

import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot_storage import _COMMANDS_LOCK, load_bot_data, save_bot_data, is_admin
from management_config import load_config, save_json
from support_storage import load_support_settings


def drive_url(url):
    parsed = urllib.parse.urlparse(html.unescape(url))
    return parsed.scheme == "https" and parsed.hostname in {"drive.google.com", "docs.google.com"} and not parsed.username


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host in {"drive.google.com", "docs.google.com", "accounts.google.com"}
            or host.endswith(".googleusercontent.com")
        ):
            raise ValueError("Untrusted Drive redirect.")
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def check_drive(url):
    if not drive_url(url):
        return "unsupported", None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())
    request = urllib.request.Request(html.unescape(url), headers={"User-Agent": "TelegramLinkHealth/1.0", "Range": "bytes=0-262143"})
    try:
        with opener.open(request, timeout=10) as response:
            if urllib.parse.urlparse(response.url).hostname == "accounts.google.com":
                return "restricted", response.status
            body = response.read(262144).decode("utf-8", errors="replace")
            body = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", "", body, flags=re.I)
            visible = re.sub(r"<[^>]+>", " ", body)
            visible = " ".join(html.unescape(visible).lower().split())
            if "the file you have requested does not exist" in visible or "the requested url was not found on this server" in visible:
                return "broken", response.status
            if "you need access" in visible or "request access" in visible:
                return "restricted", response.status
            return "ok", response.status
    except urllib.error.HTTPError as exc:
        return ("broken" if exc.code in {404, 410} else "restricted" if exc.code in {401, 403} else "unknown"), exc.code
    except (OSError, ValueError):
        return "unknown", None


class DriveChecker:
    def __init__(self, bot, registry, commands_file, config_file, support_file, state_file=None, fetcher=check_drive):
        self.bot, self.registry = bot, registry
        self.commands_file, self.config_file, self.support_file = commands_file, config_file, support_file
        self.state_file = Path(state_file or Path(config_file).parent / "link_health.json")
        self.fetcher, self.lock = fetcher, threading.RLock()
        self.stop_event = threading.Event()
        if not self.state_file.exists():
            save_json(self.state_file, {"links": {}})
        from persistent_json import read_json_text
        self.state = json.loads(read_json_text(self.state_file))
        if not isinstance(self.state, dict) or not isinstance(self.state.get("links"), dict):
            raise ValueError("Invalid link health state.")

    def links(self):
        found = {}
        for keyword, response in load_bot_data(self.commands_file)["responses"].items():
            urls = re.findall(r'https?://[^\s<>()"\']+', response["text"])
            urls += [button["url"] for button in response["buttons"]]
            for raw in urls:
                if drive_url(raw):
                    url = html.unescape(raw)
                    item = found.setdefault(url, {"keywords": set(), "raw": set()})
                    item["keywords"].add(keyword)
                    item["raw"].add(raw)
        return found

    def tick(self, now=None):
        now = time.time() if now is None else now
        cfg = load_config(self.config_file)
        if not cfg["link_check_enabled"]:
            return
        for url, info in self.links().items():
            if self.stop_event.is_set():
                return
            key = hashlib.sha256(url.encode()).hexdigest()[:20]
            with self.lock:
                previous = self.state["links"].get(key, {})
                if previous.get("next_check", 0) > now:
                    continue
            status, code = self.fetcher(url)
            record = dict(previous, url=url, raw=sorted(info["raw"]), keywords=sorted(info["keywords"]), status=status, http_status=code,
                          next_check=now + (600 if status == "unknown" else cfg["link_check_interval_seconds"]))
            if status == "broken" and (previous.get("status") != "broken" or not previous.get("message_id")):
                target = load_support_settings(self.support_file)["support_chat_id"]
                if target is not None:
                    try:
                        nonce = secrets.token_hex(8)
                        keyboard = InlineKeyboardMarkup()
                        keyboard.add(InlineKeyboardButton("✏️ עדכן קישור", callback_data=f"health:update:{key}:{nonce}"))
                        body = f"⚠️ <b>קישור Drive אינו זמין</b>\nפקודות: {html.escape(', '.join(record['keywords']))}\nHTTP: {code}\n{html.escape(url)}"
                        from admin_notifications import send_admin_message
                        sent = send_admin_message(
                            self.bot, target, body,
                            config_file=self.config_file,
                            parse_mode="HTML", reply_markup=keyboard,
                        )
                        actual_target = getattr(getattr(sent, "chat", None), "id", target)
                        record.update(message_id=sent.message_id, chat_id=actual_target, nonce=nonce)
                    except Exception as exc:
                        record["next_check"] = now + 600
                        logging.warning("Drive alert delivery failed (%s).", type(exc).__name__)
            with self.lock:
                self.state["links"][key] = record
                save_json(self.state_file, self.state)

    def callback(self, call):
        try:
            if not is_admin(call.from_user.id) or call.message is None:
                raise ValueError("אין הרשאה.")
            _, _, key, nonce = call.data.split(":")
            record = self.state["links"].get(key)
            target = record.get("chat_id") if record else None
            if not record or record.get("nonce") != nonce or call.message.chat.id != target or call.message.message_id != record.get("message_id"):
                raise ValueError("ההתראה התיישנה.")
            d = self.registry.get(call.from_user.id)
            d.cancel_pending(d.actor_id)
            if d.on_admin_navigation:
                d.on_admin_navigation(d.actor_id, d.actor_id)
            d.pending[d.actor_id] = {"action": "health_link", "key": key, "nonce": nonce}
            d.pending_chat_id = d.actor_id
            d.bot.prompt(d.actor_id, "שלח קישור Drive חדש בפרטי. הוא יחליף את הקישור בכל הפקודות המפורטות בהתראה.")
            self.bot.answer_callback_query(call.id, text="שלח את הקישור החדש בפרטי.")
        except (ValueError, OSError) as exc:
            self.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)

    def handle_input(self, d, action, state, text):
        if action != "health_link":
            return False
        try:
            if not drive_url(text.strip()):
                raise ValueError("שלח קישור HTTPS של Google Drive או Google Docs.")
            with self.lock, _COMMANDS_LOCK:
                record = self.state["links"].get(state["key"])
                if not record or record.get("nonce") != state["nonce"]:
                    raise ValueError("ההתראה כבר עודכנה.")
                if html.unescape(text.strip()) == record["url"]:
                    raise ValueError("שלח קישור שונה מהקישור התקול.")
                data = load_bot_data(self.commands_file)
                changed = False
                for keyword in record["keywords"]:
                    response = data["responses"].get(keyword)
                    if not response:
                        continue
                    for raw in record["raw"]:
                        if raw in response["text"]:
                            response["text"] = response["text"].replace(raw, html.escape(text.strip(), quote=True) if "&amp;" in raw else text.strip())
                            changed = True
                        for button in response["buttons"]:
                            if button["url"] == raw:
                                button["url"] = text.strip()
                                changed = True
                if not changed:
                    raise ValueError("הקישור כבר הוחלף בפקודות; לא בוצעה דריסה.")
                save_bot_data(data, self.commands_file)
                record.update(status="updated", nonce=None, next_check=0)
                save_json(self.state_file, self.state)
            self.bot.edit_message_reply_markup(chat_id=record["chat_id"], message_id=record["message_id"], reply_markup=None)
            d.cancel_pending(d.actor_id)
            from command_archive import archive_command, cleanup_command_sources, notify_command
            notices = []
            for keyword in record["keywords"]:
                if keyword in data["responses"]:
                    notices.append(archive_command(
                        self.bot, keyword, self.commands_file, d.management.config_file, d.actor_id, state, cleanup=False))
            if notices and all(notice.startswith("✅") for notice in notices):
                if cleanup_command_sources(self.bot, d.actor_id, state):
                    notices.append("⚠️ חלק מהודעות הקלט לא נמחקו.")
            notice = "\n".join(notices)
            if notices and all(item.startswith("✅") for item in notices):
                notify_command(d.bot, self.bot, d.actor_id, notice)
            else:
                d.bot.send_message(d.actor_id, notice)
            d.send_menu(d.actor_id)
        except (ValueError, OSError) as exc:
            d.bot.send_message(d.actor_id, str(exc))
        return True

    def start(self):
        def run():
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logging.warning("Drive checking failed (%s).", type(exc).__name__)
                self.stop_event.wait(60)
        self.thread = threading.Thread(target=run, daemon=True, name="drive-health")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, "thread"):
            self.thread.join(3)
