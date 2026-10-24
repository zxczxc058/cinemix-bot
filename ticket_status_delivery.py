"""Shared delivery and feedback for administrator ticket-status buttons."""

import html
import logging

from bot_storage import format_telegram_text
from management_config import load_config


DELIVERY_ERROR = "שגיאה בשליחת הודעה למשתמש (ייתכן שהבוט חסום)"


def send_status_notification(bot, config_file, user_id, result, title):
    # Read on every action so private admin edits apply immediately.
    template = load_config(config_file)["missing_content_messages"][result]
    body, parse_mode = format_telegram_text(
        template.replace("{CONTENT_NAME}", str(title)),
    )
    return bot.send_message(
        user_id, body, parse_mode=parse_mode, disable_web_page_preview=True,
    )


def status_feedback(bot, call, text, *, error=False):
    """Answer once with the actual delivery result, then update successful cards."""
    try:
        bot.answer_callback_query(call.id, text=text[:200], show_alert=True)
    except Exception as exc:
        logging.warning("Ticket-status callback acknowledgement failed (%s).", type(exc).__name__)
    if error:
        return
    message = call.message
    options = {
        "chat_id": message.chat.id, "message_id": message.message_id,
        "parse_mode": "HTML", "reply_markup": getattr(message, "reply_markup", None),
    }
    try:
        if getattr(message, "text", None):
            body = html.escape(message.text[:3500]) + "\n\n" + html.escape(text)
            bot.edit_message_text(body, disable_web_page_preview=True, **options)
        elif hasattr(message, "caption"):
            body = html.escape((getattr(message, "caption", None) or "")[:800])
            bot.edit_message_caption(caption=body + "\n\n" + html.escape(text), **options)
    except Exception as exc:
        # Delivery is already successful; an uneditable group card must not
        # reopen the request and cause a duplicate private notification.
        logging.warning(
            "Delivered ticket-status result could not update the card (%s).",
            type(exc).__name__,
        )
