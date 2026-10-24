"""Private support requests and owner-only replies from one configured group."""

import html
import logging
import threading
import time
from pathlib import Path

from telebot.handler_backends import BaseMiddleware, CancelUpdate, SkipHandler
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import ADMIN_USER_ID, build_inline_keyboard, format_telegram_text, is_admin
from group_management import delete_later
from support_storage import (
    SUPPORT_LOCK, SUPPORT_SETTINGS_FILE, SUPPORT_THREADS_FILE,
    load_support_settings, load_support_threads, track_legacy_header,
)
from system_features import write_json

SUPPORT_CONTENT_TYPES = [
    "text", "photo", "voice", "audio", "video", "video_note",
    "document", "animation", "sticker",
]
TRACK_HEADER_COMMAND = "/track_support_header"


def is_track_header_command(message):
    text = (getattr(message, "text", None) or "").strip()
    return bool(text) and text.split()[0].split("@", 1)[0] == TRACK_HEADER_COMMAND


def build_user_keyboard(buttons):
    keyboard = build_inline_keyboard(buttons) or InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("💬 פנייה למנהל / תמיכה", callback_data="support:open"))
    return keyboard


def request_status_keyboard(status):
    keyboard = InlineKeyboardMarkup(row_width=1)
    label, target = (
        ("מצב: פתוחה — סמן כטופלה", "handled") if status == "open"
        else ("מצב: טופלה — פתח מחדש", "open")
    )
    keyboard.add(InlineKeyboardButton(label, callback_data=f"support:status:{target}"))
    return keyboard


