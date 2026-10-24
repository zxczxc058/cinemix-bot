"""Owner-only reply moderation, persistent bans, and protected bot entry points."""

import html
import json
import logging
import threading
import time
from pathlib import Path

from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import (
    GROUPS_FILE, USERS_FILE, is_admin, load_group_ids,
    load_moderation_records, update_user_moderation,
)
from group_settings import GROUP_SETTINGS_FILE, load_group_settings
from group_management import check_group_command_access, delete_later
from management_config import CONFIG_FILE, configure_destination, load_config, save_json
from support_storage import SUPPORT_SETTINGS_FILE, SUPPORT_THREADS_FILE, load_support_settings, load_support_threads

ROUTES_FILE = Path(__file__).resolve().with_name("moderation_routes.json")
_ROUTE_LOCK = threading.Lock()


def load_moderation_routes(path=ROUTES_FILE):
    from persistent_json import read_json_text
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
        raise ValueError("Invalid moderation routing.")
    for key, uid in data["messages"].items():
        group, mid = key.split(":")
        if int(group) >= 0 or int(mid) <= 0 or type(uid) is not int or uid <= 0:
            raise ValueError("Invalid moderation recipient.")
    return data


class OwnerManagementCommands(BaseMiddleware):
    COMMANDS = {"/warn", "/ban", "/unban", "/set_main_group", "/set_channel"}

    def __init__(self, bot, users_file=USERS_FILE, groups_file=GROUPS_FILE,
                 config_file=CONFIG_FILE, support_file=SUPPORT_SETTINGS_FILE,
                 threads_file=SUPPORT_THREADS_FILE, routes_file=ROUTES_FILE,
                 group_settings_file=GROUP_SETTINGS_FILE):
        super().__init__()
        self.bot = bot
        self.users_file, self.groups_file = users_file, groups_file
        self.config_file, self.support_file = config_file, support_file
        self.threads_file, self.routes_file = threads_file, routes_file
        self.group_settings_file = group_settings_file
        self.update_sensitive = True
        self.update_types = ["message"]

    def _delete(self, chat_id, mid):
        if chat_id > 0 and is_admin(chat_id):
            return True
        try:
            self.bot.delete_message(chat_id, mid)
            return True
        except Exception as exc:
            logging.warning("Moderation cleanup failed (%s).", type(exc).__name__)
            return False

    def _notice(self, chat_id, text, uid=None, enforcement=False):
        if enforcement and chat_id > 0:
            from private_admin_protection import send_enforcement_confirmation
            sent = send_enforcement_confirmation(self.bot, chat_id, text, parse_mode="HTML")
        else:
            sent = self.bot.send_message(chat_id, text, parse_mode="HTML")
        engine = getattr(self.bot, "restriction_service", None)
        from restriction_engine import RestrictionEngine
        if not isinstance(engine, RestrictionEngine):
            engine = None
        if uid is not None and engine is not None:
            engine.track(chat_id, sent.message_id, uid)
        if uid is not None and chat_id < 0:
            with _ROUTE_LOCK:
                routes = load_moderation_routes(self.routes_file)
                routes["messages"][f"{chat_id}:{sent.message_id}"] = uid
                save_json(self.routes_file, routes)
        if engine is None or chat_id not in engine.destinations():
            delete_later(self.bot, chat_id, sent.message_id, 5)

    def _target(self, message, args, action):
        reply = getattr(message, "reply_to_message", None)
        if reply is not None and message.chat.type in {"group", "supergroup"}:
            key = f"{message.chat.id}:{reply.message_id}"
            uid = load_moderation_routes(self.routes_file)["messages"].get(key)
            engine = getattr(self.bot, "restriction_service", None)
            from restriction_engine import RestrictionEngine
            if isinstance(engine, RestrictionEngine):
                uid = uid or engine.events["messages"].get(key, {}).get("uid")
            if message.chat.id == load_support_settings(self.support_file)["support_chat_id"]:
                uid = uid or load_support_threads(self.threads_file)["messages"].get(key)
            if uid:
                try:
                    name = self.bot.get_chat(uid).first_name or "משתמש"
                except Exception:
                    name = "משתמש"
                return uid, name, args, reply.message_id
            user = getattr(reply, "from_user", None)
            if user is None or getattr(user, "is_bot", False) or getattr(reply, "sender_chat", None):
                raise ValueError("יש להשיב להודעת משתמש מזוהה או לפנייה שהבוט העביר.")
            name = " ".join(filter(None, [user.first_name, getattr(user, "last_name", "")]))
            return user.id, name, args, reply.message_id
        if action in {"/ban", "/unban"}:
            first, _, reason = args.partition(" ")
            if first.isdigit() and int(first) > 0:
                return int(first), "משתמש", reason, None
        raise ValueError("יש לבצע Reply להודעת המשתמש. עבור חסימה/שחרור אפשר גם לציין ID מפורש.")

    def _groups(self, origin):
        groups = load_group_ids(self.groups_file)
        if origin is not None and origin < 0:
            groups.add(origin)
        main = load_config(self.config_file)["main_group_id"]
        if main:
            groups.add(main)
        support = load_support_settings(self.support_file)["support_chat_id"]
        if support:
            groups.add(support)
        from allowed_chats import chat_is_active
        return {gid for gid in groups if chat_is_active(self.bot, gid)}

    def _apply_group_action(self, uid, banned, origin):
        succeeded = failed = 0
        for gid in sorted(self._groups(origin)):
            try:
                if banned:
                    result = self.bot.ban_chat_member(gid, uid, revoke_messages=True)
                else:
                    result = self.bot.unban_chat_member(gid, uid, only_if_banned=True)
                if result is False:
                    raise RuntimeError("Telegram rejected the moderation operation.")
            except Exception as exc:
                logging.warning("Group moderation operation failed (%s).", type(exc).__name__)
                failed += 1
            else:
                succeeded += 1
        return succeeded, failed

    def pre_process_message(self, message, data):
        text = (getattr(message, "text", None) or "").strip()
        pieces = text.split(maxsplit=1)
        command = pieces[0] if pieces else ""
        args = pieces[1] if len(pieces) > 1 else ""
        base, _, mention = command.partition("@")
        user = getattr(message, "from_user", None)
        group_command_access = check_group_command_access(
            self.bot, message, self.group_settings_file,
        )
        if group_command_access is False:
            return CancelUpdate()
        if base not in self.COMMANDS:
            if user is None or not is_admin(user.id):
                return None
            # Tracking a header is administration, not a reply to the user.
            # Let the support middleware authorize it even if that user is banned.
            from support_relay import is_track_header_command
            if is_track_header_command(message):
                return None
            reply = getattr(message, "reply_to_message", None)
            if (
                reply is not None and message.chat.type in {"group", "supergroup"}
                and message.chat.id == load_support_settings(self.support_file)["support_chat_id"]
            ):
                key = f"{message.chat.id}:{reply.message_id}"
                target = load_support_threads(self.threads_file)["messages"].get(key)
                if target and load_moderation_records(self.users_file).get(str(target), {}).get("banned"):
                    self._notice(message.chat.id, "המשתמש חסום. שלח /unban עם המזהה שלו לפני מענה לפנייה.")
                    self._delete(message.chat.id, message.message_id)
                    return CancelUpdate()
            return None
        group_moderation_admin = (
            group_command_access is True
            and base in {"/warn", "/ban", "/unban"}
        )
        if (
            user is None
            and not group_moderation_admin
        ) or (
            user is not None
            and not is_admin(user.id)
            and not group_moderation_admin
        ):
            return None
        clean_command = not bool(mention)
        command_cleaned = False
        try:
            if mention and mention.casefold() != self.bot.get_me().username.casefold():
                return CancelUpdate()
            clean_command = True
            command_cleaned = self._delete(message.chat.id, message.message_id)
            if base == "/set_main_group":
                if message.chat.type not in {"group", "supergroup"}:
                    raise ValueError("שלח /set_main_group בתוך קבוצת היעד.")
                configure_destination(self.bot, message.chat.id, "main_group", self.config_file)
                self._notice(message.chat.id, "הקבוצה הראשית והקישור הדינמי הוגדרו.")
            elif base == "/set_channel":
                if not args:
                    raise ValueError("שלח /set_channel @username או /set_channel -100… מתוך חשבונך המזוהה.")
                target = int(args) if args.lstrip("-").isdigit() else args
                configure_destination(self.bot, target, "channel", self.config_file)
                self._notice(message.chat.id, "הערוץ והקישור הדינמי הוגדרו.")
            else:
                uid, name, reason, original = self._target(message, args, base)
                if base == "/warn" and message.chat.type not in {"group", "supergroup"}:
                    raise ValueError("אזהרה ב-Reply זמינה בקבוצה בלבד.")
                action = base[1:]
                max_warnings = (
                    load_group_settings(self.group_settings_file)["max_warnings"]
                    if action == "warn" else 3
                )
                record = update_user_moderation(
                    uid,
                    action,
                    reason.strip(),
                    message.chat.id,
                    self.users_file,
                    max_warnings=max_warnings,
                    user_name=name,
                )
                cleaned = True
                if original is not None and action in {"warn", "ban"}:
                    cleaned = self._delete(message.chat.id, original)
                mention_name = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
                if action == "warn":
                    body = (
                        f"⚠️ אזהרה למשתמש {mention_name}!\n"
                        f"סיבה: {html.escape(reason)}\n"
                        f"סה״כ אזהרות: {record['warning_count']}/{max_warnings}"
                    )
                elif action == "ban":
                    body = f"⛔ המשתמש {mention_name} נחסם מהבוט.\nסיבה: {html.escape(reason)}"
                else:
                    body = f"✅ חסימת {mention_name} בוטלה בבוט ומונה האזהרות אופס."
                if record["banned"] or action == "unban":
                    succeeded, failed = self._apply_group_action(uid, record["banned"], message.chat.id)
                    if action == "warn":
                        body += f"\n⛔ הושגו {max_warnings} אזהרות: המשתמש נחסם מהבוט."
                    body += f"\nפעולה בקבוצות: {succeeded} הצליחו, {failed} נכשלו."
                    if failed:
                        body += "\nנדרשת הרשאת הגבלת משתמשים; חסימת הבוט נשמרה גם אם פעולה בקבוצה נכשלה."
                if not cleaned:
                    body += "\nלא ניתן למחוק את המקור — ודא שלבוט הרשאת מחיקת הודעות."
                self._notice(message.chat.id, body, uid, enforcement=action in {"ban", "unban"})
        except (ValueError, OSError) as exc:
            self._notice(message.chat.id, html.escape(str(exc)))
        except Exception as exc:
            logging.warning("Owner management command failed (%s).", type(exc).__name__)
            self._notice(message.chat.id, "הפעולה לא הושלמה. בדוק הרשאות מנהל ונסה שוב.")
        finally:
            # Class-middleware cancellation skips post hooks.
            if clean_command and not command_cleaned:
                self._delete(message.chat.id, message.message_id)
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        return None


