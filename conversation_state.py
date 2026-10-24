"""Reset transient conversation state without deleting user or content records."""

import logging
from pathlib import Path

from telebot.handler_backends import BaseMiddleware

from bot_storage import is_admin
from management_config import CONFIG_FILE, _LOCK as CONFIG_LOCK, load_config, save_json
from media_storage import MEDIA_DB_FILE, MediaRepository
from support_storage import clear_pending_support_users
from vip_access import VIP_SETTINGS_FILE, VIPSettingsStore

COMMAND_DRAFTS_FILE = Path(__file__).resolve().with_name("command_drafts.json")
VIP_MEDIA_DB_FILE = Path(__file__).resolve().with_name("vip_media_db.json")


def _attempt(label, operation):
    try:
        return operation()
    except Exception:
        logging.exception("Could not clear %s conversation state.", label)
        return None


def clear_persisted_conversation_state(
    user_id: int | None = None,
    *,
    config_file=CONFIG_FILE,
    command_drafts_file=COMMAND_DRAFTS_FILE,
    media_db_file=MEDIA_DB_FILE,
    vip_media_db_file=VIP_MEDIA_DB_FILE,
    support_threads_file=None,
    vip_settings_file=VIP_SETTINGS_FILE,
) -> dict[str, int]:
    """Clear wizard/input records for one user, or flush all such records."""
    counts = {}

    def clear_admin_drafts():
        with CONFIG_LOCK:
            config = load_config(config_file)
            drafts = config.get("admin_input_drafts", {})
            if not isinstance(drafts, dict):
                raise ValueError("Invalid saved admin input drafts.")
            if user_id is None:
                removed = len(drafts)
                config["admin_input_drafts"] = {}
            else:
                removed = int(drafts.pop(str(user_id), None) is not None)
                config["admin_input_drafts"] = drafts
            if removed:
                save_json(config_file, config)
            return removed

    counts["admin_inputs"] = _attempt("admin input", clear_admin_drafts) or 0

    def clear_report_drafts():
        if not Path(command_drafts_file).exists():
            return 0
        from command_reports import load_drafts
        from management_config import save_json as save
        data = load_drafts(command_drafts_file)
        drafts = data["drafts"]
        if user_id is None:
            removed = len(drafts)
            data["drafts"] = {}
        else:
            keys = [
                key for key, value in drafts.items()
                if value.get("admin_id") == user_id
            ]
            for key in keys:
                drafts.pop(key, None)
            removed = len(keys)
        if removed:
            save(command_drafts_file, data)
        return removed

    counts["report_drafts"] = _attempt("report draft", clear_report_drafts) or 0

    def clear_media_drafts():
        standard = MediaRepository(media_db_file).clear_drafts(user_id)
        vip = MediaRepository(vip_media_db_file).clear_drafts(user_id)
        return standard + vip

    counts["media_drafts"] = _attempt("media upload", clear_media_drafts) or 0
    counts["support_prompts"] = _attempt(
        "support prompt",
        lambda: clear_pending_support_users(user_id, support_threads_file)
        if support_threads_file is not None
        else clear_pending_support_users(user_id),
    ) or 0
    counts["vip_code_prompts"] = _attempt(
        "VIP code prompt",
        lambda: VIPSettingsStore(vip_settings_file).clear_pending(user_id),
    ) or 0
    return counts


def flush_persisted_conversation_state(**paths) -> dict[str, int]:
    """One-time process-start cleanup; durable users and saved content remain."""
    counts = clear_persisted_conversation_state(None, **paths)
    logging.info(
        "Flushed transient conversation state on startup: %s",
        ", ".join(f"{key}={value}" for key, value in counts.items()),
    )
    return counts


class ConversationStateResetMiddleware(BaseMiddleware):
    """Reset persisted wizards before authorization middleware can intercept /start."""

    def __init__(self):
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message", "callback_query"]

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        text = (getattr(message, "text", None) or "").strip()
        command = text.split(maxsplit=1)[0].partition("@")[0].casefold() if text else ""
        if user is not None and command in {"/start", "/cancel"}:
            clear_persisted_conversation_state(user.id)

    def pre_process_callback_query(self, call, data):
        user = getattr(call, "from_user", None)
        if user is not None and is_admin(user.id) and (call.data or "") == "adm:menu":
            clear_persisted_conversation_state(user.id)

    def post_process_message(self, message, data, exception):
        return None

    def post_process_callback_query(self, call, data, exception):
        return None
