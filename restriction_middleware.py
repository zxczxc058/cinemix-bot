"""Moderation commands, denial messages, spam alerts and support-button attachment."""

import html
import logging
import time
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from bot_storage import is_admin
from group_management import (
    check_group_command_access,
    contains_link,
    delete_later,
)
from management_config import _LOCK, load_config, save_json, set_admin_chat
from support_storage import load_support_threads, load_support_settings, set_support_chat
from restriction_controls import duration


class RestrictionMiddleware(BaseMiddleware):
    COMMANDS = frozenset({
        "/mute", "/unmute", "/unban", "/setsupport", "/set_support",
        "/setadmin", "/set_admin", "/setaccess",
    })

    def __init__(self, engine, registry, support):
        super().__init__()
        self.engine, self.bot, self.registry, self.support = engine, engine.bot, registry, support
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]
        self.activity = defaultdict(deque)
        self.alerted = {}

    def denied(self, uid):
        ban, mute = self.engine.state(uid)
        if ban:
            return "⛔ הורחקת מהשימוש בבוט לצמיתות.\n📌 סיבה: " + html.escape(ban["reason"])
        if mute:
            if mute["until"] is None:
                return f"🔇 הושתקת מהשימוש בבוט.\n📌 סיבה: {html.escape(mute['reason'])}\n⏳ זמן נותר לשחרור: ללא תפוגה"
            remaining = max(1, int(mute["until"] - time.time()))
            date = datetime.fromtimestamp(mute["until"], ZoneInfo("Asia/Jerusalem")).strftime("%d/%m/%Y %H:%M")
            return f"🔇 הושתקת זמנית מהשימוש בבוט.\n📌 סיבה: {html.escape(mute['reason'])}\n⏳ זמן נותר לשחרור: {remaining // 3600} שעות ו־{remaining % 3600 // 60} דקות ({date})"

    def _alert(self, message, reason):
        uid = message.from_user.id
        now = time.monotonic()
        if now - self.alerted.get(uid, -1000) < 120:
            return
        cfg = load_config(self.engine.path)
        target = cfg["admin_chat_id"] or load_support_settings(self.engine.core.support_file)["support_chat_id"]
        if target is None:
            return
        try:
            body = f"⚠️ {reason}\nמשתמש: {html.escape(message.from_user.first_name or '')}\nID: <code>{uid}</code>"
            from admin_notifications import send_admin_message
            sent = send_admin_message(
                self.bot, target, body, config_file=self.engine.path,
                parse_mode="HTML",
            )
            actual_target = getattr(sent.chat, "id", target)
            self.engine.track(actual_target, sent.message_id, uid)
            self.alerted[uid] = now
        except Exception as exc:
            logging.warning("Moderation alert failed (%s).", type(exc).__name__)

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        settings_file = getattr(self.engine.core, "group_settings_file", None)
        if settings_file is None:
            group_command_access = check_group_command_access(self.bot, message)
        else:
            group_command_access = check_group_command_access(
                self.bot, message, settings_file,
            )
        if group_command_access is False:
            return CancelUpdate()
        pieces = (getattr(message, "text", None) or "").strip().split(maxsplit=1)
        base = pieces[0].partition("@")[0] if pieces else ""
        args = pieces[1] if len(pieces) > 1 else ""
        if (
            group_command_access is True
            and base in {"/mute", "/unmute"}
            and message.chat.type in {"group", "supergroup"}
        ):
            self._command(message, base, args)
            return CancelUpdate()
        if user is None or getattr(user, "is_bot", False):
            return
        if not is_admin(user.id):
            body = self.denied(user.id)
            if body:
                if message.chat.type == "private":
                    sent = self.bot.send_message(message.chat.id, body, parse_mode="HTML")
                    delete_later(self.bot, message.chat.id, sent.message_id, 5)
                elif getattr(message, "text", None) or getattr(message, "caption", None):
                    self.engine.core._delete(message.chat.id, message.message_id)
                return CancelUpdate()
            now = time.monotonic()
            queue = self.activity[user.id]
            while queue and queue[0] < now - 10:
                queue.popleft()
            queue.append(now)
            if len(queue) >= 8:
                self._alert(message, "חשד להצפה — לפחות 8 הודעות בתוך 10 שניות.")
            if message.chat.type in {"group", "supergroup"} and contains_link(message):
                try:
                    member = self.bot.get_chat_member(message.chat.id, user.id)
                    if member.status not in {"administrator", "creator"}:
                        self._alert(message, "קישור לא מורשה בקבוצה.")
                except Exception:
                    pass
        else:
            if base in self.COMMANDS:
                mention = pieces[0].partition("@")[2] if pieces else ""
                if mention and mention.casefold() != self.bot.get_me().username.casefold():
                    return CancelUpdate()
                self._command(message, base, args)
                return CancelUpdate()
        if message.chat.type == "private" and self.support.has_pending(message):
            data["support_route_before"] = set(load_support_threads(self.support.threads_file)["messages"])

    def _command(self, message, base, args):
        user = getattr(message, "from_user", None)
        actor = (
            user.id if user is not None
            else getattr(getattr(message, "sender_chat", None), "id", None)
        )
        d = self.registry.get(actor)
        try:
            if base in {"/setsupport", "/set_support"}:
                if message.chat.type not in {"group", "supergroup"}:
                    raise ValueError("שלח את הפקודה בקבוצת התמיכה.")
                set_support_chat(message.chat.id, self.engine.core.support_file)
                self.engine.core._notice(message.chat.id, "קבוצת התמיכה הוגדרה.")
            elif base in {"/setadmin", "/set_admin"}:
                target = self.bot.get_chat(args or message.chat.id)
                if target.type not in {"group", "supergroup", "channel"}:
                    raise ValueError("בחר קבוצה או ערוץ. בפרטי: /setadmin @channel")
                set_admin_chat(target.id, self.engine.path)
                self.engine.core._notice(message.chat.id, "יעד התראות הניהול הוגדר. פניות משתמשים נשארות בקבוצת התמיכה.")
            elif base == "/setaccess":
                if message.chat.type != "private":
                    raise ValueError("שינוי קוד גישה אפשרי רק בפרטי.")
                d.cancel_pending(actor)
                self.support.cancel_user(actor, actor)
                d.pending[actor] = {"action": "access_code"}
                d.pending_chat_id = actor
                d.bot.prompt(actor, "שלח קוד גישה חדש, או ״ביטול״.")
            else:
                seconds = None
                if base == "/mute":
                    value, _, args = args.partition(" ")
                    seconds = duration(value)
                uid, name, reason, source_id = self.engine.core._target(message, args, "ban" if base == "/mute" else "unban")
                self.engine._protect(uid)
                if base == "/mute":
                    record, counts = self.engine.mute(uid, seconds, reason or "ללא סיבה", message.chat.id)
                    body = f"🔇 {html.escape(name)} הושתק.\nסיבה: {html.escape(record['reason'])}\nזמן נותר: {seconds // 60} דקות."
                elif base == "/unmute":
                    failed = self.engine.unmute(uid)
                    body = "🔊 ההשתקה בוטלה." + (f" נדרשים עוד {failed} שחזורי הרשאות בקבוצות." if failed else "")
                else:
                    self.engine.unban(uid, message.chat.id)
                    body = "✅ החסימה בוטלה."
                if getattr(message, "reply_to_message", None) and message.chat.type in {"group", "supergroup"}:
                    self.engine.core._delete(message.chat.id, message.reply_to_message.message_id)
                if base in {"/mute", "/unban", "/unmute"} and message.chat.type == "private":
                    from private_admin_protection import send_enforcement_confirmation
                    send_enforcement_confirmation(self.bot, message.chat.id, body)
                else:
                    self.engine.core._notice(message.chat.id, body, uid)
        except (ValueError, OSError) as exc:
            self.engine.core._notice(message.chat.id, html.escape(str(exc)))
        finally:
            if message.chat.type in {"group", "supergroup"}:
                self.engine.core._delete(message.chat.id, message.message_id)

    def pre_process_callback_query(self, call, data):
        if not is_admin(call.from_user.id):
            body = self.denied(call.from_user.id)
            if body:
                self.bot.answer_callback_query(call.id, text=body[:200], show_alert=True)
                return CancelUpdate()

    def post_process_message(self, message, data, exception):
        if exception is not None or "support_route_before" not in data:
            return
        routes = load_support_threads(self.support.threads_file)["messages"]
        for key in set(routes) - data["support_route_before"]:
            cid, mid = map(int, key.split(":"))
            self.engine.track(cid, mid, routes[key])

    def post_process_callback_query(self, call, data, exception):
        return None
