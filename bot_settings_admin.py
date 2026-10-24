"""Admin controls for global bot modes."""

from admin_menus import BOT_SETTINGS_ACTIONS
from bot_storage import is_admin
from force_subscribe import load_force_subscribe
from system_features import load_system_settings, toggle_anti_forward


class BotSettingsAdmin:
    def __init__(self, dashboard):
        self.dashboard = dashboard

    def menu(self, chat_id):
        system = load_system_settings(self.dashboard.advanced.system_file)
        force = load_force_subscribe(self.dashboard.force_subscribe_admin.path)
        states = {
            "adm:anti_forward_toggle": "פעילה" if system["anti_forward_enabled"] else "כבויה",
            "adm:maintenance_toggle": "פעיל" if system["maintenance_enabled"] else "כבוי",
            "adm:force_subscribe": "פעילה" if force["enabled"] else "כבויה",
        }
        rows = [
            (f"{label} — {states[callback]}" if callback in states else label, callback)
            for label, callback in BOT_SETTINGS_ACTIONS
        ]
        self.dashboard._send_submenu(
            chat_id, "⚙️ הגדרות ומצבי בוט", rows, row_width=2,
        )

    def handle_callback(self, action, call):
        if action not in {"bot_settings", "anti_forward_toggle"}:
            return False
        actor = getattr(getattr(call, "from_user", None), "id", None)
        chat = getattr(getattr(call, "message", None), "chat", None)
        if not is_admin(actor) or chat is None:
            return True
        if getattr(chat, "type", None) != "private" or chat.id != actor:
            self.dashboard.bot.answer_callback_query(
                call.id, text="הגדרות ומצבי בוט זמינים בפרטי בלבד.", show_alert=True,
            )
            return True
        self.dashboard.cancel_pending(actor)
        if action == "bot_settings":
            self.menu(chat.id)
        elif action == "anti_forward_toggle":
            enabled = toggle_anti_forward(self.dashboard.advanced.system_file)
            self.dashboard.bot.answer_callback_query(
                call.id,
                text=("הגנת העברת תוכן הופעלה." if enabled else "הגנת העברת תוכן כובתה."),
            )
            self.menu(chat.id)
        return True
