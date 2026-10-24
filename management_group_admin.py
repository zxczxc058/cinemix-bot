"""Private admin controls that reuse existing routing and message settings."""

import logging
from types import SimpleNamespace

from bot_storage import is_admin
from management_config import _LOCK, load_config, save_json
from admin_notifications import resolve_user_management_chat, resolve_admin_chat
from system_messages import MESSAGES, message_text, update_message


class ManagementGroupAdmin:
    def __init__(self, dashboard):
        self.d = dashboard

    @property
    def queue(self):
        return getattr(self.d.raw_bot, "live_chat_queue", None)

    def menu(self, chat_id):
        target = resolve_user_management_chat(config_file=self.d.management.config_file)
        logs = resolve_admin_chat(config_file=self.d.management.config_file)
        separate = load_config(self.d.management.config_file)["ADMIN_COMMANDS_CHAT_ID"]
        self.d._send_submenu(
            chat_id,
            "⚙️ ניהול קבוצת ניהול\n\n"
            f"יעד נוכחי: {target if target is not None else 'לא הוגדר'}\n"
            f"קבוצת פקודות ולוגים: {logs if separate else 'ברירת מחדל — קבוצת הפניות'}\n"
            "הגדרות קיימות נשארות ללא שינוי עד לשמירה מפורשת.\n"
            "שינוי יעד אינו משנה את קבוצת הגיבוי או קבוצת התמיכה.",
            [
                ("📍 קבוצת פניות משתמשים", "adm:mg_target_edit"),
                ("⚙️ קבוצת פקודות ולוגים", "adm:mg_logs_target_edit"),
                ("🔄 לוגים לקבוצת הפניות (ברירת מחדל)", "adm:mg_logs_clear"),
                ("✏️ עריכת תגובה לדיווח על קישור", "adm:mg_report_edit"),
                ("✏️ הודעת סגירת שיחה", "adm:mg_message_edit:chat_closed"),
                ("✏️ הודעת קבלת פנייה", "adm:mg_message_edit:request_received"),
                ("✉️ הודעות מערכת וסיום שיחה", "adm:mg_messages"),
                ("💬 מצב וכללי שיחה פעילה", "adm:mg_chat"),
            ],
            back_action="adm:menu", back_label="🏠 חזרה לתפריט ראשי",
        )

    def messages(self, chat_id):
        self.d._send_submenu(
            chat_id, "✉️ הודעות מערכת — בחר הודעה לעריכה",
            [(label, f"adm:mg_message_edit:{key}")
             for key, (label, _) in MESSAGES.items()],
            back_action="adm:management_group",
        )

    def chat_menu(self, chat_id):
        queue = self.queue
        state = queue.load() if queue is not None else {}
        active = state.get("active_session")
        config = load_config(self.d.management.config_file)
        enabled = config.get("chat_close_notice_enabled", True)
        status = (f"משתמש: {active['user_id']}\nקבוצה: {active['admin_chat_id']}"
                  if active else "אין שיחה פעילה")
        buttons = [
            ("🔄 רענון מצב", "adm:mg_chat"),
            (f"{'🟢' if enabled else '🔴'} הודעת סיום שיחה",
             "adm:mg_farewell_toggle"),
            ("✏️ עריכת הודעת סיום שיחה", "adm:mg_message_edit:chat_closed"),
        ]
        if active:
            buttons.append(("🛑 סגירת השיחה הפעילה", f"adm:mg_close:{active['token']}"))
        self.d._send_submenu(
            chat_id, "💬 מצב וכללי שיחה פעילה\n\n"
            f"{status}\nפניות בתור: {len(state.get('queue', []))}\n\n"
            "בשיחה פעילה ההודעות מועברות ישירות, ללא כרטיס פנייה חדש "
            "וללא אישור אוטומטי למשתמש.\n"
            "פניות אחרות ממתינות בשקט ונשלחות לאחר סגירת השיחה.\n"
            "שיחה קיימת נשארת בקבוצה שבה נפתחה.",
            buttons, back_action="adm:management_group",
        )

    def _pending(self, chat_id, action, **fields):
        self.d.pending_chat_id = chat_id
        self.d.pending[self.d.actor_id] = {"action": action, **fields}

    def handle_callback(self, action, call):
        if action != "management_group" and not action.startswith("mg_"):
            return False
        if (not is_admin(call.from_user.id)
                or call.message.chat.type != "private"
                or call.message.chat.id != call.from_user.id
                or call.from_user.id != self.d.actor_id):
            return True
        chat_id = call.message.chat.id
        self.d.cancel_pending(call.from_user.id)
        try:
            if action == "management_group":
                self.menu(chat_id)
            elif action == "mg_messages":
                self.messages(chat_id)
            elif action == "mg_chat":
                self.chat_menu(chat_id)
            elif action == "mg_target_edit":
                self._pending(chat_id, "mg_target")
                current = resolve_user_management_chat(
                    config_file=self.d.management.config_file)
                self.d._send_prompt(
                    chat_id, f"יעד נוכחי: {current}\n"
                    "שלח מזהה קבוצה/ערוץ שלילי, למשל ‎-100123456789.\n"
                    "הבוט חייב להיות חבר ובעל הרשאת שליחה. לביטול שלח „ביטול”.",
                )
            elif action == "mg_logs_target_edit":
                self._pending(chat_id, "mg_logs_target")
                self.d._send_prompt(
                    chat_id, "שלח מזהה שלילי של קבוצת פקודות ולוגים.\n"
                    "עדכוני תוכן, תיעוד פקודות והתראות מערכת יישלחו לשם.\n"
                    "פניות, דיווחים ושיחות משתמשים יישארו בקבוצת הפניות.\n"
                    "לביטול שלח „ביטול”.",
                )
            elif action == "mg_logs_clear":
                with _LOCK:
                    config = load_config(self.d.management.config_file)
                    config["ADMIN_COMMANDS_CHAT_ID"] = None
                    save_json(self.d.management.config_file, config)
                self.menu(chat_id)
            elif action == "mg_report_edit":
                current = load_config(self.d.management.config_file)["link_error_popup_text"]
                self._pending(chat_id, "mg_report")
                self.d._send_prompt(
                    chat_id, f"תגובה מוקפצת נוכחית:\n{current}\n\n"
                    "שלח טקסט חדש (עד 200 תווים). הדיווח ימשיך להגיע לניהול.",
                )
            elif action == "mg_message_edit":
                key = call.data.split(":", 2)[2]
                if key not in MESSAGES:
                    raise ValueError("הודעת מערכת לא מוכרת.")
                self._pending(chat_id, "mg_message", key=key)
                current = message_text(key, self.d.management.config_file,
                                       self.d.commands_file)
                self.d._send_prompt(
                    chat_id, f"{MESSAGES[key][0]}\n\nהנוסח הנוכחי:\n{current}\n\n"
                    "שלח את הנוסח החדש. ניתן להשתמש בעיצוב HTML/Markdown.",
                )
            elif action == "mg_farewell_toggle":
                with _LOCK:
                    config = load_config(self.d.management.config_file)
                    config["chat_close_notice_enabled"] = not config.get(
                        "chat_close_notice_enabled", True)
                    save_json(self.d.management.config_file, config)
                self.chat_menu(chat_id)
            elif action == "mg_close":
                token = call.data.split(":", 2)[2]
                active = self.queue.load().get("active_session") if self.queue else None
                if not active or active["token"] != token:
                    raise ValueError("השיחה כבר נסגרה או הוחלפה. רענן את המצב.")
                self.d._send_submenu(
                    chat_id, f"האם לסגור את השיחה עם {active['user_id']}?",
                    [("✅ אישור סגירת שיחה", f"adm:mg_close_confirm:{token}")],
                    back_action="adm:mg_chat",
                )
            elif action == "mg_close_confirm":
                token = call.data.split(":", 2)[2]
                active = self.queue.load().get("active_session") if self.queue else None
                if not active or active["token"] != token:
                    raise ValueError("השיחה כבר נסגרה או הוחלפה. רענן את המצב.")
                # Delegate to the existing authoritative close handler. Its lock,
                # token validation, farewell and queue flushing remain unchanged.
                self.queue.handle_callback(SimpleNamespace(
                    id=call.id, data=f"live:close:{token}", from_user=call.from_user,
                    message=SimpleNamespace(
                        chat=SimpleNamespace(
                            id=active.get("control_chat_id", active["admin_chat_id"]),
                            type="supergroup"),
                        message_id=active["request_header_id"],
                    ),
                ))
                self.chat_menu(chat_id)
            else:
                self.menu(chat_id)
        except Exception as exc:
            logging.error("Management-group admin action failed (%s).", type(exc).__name__)
            detail = str(exc) if isinstance(exc, ValueError) else "הפעולה נכשלה. ניתן לנסות שוב."
            self.d.bot.send_message(chat_id, f"לא ניתן לעדכן: {detail}")
        return True

    def handle_input(self, action, state, message, text):
        if action not in {"mg_target", "mg_logs_target", "mg_report", "mg_message"}:
            return False
        if (message.chat.type != "private" or message.from_user.id != self.d.actor_id
                or not is_admin(message.from_user.id)):
            return True
        try:
            if action in {"mg_target", "mg_logs_target"}:
                target = int(text.strip())
                if target >= 0:
                    raise ValueError("מזהה קבוצה/ערוץ חייב להיות שלילי.")
                bot = self.d.raw_bot
                chat = bot.get_chat(target)
                member = bot.get_chat_member(target, bot.get_me().id)
                if chat.type not in {"group", "supergroup", "channel"}:
                    raise ValueError("היעד אינו קבוצה או ערוץ.")
                if member.status in {"left", "kicked"} or (
                    chat.type == "channel" and (
                        member.status not in {"creator", "administrator"}
                        or getattr(member, "can_post_messages", True) is False)
                ) or (member.status == "restricted" and not getattr(member, "can_send_messages", False)):
                    raise ValueError("לבוט אין הרשאת שליחת הודעות ביעד.")
                with _LOCK:
                    config = load_config(self.d.management.config_file)
                    # Synchronize existing consumers, but never touch the quiet
                    # backup LOG_CHAT_ID, support settings, or allowed chats.
                    if action == "mg_logs_target":
                        config["ADMIN_COMMANDS_CHAT_ID"] = target
                    else:
                        for key in ("LOG_CHANNEL_ID", "ADMIN_CHAT_ID", "ADMIN_GROUP_ID", "admin_chat_id"):
                            config[key] = target
                    save_json(self.d.management.config_file, config)
                    if action == "mg_target" and self.queue is not None:
                        self.queue.update_destination(target)
            elif action == "mg_report":
                value = text.strip()
                if not value or len(value) > 200:
                    raise ValueError("שלח טקסט באורך 1–200 תווים.")
                with _LOCK:
                    config = load_config(self.d.management.config_file)
                    config["link_error_popup_text"] = value
                    save_json(self.d.management.config_file, config)
            else:
                update_message(state["key"], text, self.d.management.config_file,
                               self.d.commands_file)
            self.d.cancel_pending(message.from_user.id)
            if action == "mg_message":
                self.messages(message.chat.id)
            else:
                self.menu(message.chat.id)
        except Exception as exc:
            logging.error("Management-group input could not be saved (%s).", type(exc).__name__)
            detail = str(exc) if isinstance(exc, ValueError) else "בדוק את מזהה היעד והרשאות הבוט, ונסה שוב."
            self.d.bot.send_message(message.chat.id, f"לא ניתן לשמור: {detail}")
        return True