class SupportSystem:
    def __init__(self, bot, settings_file=SUPPORT_SETTINGS_FILE,
                 threads_file=SUPPORT_THREADS_FILE, on_admin_open=None):
        self.bot = bot
        self.settings_file = settings_file
        self.threads_file = threads_file
        self.on_admin_open = on_admin_open
        self.prompts = {}
        self.stop_event = threading.Event()
        self.reminder_thread = None

    def check_reminders(self, now=None):
        """Once per open lifecycle, owner-private, without request content.

        Reserve durably before the API call to avoid duplicates after a crash.
        A known send failure releases the reservation; an ambiguous crash may
        miss a reminder rather than send it twice.
        """
        now = int(time.time()) if now is None else now
        with SUPPORT_LOCK:
            settings = load_support_settings(self.settings_file)
            group = settings["support_chat_id"]
            if not settings["reminders_enabled"] or group is None:
                return
            state = load_support_threads(self.threads_file)
            for key, request in state["requests"].items():
                created = request.get("created_at")
                if (
                    key.split(":")[0] != str(group) or request["status"] != "open"
                    or created is None or request.get("reminder_sent", False)
                    or now - created < settings["reminder_minutes"] * 60
                ):
                    continue
                request["reminder_sent"] = True
                write_json(self.threads_file, state)
                try:
                    self.bot.send_message(
                        ADMIN_USER_ID,
                        f"תזכורת: פנייה #{key.split(':')[1]} בקבוצת התמיכה {group} "
                        f"עדיין פתוחה לאחר {settings['reminder_minutes']} דקות לפחות.\n"
                        "למעקב, פתח בתפריט הניהול את הודעות מערכת וברירת מחדל ← פניות תמיכה פתוחות.",
                    )
                except Exception as exc:
                    request["reminder_sent"] = False
                    write_json(self.threads_file, state)
                    logging.warning("Support reminder delivery failed (%s).", type(exc).__name__)

    def _reminder_loop(self):
        while not self.stop_event.is_set():
            try:
                self.check_reminders()
            except Exception as exc:
                logging.warning("Support reminder check failed (%s).", type(exc).__name__)
            self.stop_event.wait(60)

    def start(self):
        if self.reminder_thread and self.reminder_thread.is_alive():
            return
        self.stop_event.clear()
        self.reminder_thread = threading.Thread(target=self._reminder_loop, daemon=True)
        self.reminder_thread.start()

    def stop(self):
        self.stop_event.set()
        if self.reminder_thread:
            self.reminder_thread.join(timeout=5)

    def _temporary(self, chat_id, text, seconds=5):
        text, mode = format_telegram_text(text)
        sent = self.bot.send_message(chat_id, text, parse_mode=mode)
        delete_later(self.bot, chat_id, sent.message_id, seconds)

    def _delete(self, chat_id, message_id):
        try:
            self.bot.delete_message(chat_id, message_id)
        except Exception as exc:
            logging.warning("Support message cleanup failed (%s).", type(exc).__name__)

    def cancel_user(self, user_id, chat_id):
        with SUPPORT_LOCK:
            state = load_support_threads(self.threads_file)
            if user_id in state["pending_users"]:
                state["pending_users"].remove(user_id)
                write_json(self.threads_file, state)
        prompt = self.prompts.pop(user_id, None)
        if prompt:
            self._delete(*prompt)

    def handle_callback(self, call):
        self.bot.answer_callback_query(call.id)
        if call.message is None or call.message.chat.type != "private":
            return
        uid, chat_id = call.from_user.id, call.message.chat.id
        if call.data == "support:cancel":
            self.cancel_user(uid, chat_id)
            self._temporary(chat_id, "הפנייה בוטלה.")
            return
        settings = load_support_settings(self.settings_file)
        if settings["support_chat_id"] is None:
            self._temporary(chat_id, "התמיכה טרם הוגדרה. נסה שוב מאוחר יותר.")
            return
        if is_admin(uid) and self.on_admin_open:
            self.on_admin_open(uid)
        self.cancel_user(uid, chat_id)
        with SUPPORT_LOCK:
            state = load_support_threads(self.threads_file)
            if uid not in state["pending_users"]:
                state["pending_users"].append(uid)
            write_json(self.threads_file, state)
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("ביטול פנייה", callback_data="support:cancel"))
        from system_messages import message_text
        body, mode = format_telegram_text(message_text(
            "support_prompt", Path(self.settings_file).with_name("config.json"),
        ))
        prompt = self.bot.send_message(
            chat_id, body, parse_mode=mode, reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        self.prompts[uid] = (chat_id, prompt.message_id)

    def handle_status_callback(self, call):
        message = call.message
        if (
            call.from_user is None or call.from_user.id != ADMIN_USER_ID
            or message is None or message.chat.type not in {"group", "supergroup"}
        ):
            self.bot.answer_callback_query(call.id, text="אין הרשאה לשנות מצב פנייה.")
            return
        target = (call.data or "").removeprefix("support:status:")
        try:
            with SUPPORT_LOCK:
                settings = load_support_settings(self.settings_file)
                if message.chat.id != settings["support_chat_id"]:
                    self.bot.answer_callback_query(call.id, text="זו אינה קבוצת התמיכה המוגדרת.")
                    return
                state = load_support_threads(self.threads_file)
                key = f"{message.chat.id}:{message.message_id}"
                request = state["requests"].get(key)
                if request is None or target not in {"open", "handled"}:
                    self.bot.answer_callback_query(call.id, text="הפנייה אינה זמינה לשינוי מצב.")
                    return
                # Explicit target states make repeated clicks safe; keep all routes intact.
                if request["status"] != target:
                    request["status"] = target
                    if target == "open":
                        request["reminder_sent"] = False
                    write_json(self.threads_file, state)
                # Also retry the edit on a duplicate after a previous API failure.
                try:
                    self.bot.edit_message_reply_markup(
                        chat_id=message.chat.id, message_id=message.message_id,
                        reply_markup=request_status_keyboard(target),
                    )
                except ApiTelegramException as exc:
                    if exc.error_code != 400 or "message is not modified" not in exc.description.lower():
                        raise
        except Exception as exc:
            logging.warning("Support status update failed (%s).", type(exc).__name__)
            self.bot.answer_callback_query(
                call.id, text="עדכון הפנייה לא הושלם. נסה שוב; המצב השמור מופיע בתפריט הניהול.",
                show_alert=True,
            )
            return
        self.bot.answer_callback_query(
            call.id, text="הפנייה טופלה." if target == "handled" else "הפנייה נפתחה מחדש.",
        )

    def handle_track_header(self, message):
        """The owner explicitly identifies a historical header by replying to it."""
        user = getattr(message, "from_user", None)
        if user is None or user.id != ADMIN_USER_ID:
            return
        chat = message.chat
        if chat.type not in {"group", "supergroup"}:
            self.bot.send_message(
                chat.id, "יש לשלוח /track_support_header כמענה להודעת הכותרת הישנה בקבוצת התמיכה הנוכחית.",
            )
            return
        try:
            with SUPPORT_LOCK:
                if chat.id != load_support_settings(self.settings_file)["support_chat_id"]:
                    raise ValueError("זו אינה קבוצת התמיכה הנוכחית.")
                command = (message.text or "").strip().split()
                base, _, mention = command[0].partition("@")
                if mention and mention.casefold() != self.bot.user.username.casefold():
                    return
                if base != TRACK_HEADER_COMMAND or len(command) != 1:
                    raise ValueError("שלח /track_support_header ללא פרטים נוספים, כמענה לכותרת בלבד.")
                header = getattr(message, "reply_to_message", None)
                author = getattr(header, "from_user", None)
                # Validate the selected message itself, not a guessed recipient
                # or relationships from forwarding metadata or adjacent routes.
                if (
                    header is None or getattr(header, "content_type", None) != "text"
                    or author is None or author.id != self.bot.user.id
                    or getattr(header, "reply_to_message", None) is not None
                    or getattr(header, "forward_origin", None) is not None
                    or not (getattr(header, "text", None) or "").startswith("פנייה חדשה לתמיכה\n")
                ):
                    raise ValueError("יש להשיב לכותרת המקורית ששלח הבוט, לא למדיה או להודעה מועברת.")
                created, status = track_legacy_header(
                    chat.id, header.message_id, self.settings_file, self.threads_file,
                )
                try:
                    self.bot.edit_message_reply_markup(
                        chat_id=chat.id, message_id=header.message_id,
                        reply_markup=request_status_keyboard(status),
                    )
                except ApiTelegramException as exc:
                    if exc.error_code != 400 or "message is not modified" not in exc.description.lower():
                        raise
            text = "הפנייה הישנה צורפה למעקב כפתוחה." if created else "הפנייה כבר במעקב; מצבה לא שונה."
        except ValueError as exc:
            text = str(exc)
        except Exception as exc:
            logging.warning("Historical support tracking failed (%s).", type(exc).__name__)
            text = "הצירוף לא הושלם. בדוק ברשימת הפניות ונסה שוב; ניסיון חוזר אינו יוצר כפילות."
        self._temporary(chat.id, text)

    def has_pending(self, message):
        return (
            message.chat.type == "private" and message.from_user is not None
            and message.from_user.id in load_support_threads(self.threads_file)["pending_users"]
        )

    def handle_request(self, message):
        uid, chat_id = message.from_user.id, message.chat.id
        if (getattr(message, "text", None) or "").strip() == "ביטול":
            self.cancel_user(uid, chat_id)
            self._temporary(chat_id, "הפנייה בוטלה.")
            return
        if hasattr(message, "admin_delivery_result"):
            # The shared private router already sent the sole acknowledgement.
            # Exit this support-input state without a second copy or notice.
            self.cancel_user(uid, chat_id)
            return
        admin_relay = getattr(self, "admin_relay", None)
        if admin_relay:
            result = admin_relay(message, load_support_settings(self.settings_file)["support_chat_id"])
            if result is True and getattr(self, "live_chat_queue", None) is not None:
                # The persistent live-chat router owns the Admin Group request
                # and its queue. Avoid also posting a duplicate outside it.
                prompt = self.prompts.pop(uid, None)
                if prompt:
                    self._delete(*prompt)
                with SUPPORT_LOCK:
                    state = load_support_threads(self.threads_file)
                    if uid in state["pending_users"]:
                        state["pending_users"].remove(uid)
                        write_json(self.threads_file, state)
                    settings = load_support_settings(self.settings_file)
                from system_messages import message_text
                self._temporary(chat_id, message_text(
                    "request_received", Path(self.settings_file).with_name("config.json"),
                ), seconds=10)
                return
            if result is False:
                self.bot.send_message(chat_id, "⚠️ ההעברה לערוץ הניהול נכשלה. הפנייה תישלח לקבוצת התמיכה אם היא זמינה.")
        sent_ids = []
        group_id = None
        try:
            with SUPPORT_LOCK:
                settings = load_support_settings(self.settings_file)
                group_id = settings["support_chat_id"]
                if group_id is None:
                    raise ValueError("Support group is not configured.")
                user = message.from_user
                name = " ".join(filter(None, [
                    getattr(user, "first_name", ""), getattr(user, "last_name", ""),
                ])) or "ללא שם"
                username = getattr(user, "username", None)
                metadata = (
                    "<b>פנייה חדשה לתמיכה</b>\n"
                    f"<b>שם:</b> {html.escape(name)}\n"
                    f"<b>Username:</b> {html.escape('@' + username) if username else 'לא מוגדר'}\n"
                    f"<b>ID:</b> <code>{uid}</code>"
                )
                text = getattr(message, "text", None)
                caption = getattr(message, "caption", None)
                short_text = text is not None and len(name) + len(text) < 3500
                if short_text or caption:
                    body = text if short_text else caption
                    metadata += f"\n\n<b>תוכן הפנייה:</b>\n{html.escape(body)}"
                header = self.bot.send_message(
                    group_id, metadata, parse_mode="HTML",
                    reply_markup=request_status_keyboard("open"),
                )
                sent_ids.append(header.message_id)
                if not short_text:
                    copied = self.bot.copy_message(
                        group_id, chat_id, message.message_id,
                        reply_to_message_id=header.message_id,
                    )
                    sent_ids.append(copied.message_id)
                state = load_support_threads(self.threads_file)
                for message_id in sent_ids:
                    state["messages"][f"{group_id}:{message_id}"] = uid
                state["requests"][f"{group_id}:{header.message_id}"] = {
                    "status": "open", "created_at": int(time.time()), "reminder_sent": False,
                }
                if uid in state["pending_users"]:
                    state["pending_users"].remove(uid)
                write_json(self.threads_file, state)
        except Exception as exc:
            logging.warning("Support request relay failed (%s).", type(exc).__name__)
            for message_id in sent_ids:
                self._delete(group_id, message_id)
            self._temporary(chat_id, "הפנייה לא נשלחה. נסה שוב; אפשר גם לשלוח „ביטול”.")
            return
        prompt = self.prompts.pop(uid, None)
        if prompt:
            self._delete(*prompt)
        # A successfully delivered request is not rolled back if its receipt
        # cannot be sent; its persisted route must remain replyable.
        from system_messages import message_text
        self._temporary(chat_id, message_text(
            "request_received", Path(self.settings_file).with_name("config.json"),
        ), seconds=10)

    def register_handlers(self):
        self.bot.callback_query_handler(
            func=lambda call: (call.data or "").startswith("support:status:"),
        )(self.handle_status_callback)
        self.bot.callback_query_handler(
            func=lambda call: call.data in {"support:open", "support:cancel"},
        )(self.handle_callback)
        self.bot.message_handler(
            func=self.has_pending, content_types=SUPPORT_CONTENT_TYPES,
        )(self.handle_request)


class SupportOwnerRelayMiddleware(BaseMiddleware):
    def __init__(self, support):
        super().__init__()
        self.support = support
        self.update_sensitive = True
        self.update_types = ["message"]

    def pre_process_message(self, message, data):
        # Consume this administrative command before reply routing, including
        # denied attempts. Never send it to the private request recipient.
        if is_track_header_command(message):
            self.support.handle_track_header(message)
            return CancelUpdate()
        user = getattr(message, "from_user", None)
        reply = getattr(message, "reply_to_message", None)
        if user is None or not is_admin(user.id) or reply is None:
            return None
        if message.chat.type not in {"group", "supergroup"}:
            return None
        s = self.support
        inbox = getattr(s, "admin_inbox", None)
        if inbox is not None and inbox.handle_admin_reply(message):
            return CancelUpdate()
        with SUPPORT_LOCK:
            if message.chat.id != load_support_settings(s.settings_file)["support_chat_id"]:
                return SkipHandler()
            state = load_support_threads(s.threads_file)
            target = state["messages"].get(f"{message.chat.id}:{reply.message_id}")
            if target is None:
                return SkipHandler()
            key = f"{message.chat.id}:{message.message_id}"
            if key in state.setdefault("delivered_replies", {}):
                return CancelUpdate()
            try:
                s.bot.copy_message(target, message.chat.id, message.message_id)
                state["delivered_replies"][key] = target
                write_json(s.threads_file, state)
            except Exception as exc:
                logging.warning("Support reply relay failed (%s).", type(exc).__name__)
                s._temporary(message.chat.id, "התשובה לא נשלחה. המשתמש אולי חסם את הבוט; נסה שוב.")
            else:
                s._temporary(message.chat.id, "התשובה נשלחה למשתמש.")
                s._delete(message.chat.id, message.message_id)
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        return None
