"""Fail-closed group/channel approval, membership inventory and approval alerts."""

import html
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from telebot import types
from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import GROUPS_FILE, is_admin, load_group_ids, remove_group_id
from management_config import CONFIG_FILE, _LOCK, load_config, save_json

CHAT_TYPES = {"group", "supergroup", "channel"}


def chat_is_active(bot, chat_id):
    """Isolated components without the installed security service keep their behavior."""
    service = getattr(bot, "chat_allowlist", None)
    return service.is_active(chat_id) if isinstance(service, AllowedChats) else True


class AllowedChats(BaseMiddleware):
    def __init__(self, bot, config_file=CONFIG_FILE, groups_file=GROUPS_FILE):
        super().__init__()
        self.bot, self.config_file, self.groups_file = bot, Path(config_file), Path(groups_file)
        self.update_sensitive = True
        self.update_types = ["message", "edited_message", "channel_post",
                             "edited_channel_post", "callback_query"]
        self._notice_lock = threading.Lock()

    def is_active(self, chat_id):
        if type(chat_id) is not int:
            return False
        if chat_id > 0:
            return True
        try:
            config = load_config(self.config_file)
            record = config["managed_chats"].get(str(chat_id))
            return chat_id in config["allowed_chats"] and (
                record is None or record["present"])
        except Exception as exc:
            logging.error("Chat approval check failed; denying activity (%s).", type(exc).__name__)
            return False

    def observe(self, chat, present=True, joining=False):
        if chat.type not in CHAT_TYPES or type(chat.id) is not int or chat.id >= 0:
            return
        with _LOCK:
            config = load_config(self.config_file)
            key = str(chat.id)
            previous = config["managed_chats"].get(key)
            if previous is not None and not previous["present"] and present and not joining:
                return  # Late queued messages must not resurrect a chat we have left.
            record = dict(previous or {})
            rejoining = previous is not None and not previous["present"]
            record.update(title=(getattr(chat, "title", None) or record.get("title") or str(chat.id))[:200],
                          type=chat.type, present=present)
            if not present:
                config["allowed_chats"] = [cid for cid in config["allowed_chats"] if cid != chat.id]
                record["pending"] = False
            elif previous is None or rejoining:
                record["pending"] = chat.id not in config["allowed_chats"]
                record.pop("notice_message_id", None)
                record.pop("notice_attempt", None)
            config["managed_chats"][key] = record
            if previous != record or not present:
                save_json(self.config_file, config)
        if present and record.get("pending"):
            self.request_approval(chat.id)

    def membership(self, update):
        chat = update.chat
        if chat.type not in CHAT_TYPES:
            return
        member = update.new_chat_member
        present = member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and bool(getattr(member, "is_member", False)))
        self.observe(chat, present=present, joining=present)
        if not present and chat.type != "channel":
            remove_group_id(chat.id, self.groups_file)

    def bootstrap(self):
        """Inventory legacy group IDs and discover configured, accessible destinations."""
        legacy_groups = load_group_ids(self.groups_file)
        for cid in sorted(legacy_groups):
            with _LOCK:
                known = str(cid) in load_config(self.config_file)["managed_chats"]
            if not known:
                self.observe(SimpleNamespace(id=cid, type="supergroup", title=str(cid)))
        config = load_config(self.config_file)
        candidates = set(config["allowed_chats"]) | legacy_groups
        candidates.update(int(key) for key, record in config["managed_chats"].items()
                          if record["present"])
        candidates.update(config[key] for key in ("ADMIN_CHAT_ID", "main_group_id", "channel_id", "LOG_CHAT_ID")
                          if type(config.get(key)) is int and config[key] < 0)
        # Support destinations may predate the newer administrative destination.
        from support_storage import load_support_settings
        support_file = self.config_file.parent / "support_settings.json"
        if support_file.exists():
            cid = load_support_settings(support_file)["support_chat_id"]
            if type(cid) is int and cid < 0:
                candidates.add(cid)
        for cid in sorted(candidates):
            try:
                chat = self.bot.get_chat(cid)
                member = self.bot.get_chat_member(cid, self.bot.get_me().id)
                if member.status in {"member", "administrator", "creator"}:
                    self.observe(chat)
                elif member.status in {"left", "kicked"}:
                    self.observe(chat, present=False)
                    remove_group_id(cid, self.groups_file)
            except Exception as exc:
                logging.warning("Could not refresh managed chat %s (%s).", cid, type(exc).__name__)

    def request_approval(self, chat_id):
        with self._notice_lock:
            with _LOCK:
                config = load_config(self.config_file)
                record = config["managed_chats"].get(str(chat_id))
                target = config["ADMIN_CHAT_ID"]
                if not record or not record["present"] or not record.get("pending"):
                    return
                if target is None:
                    logging.warning("Chat %s awaits approval; ADMIN_CHAT_ID is not configured.", chat_id)
                    return
                if record.get("notice_chat_id") == target and record.get("notice_message_id"):
                    return
                if time.time() - record.get("notice_attempt", 0) < 60:
                    return
                record["notice_attempt"] = time.time()
                save_json(self.config_file, config)
                title, kind = record["title"], record["type"]
            keyboard = types.InlineKeyboardMarkup([
                [types.InlineKeyboardButton("✅ אישור הפעלה", callback_data=f"chatallow:approve:{chat_id}")],
                [types.InlineKeyboardButton("❌ דחייה ויציאה", callback_data=f"chatallow:reject:{chat_id}")],
            ])
            try:
                from admin_notifications import send_admin_message
                sent = send_admin_message(
                    self.bot, target,
                    f"🛡️ בקשת אישור להפעלת הבוט\n"
                    f"שם: {html.escape(title)}\nסוג: {kind}\n"
                    f"מזהה: <code>{chat_id}</code>\n🔴 מוקפא עד לאישור.",
                    config_file=self.config_file,
                    parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True,
                )
                with _LOCK:
                    config = load_config(self.config_file)
                    record = config["managed_chats"].get(str(chat_id))
                    if record and record["present"] and record.get("pending"):
                        record.update(notice_chat_id=target, notice_message_id=sent.message_id)
                        save_json(self.config_file, config)
            except Exception as exc:
                logging.error("Approval alert delivery failed for chat %s (%s).", chat_id, type(exc).__name__)

    def set_active(self, chat_id, active):
        with _LOCK:
            config = load_config(self.config_file)
            record = config["managed_chats"].get(str(chat_id))
            if not record or not record["present"]:
                raise ValueError("הבוט כבר אינו נמצא בצ׳אט הזה.")
            ids = set(config["allowed_chats"])
            if active:
                ids.add(chat_id)
            else:
                ids.discard(chat_id)
            record["pending"] = False
            config["allowed_chats"] = sorted(ids)
            save_json(self.config_file, config)
            return record["title"]

    def leave(self, chat_id):
        # Freeze before attempting Telegram's leave operation, even if it fails.
        title = self.set_active(chat_id, False)
        if self.bot.leave_chat(chat_id) is False:
            raise ValueError("טלגרם דחה את היציאה; הצ׳אט נשאר מוקפא.")
        with _LOCK:
            config = load_config(self.config_file)
            config["managed_chats"][str(chat_id)]["present"] = False
            config["allowed_chats"] = [cid for cid in config["allowed_chats"] if cid != chat_id]
            save_json(self.config_file, config)
        remove_group_id(chat_id, self.groups_file)
        return title

    def _migration(self, message):
        new_id = getattr(message, "migrate_to_chat_id", None)
        old_id = getattr(message, "migrate_from_chat_id", None)
        if new_id is None and old_id is None:
            return
        old_id, new_id = (message.chat.id, new_id) if new_id is not None else (old_id, message.chat.id)
        with _LOCK:
            config = load_config(self.config_file)
            approved = old_id in config["allowed_chats"] or new_id in config["allowed_chats"]
            previous = config["managed_chats"].get(str(old_id), {})
            title = getattr(message.chat, "title", None) or previous.get("title") or str(new_id)
            if str(old_id) in config["managed_chats"]:
                config["managed_chats"][str(old_id)].update(present=False, pending=False)
            config["managed_chats"][str(new_id)] = dict(title=title[:200], type="supergroup",
                                                       present=True, pending=not approved)
            config["allowed_chats"] = sorted((set(config["allowed_chats"]) - {old_id}) |
                                            ({new_id} if approved else set()))
            save_json(self.config_file, config)

    def _message(self, message, data):
        if message.chat.type not in CHAT_TYPES:
            return None
        self._migration(message)
        # The old group emits a service update during migration; do not resurrect it.
        if not getattr(message, "migrate_to_chat_id", None):
            self.observe(message.chat)
        elif self.is_active(message.migrate_to_chat_id):
            return None  # Let the existing membership/night-state migration handlers run.
        if not self.is_active(message.chat.id):
            # Selecting a management destination must not silently unfreeze
            # ordinary group features. Permit only authenticated support replies.
            actor = getattr(message, "from_user", None)
            live = getattr(self.bot, "live_chat_queue", None)
            if (live is not None and actor is not None and is_admin(actor.id)
                    and self._management_destination(message.chat.id)
                    and not (getattr(message, "text", None) or "").strip().startswith("/")
                    and live.handle_admin_reply(message) is True):
                return CancelUpdate()
            return CancelUpdate()
        return None

    def _management_destination(self, chat_id):
        config = load_config(self.config_file)
        return chat_id in {
            config.get("LOG_CHANNEL_ID") or config.get("ADMIN_CHAT_ID"),
            config.get("ADMIN_COMMANDS_CHAT_ID"),
        }

    pre_process_message = _message
    pre_process_edited_message = _message
    pre_process_channel_post = _message
    pre_process_edited_channel_post = _message

    def pre_process_callback_query(self, call, data):
        if (call.data or "").startswith("chatallow:"):
            return None  # Validated by the dedicated approval handler, even in a frozen admin chat.
        message = call.message
        if message is not None and message.chat.type in CHAT_TYPES:
            self.observe(message.chat)
            if not self.is_active(message.chat.id):
                actor = getattr(call, "from_user", None)
                if (actor is not None and is_admin(actor.id)
                        and ((call.data or "").startswith("live:")
                             or (self._management_destination(message.chat.id)
                                 and (call.data or "").startswith(("mod:", "missing_content:"))))):
                    # Live handlers validate durable card/session ownership.
                    # Let them validate old close controls after a group switch.
                    return None
                if (call.data or "").startswith("live:"):
                    self.bot.answer_callback_query(
                        call.id, text="הרשאה זו שמורה למנהלים בלבד!", show_alert=True,
                    )
                else:
                    self.bot.answer_callback_query(call.id)
                return CancelUpdate()
        return None

    def _post(self, message, data, exception):
        return None

    post_process_message = _post
    post_process_edited_message = _post
    post_process_channel_post = _post
    post_process_edited_channel_post = _post
    post_process_callback_query = _post

    def register_handlers(self):
        self.bot.register_callback_query_handler(self.approval_callback,
                                                func=lambda call: (call.data or "").startswith("chatallow:"))

    def approval_callback(self, call):
        message = call.message
        actor = getattr(call, "from_user", None)
        completed = False
        try:
            if actor is None or not is_admin(actor.id) or message is None:
                raise ValueError("הפעולה זמינה למנהלים מורשים בלבד.")
            parts = (call.data or "").split(":")
            if len(parts) != 3 or parts[1] not in {"approve", "reject"}:
                raise ValueError("פעולה לא תקינה.")
            chat_id = int(parts[2])
            with _LOCK:
                config = load_config(self.config_file)
                record = config["managed_chats"].get(str(chat_id))
                if (not record or not record["present"] or not record.get("pending")
                        or message.chat.id != config["ADMIN_CHAT_ID"]
                        or record.get("notice_chat_id") != message.chat.id
                        or record.get("notice_message_id") != message.message_id):
                    raise ValueError("בקשת האישור התיישנה או אינה שייכת להודעה זו.")
            if parts[1] == "approve":
                title = self.set_active(chat_id, True)
                notice = f"✅ הקבוצה {title} הופעלה בהצלחה!"
            else:
                title = self.leave(chat_id)
                notice = f"✅ הבוט יצא מהצ׳אט {title}."
            completed = True
        except ValueError as exc:
            notice = str(exc)
        except Exception as exc:
            logging.error("Chat approval operation failed (%s).", type(exc).__name__)
            notice = "הפעולה נכשלה. בדקו הרשאות ונסו שוב."
        # A failed popup is not a failed configuration change. Never answer twice.
        try:
            self.bot.answer_callback_query(call.id, text=notice[:200], show_alert=True)
        except Exception as exc:
            logging.warning("Chat approval popup unavailable (%s); no retry.", type(exc).__name__)
        if completed:
            try:
                self.bot.edit_message_reply_markup(message.chat.id, message.message_id, reply_markup=None)
            except Exception as exc:
                logging.warning("Could not clear approval buttons (%s).", type(exc).__name__)
