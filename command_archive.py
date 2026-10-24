"""Archive committed command snapshots before deleting verified private input."""

import functools
import html
import io
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from types import SimpleNamespace

from bot_storage import load_bot_data, is_admin
from management_config import _LOCK, load_config, save_json
from private_admin_protection import PrivateAdminProtection, _reason_message_id

_sources = ContextVar("command_archive_sources", default=None)
COMMAND_INPUTS = {
    "add_keyword", "add_button_keyword", "add_response", "add_button_response",
    "edit_response", "command_button_label", "command_button_url",
    "health_link",
}


def capture_source(message, allow_media=False):
    chat = getattr(message, "chat", None)
    actor = getattr(chat, "id", None)
    mid = _reason_message_id(actor, message, allow_photo=allow_media)
    if mid is None:
        return None
    return {"chat_id": actor, "user_id": actor, "message_id": mid}


def source_proof(source):
    if not isinstance(source, dict):
        return None
    return SimpleNamespace(
        chat=SimpleNamespace(id=source.get("chat_id"), type="private"),
        from_user=SimpleNamespace(id=source.get("user_id"), is_bot=False),
        message_id=source.get("message_id"), text="command input",
    )


def track_command_input(handler):
    @functools.wraps(handler)
    def wrapped(self, message):
        actor = getattr(getattr(message, "from_user", None), "id", None)
        state = self.pending.get(actor, {})
        if state.get("action") not in COMMAND_INPUTS or not self.has_pending_input(message):
            return handler(self, message)
        sources = list(state.get("command_sources", []))
        source = capture_source(message)
        if source and (message.text or "").strip() != "ביטול":
            sources.append(source)
        token = _sources.set(sources)
        try:
            return handler(self, message)
        finally:
            current = self.pending.get(actor)
            if current and (current.get("action") in COMMAND_INPUTS or
                            current.get("action") in {"command_buttons_done", "add_report_choice"}):
                current["command_sources"] = sources
            _sources.reset(token)
    return wrapped


def configure_log_chat(bot, value, config_file):
    if not re.fullmatch(r"-[1-9][0-9]*", value.strip()) or abs(int(value)) >= 2**52:
        raise ValueError("שלח מזהה קבוצה שלילי תקין, למשל ‎-100123456789.")
    target = int(value)
    try:
        chat = bot.get_chat(target)
        member = bot.get_chat_member(target, bot.get_me().id)
    except Exception as exc:
        raise ValueError("לא ניתן לאמת את קבוצת הגיבוי. בדוק את המזהה, הוסף את הבוט לקבוצה ואפשר לו לשלוח קבצים.") from exc
    if chat.type not in {"group", "supergroup"}:
        raise ValueError("יש לבחור קבוצת גיבוי, ולא צ׳אט פרטי או ערוץ.")
    if member.status not in {"creator", "administrator", "member", "restricted"}:
        raise ValueError("הוסף את הבוט לקבוצת הגיבוי תחילה.")
    if member.status == "restricted" and (
        getattr(member, "can_send_messages", False) is not True or
        getattr(member, "can_send_documents", False) is not True
    ):
        raise ValueError("אין לבוט הרשאה לשלוח הודעות בקבוצת הגיבוי.")
    if member.status not in {"creator", "administrator"}:
        permissions = getattr(chat, "permissions", None)
        if permissions and (getattr(permissions, "can_send_messages", None) is False or
                            getattr(permissions, "can_send_documents", None) is False):
            raise ValueError("אפשר לבוט לשלוח הודעות וקבצים בקבוצת הגיבוי.")
    with _LOCK:
        config = load_config(config_file)
        config["LOG_CHAT_ID"] = target
        save_json(config_file, config)
    return target


def cleanup_command_sources(bot, actor, state=None):
    sources = _sources.get()
    if sources is None:
        sources = (state or {}).get("command_sources", [])
    proofs = [source_proof(source) for source in sources]
    protection = getattr(bot, "private_admin_protection", None)
    if isinstance(protection, PrivateAdminProtection):
        return protection.delete_command_inputs(actor, proofs)
    failed = 0
    ids = {_reason_message_id(actor, proof) for proof in proofs} - {None}
    for mid in ids:
        try:
            if bot.delete_message(actor, mid) is False:
                failed += 1
            else:
                from admin_edit_flow import record_deleted
                record_deleted(actor, mid)
        except Exception:
            failed += 1
    return failed


def notify_command(ui, bot, actor, notice):
    """Input feedback adds an acknowledgement button; never send raw receipts."""
    ui.send_message(actor, notice)


def archive_command(bot, keyword, commands_file, config_file, actor, state=None, created=False, cleanup=True):
    """A failed archive never authorizes private input deletion."""
    if not is_admin(actor):
        return "אין הרשאה לגבות פקודות."
    try:
        target = load_config(config_file)["LOG_CHAT_ID"]
        if target is None:
            from admin_edit_flow import archive_warning
            archive_warning()
            return "⚠️ הפקודה נשמרה מקומית, אך לא גובתה. הגדר קבוצת גיבוי."
        response = load_bot_data(commands_file)["responses"][keyword]
        operation = "נוצרה" if created else "עודכנה"
        snapshot = {
            "keyword": keyword, "response": response, "admin_id": actor,
            "operation": "create" if created else "update",
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        document = io.BytesIO(json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8"))
        document.name = "command-backup.json"
        caption = (
            f"📦 פקודה {operation} וגובתה\nשם: {keyword}\nמנהל: {actor}\n"
            f"תוכן:\n{response['text'][:650]}\n\nהפקודה המלאה והכפתורים בקובץ המצורף."
        )
        from admin_notifications import send_admin_document
        result = send_admin_document(
            bot, target, document,
            config_file=config_file,
            caption=html.escape(caption), parse_mode="HTML",
        )
        if result is None or result is False:
            raise RuntimeError("Archive delivery was not confirmed.")
    except Exception as exc:
        from admin_edit_flow import archive_warning
        archive_warning()
        logging.warning("Command archive failed (%s).", type(exc).__name__)
        return "⚠️ הפקודה נשמרה מקומית, אך הגיבוי נכשל. בדוק את הקבוצה והרשאות הבוט."
    failed = cleanup_command_sources(bot, actor, state) if cleanup else 0
    if failed:
        return f"✅ הפקודה {operation} וגובתה בקבוצה! ⚠️ חלק מהודעות הקלט לא נמחקו."
    return f"✅ הפקודה {operation} בהצלחה וגובתה בקבוצה!"
