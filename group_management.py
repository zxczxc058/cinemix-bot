"""Group join greetings and administrator-aware link moderation."""

import html
import logging
import re
import threading

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import (
    ADMIN_USER_ID,
    USERS_FILE,
    format_telegram_text,
    is_admin,
    match_reply,
    revoke_user_warning,
    update_user_moderation,
)
from group_settings import (
    GROUP_SETTINGS_FILE,
    claim_command_rejection_notice,
    clear_last_welcome_message,
    load_group_settings,
    release_command_rejection_notice,
    set_last_welcome_message,
    set_group_flag,
    update_group_welcome,
)
from group_commands import (
    delete_group_command, list_group_commands, match_group_command, save_group_command,
)
from management_config import CONFIG_FILE

GROUP_COMMANDS = (
    "/set_welcome", "/welcome_on", "/welcome_off", "/anti_link_on",
    "/anti_link_off", "/group_stats", "/add_command", "/del_command",
    "/group_commands", "/admins", "/rules", "/id", "/link", "/report",
)
GROUP_COMMAND_SET = frozenset(GROUP_COMMANDS)
GROUP_ADMIN_ONLY_COMMANDS = frozenset({
    "/set_welcome", "/welcome_on", "/welcome_off", "/anti_link_on",
    "/anti_link_off", "/group_stats", "/add_command", "/del_command",
    "/group_commands", "/warn", "/ban", "/unban", "/mute", "/unmute",
})
OWNER_ONLY_GROUP_COMMANDS = frozenset({
    "/set_main_group", "/set_channel", "/setsupport", "/set_support",
    "/setadmin", "/set_admin", "/setaccess", "/set_support_chat",
    "/addcmd", "/removeadmin",
})
_GROUP_REJECTION_LOCK = threading.Lock()
_WELCOME_LOCK = threading.RLock()

_LINK = re.compile(
    r"(?:https?://|www\.|(?:t|telegram)\.me/|tg://|"
    r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}(?:/|\b))",
    re.IGNORECASE,
)
_USERNAME = re.compile(r"@[a-zA-Z0-9_]{4,}")


def _message_command(message):
    text = (getattr(message, "text", None) or "").strip()
    if not text:
        return None
    token = text.split(maxsplit=1)[0]
    command, separator, recipient = token.partition("@")
    if not command.startswith("/"):
        return None
    command = command.casefold()
    if separator:
        return command, recipient
    return command, None


def is_group_admin(bot, message) -> bool:
    """Return true only for Telegram group admins/creators or anonymous group admins."""
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", None) not in {"group", "supergroup"}:
        return False
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return getattr(sender_chat, "id", None) == getattr(chat, "id", None)
    user = getattr(message, "from_user", None)
    if user is None or getattr(user, "is_bot", False):
        return False
    try:
        member = bot.get_chat_member(chat.id, user.id)
    except Exception as exc:
        logging.warning("Could not verify group command permissions (%s).", type(exc).__name__)
        return False
    return getattr(member, "status", None) in {"administrator", "creator"}


