"""Private admin controls for optional VIP discovery and genres."""

from collections import Counter
from media_genres import genre_list, genre_key
from vip_experience import ExperienceStore, MESSAGES
from bot_storage import is_admin


class ExperienceAdmin:
    def __init__(self, dashboard):
        self.d = dashboard
        self.store = ExperienceStore(dashboard.vip_admin.store)

    def menu(self, chat_id):
        state = self.store.snapshot()
        status = lambda value: "🟢 ON" if value else "🔴 OFF"
        label = {"vip": "VIP ONLY", "public": "PUBLIC", "off": "OFF"}
        self.d._send_submenu(chat_id, "⚙️ הגדרות ז'אנרים ו-VIP", [
            (f"תיוג ז'אנר בהעלאה: {status(state['genre_tagging'])}", "adm:experience_toggle:genre_tagging"),
            (f"תפריט VIP: {status(state['keyboard_enabled'])}", "adm:experience_toggle:keyboard_enabled"),
            (f"סינון ז'אנרים בהגרלה: {label[state['random_policy']]}", "adm:experience_policy:random_policy"),
            (f"מעקב אחרי סדרות: {label[state['tracking_policy']]}", "adm:experience_policy:tracking_policy"),
            ("✏️ עריכת הודעות המערכת ו-VIP", "adm:experience_messages"),
            ("📊 סטטיסטיקת תכנים וז'אנרים", "adm:experience_stats"),
        ])

    def handle_callback(self, action, call):
        if not action.startswith("experience"):
            return False
        uid = call.from_user.id
        if not is_admin(uid) or call.message.chat.type != "private" or call.message.chat.id != uid:
            return True
        self.d.cancel_pending(uid)
        parts = call.data.split(":")
        chat_id = call.message.chat.id
        if action == "experience":
            self.menu(chat_id)
        elif action == "experience_keyboard":
            state = self.store.snapshot()
            self.store.set("keyboard_enabled", not state["keyboard_enabled"])
            self.d.vip_admin.menu(chat_id, "✅ מצב תפריט VIP עודכן.")
        elif action == "experience_toggle" and parts[2] in {"genre_tagging", "keyboard_enabled"}:
            key = parts[2]
            self.store.set(key, not self.store.snapshot()[key])
            self.menu(chat_id)
        elif action == "experience_policy" and parts[2] in {"random_policy", "tracking_policy"}:
            key = parts[2]
            self.d._send_submenu(chat_id, "בחר הרשאות:", [
                (label, f"adm:experience_set_policy:{key}:{value}")
                for label, value in [("⭐ VIP ONLY", "vip"), ("🌐 PUBLIC", "public"), ("🔴 OFF", "off")]
            ], back_action="adm:experience")
        elif action == "experience_set_policy":
            self.store.set(parts[2], parts[3])
            self.menu(chat_id)
        elif action == "experience_messages":
            labels = {
                "restricted": "הודעת הגבלת VIP", "empty": "אין תוכן",
                "choose_genre": "בחירת ז'אנר", "tracking_title": "רשימת מעקב",
                "following": "אישור מעקב", "unfollowed": "הסרת מעקב",
                "episode": "התראת פרקים חדשים", "benefits": "יתרונות VIP",
                "revoked": "הסרת VIP", "disabled": "אפשרות כבויה",
                "moderated": "הגבלת משתמש חסום / מושתק",
            }
            self.d._send_submenu(chat_id, "✏️ הודעות ז'אנרים ו-VIP", [
                ("✏️ " + label, "adm:experience_edit:" + key)
                for key, label in labels.items()
            ], back_action="adm:experience")
        elif action == "experience_benefits" or (action == "experience_edit" and parts[2] in MESSAGES):
            key = "benefits" if action == "experience_benefits" else parts[2]
            self.d.pending[uid] = {"action": "experience_text", "message_key": key}
            if action == "experience_benefits":
                self.d.pending[uid]["return_to"] = "adm:vip"
            self.d.pending_chat_id = chat_id
            self.d._send_prompt(chat_id, "שלח את הנוסח החדש.\n"
                "להתראת פרקים: {CONTENT_NAME}, {COMMAND_NAME}\n\n"
                "הנוסח הנוכחי:\n" + self.store.text(key))
        elif action == "experience_stats":
            counts = Counter()
            total = 0
            for catalogue in (self.d.media_manager, self.d.vip_media_manager):
                if catalogue is None:
                    continue
                for item in catalogue.repository.list_media():
                    counts.update([genre_key(g) for g in genre_list(item.get("genre"))] or ["ללא ז'אנר"])
                    total += 1
            self.d._send_submenu(chat_id, f"📊 סך הכל: {total}\n" + "\n".join(
                f"{genre}: {count}" for genre, count in sorted(counts.items())
            ), [], back_action="adm:experience")
        return True

    def handle_input(self, action, message, text):
        if action != "experience_text":
            return False
        uid = message.from_user.id
        if not is_admin(uid) or message.chat.type != "private":
            return True
        state = self.d.pending[uid]
        key = state["message_key"]
        if not text.strip() or len(text) > 4000:
            self.d._send_prompt(message.chat.id, "שלח טקסט באורך 1–4000 תווים.")
            return True
        self.store.mutate(lambda state: state["messages"].update({key: text}))
        self.d.cancel_pending(uid)
        if state.get("return_to") == "adm:vip":
            self.d.vip_admin.menu(message.chat.id, "✅ יתרונות VIP עודכנו.")
        else:
            self.menu(message.chat.id)
        return True
