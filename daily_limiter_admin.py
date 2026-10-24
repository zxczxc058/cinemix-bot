"""Private quota configuration and manual counter resets."""

from bot_storage import is_admin
from daily_command_limiter import DailyCommandLimiter


class DailyLimiterAdmin:
    def __init__(self, dashboard):
        self.d = dashboard
        self.limiter = DailyCommandLimiter(dashboard.management.config_file)

    def menu(self, chat_id):
        self.d.cancel_pending(self.d.actor_id)
        data = self.limiter.settings()
        self.d._send_submenu(
            chat_id,
            "⚙️ הגבלת פקודות יומית\n\n"
            f"מצב: {'פעיל' if data['enabled'] else 'כבוי'}\n"
            f"מכסה: {data['limit']} פקודות/חיפושים ליום\n"
            f"איפוס: 00:00 ({data['timezone']})\n"
            "מנהלים ומשתמשי VIP פטורים לחלוטין.\n\n"
            f"הודעה בעת חריגה:\n{data['exceeded_message']}",
            [
                ("🟢 דלוק — לחץ לכיבוי" if data["enabled"] else "🔴 כבוי — לחץ להפעלה", "adm:daily_limiter_toggle"),
                ("🔢 הגדרת מכסה יומית", "adm:daily_limiter_limit"),
                ("✏️ עריכת הודעת חריגה", "adm:daily_limiter_message"),
                ("🕛 החלף איפוס UTC / זמן ישראל", "adm:daily_limiter_timezone"),
                ("🔓 איפוס מכסה למשתמש", "adm:daily_limiter_reset"),
            ],
            back_action="adm:category_settings",
        )

    def handle_callback(self, action, call):
        if not action.startswith("daily_limiter"):
            return False
        if (
            call.message.chat.type != "private" or call.message.chat.id != call.from_user.id
            or not is_admin(call.from_user.id)
        ):
            return True
        if action == "daily_limiter_toggle":
            self.limiter.update(enabled=not self.limiter.settings()["enabled"])
        elif action == "daily_limiter_timezone":
            self.limiter.update(
                timezone="Asia/Jerusalem" if self.limiter.settings()["timezone"] == "UTC" else "UTC",
            )
        elif action in {"daily_limiter_limit", "daily_limiter_message", "daily_limiter_reset"}:
            prompts = {
                "daily_limiter_limit": "שלח מכסה יומית (מספר בין 1 ל־100000).",
                "daily_limiter_message": (
                    "שלח את הודעת החריגה המדויקת.\n"
                    "משתנים זמינים: {remaining_time}, {hours}, {minutes}, {time_remaining}.\n"
                    "זמן עד האיפוס יתווסף אוטומטית אם לא ציינת משתנים."
                ),
                "daily_limiter_reset": "שלח את מזהה המשתמש שמכסתו תאופס כעת.",
            }
            self.d.pending[call.from_user.id] = {"action": action, "return_to": "adm:daily_limiter"}
            self.d.pending_chat_id = call.from_user.id
            self.d._send_submenu(
                call.from_user.id, prompts[action], [], back_action="adm:daily_limiter",
            )
            return True
        self.menu(call.from_user.id)
        return True

    def handle_input(self, action, message, text):
        if action not in {"daily_limiter_limit", "daily_limiter_message", "daily_limiter_reset"}:
            return False
        try:
            if action == "daily_limiter_limit":
                self.limiter.update(limit=int(text.strip()))
            elif action == "daily_limiter_message":
                self.limiter.update(exceeded_message=text)
            else:
                self.limiter.reset_user(int(text.strip()))
        except ValueError as exc:
            self.d.bot.send_message(message.chat.id, str(exc))
            return True
        self.menu(message.chat.id)
        return True