def check_group_command_access(bot, message, settings_file=GROUP_SETTINGS_FILE):
    """Return None for unrelated messages, True for allowed commands, False after rejecting."""
    chat = getattr(message, "chat", None)
    if chat is None or getattr(chat, "type", None) not in {"group", "supergroup"}:
        return None
    parsed = _message_command(message)
    if parsed is None:
        return None
    command, recipient = parsed
    if command not in GROUP_ADMIN_ONLY_COMMANDS | OWNER_ONLY_GROUP_COMMANDS:
        return None
    user = getattr(message, "from_user", None)
    if user is not None and getattr(user, "is_bot", False):
        return None
    if recipient:
        try:
            username = (getattr(bot.get_me(), "username", None) or "").casefold()
        except Exception as exc:
            logging.warning("Could not verify addressed group command (%s).", type(exc).__name__)
            return None
        if not username or recipient.casefold() != username:
            return None

    group_admin = (
        user is not None and user.id == ADMIN_USER_ID
    ) or is_group_admin(bot, message)
    if command in OWNER_ONLY_GROUP_COMMANDS:
        authorized = group_admin and user is not None and is_admin(user.id)
    else:
        authorized = group_admin
    if authorized:
        return True

    try:
        bot.delete_message(chat.id, message.message_id)
    except Exception as exc:
        logging.warning("Unauthorized group command cleanup failed (%s).", type(exc).__name__)

    actor_id = getattr(user, "id", None)
    if actor_id is None:
        actor_id = getattr(getattr(message, "sender_chat", None), "id", None)
    if actor_id is None:
        return False
    notice_key = (int(chat.id), int(actor_id))
    try:
        first_attempt = claim_command_rejection_notice(
            notice_key[0], notice_key[1], settings_file,
        )
    except Exception as exc:
        logging.warning(
            "Could not persist group command rejection state (%s).",
            type(exc).__name__,
        )
        with _GROUP_REJECTION_LOCK:
            notices = getattr(bot, "_group_command_rejection_notices", None)
            if not isinstance(notices, set):
                notices = set()
                setattr(bot, "_group_command_rejection_notices", notices)
            first_attempt = notice_key not in notices
            if first_attempt:
                notices.add(notice_key)
    if first_attempt:
        try:
            rejection = load_group_settings(settings_file)["command_rejection_text"]
        except Exception as exc:
            logging.warning("Could not load group command rejection text (%s).", type(exc).__name__)
            rejection = "⛔ אין לך הרשאה לבצע פקודה זו!"
        body, parse_mode = format_telegram_text(rejection)
        try:
            bot.send_message(
                chat.id, body, parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
        except Exception as exc:
            try:
                release_command_rejection_notice(
                    notice_key[0], notice_key[1], settings_file,
                )
            except Exception as release_exc:
                logging.warning(
                    "Could not release failed rejection notice (%s).",
                    type(release_exc).__name__,
                )
                with _GROUP_REJECTION_LOCK:
                    notices = getattr(bot, "_group_command_rejection_notices", None)
                    if isinstance(notices, set):
                        notices.discard(notice_key)
            logging.warning("Unauthorized group command response failed (%s).", type(exc).__name__)
    return False


def contains_link(message, bot_username=None) -> bool:
    for entities in (
        getattr(message, "entities", None),
        getattr(message, "caption_entities", None),
    ):
        if entities and any(entity.type in {"url", "text_link"} for entity in entities):
            return True
    text = ((getattr(message, "text", None) or "") + "\n"
            + (getattr(message, "caption", None) or ""))
    if isinstance(bot_username, str) and bot_username:
        # An addressed command is not an advertisement for another account.
        text = re.sub(
            rf"^(/\w+)@{re.escape(bot_username)}(?=\s|$)", r"\1", text,
            flags=re.IGNORECASE,
        )
    return bool(_LINK.search(text) or _USERNAME.search(text))


def delete_later(bot, chat_id: int, message_id: int, seconds: float) -> None:
    # Private administrator replies/notices must remain. Only explicit menu
    # navigation may delete a bot-authored message there.
    if type(chat_id) is int and chat_id > 0 and is_admin(chat_id):
        return
    def delete() -> None:
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as exc:
            logging.warning("Temporary group message deletion failed (%s).", type(exc).__name__)

    timer = threading.Timer(seconds, delete)
    timer.daemon = True
    timer.start()


class GroupManagementMiddleware(BaseMiddleware):
    """Run before any text/admin handler, including for edited media captions."""

    DEFERRED_COMMANDS = {
        "/start", "/help", "/users", "/set_support_chat", "/cancel",
        "/warn", "/ban", "/unban", "/set_main_group", "/set_channel",
        "/mute", "/unmute", "/setsupport", "/set_support", "/setadmin",
        "/set_admin", "/setaccess", "/addcmd", "/removeadmin",
        "/add_command", "/del_command", "/group_commands",
    }
    NO_ARGUMENT_COMMANDS = {
        "/welcome_on", "/welcome_off", "/anti_link_on", "/anti_link_off",
        "/group_stats", "/group_commands", "/admins", "/rules", "/id", "/link",
    }

    def __init__(
        self,
        bot,
        settings_file=GROUP_SETTINGS_FILE,
        config_file=CONFIG_FILE,
        users_file=USERS_FILE,
    ):
        super().__init__()
        self.bot = bot
        self.settings_file = settings_file
        self.config_file = config_file
        self.users_file = users_file
        self.update_sensitive = True
        self.update_types = ["message", "edited_message", "callback_query"]

    def pre_process_message(self, message, data):
        return self._process(message, greet=True)

    def pre_process_edited_message(self, message, data):
        return self._process(message, greet=False)

    def post_process_message(self, message, data, exception):
        return None

    def post_process_edited_message(self, message, data, exception):
        return None

    def pre_process_callback_query(self, call, data):
        return self._handle_warning_revoke(call)

    def post_process_callback_query(self, call, data, exception):
        return None

    def _process(self, message, greet):
        if message.chat.type not in {"group", "supergroup"}:
            return None
        command_access = (
            check_group_command_access(self.bot, message, self.settings_file)
            if greet else None
        )
        if command_access is False:
            return CancelUpdate()
        settings = load_group_settings(self.settings_file, chat_id=message.chat.id)
        if greet and settings["welcome_enabled"]:
            for user in getattr(message, "new_chat_members", None) or []:
                if not user.is_bot:
                    self._welcome(
                        message.chat.id,
                        user,
                        getattr(message.chat, "title", None),
                    )
        if greet and settings["delete_service_messages"] and self._is_service_message(message):
            try:
                self.bot.delete_message(message.chat.id, message.message_id)
            except Exception as exc:
                logging.warning("Group service-message cleanup failed (%s).", type(exc).__name__)
            return CancelUpdate()
        text = ((getattr(message, "text", None) or "") + "\n"
                + (getattr(message, "caption", None) or "")).casefold()
        # Hidden clickable URLs are part of the message even when their display
        # text does not contain the forbidden link.
        for entities in (getattr(message, "entities", None), getattr(message, "caption_entities", None)):
            for entity in entities or []:
                url = getattr(entity, "url", None)
                if isinstance(url, str):
                    text += "\n" + url.casefold()
        blacklisted = any(entry.casefold() in text for entry in settings["blacklist"])
        bot_username = None
        parsed = _message_command(message)
        if parsed is not None and parsed[1]:
            try:
                bot_username = self.bot.get_me().username
            except Exception:
                logging.warning("Could not identify an addressed group command.")
        forbidden_link = settings["delete_links"] and contains_link(message, bot_username)
        if not blacklisted and not forbidden_link:
            return self._process_group_commands(message, greet, command_access)
        sender_chat = getattr(message, "sender_chat", None)
        # Telegram represents anonymous group administrators by their own group.
        if sender_chat is not None and sender_chat.id == message.chat.id:
            return self._process_group_commands(message, greet, command_access)
        user = getattr(message, "from_user", None)
        if sender_chat is None:
            if user is None or is_admin(user.id):
                return self._process_group_commands(message, greet, command_access)
            try:
                member = self.bot.get_chat_member(message.chat.id, user.id)
            except Exception as exc:
                # Never delete a possible administrator's message on a lookup failure.
                logging.warning("Could not verify group sender permissions (%s).", type(exc).__name__)
                return self._process_group_commands(message, greet, command_access)
            if member.status in {"administrator", "creator"}:
                return self._process_group_commands(message, greet, command_access)
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception as exc:
            logging.warning("Group link deletion failed (%s).", type(exc).__name__)
        if user is None or getattr(user, "is_bot", False):
            return CancelUpdate()
        if blacklisted:
            try:
                body, mode = self._render_user_template(
                    "{user_mention}, הודעתך נמחקה מכיוון שהיא מכילה מילה מתוך הרשימה השחורה.",
                    user, getattr(message.chat, "title", None),
                )
                self.bot.send_message(
                    message.chat.id, body, parse_mode=mode, disable_web_page_preview=True,
                )
            except Exception:
                logging.exception("Could not deliver a group blacklist warning.")
            return CancelUpdate()
        try:
            record = update_user_moderation(
                user.id,
                "warn",
                reason="שליחת קישור שאינו מורשה",
                chat_id=message.chat.id,
                path=self.users_file,
                max_warnings=settings["max_warnings"],
                user_name=self._display_name(user),
            )
            warning_id = record["warnings"][-1]["warning_id"]
            warning_text, parse_mode = self._render_user_template(
                settings["link_warning_text"],
                user,
                getattr(message.chat, "title", None),
                warnings_count=record["warning_count"],
                max_warnings=settings["max_warnings"],
            )
            keyboard = InlineKeyboardMarkup()
            keyboard.add(
                InlineKeyboardButton(
                    "↩️ ביטול אזהרה",
                    callback_data=(
                        f"gm:warn_revoke:{user.id}:{warning_id}:{message.chat.id}"
                    ),
                )
            )
            self.bot.send_message(
                message.chat.id,
                warning_text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
            if record.get("banned"):
                try:
                    self.bot.ban_chat_member(
                        message.chat.id, user.id, revoke_messages=True,
                    )
                except Exception as exc:
                    logging.warning("Automatic group ban failed (%s).", type(exc).__name__)
        except Exception as exc:
            logging.exception("Group link warning failed (%s).", type(exc).__name__)
        return CancelUpdate()

    def _process_group_commands(self, message, greet, command_access):
        if greet and (
            self._group_command(message, command_access)
            or self._unknown_group_command(message)
        ):
            return CancelUpdate()
        return None

    @staticmethod
    def _is_service_message(message) -> bool:
        service_types = {
            "new_chat_members", "left_chat_member", "new_chat_title",
            "new_chat_photo", "delete_chat_photo", "pinned_message",
            "forum_topic_created", "forum_topic_edited", "forum_topic_closed",
            "forum_topic_reopened", "general_forum_topic_hidden",
            "general_forum_topic_unhidden", "video_chat_scheduled",
            "video_chat_started", "video_chat_ended", "video_chat_participants_invited",
            "write_access_allowed", "boost_added",
        }
        return (
            getattr(message, "content_type", None) in service_types
            or any(getattr(message, field, None) for field in service_types)
        )

    @staticmethod
    def _welcome_command_help() -> str:
        return (
            "כדי להגדיר הודעת ברוכים הבאים חדשה, שלח כעת:\n"
            "/set_welcome [הטקסט שלך]\n\n"
            "משתנים נתמכים:\n"
            "• {mention} - תיוג לחיץ של המשתמש\n"
            "• {first_name} - השם הפרטי של המשתמש\n"
            "• {chatname} - שם הקבוצה\n"
            "• {id} - מזהה המשתמש (ID)"
        )

    @staticmethod
    def _display_name(user) -> str:
        return (
            " ".join(filter(None, [
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
            ]))
            or getattr(user, "username", None)
            or "משתמש"
        )

    @classmethod
    def _render_user_template(
        cls,
        template: str,
        user,
        group_name=None,
        *,
        warnings_count=None,
        max_warnings=None,
    ):
        """Render safe HTML while keeping Telegram user mentions clickable."""
        name = cls._display_name(user)
        first_name = getattr(user, "first_name", None) or name
        group_name = group_name or "הקבוצה"
        mention_token = "TGUSERMENTIONTOKEN"
        replacements = {
            "{user_mention}": mention_token,
            "{mention}": mention_token,
            "{name}": "TGUSERNAMEVALUE",
            "{first_name}": "TGFIRSTNAMEVALUE",
            "{group_name}": "TGGROUPNAMEVALUE",
            "{chatname}": "TGGROUPNAMEVALUE",
            "{id}": "TGUSERIDVALUE",
            "{warnings_count}": "TGWARNINGCOUNTVALUE",
            "{max_warnings}": "TGMAXWARNINGSVALUE",
        }
        source = template
        for placeholder, token in replacements.items():
            source = source.replace(placeholder, token)

        rendered, mode = format_telegram_text(source)
        user_id = getattr(user, "id", None)
        if mode is None and ("{user_mention}" in template or "{mention}" in template):
            rendered = html.escape(source)
            mode = "HTML"
        elif mode is None:
            rendered = html.escape(source)
            mode = "HTML"
        if mode == "HTML":
            mention = html.escape(name)
            if type(user_id) is int and user_id > 0:
                mention = f'<a href="tg://user?id={user_id}">{html.escape(name)}</a>'
            values = {
                mention_token: mention,
                "TGUSERNAMEVALUE": html.escape(name),
                "TGFIRSTNAMEVALUE": html.escape(first_name),
                "TGGROUPNAMEVALUE": html.escape(str(group_name)),
                "TGUSERIDVALUE": html.escape(
                    "" if user_id is None else str(user_id)
                ),
                "TGWARNINGCOUNTVALUE": html.escape(
                    "" if warnings_count is None else str(warnings_count)
                ),
                "TGMAXWARNINGSVALUE": html.escape(
                    "" if max_warnings is None else str(max_warnings)
                ),
            }
            for token, value in values.items():
                rendered = rendered.replace(token, value)
            return rendered, "HTML"

        values = {
            mention_token: name,
            "TGUSERNAMEVALUE": name,
            "TGFIRSTNAMEVALUE": first_name,
            "TGGROUPNAMEVALUE": str(group_name),
            "TGUSERIDVALUE": "" if user_id is None else str(user_id),
            "TGWARNINGCOUNTVALUE": "" if warnings_count is None else str(warnings_count),
            "TGMAXWARNINGSVALUE": "" if max_warnings is None else str(max_warnings),
        }
        for token, value in values.items():
            rendered = rendered.replace(token, value)
        return rendered, None

    def _handle_warning_revoke(self, call):
        callback_data = getattr(call, "data", None) or ""
        if not callback_data.startswith("gm:warn_revoke:"):
            return None
        message = getattr(call, "message", None)
        user = getattr(call, "from_user", None)
        if (
            message is None
            or user is None
            or getattr(message.chat, "type", None) not in {"group", "supergroup"}
        ):
            self._answer_callback(
                call.id, "הפעולה זמינה רק מתוך קבוצת ההתראה.", show_alert=True,
            )
            return CancelUpdate()

        try:
            _, _, target_text, warning_id, expected_chat_text = callback_data.split(":")
            target_id = int(target_text)
            expected_chat_id = int(expected_chat_text)
            if expected_chat_id != message.chat.id:
                raise ValueError("Chat mismatch.")
        except (ValueError, TypeError):
            self._answer_callback(call.id, "ההתראה אינה תקינה.", show_alert=True)
            return CancelUpdate()

        authorized = is_admin(user.id)
        if not authorized:
            try:
                member = self.bot.get_chat_member(message.chat.id, user.id)
                authorized = member.status in {"administrator", "creator"}
            except Exception as exc:
                logging.warning("Warning-revoke permission lookup failed (%s).", type(exc).__name__)
        if not authorized:
            self._answer_callback(
                call.id, "הרשאה זו שמורה למנהלים בלבד!", show_alert=True,
            )
            return CancelUpdate()

        self._answer_callback(call.id)
        try:
            settings = load_group_settings(self.settings_file)
            record, revoked_warning = revoke_user_warning(
                target_id,
                warning_id,
                max_warnings=settings["max_warnings"],
                actor_id=user.id,
                path=self.users_file,
                expected_chat_id=message.chat.id,
            )
            if record.get("ban_lifted"):
                try:
                    self.bot.unban_chat_member(
                        message.chat.id, target_id, only_if_banned=True,
                    )
                except Exception as exc:
                    logging.warning("Automatic group unban failed (%s).", type(exc).__name__)
            target_name = revoked_warning.get("user_name") or str(target_id)
            admin_name = self._display_name(user)
            admin_mention = (
                f'<a href="tg://user?id={int(user.id)}">'
                f"{html.escape(admin_name)}</a>"
            )
            target_mention = (
                f'<a href="tg://user?id={target_id}">'
                f"{html.escape(target_name)}</a>"
            )
            self.bot.edit_message_text(
                f"✅ המנהל {admin_mention} ביטל את האזהרה של {target_mention}! "
                f"(אזהרות נוכחיות: {record['warning_count']})",
                chat_id=message.chat.id,
                message_id=message.message_id,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=True,
            )
        except ValueError as exc:
            logging.info("Warning revoke was not applied: %s", exc)
        except Exception:
            logging.exception("Could not revoke a group warning.")
        return CancelUpdate()

    def _answer_callback(self, callback_id, text=None, show_alert=False):
        try:
            self.bot.answer_callback_query(
                callback_id, text=text, show_alert=show_alert,
            )
        except Exception as exc:
            logging.warning("Group moderation callback acknowledgement failed (%s).", type(exc).__name__)

    def _group_command(self, message, group_admin_access=None):
        text = getattr(message, "text", None) or ""
        parts = text.split(maxsplit=1)
        if not parts:
            return False
        token = parts[0]
        command, separator, recipient = token.partition("@")
        if command not in GROUP_COMMAND_SET:
            return self._custom_group_response(message, text)
        if separator:
            try:
                username = self.bot.get_me().username or ""
            except Exception:
                return False
            if recipient.lower() != username.lower():
                return False
        if command in self.NO_ARGUMENT_COMMANDS and len(parts) > 1:
            return False

        user = getattr(message, "from_user", None)
        if command in GROUP_ADMIN_ONLY_COMMANDS:
            authorized = (
                group_admin_access
                if group_admin_access is not None
                else is_group_admin(self.bot, message)
            )
        else:
            authorized = False
        if command == "/report":
            self._delete_command_input(message)
            self._send_group_report(message, text)
            return True
        if command in {"/add_command", "/del_command", "/group_commands"} and not authorized:
            return False

        if command in {"/add_command", "/del_command"}:
            try:
                command_parts = text.split(maxsplit=2)
                if command == "/add_command":
                    if len(command_parts) != 3:
                        raise ValueError("שימוש: /add_command שם_פקודה תוכן")
                    keyword = save_group_command(
                        message.chat.id, command_parts[1], command_parts[2], self.config_file,
                    )
                    reply = f"✅ הפקודה ״{keyword}״ נשמרה לקבוצה הזו."
                else:
                    if len(command_parts) < 2:
                        raise ValueError("שימוש: /del_command שם_פקודה")
                    keyword = delete_group_command(
                        message.chat.id, command_parts[1], self.config_file,
                    )
                    reply = f"✅ הפקודה ״{keyword}״ נמחקה מהקבוצה הזו."
            except (OSError, ValueError) as exc:
                reply = f"❌ {exc}"
            self._delete_command_input(message)
            self._send_group_notice(message.chat.id, reply, 5)
            return True

        if command == "/group_commands":
            commands = list_group_commands(message.chat.id, self.config_file)
            if commands:
                lines = ["📋 פקודות מותאמות לקבוצה:"]
                size = len(lines[0])
                for keyword, response in commands.items():
                    line = f"\n• {keyword}: {response}"
                    if size + len(line) > 3600:
                        lines.append("\n… הרשימה ארוכה; מוצגות הפקודות הראשונות.")
                        break
                    lines.append(line)
                    size += len(line)
                reply = "".join(lines)
            else:
                reply = "אין פקודות מותאמות לקבוצה הזו."
            self._delete_command_input(message)
            self._send_group_notice(message.chat.id, reply, 15)
            return True

        if self._custom_group_response(message, text):
            return True

        if command == "/admins":
            try:
                admins = self.bot.get_chat_administrators(message.chat.id)
                names = [
                    " ".join(filter(None, [
                        getattr(member.user, "first_name", None),
                        getattr(member.user, "last_name", None),
                    ])) or getattr(member.user, "username", None) or str(member.user.id)
                    for member in admins
                ]
                reply = "👥 מנהלי הקבוצה:\n" + "\n".join(f"• {name}" for name in names)
            except Exception as exc:
                logging.warning("Group administrators lookup failed (%s).", type(exc).__name__)
                reply = "לא ניתן להציג את מנהלי הקבוצה כרגע."
        elif command == "/rules":
            reply = "לא הוגדרו כללי קבוצה. אפשר לשמור אותם כפקודה בשם rules עם /add_command."
        elif command == "/id":
            reply = f"מזהה הקבוצה: {message.chat.id}"
            if user is not None:
                reply += f"\nמזהה המשתמש: {user.id}"
        elif command == "/link":
            reply = self._group_link(message.chat.id)
        else:
            if not authorized:
                return False
            try:
                if command == "/set_welcome":
                    command_parts = text.split(maxsplit=1)
                    if len(command_parts) != 2:
                        raise ValueError(self._welcome_command_help())
                    update_group_welcome(
                        command_parts[1], self.settings_file,
                        chat_id=message.chat.id,
                    )
                    reply = "✅ הודעת ברוכים הבאים עודכנה לקבוצה הזו."
                elif command in {"/welcome_on", "/welcome_off", "/anti_link_on", "/anti_link_off"}:
                    key = "welcome_enabled" if command.startswith("/welcome_") else "delete_links"
                    enabled = command.endswith("_on")
                    set_group_flag(
                        key, enabled, self.settings_file,
                        chat_id=message.chat.id if key == "welcome_enabled" else None,
                    )
                    label = "הודעת ברוכים הבאים" if key == "welcome_enabled" else "הגנת קישורים"
                    suffix = "לקבוצה הזו" if key == "welcome_enabled" else "לכל הקבוצות"
                    reply = f"✅ {label} {'הופעלה' if enabled else 'כובתה'} {suffix}."
                else:
                    settings = load_group_settings(
                        self.settings_file, chat_id=message.chat.id,
                    )
                    count = self.bot.get_chat_member_count(message.chat.id)
                    reply = (f"סטטיסטיקות הקבוצה:\nמספר חברים: {count}\n"
                             f"ברוכים הבאים: {'פעיל' if settings['welcome_enabled'] else 'כבוי'}\n"
                             f"הגנת קישורים: {'פעילה' if settings['delete_links'] else 'כבויה'}")
            except ValueError as exc:
                reply = str(exc)
            except Exception as exc:
                logging.warning("Group command failed (%s).", type(exc).__name__)
                reply = "לא ניתן לבצע את הפקודה. בדקו את הרשאות הבוט ונסו שוב."
        self.bot.send_message(message.chat.id, reply)
        self._delete_command_input(message)
        return True

    def _unknown_group_command(self, message):
        text = getattr(message, "text", None) or ""
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False
        token = stripped.split(maxsplit=1)[0].split("@", 1)[0].casefold()
        if token in self.DEFERRED_COMMANDS:
            return False
        return match_reply(stripped) is None

    def _custom_group_response(self, message, text):
        match = match_group_command(message.chat.id, text, self.config_file)
        if match is None:
            return False
        body, mode = format_telegram_text(match[1])
        try:
            self.bot.send_message(message.chat.id, body, parse_mode=mode)
        except Exception as exc:
            logging.warning("Per-group command response failed (%s).", type(exc).__name__)
        return True

    def _delete_command_input(self, message):
        try:
            self.bot.delete_message(message.chat.id, message.message_id)
        except Exception as exc:
            logging.warning("Group command input cleanup failed (%s).", type(exc).__name__)

    def _send_group_notice(self, chat_id, text, delay):
        try:
            sent = self.bot.send_message(chat_id, text)
            delete_later(self.bot, chat_id, sent.message_id, delay)
        except Exception as exc:
            logging.warning("Group command response failed (%s).", type(exc).__name__)

    def _group_link(self, chat_id):
        try:
            chat = self.bot.get_chat(chat_id)
            link = getattr(chat, "invite_link", None)
            username = getattr(chat, "username", None)
            if link:
                return f"קישור לקבוצה: {link}"
            if username:
                return f"קישור לקבוצה: https://t.me/{username}"
            return f"קישור לקבוצה: {self.bot.export_chat_invite_link(chat_id)}"
        except Exception as exc:
            logging.warning("Group invite link lookup failed (%s).", type(exc).__name__)
            return "לא ניתן להפיק קישור לקבוצה. בדקו שלבוט יש הרשאת הזמנת משתמשים."

    def _send_group_report(self, message, command_text):
        reported = getattr(message, "reply_to_message", None)
        if reported is None:
            logging.info("/report in group ignored because it did not reply to a message.")
            return
        try:
            from management_config import load_config
            config = load_config(self.config_file)
            target = next(
                (
                    destination for destination in (
                        config["ADMIN_CHAT_ID"], config["LOG_CHAT_ID"],
                    )
                    if destination is not None and destination != message.chat.id
                ),
                None,
            )
            from admin_notifications import resolve_user_management_chat
            target = resolve_user_management_chat(
                target, config_file=self.config_file,
            )
            if target is None:
                logging.warning("Group report has no separate configured management destination.")
                return

            reporter = getattr(message, "from_user", None)
            reporter_name = "משתמש"
            reporter_id = None
            if reporter is not None:
                reporter_name = " ".join(filter(None, [
                    getattr(reporter, "first_name", None),
                    getattr(reporter, "last_name", None),
                ])) or "משתמש"
                username = getattr(reporter, "username", None)
                reporter_name += f" (@{username})" if username else ""
                reporter_id = reporter.id

            author = getattr(reported, "from_user", None)
            reported_by = "משתמש לא מזוהה"
            reported_id = None
            if author is not None:
                reported_by = " ".join(filter(None, [
                    getattr(author, "first_name", None),
                    getattr(author, "last_name", None),
                ])) or "משתמש"
                username = getattr(author, "username", None)
                reported_by += f" (@{username})" if username else ""
                reported_id = author.id

            content = getattr(reported, "text", None) or getattr(reported, "caption", None)
            if not content:
                content_type = getattr(reported, "content_type", "הודעה")
                content = f"[{content_type}]"
            content = html.escape(content[:1400])

            group_title = html.escape(
                getattr(message.chat, "title", None) or str(message.chat.id),
            )
            group_link = self._message_link(message.chat, reported.message_id)
            if group_link:
                link_part = (
                    f'🔗 <a href="{html.escape(group_link, quote=True)}">'
                    "מעבר להודעה המדווחת</a>"
                )
            else:
                link_part = f"🔗 מזהה הודעה: <code>{reported.message_id}</code>"
            reason = command_text.split(maxsplit=1)
            reason_part = (
                f"\n📝 <b>סיבה:</b> {html.escape(reason[1][:500])}"
                if len(reason) > 1 else ""
            )
            body = (
                "<b>🚨 דיווח על הודעה בקבוצה</b>\n"
                f"📁 <b>קבוצה:</b> {group_title} (<code>{message.chat.id}</code>)\n"
                f"👤 <b>דווח על ידי:</b> {html.escape(reporter_name)}"
                f"{f' (<code>{reporter_id}</code>)' if reporter_id is not None else ''}\n"
                f"🎯 <b>כותב ההודעה:</b> {html.escape(reported_by)}"
                f"{f' (<code>{reported_id}</code>)' if reported_id is not None else ''}\n"
                f"💬 <b>תוכן:</b>\n<blockquote>{content}</blockquote>"
                f"{reason_part}\n{link_part}"
            )
            from admin_notifications import send_admin_message
            send_admin_message(
                self.bot, target, body, config_file=self.config_file,
                parse_mode="HTML", disable_notification=True, disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.error("Group /report delivery failed (%s).", type(exc).__name__)

    @staticmethod
    def _message_link(chat, message_id):
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{message_id}"
        digits = str(abs(chat.id))
        if digits.startswith("100") and len(digits) > 3:
            return f"https://t.me/c/{digits[3:]}/{message_id}"
        return None

    def _welcome(self, chat_id, user, group_name=None):
        settings = load_group_settings(self.settings_file, chat_id=chat_id)
        template = settings["welcome_text"]
        if not any(
            placeholder in template
            for placeholder in (
                "{name}", "{first_name}", "{user_mention}", "{mention}",
                "{group_name}", "{chatname}", "{id}",
            )
        ):
            template = template.rstrip() + "\n{name}"
        text, parse_mode = self._render_user_template(template, user, group_name)
        with _WELCOME_LOCK:
            settings = load_group_settings(self.settings_file, chat_id=chat_id)
            deletion_mode = settings["welcome_delete_mode"]
            keyboard = None
            if settings["welcome_button_enabled"] and settings["welcome_button_url"]:
                keyboard = InlineKeyboardMarkup()
                keyboard.add(InlineKeyboardButton(
                    settings["welcome_button_text"],
                    url=settings["welcome_button_url"],
                ))
            previous_id = settings["last_welcome_messages"].get(str(chat_id))
            if deletion_mode != "none" and previous_id:
                try:
                    self.bot.delete_message(chat_id, previous_id)
                except Exception as exc:
                    logging.warning(
                        "Previous group welcome deletion failed (%s).",
                        type(exc).__name__,
                    )
                clear_last_welcome_message(
                    chat_id, self.settings_file, expected_message_id=previous_id,
                )
            elif deletion_mode == "none" and previous_id:
                clear_last_welcome_message(
                    chat_id, self.settings_file, expected_message_id=previous_id,
                )
            try:
                sent = self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode=parse_mode,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                if deletion_mode != "none":
                    set_last_welcome_message(
                        chat_id, sent.message_id, self.settings_file,
                    )
                if deletion_mode == "after_5_minutes":
                    timer = threading.Timer(
                        300.0, self._delete_welcome_if_current,
                        args=(chat_id, sent.message_id),
                    )
                    timer.daemon = True
                    timer.start()
            except Exception as exc:
                logging.warning("Group welcome failed (%s).", type(exc).__name__)

    def _delete_welcome_if_current(self, chat_id, message_id):
        """Delete only the still-current greeting; a newer join supersedes its timer."""
        with _WELCOME_LOCK:
            try:
                settings = load_group_settings(self.settings_file)
                if settings["last_welcome_messages"].get(str(chat_id)) != message_id:
                    return
                self.bot.delete_message(chat_id, message_id)
                clear_last_welcome_message(
                    chat_id, self.settings_file, expected_message_id=message_id,
                )
            except Exception as exc:
                logging.warning("Timed group welcome deletion failed (%s).", type(exc).__name__)
