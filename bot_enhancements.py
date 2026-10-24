"""Small, private-only extensions; existing routing and moderation stay authoritative."""

from html import escape
import logging
from pathlib import Path
from bot_storage import is_admin, load_user_ids, add_command, load_bot_data
from access_control import load_access, revoke_access
from media_storage import media_token
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def indexed_users(users_path, access_path, vip):
    """Derive roles from authoritative stores, never copy or renumber accounts."""
    access = load_access(access_path)
    state = vip.snapshot()
    ids = set(load_user_ids(users_path)) | set(access["authorized_users"]) | set(state["vip_users"])
    for records in (access.get("authentication_state", {}), state.get("vip_membership", {})):
        ids.update(int(uid) for uid in records if str(uid).isdigit() and int(uid) > 0)
    return [(uid, "vip" if vip.is_member(uid) else "regular") for uid in sorted(ids)]


class EnhancedAdmin:
    def __init__(self, dashboard):
        self.d = dashboard

    def screen(self, cid, text, buttons, back="adm:category_users"):
        self.d._send_submenu(cid, text, buttons, back_action=back)

    def handle_callback(self, action, call):
        if not action.startswith("enhance_"):
            return False
        uid = call.from_user.id
        if not is_admin(uid) or call.message.chat.type != "private" or call.message.chat.id != uid:
            return True
        cid = call.message.chat.id
        parts = call.data.split(":")
        try:
            if action == "enhance_drive_cancel":
                from drive_transfer_control import cancel_upload
                if len(parts) == 3 and cancel_upload(uid, parts[2]):
                    self.d.bot.edit_message_text(
                        "⏳ מבטל את ההעלאה…", chat_id=cid, message_id=call.message.message_id,
                    )
            elif action == "enhance_users":
                role, page = parts[2], max(0, int(parts[3]))
                if role not in {"all", "vip", "regular"}:
                    return True
                entries = indexed_users(self.d.users_file, self.d.access.path, self.d.vip_admin.store)
                entries = [entry for entry in entries if role == "all" or entry[1] == role]
                page = min(page, max(0, (len(entries)-1)//15))
                buttons = [
                    ("👥 כולם", "adm:enhance_users:all:0"),
                    ("👤 משתמשים רגילים", "adm:enhance_users:regular:0"),
                    ("⭐ משתמשי VIP", "adm:enhance_users:vip:0"),
                ]
                buttons += [
                    (f"{'⭐' if tier == 'vip' else '👤'} {target}", f"adm:enhance_user:{target}")
                    for target, tier in entries[page*15:(page+1)*15]
                ]
                if page:
                    buttons.append(("⬅️ הקודם", f"adm:enhance_users:{role}:{page-1}"))
                if (page+1)*15 < len(entries):
                    buttons.append(("➡️ הבא", f"adm:enhance_users:{role}:{page+1}"))
                self.screen(cid, f"👥 משתמשים רשומים: {len(entries)} · עמוד {page+1}\nהסטטוס נקרא בזמן אמת; מזהי המשתמשים נשארים ללא שינוי.", buttons)
            elif action == "enhance_user":
                target = int(parts[2])
                if target <= 0:
                    raise ValueError("Invalid user.")
                buttons = [
                    ("🚫 חסום משתמש", f"mod:ban:{target}"),
                    ("🔊 השתק משתמש", f"mod:mute:{target}"),
                    ("🔓 ביטול חסימה", f"mod:unban:{target}"),
                    ("🔈 ביטול השתקה", f"mod:unmute:{target}"),
                    ("🔑 הסרת גישה רגילה", f"adm:enhance_revoke:{target}"),
                ]
                if self.d.vip_admin.store.is_member(target):
                    buttons.append(("⭐ הסרת VIP", f"adm:vip_user_revoke:{target}:0"))
                buttons.append(("🔙 חזרה לרשימת משתמשים", "adm:enhance_users:all:0"))
                self.screen(cid, f"ניהול משתמש: {target}", buttons)
            elif action == "enhance_revoke":
                target = int(parts[2])
                self.screen(cid, f"להסיר הרשאה רגילה למשתמש {target}?", [
                    ("✅ אישור", f"adm:enhance_revoke_confirm:{target}"),
                    ("🔙 חזרה", f"adm:enhance_user:{target}"),
                ])
            elif action == "enhance_revoke_confirm":
                revoke_access(int(parts[2]), self.d.access.path)
                self.screen(cid, "✅ הגישה הרגילה הוסרה.", [("🔙 חזרה לרשימה", "adm:enhance_users:regular:0")])
            elif action == "enhance_command":
                self.d.pending[uid] = {"action": "enhance_trigger", "return_to": "adm:category_broadcast"}
                self.d.pending_chat_id = cid
                self.d._send_prompt(cid, "שלח את מילת ההפעלה המדויקת (command_trigger), למשל /mycommand. לביטול: ביטול.")
            elif action == "enhance_report":
                state = self.d.pending.get(uid, {})
                if state.get("action") != "enhance_report_choice" or parts[2] not in {"yes", "no"}:
                    raise ValueError("פתח שוב את יצירת הפקודה.")
                if not add_command(state["trigger"], state["response"], self.d.commands_file,
                                   display_title=state["title"], report_enabled=parts[2] == "yes"):
                    raise ValueError("הפקודה כבר קיימת.")
                self.d.cancel_pending(uid)
                self.screen(cid, "✅ הפקודה נשמרה.\nמילת הפעלה: " + escape(state["trigger"]) +
                            "\nשם תצוגה: " + escape(state["title"]),
                            [("✏️ ניהול פקודות", "adm:text_menu")], back="adm:category_broadcast")
            elif action == "enhance_command_titles":
                items = list(load_bot_data(self.d.commands_file)["responses"])
                page = max(0, min(int(parts[2]) if len(parts) > 2 else 0, max(0, (len(items)-1)//15)))
                buttons = [(key[:64], f"adm:enhance_command_title:{media_token(key)}")
                           for key in items[page*15:(page+1)*15]]
                if page:
                    buttons.append(("⬅️ הקודם", f"adm:enhance_command_titles:{page-1}"))
                if (page+1)*15 < len(items):
                    buttons.append(("➡️ הבא", f"adm:enhance_command_titles:{page+1}"))
                self.screen(cid, "✏️ שם תצוגה לפקודה — בחר פקודה", buttons)
            elif action == "enhance_command_title":
                items = list(load_bot_data(self.d.commands_file)["responses"])
                key = next((key for key in items if media_token(key) == parts[2]), None)
                if key is None:
                    raise ValueError("בחר שוב פקודה.")
                self.d.pending[uid] = {"action": "enhance_edit_title", "trigger": key,
                                      "return_to": "adm:enhance_command_titles:0"}
                self.d.pending_chat_id = cid
                self.d._send_prompt(cid, "שלח שם תצוגה חדש. מילת ההפעלה והתוכן לא ישתנו.")
            elif action.startswith("enhance_drive"):
                from drive_uploads import DriveUploads
                drive = DriveUploads(self.d.raw_bot, self.d.management.config_file)
                if action == "enhance_drive_disconnect":
                    drive.logout(uid)
                    self.d.cancel_pending(uid)
                    self.drive_menu(cid, "החשבון נותק בהצלחה! כעת ניתן ללחוץ על התחברות ולבחור כל חשבון Google שתרצה.")
                elif action == "enhance_drive_connect":
                    url = drive.auth_link(uid)
                    drive.enable(True)
                    self.d.pending[uid] = {"action": "enhance_drive_video", "return_to": "adm:enhance_drive"}
                    self.d.pending_chat_id = cid
                    keyboard = InlineKeyboardMarkup().row(InlineKeyboardButton(
                        "🔗 בחירת חשבון Google", url=url,
                    ))
                    self.d.bot.send_message(cid,
                        "בחר חשבון Google בדפדפן ואשר גישה ל-Drive. לאחר ההתחברות חזור לכאן ושלח קובץ. הקישור תקף ל-10 דקות.",
                        reply_markup=keyboard)
                elif action == "enhance_drive_upload":
                    if not drive.enabled():
                        self.drive_menu(cid, "יש לחבר את החשבון תחילה.")
                    else:
                        self.d.pending[uid] = {"action": "enhance_drive_video", "return_to": "adm:enhance_drive"}
                        self.d.pending_chat_id = cid
                        self.d._send_prompt(cid, "שלח וידאו או קובץ מדיה. אפשר לשלוח כמה קבצים ברצף; הם יעלו אחד אחרי השני. הקישורים יישלחו רק אליך. לביטול: ביטול.")
                else:
                    if drive.enabled():
                        self.d.pending[uid] = {"action": "enhance_drive_video", "return_to": "adm:enhance_drive"}
                        self.d.pending_chat_id = cid
                        self.drive_menu(cid, "שלח או העבר כעת וידאו או קובץ מדיה (עד 4GB). אפשר לשלוח כמה קבצים ברצף; הקישורים יישלחו רק אליך ולא יצורפו לתוכן. לביטול: ביטול.")
                    else:
                        self.drive_menu(cid, "לחץ על התחברות/החלפת חשבון Google כדי להפעיל העלאות, ואז שלח וידאו.")
        except Exception as exc:
            logging.warning("Admin enhancement failed (%s).", type(exc).__name__)
            # Drive helper errors contain only fixed, credential-free instructions.
            notice = str(exc) if action.startswith("enhance_drive") and isinstance(exc, ValueError) else "לא ניתן לבצע את הפעולה כרגע. בדוק את החיבור או נסה שוב."
            self.screen(cid, notice, [])
        return True

    def drive_menu(self, cid, notice=""):
        from drive_uploads import DriveUploads
        drive = DriveUploads(self.d.raw_bot, self.d.management.config_file)
        try:
            enabled = drive.enabled() and drive.oauth_operation("status", cid).get("connected", False)
        except ValueError:
            enabled = False
        self.d._send_submenu(cid, (notice + "\n\n" if notice else "") +
                             "☁️ העלאה ל-Google Drive\nמצב: " + ("מחובר" if enabled else "מנותק") +
                              "\nכל מנהל מתחבר לחשבון Google משלו. לניתוק: /logout.\nלסנכרון הקטלוג: /sync או /sync ואחריו קישור לתיקייה.\nהקבצים נשארים פרטיים לחשבון. שיתוף ציבורי נעשה ידנית ב-Drive.",
                             [
                                 ("🔗 התחברות/החלפת חשבון Google", "adm:enhance_drive_connect"),
                                 ("🚪 התנתקות מחשבון Google", "adm:enhance_drive_disconnect"),
                                 ("🎞️ העלאת וידאו", "adm:enhance_drive_upload"),
                             ], back_action="adm:menu")

    def handle_input(self, action, state, message):
        if not action.startswith("enhance_"):
            return False
        uid, cid = message.from_user.id, message.chat.id
        if not is_admin(uid) or message.chat.type != "private" or cid != uid:
            return True
        text = (getattr(message, "text", None) or "").strip()
        if text == "ביטול":
            self.d.cancel_pending(uid)
            self.d.send_menu(cid)
            return True
        try:
            if action == "enhance_drive_video":
                from drive_uploads import DriveUploads
                from types import SimpleNamespace
                file = next((
                    getattr(message, kind, None)
                    for kind in ("video", "video_note", "animation", "audio", "voice", "document")
                    if getattr(message, kind, None) is not None
                ), None)
                media_kind = None
                if file is None:
                    photos = getattr(message, "photo", None)
                    if photos:
                        file = max(photos, key=lambda photo: getattr(photo, "file_size", 0))
                        media_kind = "photo"
                if not file:
                    self.d._send_prompt(cid, "שלח וידאו, תמונה או קובץ מדיה.")
                    return True
                mime = getattr(file, "mime_type", None)
                name = getattr(file, "file_name", None)
                if media_kind == "photo":
                    mime = mime or "image/jpeg"
                    name = name or "telegram-image.jpg"
                elif getattr(message, "video_note", None) is file:
                    mime = mime or "video/mp4"
                    name = name or "telegram-video.mp4"
                elif getattr(message, "animation", None) is file:
                    mime = mime or "video/mp4"
                    name = name or "telegram-animation.mp4"
                elif getattr(message, "voice", None) is file:
                    mime = mime or "audio/ogg"
                    name = name or "telegram-voice.ogg"
                elif getattr(message, "audio", None) is file:
                    mime = mime or "audio/mpeg"
                    name = name or "telegram-audio.mp3"
                else:
                    if mime is None and getattr(message, "video", None) is file:
                        mime = "video/mp4"
                    name = name or ("telegram-video.mp4" if mime == "video/mp4" else "telegram-upload.bin")
                if mime is None:
                    mime = "application/octet-stream"
                file = SimpleNamespace(
                    file_id=file.file_id,
                    file_size=getattr(file, "file_size", None),
                    file_name=name,
                    mime_type=mime,
                )
                DriveUploads(self.d.raw_bot, self.d.management.config_file).start(uid, file)
                return True
            if not text or len(text) > 4096:
                raise ValueError("שלח טקסט באורך תקין.")
            if action == "enhance_trigger":
                import re
                if len(text) > 128 or (text.startswith("/") and not re.fullmatch(r"/[A-Za-z0-9_]{1,32}", text)):
                    raise ValueError("מילת הפעלה אינה תקינה.")
                if text in load_bot_data(self.d.commands_file)["responses"]:
                    raise ValueError("הפקודה כבר קיימת.")
                self.d.pending[uid] = {"action": "enhance_title", "trigger": text,
                                      "return_to": "adm:category_broadcast"}
                self.d._send_prompt(cid, "שלח שם להצגה (display_title), בנפרד ממילת ההפעלה.")
            elif action == "enhance_title":
                if len(text) > 128:
                    raise ValueError("שם התצוגה ארוך מדי.")
                self.d.pending[uid] = {**state, "action": "enhance_response", "title": text}
                self.d._send_prompt(cid, "שלח את תגובת הפקודה. אפשר לכלול {DISPLAY_TITLE} להצגת השם.")
            elif action == "enhance_response":
                self.d.pending[uid] = {**state, "action": "enhance_report_choice", "response": message.text}
                self.screen(cid, "לצרף כפתור דיווח לפקודה לפני השמירה?", [
                    ("✅ שמור עם כפתור דיווח", "adm:enhance_report:yes"),
                    ("✅ שמור ללא כפתור דיווח", "adm:enhance_report:no"),
                ], back="adm:category_broadcast")
            elif action == "enhance_edit_title":
                from bot_storage import set_command_display_title
                set_command_display_title(state["trigger"], text, self.d.commands_file)
                self.d.cancel_pending(uid)
                self.screen(cid, "✅ שם התצוגה נשמר.", [("🔙 חזרה לפקודות", "adm:enhance_command_titles:0")],
                            back="adm:category_broadcast")
            try:
                self.d.raw_bot.delete_message(cid, message.message_id)
            except Exception:
                pass
        except (ValueError, OSError) as exc:
            self.d._send_prompt(cid, str(exc))
        return True
