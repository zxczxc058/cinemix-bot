"""Private admin entry points for the independent Telegram web streamer."""

from bot_storage import is_admin
from web_streaming import WebStreams


class WebStreamAdmin:
    def __init__(self, dashboard):
        self.d = dashboard

    def handle_callback(self, action, call):
        if action != "web_stream":
            return False
        uid, chat = call.from_user.id, call.message.chat
        if not is_admin(uid) or chat.type != "private" or chat.id != uid:
            return True
        self.d.pending[uid] = {"action": "web_stream_file", "return_to": "adm:menu"}
        self.d.pending_chat_id = uid
        self.d._send_prompt(
            uid, "🌐 העלאה לקישור צפייה\n\nשלח או העבר וידאו/קובץ עד 4GB.\n"
            "הקובץ נשאר בטלגרם: אין העלאה לשירות חיצוני ואין API בתשלום. "
            "תוצג התקדמות אמיתית של בדיקת הקובץ והכנת הקישור.\n"
            "כל מי שמחזיק בקישור יכול לפתוח אותו. אין תאריך תפוגה יזום, "
            "אך נדרשת זמינות השרת והקובץ בטלגרם. תעבורת הצפייה משתמשת באירוח הקיים.\n"
            "הנגן אינו ממיר פורמטים; MP4/WebM עם קידוד נתמך מתאימים לצפייה בדפדפן.\n"
            "הקישור יישלח רק אליך ולא יצורף לתוכן. לביטול: ביטול."
        )
        return True

    def handle_input(self, action, state, message):
        if action != "web_stream_file":
            return False
        uid, chat = message.from_user.id, message.chat
        if not is_admin(uid) or chat.type != "private" or chat.id != uid:
            return True
        if (getattr(message, "text", None) or "").strip() == "ביטול":
            self.d.cancel_pending(uid)
            self.d.send_menu(uid)
            return True
        file = getattr(message, "video", None) or getattr(message, "document", None)
        if not file:
            self.d._send_prompt(uid, "שלח קובץ וידאו או מסמך. לביטול: ביטול.")
            return True
        try:
            WebStreams(self.d.raw_bot).start(uid, file, source_message=message)
        except ValueError as exc:
            self.d._send_prompt(uid, str(exc))
            return True
        self.d.cancel_pending(uid)
        return True