class BannedUserMiddleware(BaseMiddleware):
    def __init__(self, bot, users_file=USERS_FILE):
        super().__init__()
        self.bot, self.users_file = bot, users_file
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]
        self._notices = {}
        self._removals = {}

    def _banned(self, uid):
        return not is_admin(uid) and load_moderation_records(self.users_file).get(str(uid), {}).get("banned", False)

    def pre_process_message(self, message, data):
        group = message.chat.type in {"group", "supergroup"}
        if group:
            remaining = []
            for member in getattr(message, "new_chat_members", []) or []:
                if self._banned(member.id):
                    try:
                        self.bot.ban_chat_member(message.chat.id, member.id, revoke_messages=True)
                    except Exception as exc:
                        logging.warning("Banned member removal failed (%s).", type(exc).__name__)
                else:
                    remaining.append(member)
            if getattr(message, "new_chat_members", None):
                message.new_chat_members = remaining
        user = getattr(message, "from_user", None)
        if user is None or not self._banned(user.id):
            return None
        if group:
            key = (message.chat.id, user.id)
            now = time.monotonic()
            if now - self._removals.get(key, -60) >= 60:
                self._removals[key] = now
                try:
                    self.bot.ban_chat_member(message.chat.id, user.id, revoke_messages=True)
                except Exception as exc:
                    logging.warning("Banned participant removal failed (%s).", type(exc).__name__)
            try:
                self.bot.delete_message(message.chat.id, message.message_id)
            except Exception as exc:
                logging.warning("Banned-user message cleanup failed (%s).", type(exc).__name__)
        elif message.chat.type == "private":
            now = time.monotonic()
            if now - self._notices.get(user.id, -60) >= 60:
                self._notices[user.id] = now
                sent = self.bot.send_message(message.chat.id, "⛔ הגישה שלך לבוט נחסמה. פנה למנהל.")
                delete_later(self.bot, message.chat.id, sent.message_id, 5)
        return CancelUpdate()

    def pre_process_callback_query(self, call, data):
        if self._banned(call.from_user.id):
            self.bot.answer_callback_query(call.id, text="הגישה שלך לבוט נחסמה.", show_alert=True)
            return CancelUpdate()

    def post_process_message(self, message, data, exception):
        return None

    def post_process_callback_query(self, call, data, exception):
        return None
