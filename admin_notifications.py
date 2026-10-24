"""Direct delivery helpers for notifications sent to the admin group."""

import logging
import math

from requests.exceptions import ConnectionError as RequestConnectionError, Timeout

from management_config import CONFIG_FILE, load_config

logger = logging.getLogger(__name__)


def delivery_error_code(exc):
    code = getattr(exc, "error_code", None)
    if code is None:
        response = getattr(exc, "result", None) or getattr(exc, "response", None)
        code = getattr(response, "status_code", None)
    return code


def delivery_retry_delay(exc, attempts):
    """Return a bounded delay for temporary failures, or None for correction."""
    code = delivery_error_code(exc)
    result = getattr(exc, "result_json", None) or {}
    if code == 429:
        value = result.get("parameters", {}).get("retry_after", 0)
        try:
            retry_after = float(value)
        except (TypeError, ValueError):
            retry_after = 0
        if not math.isfinite(retry_after):
            retry_after = 0
        # Never retry before Telegram's requested time, even above our cap.
        return max(min(300, 5 * 2 ** min(attempts - 1, 6)), retry_after)
    if code in {500, 502, 503, 504} or isinstance(
        exc, (Timeout, RequestConnectionError, TimeoutError, ConnectionError),
    ):
        return min(300, 5 * 2 ** min(attempts - 1, 6))
    return None


def controls_fallback_allowed(exc):
    """Do not immediately repeat timed-out, rate-limited or forbidden sends."""
    code = delivery_error_code(exc)
    if delivery_retry_delay(exc, 1) is not None or code == 403:
        return False
    if code == 400:
        description = str(getattr(exc, "description", "")).lower()
        return any(word in description for word in (
            "reply markup", "reply_markup", "button", "callback_data", "keyboard",
        ))
    return code is None


def _resolve_legacy_admin_chat(fallback_chat_id=None, *, config_file=CONFIG_FILE):
    """Resolve the configured admin group, accepting legacy ID field names."""
    try:
        config = load_config(config_file)
    except FileNotFoundError:
        config = {}
    except Exception as exc:
        logger.exception(
            "Could not load the admin destination from %s (%s).",
            config_file, type(exc).__name__,
        )
        raise

    configured = next(
        (
            config.get(key)
            for key in ("ADMIN_CHAT_ID", "ADMIN_GROUP_ID", "admin_chat_id")
            if config.get(key) is not None
        ),
        None,
    )
    if configured is not None:
        if type(configured) is int and configured < 0:
            return configured
        logger.error(
            "Configured admin destination is invalid (field=%s, type=%s).",
            "ADMIN_CHAT_ID/ADMIN_GROUP_ID", type(configured).__name__,
        )
        raise ValueError("The configured admin destination must be a negative Telegram chat ID.")

    # LOG_CHANNEL_ID is a dedicated delivery fallback, not the backup group's
    # canonical field. Never rewrite LOG_CHAT_ID when a log channel is set.
    log_channel = config.get("LOG_CHANNEL_ID")
    if log_channel is not None:
        fallback_chat_id = log_channel
    elif fallback_chat_id is None:
        fallback_chat_id = config.get("LOG_CHAT_ID")
    if fallback_chat_id is None:
        return None
    if type(fallback_chat_id) is int and fallback_chat_id < 0:
        return fallback_chat_id
    logger.error(
        "Legacy admin destination is invalid (type=%s).",
        type(fallback_chat_id).__name__,
    )
    raise ValueError("The fallback admin destination must be a negative Telegram chat ID.")


def resolve_user_management_chat(fallback_chat_id=None, *, config_file=CONFIG_FILE):
    """Support traffic never follows the separate commands/logs destination."""
    try:
        config = load_config(config_file)
    except FileNotFoundError:
        config = {}
    target = config.get("LOG_CHANNEL_ID")
    if target is None:
        return _resolve_legacy_admin_chat(fallback_chat_id, config_file=config_file)
    if type(target) is not int or target >= 0:
        raise ValueError("LOG_CHANNEL_ID must be a negative Telegram chat ID.")
    return target


def resolve_admin_chat(fallback_chat_id=None, *, config_file=CONFIG_FILE):
    """Operational notifications use the logs group, defaulting to support."""
    try:
        config = load_config(config_file)
    except FileNotFoundError:
        config = {}
    target = config.get("ADMIN_COMMANDS_CHAT_ID")
    if target is None:
        return resolve_user_management_chat(fallback_chat_id, config_file=config_file)
    if type(target) is not int or target >= 0:
        raise ValueError("ADMIN_COMMANDS_CHAT_ID must be a negative Telegram chat ID.")
    return target


def send_user_management_message(bot, fallback_chat_id, text, *, config_file=CONFIG_FILE, **kwargs):
    target = resolve_user_management_chat(fallback_chat_id, config_file=config_file)
    if target is None:
        raise ValueError("לא הוגדרה קבוצת פניות משתמשים.")
    return _call_admin_api(bot, "send_message", target, text, **kwargs)


def _require_admin_chat(fallback_chat_id, config_file):
    target = resolve_admin_chat(fallback_chat_id, config_file=config_file)
    if target is None:
        logger.error("Admin notification was not sent: no admin group is configured.")
        raise ValueError("לא הוגדר ADMIN_CHAT_ID שלילי לקבוצת הניהול.")
    return target


def _call_admin_api(bot, method_name, target, *args, **kwargs):
    try:
        return getattr(bot, method_name)(target, *args, **kwargs)
    except Exception as exc:
        logger.error(
            "Admin-group delivery failed (method=%s, chat_id=%s, error=%s, code=%s).",
            method_name, target, type(exc).__name__, getattr(exc, "error_code", None),
        )
        raise


def send_admin_message(
    bot,
    fallback_chat_id,
    text,
    *,
    config_file=CONFIG_FILE,
    **kwargs,
):
    target = _require_admin_chat(fallback_chat_id, config_file)
    return _call_admin_api(bot, "send_message", target, text, **kwargs)


def send_admin_document(
    bot,
    fallback_chat_id,
    document,
    *,
    config_file=CONFIG_FILE,
    **kwargs,
):
    target = _require_admin_chat(fallback_chat_id, config_file)
    return _call_admin_api(bot, "send_document", target, document, **kwargs)


def copy_admin_message(
    bot,
    fallback_chat_id,
    from_chat_id,
    message_id,
    *,
    config_file=CONFIG_FILE,
    **kwargs,
):
    target = _require_admin_chat(fallback_chat_id, config_file)
    return _call_admin_api(
        bot, "copy_message", target, from_chat_id, message_id, **kwargs,
    )
