"""Text/photo broadcasts with URL/support buttons and truthful user/group reports."""

import logging
import time

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot_storage import format_telegram_text, load_group_ids, load_moderation_records, load_user_ids
from management_config import is_link_input, resolve_text, load_config
from broadcast_presets import validate_payload


def broadcast_markup(buttons, config_file):
    if not buttons:
        return None
    keyboard = InlineKeyboardMarkup(row_width=1)
    for button in buttons:
        if button.get("support"):
            keyboard.add(InlineKeyboardButton(button["text"], callback_data="support:open"))
        else:
            url = resolve_text(button["url"], config_file)
            if not is_link_input(url) or url.startswith("{"):
                raise ValueError("קישור תפוצה לא תקין.")
            keyboard.add(InlineKeyboardButton(button["text"], url=url))
    return keyboard


def send_payload(bot, target, payload, markup, config_file):
    payload = validate_payload(payload)
    text, mode = format_telegram_text(resolve_text(payload["text"], config_file))
    if payload["kind"] == "text":
        return bot.send_message(
            target, text, parse_mode=mode, reply_markup=markup,
            disable_web_page_preview=True,
        )
    sender = getattr(bot, f"send_{payload['kind']}")
    return sender(
        target, payload["file_id"], caption=text or None, parse_mode=mode,
        reply_markup=markup,
    )


def _send_with_retry(bot, target, payload, markup, config_file):
    try:
        return send_payload(bot, target, payload, markup, config_file)
    except Exception as exc:
        response = getattr(exc, "result_json", None)
        parameters = response.get("parameters", {}) if isinstance(response, dict) else {}
        parameters = parameters if isinstance(parameters, dict) else {}
        retry = parameters.get("retry_after")
        if getattr(exc, "error_code", None) == 429 and isinstance(retry, (int, float)) and 0 <= retry <= 60:
            time.sleep(retry + 0.1)
            return send_payload(bot, target, payload, markup, config_file)
        raise


def send_broadcast(bot, users_file, groups_file, support_file, config_file,
                   payload, buttons=None, include_groups=True, pace=0.04, initiator_id=None,
                   include_users=True):
    config = load_config(config_file)
    report_chat = config["ADMIN_CHAT_ID"]
    if report_chat is None:
        raise ValueError("הגדר קודם קבוצת ניהול עם /set_admin כדי לקבל את דוח התפוצה.")
    markup = broadcast_markup(buttons or [], config_file)
    # Validate content and dynamic links before sending any recipient's message.
    payload = validate_payload(payload)
    resolve_text(payload["text"], config_file)
    # Fail before delivery if bot identity cannot be verified.
    try:
        bot_id = bot.get_me().id
    except Exception as exc:
        raise ValueError("לא ניתן לאמת את מזהה הבוט לפני התפוצה. נסה שוב מאוחר יותר.") from exc
    if type(bot_id) is not int or bot_id <= 0:
        raise ValueError("מזהה הבוט אינו תקין; התפוצה לא נשלחה.")
    excluded_ids = set(config["admin_ids"]) | {bot_id}
    if type(initiator_id) is int and initiator_id > 0:
        excluded_ids.add(initiator_id)
    users = load_user_ids(users_file) if include_users else set()
    excluded = users & excluded_ids
    records = load_moderation_records(users_file) if include_users else {}
    banned = {uid for uid in users - excluded if records.get(str(uid), {}).get("banned")}
    result = {"total": len(users), "sent": 0, "blocked": 0, "failed": 0, "skipped": len(banned),
              "excluded": len(excluded),
              "groups_total": 0, "groups_sent": 0, "groups_failed": 0}
    for uid in sorted(users - banned - excluded):
        try:
            _send_with_retry(bot, uid, payload, markup, config_file)
        except Exception as exc:
            description = str(getattr(exc, "description", None) or exc).lower()
            if getattr(exc, "error_code", None) == 403 and "blocked by the user" in description:
                result["blocked"] += 1
            else:
                result["failed"] += 1
            logging.warning("Broadcast delivery failed (%s).", type(exc).__name__)
        else:
            result["sent"] += 1
        if pace:
            time.sleep(pace)
    if include_groups:
        from allowed_chats import chat_is_active
        groups = {gid for gid in load_group_ids(groups_file) if chat_is_active(bot, gid)}
        result["groups_total"] = len(groups)
        for gid in sorted(groups):
            try:
                _send_with_retry(bot, gid, payload, markup, config_file)
            except Exception as exc:
                result["groups_failed"] += 1
                logging.warning("Group broadcast failed (%s).", type(exc).__name__)
            else:
                result["groups_sent"] += 1
            if pace:
                time.sleep(pace)
    report = (
        f"📊 דוח תפוצה מפורט\n"
        f"📨 סך הכל נשלחו: {result['sent'] + result['groups_sent']}\n"
        f"❌ סך הכל נכשלו: {result['failed'] + result['blocked'] + result['groups_failed']}\n"
        f"👤 פרטיים — סך הכל משתמשים: {result['total']}\n✅ נשלח בהצלחה: {result['sent']}\n"
        f"⛔ חסמו את הבוט: {result['blocked']}\n❌ נכשלו: {result['failed']}\n"
        f"דילוג על חסומים בניהול הבוט: {result['skipped']}\n"
        f"הוחרגו מנהלים, מפעיל התפוצה והבוט: {result['excluded']}\n"
        f"👥 קבוצות — סך הכל יעדים: {result['groups_total']}\n"
        f"קבוצות: {result['groups_sent']} הצליחו, {result['groups_failed']} נכשלו."
    )
    try:
        from admin_notifications import send_admin_message
        send_admin_message(
            bot, report_chat, report, config_file=config_file,
        )
        result["report_delivered"] = True
    except Exception as exc:
        logging.warning("Broadcast report failed (%s).", type(exc).__name__)
        result["report_delivered"] = False
    return result
