"""Persistent ban/mute records and synchronized, live management keyboards."""

import json
import logging
import math
import secrets
import threading
import time
from pathlib import Path

from telebot.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from bot_storage import is_admin, load_moderation_records, update_user_moderation
from management_config import _LOCK, load_config, save_json
from support_storage import load_support_settings

PERMISSIONS = (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_change_info",
    "can_invite_users", "can_pin_messages", "can_manage_topics",
)


class RestrictionEngine:
    def __init__(self, bot, core, events_file=None):
        self.bot, self.core = bot, core
        self.path = core.config_file
        self.events_file = Path(events_file or Path(self.path).parent / "moderation_events.json")
        self.operation_lock = threading.RLock()
        self.edit_backoff = {}
        self.stop_event = threading.Event()
        if not self.events_file.exists():
            save_json(self.events_file, {"messages": {}})
        from persistent_json import read_json_text
        self.events = json.loads(read_json_text(self.events_file))
        if not isinstance(self.events, dict) or not isinstance(self.events.get("messages"), dict):
            raise ValueError("Invalid moderation event routes.")

    def destinations(self):
        config = load_config(self.path)
        return {
            target for target in (
                config["admin_chat_id"], config.get("LOG_CHANNEL_ID"),
                config.get("LOG_CHAT_ID"),
                load_support_settings(self.core.support_file)["support_chat_id"],
            ) if target is not None
        }

    def state(self, uid):
        config = load_config(self.path)
        ban = config["banned_users"].get(str(uid))
        if not ban:
            legacy = load_moderation_records(self.core.users_file).get(str(uid), {})
            if legacy.get("banned"):
                ban = {"reason": legacy.get("ban_reason") or "ללא סיבה"}
                with _LOCK:
                    latest = load_config(self.path)
                    latest["banned_users"].setdefault(str(uid), ban)
                    save_json(self.path, latest)
        mute = config["muted_users"].get(str(uid))
        return ban, mute if mute and (mute["until"] is None or mute["until"] > time.time()) else None

    def _protect(self, uid):
        if type(uid) is not int or uid <= 0 or is_admin(uid):
            raise ValueError("אי אפשר להרחיק מנהל מורשה או מזהה לא תקין.")

    def ban(self, uid, reason, origin=None):
        self._protect(uid)
        with self.operation_lock:
            update_user_moderation(uid, "ban", reason or "ללא סיבה", origin, self.core.users_file)
            record = {"reason": reason or "ללא סיבה", "created_at": time.time(), "nonce": secrets.token_hex(8)}
            with _LOCK:
                config = load_config(self.path)
                config["banned_users"][str(uid)] = record
                config["muted_users"].pop(str(uid), None)
                save_json(self.path, config)
            try:
                counts = self.core._apply_group_action(uid, True, origin)
            finally:
                self.refresh(uid)
            return record, counts

    def unban(self, uid, origin=None, on_local_release=None):
        self._protect(uid)
        with self.operation_lock:
            update_user_moderation(uid, "unban", "", origin, self.core.users_file)
            with _LOCK:
                config = load_config(self.path)
                config["banned_users"].pop(str(uid), None)
                save_json(self.path, config)
            if on_local_release:
                on_local_release()
            try:
                counts = self.core._apply_group_action(uid, False, origin)
            finally:
                self.refresh(uid)
            return counts

    def mute(self, uid, seconds, reason, origin=None):
        self._protect(uid)
        if not 60 <= seconds <= 365 * 86400:
            raise ValueError("משך ההשתקה חייב להיות בין דקה ל־365 ימים.")
        with self.operation_lock:
            if self.state(uid)[0]:
                raise ValueError("המשתמש חסום לצמיתות. יש לבטל חסימה קודם.")
            until = int(time.time()) + seconds
            snapshots, failed = {}, 0
            existing = load_config(self.path)["muted_users"].get(str(uid), {})
            for gid in self.core._groups(origin):
                try:
                    member = self.bot.get_chat_member(gid, uid)
                    if member.status in {"administrator", "creator"}:
                        raise ValueError("Cannot restrict a group administrator.")
                    previous = existing.get("groups", {}).get(str(gid))
                    snapshot = previous or {
                        "permissions": {name: bool(getattr(member, name, False)) for name in PERMISSIONS} if member.status == "restricted" else None,
                        "until": int(getattr(member, "until_date", 0) or 0),
                    }
                    if until - time.time() < 30:
                        raise ValueError("The mute is too close to expiry.")
                    result = self.bot.restrict_chat_member(gid, uid, ChatPermissions(**{name: False for name in PERMISSIONS}), until_date=until, use_independent_chat_permissions=True)
                    if result is False:
                        raise RuntimeError("Restriction rejected.")
                    snapshots[str(gid)] = snapshot
                except Exception as exc:
                    logging.warning("Group mute failed (%s).", type(exc).__name__)
                    failed += 1
            record = {"reason": reason or "ללא סיבה", "until": until, "created_at": time.time(), "groups": snapshots, "nonce": secrets.token_hex(8)}
            with _LOCK:
                config = load_config(self.path)
                config["muted_users"][str(uid)] = record
                save_json(self.path, config)
            self.refresh(uid)
            return record, (len(snapshots), failed)

    def unmute(self, uid, on_local_release=None):
        self._protect(uid)
        with self.operation_lock:
            record = load_config(self.path)["muted_users"].get(str(uid))
            if not record:
                if on_local_release:
                    on_local_release()
                self.refresh(uid)
                return 0
            # Release private access before slower Telegram permission restores.
            with _LOCK:
                config = load_config(self.path)
                config["muted_users"].pop(str(uid), None)
                save_json(self.path, config)
            if on_local_release:
                on_local_release()
            failed = {}
            for gid, snapshot in record.get("groups", {}).items():
                try:
                    expiry = snapshot["until"]
                    original = snapshot["permissions"] if expiry is None or expiry == 0 or expiry > time.time() else None
                    permissions = original or {name: True for name in PERMISSIONS}
                    from allowed_chats import chat_is_active
                    if not chat_is_active(self.bot, int(gid)):
                        failed[gid] = snapshot
                        continue
                    result = self.bot.restrict_chat_member(int(gid), uid, ChatPermissions(**permissions), until_date=(expiry or 0) if original else 0, use_independent_chat_permissions=True)
                    if result is False:
                        raise RuntimeError("Restoration rejected.")
                except Exception as exc:
                    failed[gid] = snapshot
                    logging.warning("Group mute restoration failed (%s).", type(exc).__name__)
            with _LOCK:
                config = load_config(self.path)
                if failed:
                    expires = record["until"]
                    record.update(until=min(expires, time.time()) if expires is not None else time.time(), groups=failed)
                    config["muted_users"][str(uid)] = record
                else:
                    config["muted_users"].pop(str(uid), None)
                save_json(self.path, config)
            self.refresh(uid)
            return len(failed)

    def update_reason(self, uid, reason, nonce):
        with _LOCK:
            config = load_config(self.path)
            record = config["banned_users"].get(str(uid))
            if not record or record.get("nonce") != nonce:
                raise ValueError("החסימה השתנתה או בוטלה. הסיבה לא עודכנה.")
            record["reason"] = reason
            save_json(self.path, config)

    def markup(self, uid, base=None):
        keyboard = InlineKeyboardMarkup(row_width=2)
        if base:
            for row in base.keyboard:
                kept = [button for button in row if not (getattr(button, "callback_data", None) or "").startswith("mod:")]
                if kept:
                    keyboard.row(*kept)
        ban, mute = self.state(uid)
        request_controls = base is not None and any(
            (getattr(button, "callback_data", None) or "").startswith(
                ("live:resolve:", "missing_content:"),
            )
            for row in base.keyboard for button in row
        )
        keyboard.row(
            InlineKeyboardButton(
                "🔊 בטל השתקת משתמש" if mute else
                ("🔊 השתק משתמש" if request_controls else "🔇 השתק משתמש"),
                callback_data=f"mod:{'unmute' if mute else 'mute'}:{uid}",
            ),
            InlineKeyboardButton("✅ בטל חסימת משתמש" if ban else "🚫 חסום משתמש", callback_data=f"mod:{'unban' if ban else 'ban'}:{uid}"),
        )
        if request_controls and len(keyboard.keyboard) > 2:
            keyboard.keyboard.insert(1, keyboard.keyboard.pop())
        return keyboard

    def track(self, chat_id, message_id, uid, markup=None):
        with self.operation_lock:
            key = f"{chat_id}:{message_id}"
            existing = self.events["messages"].get(key)
            # A group switch must not leave old, already-owned ticket layouts
            # stale: later moderation refreshes would restore an obsolete live button.
            if (chat_id not in self.destinations()
                    and (not existing or existing.get("uid") != uid)):
                return
            base = markup or getattr(self.bot, "markup_cache", {}).get((chat_id, message_id))
            self.events["messages"][key] = {"uid": uid, "markup": base.to_dict() if base else None}
            save_json(self.events_file, self.events)
            self._edit(chat_id, message_id, uid, markup)

    def _edit(self, chat_id, message_id, uid, markup=None):
        if self.edit_backoff.get(chat_id, 0) > time.time():
            return
        base = markup or getattr(self.bot, "markup_cache", {}).get((chat_id, message_id))
        if base is None:
            saved = self.events["messages"].get(f"{chat_id}:{message_id}", {}).get("markup")
            if saved:
                base = InlineKeyboardMarkup.de_json(saved)
        try:
            self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=self.markup(uid, base))
        except Exception as exc:
            description = str(getattr(exc, "description", "")).lower()
            code = getattr(exc, "error_code", None)
            if "message is not modified" in description:
                return
            if code == 400 and any(reason in description for reason in (
                "message to edit not found", "message can't be edited",
                "message cannot be edited", "message_id_invalid",
            )):
                # Retire only the keyboard-refresh reference. Ticket history,
                # live-chat routes, and valid messages remain unchanged.
                with self.operation_lock:
                    self.events["messages"].pop(f"{chat_id}:{message_id}", None)
                    getattr(self.bot, "markup_cache", {}).pop((chat_id, message_id), None)
                    try:
                        save_json(self.events_file, self.events)
                    except Exception:
                        logging.exception("Could not persist retired moderation card.")
                logging.info("Retired a permanently unavailable moderation card.")
                return
            # Do not hammer every historical card when the entire group is
            # temporarily inaccessible or Telegram is rate-limiting edits.
            retry = 300 if code == 403 else 5
            if code == 429:
                parameters = (getattr(exc, "result_json", None) or {}).get("parameters", {})
                try:
                    requested = float(parameters.get("retry_after", 5))
                    retry = max(5, requested) if math.isfinite(requested) else 5
                except (ValueError, TypeError):
                    retry = 5
            self.edit_backoff[chat_id] = time.time() + retry
            logging.warning(
                "Moderation keyboard refresh failed (%s, code=%s): %s",
                type(exc).__name__, code, description[:200],
            )

    def refresh(self, uid):
        targets = self.destinations()
        for key, record in list(self.events["messages"].items()):
            cid, mid = map(int, key.split(":"))
            if record["uid"] == uid and cid in targets:
                self._edit(cid, mid, uid)

    def tick(self):
        for uid, record in load_config(self.path)["muted_users"].items():
            expires = record["until"]
            if expires is not None and expires <= time.time():
                try:
                    self.unmute(int(uid))
                except Exception as exc:
                    logging.warning("An expired mute could not be restored (%s).", type(exc).__name__)

    def start(self):
        def run():
            while not self.stop_event.wait(30):
                try:
                    self.tick()
                except Exception as exc:
                    logging.warning("Mute expiry worker failed (%s).", type(exc).__name__)
        self.thread = threading.Thread(target=run, daemon=True, name="mute-expiry")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if hasattr(self, "thread"):
            self.thread.join(3)
