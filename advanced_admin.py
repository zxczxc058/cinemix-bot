"""Admin conversations for maintenance, hourly activity, and night settings."""

from pathlib import Path
import secrets

from admin_menus import MISSING_CONTENT_MESSAGES_MENU, SYSTEM_MENU, build_menu
from bot_storage import ADMIN_USER_ID, is_admin, load_user_ids
from group_settings import (
    load_group_settings, toggle_night_lock, update_night_hours,
    update_night_message, validate_time,
)
from system_features import (
    ACTIVITY_FILE, SYSTEM_SETTINGS_FILE, hourly_activity_report,
    load_system_settings, toggle_maintenance, update_maintenance_message,
)
from management_config import (
    DEFAULT_MISSING_CONTENT_MESSAGES, _LOCK, load_config, save_json,
)


class AdvancedAdmin:
    def __init__(self, dashboard, system_file=SYSTEM_SETTINGS_FILE,
                 activity_file=ACTIVITY_FILE, night_manager=None):
        self.dashboard = dashboard
        self.bot = dashboard.bot
        self.system_file = Path(system_file)
        self.activity_file = Path(activity_file)
        self.night_manager = night_manager

    def _system_buttons(self):
        return [("✏️ ניהול הודעות אוטומטיות", "adm:system_texts"), *SYSTEM_MENU]

    def send_system_texts_menu(self, chat_id):
        from system_messages import MESSAGES
        self.dashboard._send_submenu(
            chat_id, "✏️ ניהול הודעות מערכת — בחר הודעה לעריכה",
            [(label, f"adm:sysmsg_edit_{key}") for key, (label, _) in MESSAGES.items()],
            back_action="adm:system_menu", back_label="🔙 חזרה להודעות מערכת",
        )

    def send_system_menu(self, chat_id):
        self.dashboard._send_submenu(
            chat_id, "הודעות מערכת וברירת מחדל", self._system_buttons()
        )

    def send_missing_content_messages_menu(self, chat_id):
        self.dashboard._send_submenu(
            chat_id,
            "⚙️ הודעות מערכת\n"
            "אפשר לערוך את נוסחי העדכון שנשלחים למשתמשים שביקשו תוכן.\n"
            "המשתנה {CONTENT_NAME} יוחלף בשם הסרט או הסדרה.",
            MISSING_CONTENT_MESSAGES_MENU,
        )

    def send_night_menu(self, chat_id):
        settings = load_group_settings(self.dashboard.group_settings_file)["night_lock"]
        status = "פעיל" if settings["enabled"] else "כבוי"
        self.dashboard._send_submenu(
            chat_id,
            f"מצב לילה — שעון ישראל\nסגירה: {settings['close_time']} | פתיחה: {settings['open_time']}\n"
            "נדרשת הרשאת הגבלת משתמשים. ההגדרות חלות על כל הקבוצות הרשומות.",
            [
                (f"🔘 הפעלה / כיבוי מצב לילה — {status}", "adm:night_toggle"),
                ("⏰ הגדרת שעות פעילות", "adm:night_hours"),
                ("✏️ עריכת הודעת סגירה", "adm:night_close_message"),
                ("✏️ עריכת הודעת פתיחה", "adm:night_open_message"),
            ],
        )

    def _pending(self, chat_id, action, **fields):
        d = self.dashboard
        d.pending_chat_id = chat_id
        d.pending[ADMIN_USER_ID] = {"action": action, **fields}

    def _wake_night(self):
        if self.night_manager:
            self.night_manager.wake()

    def handle_callback(self, action, call):
        if not is_admin(call.from_user.id):
            return False
        d = self.dashboard
        chat_id = call.message.chat.id
        if (action == "system_texts" or action.startswith("sysmsg_edit_")) and (
            call.message.chat.type != "private" or chat_id != call.from_user.id
        ):
            return True
        try:
            if action == "system_texts":
                d.cancel_pending(ADMIN_USER_ID)
                self.send_system_texts_menu(chat_id)
            elif action.startswith("sysmsg_edit_"):
                from system_messages import MESSAGES, message_text
                key = action.removeprefix("sysmsg_edit_")
                if key not in MESSAGES or call.message.chat.type != "private":
                    return False
                current = message_text(key, d.management.config_file, d.commands_file)
                self._pending(chat_id, "system_text_edit", key=key)
                d._send_prompt(
                    chat_id, f"{MESSAGES[key][0]}\n\nהנוסח הנוכחי:\n{current}\n\n"
                    "שלח את הנוסח החדש. לביטול שלח „ביטול”.",
                )
            elif action == "maintenance_toggle":
                d.cancel_pending(ADMIN_USER_ID)
                toggle_maintenance(self.system_file)
                d.bot_settings_admin.menu(chat_id)
                token = secrets.token_hex(8)
                self._pending(chat_id, "maintenance_offer", token=token)
                self.bot.send_message(
                    chat_id,
                    "האם לשלוח הודעת עדכון לכל המשתמשים?",
                    reply_markup=build_menu([
                        ("כן", f"adm:maintenance_yes:{token}"),
                        ("לא", f"adm:maintenance_no:{token}"),
                    ]),
                )
            elif action in {"maintenance_yes", "maintenance_no"}:
                token = (call.data or "").split(":")[-1]
                state = d.pending.get(ADMIN_USER_ID, {})
                if (
                    state.get("action") != "maintenance_offer"
                    or state.get("token") != token or d.pending_chat_id != chat_id
                ):
                    self.bot.send_message(chat_id, "הבחירה התיישנה. פתח את תפריט המערכת מחדש.")
                elif action == "maintenance_no":
                    d.cancel_pending(ADMIN_USER_ID)
                    d.bot_settings_admin.menu(chat_id)
                else:
                    self._pending(chat_id, "maintenance_broadcast")
                    count = len(load_user_ids(d.users_file))
                    d._send_prompt(
                        chat_id,
                        f"שלח הודעת עדכון מותאמת אישית. הטקסט הבא יישלח מיד ל־{count} "
                        "המשתמשים הפרטיים הרשומים, ללא הקבוצות. לביטול שלח „ביטול”.",
                    )
            elif action == "maintenance_edit":
                self._pending(chat_id, "maintenance_message")
                d._send_prompt(chat_id, "שלח את הודעת התחזוקה החדשה. לביטול שלח „ביטול”.")
            elif action == "link_error_popup_edit":
                current = load_config(d.management.config_file)["link_error_popup_text"]
                self._pending(
                    chat_id, "link_error_popup_text",
                    return_to_bot_settings=True,
                )
                d._send_prompt(
                    chat_id,
                    "שלח את הטקסט שיוצג בחלונית הקופצת אחרי דיווח על קישור תקול "
                    "(עד 200 תווים).\n"
                    f"הנוסח הנוכחי:\n{current}\n\nלביטול שלח „ביטול”.",
                )
            elif action == "missing_messages":
                d.cancel_pending(ADMIN_USER_ID)
                self.send_missing_content_messages_menu(chat_id)
            elif action in {"missing_message_uploaded", "missing_message_unavailable"}:
                key = "uploaded" if action == "missing_message_uploaded" else "unavailable"
                current = load_config(d.management.config_file)["missing_content_messages"][key]
                self._pending(chat_id, "missing_content_response", key=key)
                d._send_prompt(
                    chat_id,
                    f"נוסח נוכחי:\n{current}\n\n"
                    "שלח את הנוסח החדש. אפשר להשתמש ב־{CONTENT_NAME} כדי להציג את שם התוכן. "
                    "לביטול שלח „ביטול”.",
                )
            elif action == "missing_messages_reset":
                with _LOCK:
                    config = load_config(d.management.config_file)
                    config["missing_content_messages"] = dict(DEFAULT_MISSING_CONTENT_MESSAGES)
                    save_json(d.management.config_file, config)
                d.cancel_pending(ADMIN_USER_ID)
                d.bot.answer_callback_query(
                    call.id, text="✅ הודעות המערכת אופסו לברירת המחדל."
                )
                self.send_missing_content_messages_menu(chat_id)
            elif action == "hourly_activity":
                d.cancel_pending(ADMIN_USER_ID)
                d._send_temporary_stat(chat_id, hourly_activity_report(self.activity_file))
            elif action == "night_menu":
                d.cancel_pending(ADMIN_USER_ID)
                self.send_night_menu(chat_id)
            elif action == "night_toggle":
                d.cancel_pending(ADMIN_USER_ID)
                toggle_night_lock(d.group_settings_file)
                self._wake_night()
                self.send_night_menu(chat_id)
            elif action == "night_hours":
                self._pending(chat_id, "night_hours_close")
                d._send_prompt(chat_id, "שלח את שעת הסגירה בשעון ישראל בפורמט HH:MM, למשל 23:00. לביטול שלח „ביטול”.")
            elif action in {"night_close_message", "night_open_message"}:
                key = "close_message" if action == "night_close_message" else "open_message"
                self._pending(chat_id, "night_message", key=key)
                d._send_prompt(chat_id, "שלח את הודעת הסגירה או הפתיחה החדשה. לביטול שלח „ביטול”.")
            else:
                return False
        except (OSError, ValueError) as exc:
            self.bot.send_message(chat_id, f"לא ניתן לקרוא או לשמור את ההגדרות: {exc}")
        return True

    def handle_input(self, action, state, text, trimmed, chat_id, message=None):
        d = self.dashboard
        try:
            if action == "system_text_edit":
                if message is None or message.chat.type != "private":
                    return False
                from system_messages import update_message
                update_message(state["key"], text, d.management.config_file, d.commands_file)
                d.cancel_pending(ADMIN_USER_ID)
                self.send_system_texts_menu(chat_id)
            elif action == "maintenance_message":
                update_maintenance_message(text, self.system_file)
                d.cancel_pending(ADMIN_USER_ID)
                self.bot.send_message(chat_id, "הודעת התחזוקה נשמרה.")
                self.send_system_menu(chat_id)
            elif action == "link_error_popup_text":
                value = text.strip()
                if not value or len(value) > 200:
                    raise ValueError("הטקסט חייב להכיל 1–200 תווים.")
                with _LOCK:
                    config = load_config(d.management.config_file)
                    config["link_error_popup_text"] = value
                    save_json(d.management.config_file, config)
                d.cancel_pending(ADMIN_USER_ID)
                if state.get("return_to_bot_settings"):
                    d.bot_settings_admin.menu(chat_id)
                else:
                    self.send_system_menu(chat_id)
            elif action == "maintenance_broadcast":
                d.cancel_pending(ADMIN_USER_ID)
                d._broadcast(chat_id, text, include_groups=False, source_message=message)
            elif action == "missing_content_response":
                key = state.get("key")
                if key not in DEFAULT_MISSING_CONTENT_MESSAGES:
                    raise ValueError("לא ניתן לזהות איזו הודעה לערוך.")
                if not text.strip() or len(text) > 4096:
                    raise ValueError("ההודעה חייבת להכיל 1–4096 תווים.")
                with _LOCK:
                    config = load_config(d.management.config_file)
                    config["missing_content_messages"][key] = text
                    save_json(d.management.config_file, config)
                d.cancel_pending(ADMIN_USER_ID)
                self.send_missing_content_messages_menu(chat_id)
            elif action == "night_hours_close":
                self._pending(chat_id, "night_hours_open", close_time=validate_time(trimmed))
                d._send_prompt(chat_id, "שלח את שעת הפתיחה בפורמט HH:MM, למשל 07:00. לביטול שלח „ביטול”.")
            elif action == "night_hours_open":
                update_night_hours(state["close_time"], trimmed, d.group_settings_file)
                d.cancel_pending(ADMIN_USER_ID)
                self._wake_night()
                self.bot.send_message(chat_id, "שעות מצב הלילה נשמרו.")
                self.send_night_menu(chat_id)
            elif action == "night_message":
                update_night_message(state["key"], text, d.group_settings_file)
                d.cancel_pending(ADMIN_USER_ID)
                self.bot.send_message(chat_id, "הודעת מצב הלילה נשמרה.")
                self.send_night_menu(chat_id)
            else:
                return False
        except (OSError, ValueError) as exc:
            self.bot.send_message(chat_id, f"לא ניתן לשמור: {exc}\nנסה שוב או שלח „ביטול”.")
        return True
