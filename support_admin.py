"""Support settings, restricted to the configured sole bot administrator."""

from pathlib import Path

from admin_menus import SUPPORT_ADMIN_ACTIONS, build_menu
from bot_storage import ADMIN_USER_ID, is_admin
from support_storage import (
    SUPPORT_SETTINGS_FILE, SUPPORT_THREADS_FILE, load_support_settings,
    open_support_requests, set_support_chat, update_support_confirmation,
    update_support_reminders,
)


class SupportAdmin:
    def __init__(self, dashboard, settings_file=SUPPORT_SETTINGS_FILE,
                 threads_file=SUPPORT_THREADS_FILE):
        self.d = dashboard
        self.path = Path(settings_file)
        self.threads_path = Path(threads_file)

    def open_count(self):
        return len(open_support_requests(self.path, self.threads_path))

    def menu(self, chat_id):
        if chat_id > 0 and is_admin(chat_id):
            self.d._send_submenu(
                chat_id,
                "💬 תמיכה ופניות",
                [
                    (
                        f"{label} ({self.open_count()})" if callback == "adm:support_open:0" else label,
                        callback,
                    )
                    for label, callback in SUPPORT_ADMIN_ACTIONS
                ],
                row_width=2,
            )

    def send_open_requests(self, chat_id, page=0):
        ids = open_support_requests(self.path, self.threads_path)
        last = max(0, (len(ids) - 1) // 20)
        page = max(0, min(page, last))
        group = load_support_settings(self.path)["support_chat_id"]
        lines = [f"פניות פתוחות: {len(ids)} — עמוד {page + 1} מתוך {last + 1}"]
        lines.append("הרשימה כוללת פניות חדשות וכותרות ישנות שצורפו במפורש למעקב; מדיה אינה נספרת בנפרד.")
        lines.extend(f"• פנייה #{mid} (קבוצה {group})" for mid in ids[page * 20:(page + 1) * 20])
        if group is None:
            lines.append("קבוצת התמיכה טרם הוגדרה.")
        elif not ids:
            lines.append("אין פניות פתוחות.")
        buttons = []
        if page:
            buttons.append(("הקודם", f"adm:support_open:{page - 1}"))
        if page < last:
            buttons.append(("הבא", f"adm:support_open:{page + 1}"))
        buttons.append(("רענון", f"adm:support_open:{page}"))
        buttons.append(("איך לצרף פנייה ישנה למעקב", "adm:support_track_help"))
        self.d.bot.send_message(chat_id, "\n".join(lines), reply_markup=build_menu(buttons, back=True))

    def set_chat(self, message):
        if not is_admin(message.from_user.id):
            return
        if message.chat.type not in {"group", "supergroup"}:
            self.d.bot.send_message(message.chat.id, "יש לשלוח /set_support_chat בתוך קבוצת התמיכה הרצויה.")
            return
        try:
            set_support_chat(message.chat.id, self.path)
        except (OSError, ValueError) as exc:
            self.d.bot.send_message(message.chat.id, f"לא ניתן לשמור את קבוצת התמיכה: {exc}")
            return
        self.d.bot.send_message(message.chat.id, "קבוצה זו הוגדרה כקבוצת התמיכה היחידה.")

    def handle_callback(self, action, call):
        if not is_admin(call.from_user.id):
            return False
        reminder_actions = {"support_reminders", "support_reminders_toggle", "support_reminders_minutes"}
        if action in reminder_actions | {"support_open", "support_track_help"} and call.from_user.id != ADMIN_USER_ID:
            return True
        if action in reminder_actions | {"support_track_help"} and (
            call.message is None or call.message.chat.id != ADMIN_USER_ID
            or getattr(call.message.chat, "type", "private") != "private"
        ):
            return True
        d, chat_id = self.d, call.message.chat.id
        if action in reminder_actions:
            d.cancel_pending(ADMIN_USER_ID)
            try:
                if action == "support_reminders_toggle":
                    settings = load_support_settings(self.path)
                    update_support_reminders(enabled=not settings["reminders_enabled"], path=self.path)
                if action == "support_reminders_minutes":
                    d.pending_chat_id = chat_id
                    d.pending[ADMIN_USER_ID] = {"action": "support_reminders_minutes"}
                    d._send_prompt(chat_id, "שלח זמן המתנה בדקות (1–43200). לביטול שלח „ביטול”.")
                else:
                    self.send_reminder_menu(chat_id)
            except (OSError, ValueError):
                d.bot.send_message(chat_id, "לא ניתן לעדכן את התזכורות. נסה שוב.")
        elif action == "support_track_help":
            d.cancel_pending(ADMIN_USER_ID)
            d.bot.send_message(
                chat_id,
                "צירוף פנייה ישנה למעקב הוא פעולה אופציונלית למנהל הראשי בלבד.\n"
                "בקבוצת התמיכה הנוכחית, מצא את כותרת הפנייה המקורית ששלח הבוט "
                "(מתחילה ב־„פנייה חדשה לתמיכה”), והשב לה עם /track_support_header בלבד.\n"
                "בחר את הכותרת בלבד, לא תמונה, הודעה קולית או העתק של תוכן הפנייה. "
                "אין לצרף כמה הודעות מאותה פנייה.\n"
                "תתווסף פנייה פתוחה אחת, בלי לשנות מסלולי מענה או לשמור תוכן פרטי. "
                "צירוף חוזר אינו משנה מצב ואינו מגדיל את הספירה. "
                "לפנייה הישנה לא יומצא זמן יצירה, ולכן היא לא תקבל תזכורות אוטומטיות.",
            )
        elif action == "set_support_chat":
            # Pass only the authenticated sender, not the bot-authored message's sender.
            from types import SimpleNamespace
            self.set_chat(SimpleNamespace(from_user=call.from_user, chat=call.message.chat))
        elif action == "support_menu":
            d.cancel_pending(ADMIN_USER_ID)
            self.menu(chat_id)
        elif action == "support_confirmation":
            d.pending_chat_id = chat_id
            d.pending[ADMIN_USER_ID] = {"action": "support_confirmation"}
            d._send_prompt(chat_id, "שלח את הודעת אישור התמיכה החדשה. היא תימחק לאחר 10 שניות. לביטול שלח „ביטול”.")
        elif action == "support_open":
            d.cancel_pending(ADMIN_USER_ID)
            try:
                parts = (call.data or "").split(":")
                page = int(parts[2]) if len(parts) == 3 else 0
                self.send_open_requests(chat_id, page)
            except (OSError, ValueError):
                d.bot.send_message(chat_id, "לא ניתן לקרוא את רשימת הפניות. נסה שוב.")
        else:
            return False
        return True

    def send_reminder_menu(self, chat_id):
        if self.d.actor_id != ADMIN_USER_ID or chat_id != ADMIN_USER_ID:
            return
        settings = load_support_settings(self.path)
        status = "פעילות" if settings["reminders_enabled"] else "כבויות"
        self.d._send_submenu(
            chat_id,
            f"תזכורות תמיכה — {status}\nזמן המתנה: {settings['reminder_minutes']} דקות.\n"
            "תזכורת אחת לכל פנייה פתוחה, בפרטי למנהל בלבד וללא תוכן הפנייה. "
            "הבדיקה מתבצעת בכל דקה ורק עבור קבוצת התמיכה הנוכחית.\n"
            "סימון כטופלה מפסיק תזכורות. פתיחה מחדש מאפשרת תזכורת חדשה "
            "לפי זמן היצירה המקורי. פניות ישנות ללא זמן יצירה אינן נכללות.",
            [
                ("הפעלה / כיבוי תזכורות", "adm:support_reminders_toggle"),
                ("הגדרת זמן המתנה בדקות", "adm:support_reminders_minutes"),
            ],
        )

    def handle_input(self, action, text, chat_id):
        if action == "support_reminders_minutes":
            if self.d.actor_id != ADMIN_USER_ID or chat_id != ADMIN_USER_ID:
                return True
            try:
                update_support_reminders(minutes=int(text.strip()), path=self.path)
            except (OSError, ValueError):
                self.d.bot.send_message(chat_id, "לא ניתן לשמור. שלח מספר דקות שלם בין 1 ל־43200.")
                return True
            self.d.cancel_pending(ADMIN_USER_ID)
            self.d.bot.send_message(chat_id, "זמן ההמתנה לתזכורות נשמר.")
            self.send_reminder_menu(chat_id)
            return True
        if action != "support_confirmation":
            return False
        try:
            update_support_confirmation(text, self.path)
        except (OSError, ValueError) as exc:
            self.d.bot.send_message(chat_id, f"לא ניתן לשמור: {exc}")
            return True
        self.d.cancel_pending(ADMIN_USER_ID)
        self.d.bot.send_message(chat_id, "הודעת אישור התמיכה נשמרה.")
        self.menu(chat_id)
        return True
