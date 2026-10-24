"""Private admin CRUD for welcome URL and callback buttons."""

from uuid import uuid4
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
from bot_storage import get_start_reply, is_admin
from start_keyboard import load_buttons, save_buttons, validate_button


class StartButtonsAdmin:
    def __init__(self, dashboard, profile=None):
        self.d = dashboard
        self.profile = profile
        self.prefix = f"{profile}_inline" if profile else "start_inline"
        self.parent = f"adm:welcome_profile:{profile}" if profile else "adm:start_menu"

    def save(self, buttons):
        from welcome_profiles import button_key
        from start_keyboard import KEY
        save_buttons(buttons, self.d.management.config_file,
                     key=button_key(self.profile) if self.profile else KEY)

    def load(self):
        from welcome_profiles import button_key
        from start_keyboard import KEY
        if self.profile == "before":
            from management_config import load_config
            from welcome_profiles import before_code_keyboard
            if button_key("before") not in load_config(self.d.management.config_file):
                markup = before_code_keyboard(self.d.management.config_file, ignore_enabled=True)
                buttons = [
                    {"id": uuid4().hex[:12], "text": b.text, "url": b.url, "enabled": True}
                    for row in (markup.keyboard if markup else []) for b in row
                ]
                self.save(buttons)
        elif self.profile == "first":
            from management_config import load_config
            # Opening this editor must not copy another welcome's buttons.
            if button_key("first") not in load_config(self.d.management.config_file):
                self.save([])
        return load_buttons(self.d.management.config_file,
                            get_start_reply(self.d.commands_file).buttons, persist=True,
                            key=button_key(self.profile) if self.profile else KEY)

    def menu(self, chat_id):
        self.d.cancel_pending(self.d.actor_id)
        buttons = self.load()
        self.d._send_submenu(
            chat_id, "🔘 ניהול כל כפתורי הודעת /start\n"
            "כל שינוי נשמר מייד ומשפיע על הפתיחה הבאה.\n"
            "רשימה ריקה = הודעת פתיחה ללא כפתורים.",
            [(("🟢 " if b.get("enabled", True) else "🔴 ") + b["text"], f"adm:{self.prefix}_item:{b['id']}")
             for b in buttons] + [
                ("➕ הוסף כפתור", f"adm:{self.prefix}_add"),
                ("🗑️ הסר את כל הכפתורים", f"adm:{self.prefix}_clear"),
                ("👁️ תצוגה מקדימה", f"adm:welcome_preview:{self.profile}" if self.profile else "adm:start_preview"),
            ], back_action=self.parent,
        )

    def item(self, chat_id, button_id):
        button = next((b for b in self.load() if b["id"] == button_id), None)
        if button is None:
            self.menu(chat_id)
            return
        self.d._send_submenu(
            chat_id, f"🔘 {button['text']}\nיעד: {button.get('url') or button.get('callback_data')}",
            [
                ("🔘 הפעלה / כיבוי", f"adm:{self.prefix}_toggle:{button_id}"),
                ("✏️ עריכת כיתוב ויעד", f"adm:{self.prefix}_edit:{button_id}"),
                ("🗑️ מחיקת כפתור", f"adm:{self.prefix}_delete:{button_id}"),
            ], back_action=f"adm:{self.prefix}",
        )

    def handle_callback(self, action, call):
        if not action.startswith(self.prefix) and not (not self.profile and action == "start_buttons"):
            return False
        action = action.replace(self.prefix, "start_inline", 1)
        if call.message.chat.type != "private" or not is_admin(call.from_user.id):
            return True
        chat_id = call.message.chat.id
        self.d.cancel_pending(call.from_user.id)
        if self.d.on_admin_navigation:
            self.d.on_admin_navigation(call.from_user.id, chat_id)
        parts = call.data.split(":")
        button_id = parts[2] if len(parts) > 2 else None
        if action in {"start_buttons", "start_inline"}:
            self.menu(chat_id)
        elif action == "start_inline_item":
            self.item(chat_id, button_id)
        elif action == "start_inline_clear":
            self.d._send_submenu(
                chat_id, "האם להסיר את כל כפתורי הפתיחה המותאמים?",
                [("✅ אישור הסרת הכול", f"adm:{self.prefix}_clear_confirm")],
                back_action=f"adm:{self.prefix}",
            )
        elif action == "start_inline_clear_confirm":
            self.save([])
            self.menu(chat_id)
        elif action in {"start_inline_toggle", "start_inline_delete"}:
            buttons = self.load()
            for b in buttons:
                if b["id"] == button_id:
                    b["enabled"] = not b.get("enabled", True)
            if action.endswith("delete"):
                buttons = [b for b in buttons if b["id"] != button_id]
            self.save(buttons)
            self.menu(chat_id)
        elif action in {"start_inline_add", "start_inline_edit"}:
            current = next((b for b in self.load() if b["id"] == button_id), None)
            if action.endswith("edit") and current is None:
                self.menu(chat_id)
                return True
            self.d.pending[call.from_user.id] = {
                "action": "start_inline_input", "button_id": button_id,
                "return_to": f"adm:{self.prefix}", "welcome_profile": self.profile,
            }
            self.d.pending_chat_id = chat_id
            markup = InlineKeyboardMarkup(row_width=1)
            markup.add(
                InlineKeyboardButton("🔙 חזרה", callback_data=f"adm:{self.prefix}"),
                InlineKeyboardButton("🏠 חזרה לתפריט ראשי", callback_data="adm:menu"),
            )
            self.d.bot.send_message(
                chat_id, "שלח כיתוב ויעד מופרדים ב־|:\n"
                "שם הכפתור | https://t.me/username\n"
                "או: שם הכפתור | callback:catalog:list:0\n"
                "callback חייב להפנות לפעולה קיימת בבוט. לביטול: ביטול.\n"
                + (f"ערך נוכחי: {current['text']} | {current.get('url') or 'callback:' + current['callback_data']}"
                   if current else ""), reply_markup=markup,
            )
        return True

    def handle_input(self, action, state, message, text):
        if action != "start_inline_input" or state.get("welcome_profile") != self.profile:
            return False
        if message.chat.type != "private" or not is_admin(message.from_user.id):
            return True
        try:
            label, target = (s.strip() for s in text.split("|", 1))
            button = {"id": state.get("button_id") or uuid4().hex[:12],
                      "text": label, "enabled": True}
            if target.startswith("callback:"):
                button["callback_data"] = target.removeprefix("callback:")
            else:
                button["url"] = target
            validate_button(button)
            buttons = self.load()
            old = next((b for b in buttons if b["id"] == button["id"]), None)
            if state.get("button_id") and old is None:
                raise ValueError("הכפתור כבר נמחק. חזור לרשימה.")
            if old:
                button["enabled"] = old.get("enabled", True)
                buttons[buttons.index(old)] = button
            else:
                buttons.append(button)
            self.save(buttons)
            self.menu(message.chat.id)
            # Existing input-feedback integration cleans persisted input and
            # provides a fresh confirmation button without a text receipt.
            self.d.bot.send_message(message.chat.id, "הכפתור נשמר בהצלחה.")
        except ValueError as exc:
            self.d.bot.send_message(message.chat.id, f"לא ניתן לשמור: {exc}")
        return True
