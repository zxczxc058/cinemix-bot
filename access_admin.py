"""Owner-only passcode configuration and authorization removal menus."""

from pathlib import Path
import secrets

from access_control import ACCESS_FILE, change_passcode, load_access, revoke_access, toggle_access
from bot_storage import ADMIN_USER_ID, get_start_reply, is_admin
from management_config import load_config, normalize_button_link, save_json
from system_features import write_json


class AccessAdmin:
    PER_PAGE = 20

    def __init__(self, dashboard, access_file=ACCESS_FILE):
        self.d = dashboard
        self.path = Path(access_file)
        self.config_path = Path(self.d.commands_file).parent / "config.json"
        self.targets = {}

    def menu(self, chat_id):
        settings = load_access(self.path)
        config = load_config(self.config_path)
        status = "פעיל" if settings["enabled"] else "כבוי"
        note = "\nטרם הוגדר קוד: רק המנהל יכול להיכנס." if not settings["code_hash"] and settings["enabled"] else ""
        link_status = "מוגדר" if config["access_prompt_button_url"] else "לא הוגדר"
        support_status = (
            "מוצג" if settings["standard_welcome_support_button_enabled"] else "מוסתר"
        )
        self.d._send_submenu(
            chat_id, "ניהול קוד גישה" + note,
            [
                (f"🔘 הפעלה / כיבוי דרישת קוד — {status}", "adm:access_toggle"),
                ("✏️ שינוי קוד הגישה הראשי", "adm:access_code"),
                ("❌ עריכת הודעת קוד שגוי", "adm:access_invalid_code_edit"),
                (f"🔗 הגדר קישור לכפתור לקבלת קוד — {link_status}", "adm:access_prompt_link"),
                ("✏️ עריכת הודעת ברוכים הבאים (מנוי רגיל)", "adm:standard_welcome_edit"),
                (f"💬 כפתור פנייה למנהל / תמיכה — {support_status}", "adm:standard_support_button_toggle"),
                ("⚙️ הגדרות הודעת גישה", "adm:unauthorized_menu"),
                ("👥 ניהול משתמשים מורשים", "adm:access_users:0"),
            ],
        )

    def users(self, chat_id, page=0):
        users = load_access(self.path)["authorized_users"]
        last = max(0, (len(users) - 1) // self.PER_PAGE)
        page = max(0, min(page, last))
        buttons = []
        for user_id in users[page * self.PER_PAGE : (page + 1) * self.PER_PAGE]:
            token = secrets.token_hex(8)
            self.targets[token] = user_id
            buttons.append((f"הסרת גישה: {user_id}", f"adm:access_revoke:{token}"))
        if page:
            buttons.append(("הקודם", f"adm:access_users:{page - 1}"))
        if page < last:
            buttons.append(("הבא", f"adm:access_users:{page + 1}"))
        buttons.append(("🔙 חזרה לניהול קוד", "adm:access_menu"))
        self.d._send_submenu(
            chat_id,
            f"משתמשים מורשים: {len(users)} | עמוד {page + 1}/{last + 1}\n"
            "שינוי הקוד אינו מסיר אישורים קיימים. כשהדרישה כבויה, הסרת הרשאה תחול לאחר הפעלתה.",
            buttons,
        )

    def handle_callback(self, action, call):
        if not is_admin(call.from_user.id):
            return False
        d, chat_id = self.d, call.message.chat.id
        parts = call.data.split(":")
        try:
            if action == "access_menu":
                d.cancel_pending(ADMIN_USER_ID)
                self.menu(chat_id)
            elif action == "access_toggle":
                toggle_access(self.path)
                d.cancel_pending(ADMIN_USER_ID)
                self.menu(chat_id)
            elif action == "access_code":
                if getattr(call.message.chat, "type", "private") != "private":
                    d.bot.send_message(chat_id, "שנה את קוד הגישה בצ׳אט פרטי עם הבוט בלבד.")
                    return True
                d.pending_chat_id = chat_id
                d.pending[ADMIN_USER_ID] = {"action": "access_code"}
                d._send_prompt(
                    chat_id,
                    "שלח קוד חדש באורך 4–128 תווים. הוא יישמר כגיבוב, והודעת הקוד תימחק לאחר העיבוד. "
                    "ההרשאות הקיימות יישמרו. לביטול שלח „ביטול”.",
                )
            elif action == "access_invalid_code_edit":
                if getattr(call.message.chat, "type", "private") != "private":
                    return True
                d.pending_chat_id = chat_id
                d.pending[ADMIN_USER_ID] = {
                    "action": "access_invalid_code_message",
                    "return_to": "adm:access_menu",
                }
                d._send_prompt(
                    chat_id,
                    "שלח כעת את הטקסט שיוצג למשתמש שמזין קוד שגוי (ההודעה תמחק אוטומטית אחרי 5 שניות):",
                )
            elif action == "access_prompt_link":
                if getattr(call.message.chat, "type", "private") != "private":
                    d.bot.send_message(chat_id, "שנה את קישור קבלת הקוד בצ׳אט פרטי עם הבוט בלבד.")
                    return True
                d.pending_chat_id = chat_id
                d.pending[ADMIN_USER_ID] = {"action": "access_prompt_link"}
                d._send_prompt(
                    chat_id,
                    "שלח שם משתמש בטלגרם (למשל @username) או קישור HTTP/HTTPS "
                    "שיופיע בכפתור „לחצו כאן” בהודעת קוד הגישה. לביטול שלח „ביטול”.",
                )
            elif action == "standard_welcome_edit":
                if getattr(call.message.chat, "type", "private") != "private":
                    d.bot.send_message(chat_id, "עריכת הודעת הברוכים הבאים זמינה בפרטי בלבד.")
                    return True
                settings = load_access(self.path)
                current = settings.get("standard_welcome_text")
                if current is None:
                    current = get_start_reply(d.commands_file).text
                d.pending_chat_id = chat_id
                d.pending[ADMIN_USER_ID] = {"action": "standard_welcome_text"}
                d._send_prompt(
                    chat_id,
                    "שלח את הודעת הברוכים הבאים למנוי רגיל. אפשר להשתמש ב-Markdown/HTML.\n"
                    f"הנוסח הנוכחי:\n{current}\n\n"
                    "שלח „ברירת מחדל” כדי להשתמש שוב בהודעת הפתיחה הרגילה, או „ביטול” ליציאה.",
                )
            elif action == "standard_support_button_toggle":
                if getattr(call.message.chat, "type", "private") != "private":
                    d.bot.send_message(chat_id, "הגדרות הודעת הברוכים הבאים זמינות בפרטי בלבד.")
                    return True
                settings = load_access(self.path)
                settings["standard_welcome_support_button_enabled"] = not settings[
                    "standard_welcome_support_button_enabled"
                ]
                write_json(self.path, settings)
                self.menu(chat_id)
            elif action == "access_users":
                self.users(chat_id, int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0)
            elif action in {"access_revoke", "access_confirm"} and len(parts) == 3:
                user_id = self.targets.get(parts[2])
                if user_id is None:
                    d.bot.send_message(chat_id, "הרשימה התיישנה. פתח אותה מחדש.")
                elif action == "access_revoke":
                    d._send_submenu(chat_id, f"להסיר את הגישה של {user_id}?", [
                        ("כן, להסיר גישה", f"adm:access_confirm:{parts[2]}"),
                        ("ביטול", "adm:access_users:0"),
                    ])
                else:
                    revoke_access(user_id, self.path)
                    self.targets.pop(parts[2], None)
                    d.bot.send_message(chat_id, "הגישה הוסרה.")
                    self.users(chat_id)
            else:
                return False
        except (OSError, ValueError) as exc:
            d.bot.send_message(chat_id, f"לא ניתן לעדכן את הגישה: {exc}")
        return True

    def handle_input(self, action, text, chat_id):
        if action == "access_invalid_code_message":
            from system_messages import update_message
            try:
                update_message("invalid_code", text, self.config_path)
            except (OSError, ValueError) as exc:
                self.d.bot.send_message(chat_id, f"לא ניתן לשמור: {exc}")
                return True
            self.d.cancel_pending(ADMIN_USER_ID)
            self.d.bot.send_message(chat_id, "הודעת הקוד השגוי נשמרה בהצלחה.")
            self.menu(chat_id)
            return True
        if action == "standard_welcome_text":
            try:
                settings = load_access(self.path)
                value = text.strip()
                settings["standard_welcome_text"] = (
                    None if value.casefold() == "ברירת מחדל" else text
                )
                if settings["standard_welcome_text"] is not None and not value:
                    raise ValueError("הודעת הברוכים הבאים אינה יכולה להיות ריקה.")
                write_json(self.path, settings)
                config_path = self.d.management.config_file
                config = load_config(config_path)
                if settings["standard_welcome_text"] is None:
                    config.setdefault("system_messages", {}).pop("standard_code_success", None)
                else:
                    config.setdefault("system_messages", {})["standard_code_success"] = text
                save_json(config_path, config)
            except (OSError, ValueError) as exc:
                from admin_ui import collect_private_feedback

                collect_private_feedback(chat_id, f"❌ לא ניתן לשמור את ההודעה: {exc}")
                return True
            self.d.cancel_pending(ADMIN_USER_ID)
            self.menu(chat_id)
            return True
        if action == "access_prompt_link":
            try:
                link = normalize_button_link(text)
                config = load_config(self.config_path)
                config["access_prompt_button_url"] = link
                save_json(self.config_path, config)
            except (OSError, ValueError) as exc:
                from admin_ui import collect_private_feedback

                collect_private_feedback(chat_id, f"❌ לא ניתן לשמור את הקישור: {exc}")
                return True
            self.d.cancel_pending(ADMIN_USER_ID)
            self.menu(chat_id)
            return True
        if action != "access_code":
            return False
        try:
            change_passcode(text, self.path)
        except (OSError, ValueError) as exc:
            self.d.bot.send_message(chat_id, f"לא ניתן לשמור: {exc}")
            return True
        self.d.cancel_pending(ADMIN_USER_ID)
        self.d.bot.send_message(chat_id, "קוד הגישה עודכן.")
        self.menu(chat_id)
        return True
