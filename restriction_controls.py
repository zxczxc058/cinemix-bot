"""Authorized, private moderation inputs and in-place management-channel actions."""

import html
import logging
import re
import threading
import secrets
from concurrent.futures import ThreadPoolExecutor
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot_storage import is_admin
from management_config import _LOCK, load_config, save_json, is_link_input
from private_admin_protection import send_enforcement_confirmation


def duration(value):
    match = re.fullmatch(r"(\d+)([mhd])", value.strip().lower())
    if not match:
        raise ValueError("משך לא תקין: למשל 30m, 1h, 24h או 7d.")
    seconds = int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]]
    if not 60 <= seconds <= 365 * 86400:
        raise ValueError("משך ההשתקה חייב להיות בין דקה ל־365 ימים.")
    return seconds


class RestrictionControls:
    def __init__(self, engine, registry, submit=None):
        self.engine, self.registry = engine, registry
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="release") if submit is None else None
        self.submit = submit or self.executor.submit
        self.inflight = set()
        self.inflight_lock = threading.Lock()
        # Independent action contexts, keyed by the authenticated administrator.
        # Telegram's callback object and the dashboard's transient input map are
        # not the source of truth for a continuing restriction wizard.
        self.contexts = {}
        self.context_lock = threading.RLock()

    def cancel_context(self, actor):
        with self.context_lock:
            self.contexts.pop(actor, None)

    def restore_context(self, d):
        with self.context_lock:
            state = self.contexts.get(d.actor_id)
            if state is not None and not d.pending.get(d.actor_id):
                d.pending[d.actor_id] = state
                d.pending_chat_id = d.actor_id

    def _notify_completed(self, state, action, seconds=None):
        uid = state["uid"]
        reason = html.escape(state.get("reason") or "ללא סיבה")
        verb = "נחסם" if action == "ban" else "הושתק"
        suffix = f"\nמשך: {seconds // 60} דקות" if seconds is not None else ""
        user_text = f"המשתמש {verb}.\nסיבה: {reason}{suffix}"
        group_text = f"✅ המשתמש <code>{uid}</code> {verb}.\nסיבה: {reason}{suffix}"
        # A blocked user or removed group must not invalidate a completed
        # restriction. Log each failed delivery, and continue independently.
        destinations = [(uid, user_text)]
        origin = state.get("origin")
        if origin is not None:
            destinations.append((origin, group_text))
        else:
            from admin_notifications import resolve_user_management_chat
            try:
                target = resolve_user_management_chat(config_file=self.engine.path)
            except Exception:
                logging.exception("Could not resolve the restriction confirmation group.")
                target = None
            if target is not None:
                destinations.append((target, group_text))
        for target, text in destinations:
            try:
                self.engine.bot.send_message(
                    target, text, parse_mode="HTML", disable_web_page_preview=True,
                )
            except Exception:
                logging.exception("Restriction confirmation delivery failed for destination %s.", target)

    def _release(self, call, action, uid, cid, mid):
        actor = call.from_user.id
        key = (action, uid)
        answered = False
        def popup():
            nonlocal answered
            if cid != actor or answered:
                return
            answered = True
            try:
                self.engine.bot.answer_callback_query(
                    call.id, text="החסימה בוטלה בהצלחה" if action == "unban" else "ההשתקה בוטלה בהצלחה",
                    show_alert=True,
                )
            except Exception as exc:
                logging.warning("Restriction popup failed (%s).", type(exc).__name__)
        try:
            if not is_admin(actor):
                return
            if action == "unban":
                counts = self.engine.unban(uid, cid if cid < 0 else None, on_local_release=popup)
                text = "החסימה בוטלה בהצלחה"
                if counts[1]:
                    text += f". שחרור בטלגרם נכשל ב־{counts[1]} קבוצות."
            else:
                failed = self.engine.unmute(uid, on_local_release=popup)
                text = "ההשתקה בוטלה בהצלחה"
                if failed:
                    text += f". שחזור הרשאות נכשל ב־{failed} קבוצות; יתבצע ניסיון חוזר."
            if cid == actor:
                d = self.registry.get(actor)
                # Do not cancel a new wizard opened while Telegram was restoring
                # group permissions in this background job.
                if not d.pending.get(actor) and d.bot.adopt_menu(call.message):
                    d.send_menu(actor)
        except Exception as exc:
            logging.error("Asynchronous restriction release failed (%s).", type(exc).__name__)
            if cid == actor and not answered:
                try:
                    self.engine.bot.answer_callback_query(call.id, text="ביטול ההרחקה נכשל. נסה שוב.", show_alert=True)
                except Exception:
                    pass
            elif cid != actor:
                self.engine.bot.send_message(actor, "ביטול ההרחקה נכשל. בדוק את רשימת החסומים ונסה שוב.")
        finally:
            with self.inflight_lock:
                self.inflight.discard(key)

    def _prompt(self, d, uid, action, reason_action, **state):
        d.cancel_pending(d.actor_id)
        if d.on_admin_navigation:
            d.on_admin_navigation(d.actor_id, d.actor_id)
        nonce = secrets.token_hex(6)
        d.pending[d.actor_id] = dict(
            action=reason_action, uid=uid, target_user_id=uid,
            action_type=action, nonce=nonce, **state,
        )
        with self.context_lock:
            self.contexts[d.actor_id] = d.pending[d.actor_id]
        d.pending_chat_id = d.actor_id
        keyboard = InlineKeyboardMarkup()
        keyboard.add(InlineKeyboardButton("דילוג / ללא סיבה", callback_data=f"adm:restriction_skip:{nonce}"))
        keyboard.add(InlineKeyboardButton("ביטול", callback_data="adm:menu"))
        d.bot.send_message(d.actor_id, "אנא הקלד את הסיבה לחסימה/השתקה:" if action != "id" else "שלח את ה־Telegram ID של המשתמש:", reply_markup=keyboard)

    def action(self, call):
        actor = call.from_user.id
        answered = False
        if not is_admin(actor) or call.message is None:
            self.engine.bot.answer_callback_query(call.id, text="אין הרשאה.", show_alert=True)
            return
        try:
            _, action, value = call.data.split(":")
            uid = int(value)
            cid, mid = call.message.chat.id, call.message.message_id
            route = self.engine.events["messages"].get(f"{cid}:{mid}")
            if cid != actor and (cid not in self.engine.destinations() or not route or route["uid"] != uid):
                raise ValueError("הכפתור אינו שייך להודעת ניהול מוכרת.")
            self.engine._protect(uid)
            if getattr(call.message, "reply_markup", None):
                self.engine.bot.markup_cache[(cid, mid)] = call.message.reply_markup
            if action in {"unban", "unmute"}:
                # Stop Telegram's spinner before JSON writes or remote group calls.
                if cid != actor:
                    self.engine.bot.answer_callback_query(call.id, text="הבקשה התקבלה, מבטל את ההרחקה.")
                if cid == actor:
                    self.registry.get(actor).cancel_pending(actor)
                key = (action, uid)
                with self.inflight_lock:
                    if key in self.inflight:
                        if cid == actor:
                            self.engine.bot.answer_callback_query(call.id, text="ביטול ההרחקה כבר מתבצע.", show_alert=True)
                        return
                    self.inflight.add(key)
                try:
                    self.submit(self._release, call, action, uid, cid, mid)
                except Exception as exc:
                    with self.inflight_lock:
                        self.inflight.discard(key)
                    logging.error("Could not schedule restriction release (%s).", type(exc).__name__)
                    if cid == actor:
                        self.engine.bot.answer_callback_query(call.id, text="לא ניתן לבצע את הפעולה כרגע. נסה שוב.", show_alert=True)
                    else:
                        self.engine.bot.send_message(actor, "לא ניתן לבצע את הפעולה כרגע. נסה שוב.")
                return
            d = self.registry.get(actor)
            if action in {"ban", "mute"}:
                text = (
                    "שלח סיבה בפרטי או בחר דילוג כדי לחסום." if action == "ban"
                    else "המשך הזנת סיבה ומשך השתקה בפרטי."
                )
                self.engine.bot.answer_callback_query(call.id, text=text[:200], show_alert=cid == actor)
                answered = True
            if action == "ban":
                self._prompt(d, uid, "ban", "restriction_ban_reason", origin=cid if cid < 0 else None)
            elif action == "mute":
                self._prompt(d, uid, "mute", "restriction_mute_reason", origin=cid if cid < 0 else None)
            else:
                raise ValueError("פעולה לא מוכרת.")
            if cid == actor:
                self.engine._edit(cid, mid, uid, getattr(call.message, "reply_markup", None))
        except Exception as exc:
            if not answered:
                self.engine.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)
            else:
                logging.exception("Could not open private restriction editor.")

    def handle_callback(self, d, action, call):
        if action not in {"restriction_menu", "restriction_id_ban", "restriction_id_mute", "restriction_skip", "restriction_duration", "unauthorized_menu", "unauthorized_text", "unauthorized_button", "unauthorized_clear_text", "unauthorized_clear_buttons"}:
            return False
        if call.message.chat.id != call.from_user.id:
            d.bot.send_message(call.message.chat.id, "הניהול הזה זמין בפרטי בלבד.")
            return True
        actor = call.from_user.id
        self.restore_context(d)
        state = d.pending.get(actor, {})
        parts = (call.data or "").split(":")
        if action in {"restriction_skip", "restriction_duration"} and parts[:2] == ["adm", action]:
            nonce = parts[2] if action == "restriction_skip" and len(parts) == 3 else (
                parts[3] if action == "restriction_duration" and len(parts) == 4 else None
            )
            if nonce is not None and nonce != state.get("nonce"):
                d.bot.answer_callback_query(call.id, text="בחר את הכפתורים של הפעולה הנוכחית.", show_alert=True)
                return True
        if action == "restriction_menu":
            d.cancel_pending(actor)
            cfg = load_config(self.engine.path)
            rows = [("🚫 חסימה לפי ID", "adm:restriction_id_ban"), ("🔇 השתקה לפי ID", "adm:restriction_id_mute")]
            body = "🚫 חסומים ומורחקים\n"
            for field, name, op in (("banned_users", "חסום", "unban"), ("muted_users", "מושתק", "unmute")):
                for uid, record in list(cfg[field].items())[:20]:
                    body += f"\n{uid} — {name}: {record['reason'][:80]}"
                    rows.append((f"בטל {name}: {uid}", f"mod:{op}:{uid}"))
            rows.append(("🔙 חזרה לתפריט ראשי", "adm:menu"))
            from admin_menus import build_menu
            d.bot.send_message(actor, body, reply_markup=build_menu(rows))
        elif action.startswith("restriction_id_"):
            self._prompt(d, None, "id", "restriction_id", kind="ban" if action.endswith("ban") else "mute")
        elif action == "restriction_skip":
            state = d.pending.get(actor, {})
            self.handle_input(d, state.get("action"), state, call.message, "ללא סיבה")
        elif action == "restriction_duration":
            state = d.pending.get(actor, {})
            if state.get("action") != "restriction_duration":
                if not state:
                    self.handle_callback(d, "restriction_menu", call)
                else:
                    d.bot.answer_callback_query(call.id, text="פתח פעולת השתקה חדשה.", show_alert=True)
            else:
                value = call.data.split(":")[2]
                if value == "custom":
                    d.bot.prompt(actor, "שלח משך מותאם, למשל 90m או 2h.")
                else:
                    self._finish_mute(d, state, duration(value))
        elif action == "unauthorized_menu":
            d.cancel_pending(actor)
            from admin_menus import build_menu
            d.bot.send_message(actor, "הגדרות הודעת גישה — ניתן לשמור טקסט, כפתורים או שניהם.", reply_markup=build_menu([
                ("✏️ עריכת טקסט", "adm:unauthorized_text"), ("🔘 עריכת כפתור וקישור", "adm:unauthorized_button"),
                ("ללא טקסט", "adm:unauthorized_clear_text"), ("ללא כפתורים", "adm:unauthorized_clear_buttons"), ("🔙 חזרה", "adm:access_menu")]))
        elif action in {"unauthorized_text", "unauthorized_button"}:
            d.pending[actor] = {"action": action}
            d.pending_chat_id = actor
            d.bot.prompt(actor, "שלח טקסט הודעת גישה." if action.endswith("text") else "שלח שם הכפתור | קישור https://t.me/username")
        else:
            with _LOCK:
                cfg = load_config(self.engine.path)
                if action.endswith("text"):
                    if not cfg["unauthorized_buttons"]:
                        d.bot.send_message(actor, "הוסף כפתור לפני הסרת הטקסט.")
                        return True
                    cfg["unauthorized_message"] = ""
                else:
                    if not cfg["unauthorized_message"]:
                        d.bot.send_message(actor, "הוסף טקסט לפני הסרת הכפתורים.")
                        return True
                    cfg["unauthorized_buttons"] = []
                save_json(self.engine.path, cfg)
            d.bot.send_message(actor, "ההגדרות נשמרו.")
        return True

    def _finish_mute(self, d, state, seconds):
        record, counts = self.engine.mute(state["uid"], seconds, state["reason"], state.get("origin"))
        d.cancel_pending(d.actor_id)
        self.cancel_context(d.actor_id)
        self._notify_completed(state, "mute", seconds)
        send_enforcement_confirmation(
            self.engine.bot, d.actor_id,
            f"המשתמש הושתק. הוחל ב־{counts[0]} קבוצות; נכשל ב־{counts[1]}.",
            reason_message=state.get("reason_message"),
        )
        d.send_menu(d.actor_id)

    def handle_input(self, d, action, state, message, text):
        if not action or not (action.startswith("restriction_") or action.startswith("unauthorized_")):
            return False
        actor = d.actor_id
        try:
            if action == "restriction_id":
                uid = int(text)
                self.engine._protect(uid)
                if state["kind"] == "ban":
                    self._prompt(d, uid, "ban", "restriction_ban_reason", origin=None)
                else:
                    self._prompt(d, uid, "mute", "restriction_mute_reason", origin=None)
            elif action == "restriction_ban_reason":
                if d.pending.get(actor) != state:
                    raise ValueError("הפעולה התיישנה או בוטלה.")
                from private_admin_protection import _reason_message_id
                state["reason_message_id"] = _reason_message_id(actor, message)
                with self.engine.operation_lock:
                    if self.engine.state(state["uid"])[0] is not None:
                        raise ValueError("המשתמש כבר חסום.")
                    _, counts = self.engine.ban(state["uid"], text[:1000], state.get("origin"))
                d.cancel_pending(actor)
                self.cancel_context(actor)
                state["reason"] = text[:1000]
                self._notify_completed(state, "ban")
                send_enforcement_confirmation(
                    self.engine.bot, actor,
                    f"המשתמש נחסם. הוחל ב־{counts[0]} קבוצות; נכשל ב־{counts[1]}.",
                    reason_message=message,
                )
                d.send_menu(actor)
            elif action == "restriction_mute_reason":
                if d.pending.get(actor) != state:
                    raise ValueError("הפעולה התיישנה או בוטלה.")
                from private_admin_protection import _reason_message_id
                reason_id = _reason_message_id(actor, message)
                state.update(
                    action="restriction_duration", reason=text[:1000],
                    reason_message_id=reason_id,
                    reason_message=message if reason_id else None,
                )
                from admin_menus import build_menu
                nonce = state["nonce"]
                d.bot.send_message(actor, "בחר משך השתקה:", reply_markup=build_menu([
                    ("שעה", f"adm:restriction_duration:1h:{nonce}"), ("24 שעות", f"adm:restriction_duration:24h:{nonce}"),
                    ("7 ימים", f"adm:restriction_duration:7d:{nonce}"), ("זמן מותאם", f"adm:restriction_duration:custom:{nonce}"), ("ביטול", "adm:menu")]))
            elif action == "restriction_duration":
                self._finish_mute(d, state, duration(text))
            else:
                with _LOCK:
                    cfg = load_config(self.engine.path)
                    if action == "unauthorized_text":
                        if not text.strip() or len(text) > 4096:
                            raise ValueError("טקסט לא תקין.")
                        cfg["unauthorized_message"] = text
                    else:
                        label, sep, url = text.partition("|")
                        if not sep or not label.strip() or len(label.strip()) > 64 or not is_link_input(url.strip()):
                            raise ValueError("שלח שם כפתור | קישור HTTP/HTTPS.")
                        cfg["unauthorized_buttons"] = [{"text": label.strip(), "url": url.strip()}]
                    save_json(self.engine.path, cfg)
                d.cancel_pending(actor)
                d.bot.send_message(actor, "הודעת הגישה נשמרה.")
                d.send_menu(actor)
        except (ValueError, OSError) as exc:
            d.bot.send_message(actor, str(exc))
        return True
