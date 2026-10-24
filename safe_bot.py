"""One sending policy for formatting fallback and disabled link previews."""

from collections import OrderedDict
import threading
import logging
import os
import functools
from pathlib import Path

import telebot
from telebot import types
from telebot.handler_backends import BaseMiddleware

from safe_formatting import prepare
from support_storage import load_support_settings, load_support_threads
from bot_storage import is_admin


def entity_error(exc):
    return getattr(exc, "error_code", None) == 400 and any(
        phrase in str(getattr(exc, "description", "")).lower()
        for phrase in ("parse entities", "unsupported start tag", "entity offset", "wrong entity")
    )


class SafeTelegramBot(telebot.TeleBot):
    def answer_callback_query(self, callback_query_id, *args, **kwargs):
        try:
            return super().answer_callback_query(callback_query_id, *args, **kwargs)
        except Exception as exc:
            description = str(getattr(exc, "description", "")).lower()
            if getattr(exc, "error_code", None) == 400 and any(
                phrase in description for phrase in (
                    "query is too old", "response timeout expired", "query id is invalid"
                )
            ):
                # Queued callbacks survive a restart, but Telegram's popup IDs do not.
                # Losing that popup must not interrupt message polling or repeat actions.
                logging.info("Expired Telegram callback acknowledgement skipped.")
                return False
            raise

    def __init__(self, *args, **kwargs):
        self.polling_health = kwargs.pop("polling_health", None)
        self._shutdown_callback = kwargs.pop("shutdown_callback", None)
        super().__init__(*args, **kwargs)
        self._sources = OrderedDict()
        self._source_lock = threading.Lock()
        self.markup_cache = OrderedDict()

    def begin_shutdown(self):
        if self._shutdown_callback is not None:
            self._shutdown_callback()

    def stop_bot(self):
        self.begin_shutdown()
        # SDK stop_bot closes and joins every handler worker; no Telegram IO.
        super().stop_bot()

    def get_updates(self, *args, **kwargs):
        try:
            updates = super().get_updates(*args, **kwargs)
        except Exception as exc:
            if self.polling_health is not None:
                self.polling_health.failure(exc)
                if (
                    self.polling_health.runtime == "preview"
                    and getattr(exc, "error_code", None) == 409
                ):
                    logging.critical(
                        "Workspace preview stopped: another Telegram poller is active."
                    )
                    os._exit(1)
            raise
        if self.polling_health is not None:
            # Empty long-poll responses prove connectivity too.
            self.polling_health.success()
        return updates

    def polling(self, *args, **kwargs):
        failures_before = (
            self.polling_health.snapshot()["failure_count"]
            if self.polling_health is not None else None
        )
        try:
            return super().polling(*args, **kwargs)
        except BaseException as exc:
            from bot_shutdown import stop_interrupted_polling
            if not isinstance(exc, Exception):
                self.begin_shutdown()
            stop_interrupted_polling(self, exc)
            if (
                isinstance(exc, Exception)
                and self.polling_health is not None
                and self.polling_health.snapshot()["failure_count"] == failures_before
            ):
                self.polling_health.failure(exc)
            raise

    def infinity_polling(self, *args, **kwargs):
        if self.polling_health is not None:
            self.polling_health.start()
        try:
            return super().infinity_polling(*args, **kwargs)
        finally:
            if self.polling_health is not None:
                self.polling_health.stop()

    def _store_markup(self, chat_id, message_id, markup):
        key = (chat_id, message_id)
        self.markup_cache[key] = markup
        self.markup_cache.move_to_end(key)
        while len(self.markup_cache) > 2048:
            self.markup_cache.popitem(last=False)
        engine = getattr(self, "restriction_service", None)
        if engine is not None:
            event_key = f"{chat_id}:{message_id}"
            with engine.operation_lock:
                record = engine.events["messages"].get(event_key)
                if record is not None:
                    from management_config import save_json
                    record["markup"] = markup.to_dict() if markup else None
                    save_json(engine.events_file, engine.events)

    def remember(self, message):
        with self._source_lock:
            key = (message.chat.id, message.message_id)
            self._sources[key] = message
            self._sources.move_to_end(key)
            while len(self._sources) > 512:
                self._sources.popitem(last=False)

    def _formatted(self, method, text, kwargs, previews=False):
        kwargs = dict(kwargs)
        if previews:
            kwargs["disable_web_page_preview"] = True
            kwargs["link_preview_options"] = types.LinkPreviewOptions(is_disabled=True)
        original = text
        if kwargs.get("entities") or kwargs.get("caption_entities"):
            kwargs["parse_mode"] = None
        else:
            text, kwargs["parse_mode"] = prepare(text, kwargs.get("parse_mode"))
        try:
            return method(text, kwargs)
        except Exception as exc:
            if not entity_error(exc):
                raise
            kwargs.update(parse_mode=None)
            kwargs.pop("entities", None)
            kwargs.pop("caption_entities", None)
            return method(original, kwargs)

    def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        kwargs["parse_mode"] = parse_mode
        sent = self._formatted(
            lambda body, options: super(SafeTelegramBot, self).send_message(chat_id, body, **options),
            text, kwargs, previews=True,
        )
        self._store_markup(chat_id, sent.message_id, kwargs.get("reply_markup"))
        return sent

    def edit_message_text(self, text, **kwargs):
        result = self._formatted(
            lambda body, options: super(SafeTelegramBot, self).edit_message_text(body, **options),
            text, kwargs, previews=True,
        )
        if "reply_markup" in kwargs:
            self._store_markup(kwargs.get("chat_id"), kwargs.get("message_id"), kwargs["reply_markup"])
        return result

    def edit_message_reply_markup(self, *args, **kwargs):
        result = super().edit_message_reply_markup(*args, **kwargs)
        cid = kwargs.get("chat_id", args[0] if args else None)
        mid = kwargs.get("message_id", args[1] if len(args) > 1 else None)
        self._store_markup(cid, mid, kwargs.get("reply_markup"))
        return result

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        if caption is None:
            return super().send_photo(chat_id, photo, **kwargs)
        return self._formatted(
            lambda body, options: super(SafeTelegramBot, self).send_photo(chat_id, photo, caption=body, **options),
            caption, kwargs,
        )

    def send_document(self, chat_id, document, caption=None, **kwargs):
        if caption is None:
            return super().send_document(chat_id, document, **kwargs)
        return self._formatted(
            lambda body, options: super(SafeTelegramBot, self).send_document(chat_id, document, caption=body, **options),
            caption, kwargs,
        )

    def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        with self._source_lock:
            source = self._sources.get((from_chat_id, message_id))
        if source is not None and getattr(source, "text", None) is not None:
            text = source.html_text if source.entities else source.text
            options = {key: value for key, value in kwargs.items() if key not in {"caption", "caption_entities", "parse_mode", "show_caption_above_media", "video_start_timestamp"}}
            options["parse_mode"] = "HTML" if source.entities else None
            return types.MessageID(self.send_message(chat_id, text, **options).message_id)
        if source is not None and getattr(source, "caption", None):
            caption = source.html_caption if source.caption_entities else source.caption
            kwargs["caption"] = caption
            kwargs["parse_mode"] = "HTML" if source.caption_entities else None
        if kwargs.get("caption"):
            result = self._formatted(
                lambda body, options: super(SafeTelegramBot, self).copy_message(
                    chat_id, from_chat_id, message_id, caption=body, **options
                ), kwargs["caption"], {key: value for key, value in kwargs.items() if key != "caption"},
            )
            return _record_outgoing(self, result)
        return _record_outgoing(self, super().copy_message(chat_id, from_chat_id, message_id, **kwargs))


def _record_outgoing(bot, result):
    items = result if isinstance(result, (list, tuple)) else (result,)
    count = sum(getattr(item, "message_id", None) is not None for item in items)
    if not count:
        return result
    try:
        from system_features import record_sent_message
        record_sent_message(count, getattr(bot, "activity_file", None)
                            or Path(__file__).resolve().with_name("activity.json"))
    except Exception as exc:
        logging.warning("Could not update sent-message statistics (%s).", type(exc).__name__)
    return result


def _track_outgoing(method):
    @functools.wraps(method)
    def tracked(self, *args, **kwargs):
        chat_id = kwargs.get("chat_id", args[0] if args else None)
        if chat_id is not None:
            try:
                from system_features import load_system_settings
                if load_system_settings().get("anti_forward_enabled"):
                    from management_config import load_config
                    config = load_config()
                    protected_destinations = {
                        config.get("ADMIN_CHAT_ID"), config.get("LOG_CHAT_ID"),
                    }
                    try:
                        numeric_chat_id = int(chat_id)
                    except (TypeError, ValueError):
                        numeric_chat_id = None
                    is_management_chat = numeric_chat_id in protected_destinations
                    is_private_admin = (
                        numeric_chat_id is not None
                        and numeric_chat_id > 0
                        and is_admin(numeric_chat_id)
                    )
                    if not is_management_chat and not is_private_admin:
                        kwargs.setdefault("protect_content", True)
            except Exception as exc:
                # A broken settings file must be visible, but should not prevent
                # the bot from delivering unrelated messages.
                logging.error("Could not apply anti-forward protection (%s).", type(exc).__name__)
        result = method(self, *args, **kwargs)
        return _record_outgoing(self, result)
    return tracked


# Count successful Bot API sends, including media. `copy_message` is counted in
# its specialized override so cached text copies that call send_message aren't doubled.
for _outgoing_method in (
    "send_message", "send_photo", "send_document", "send_audio", "send_video",
    "send_voice", "send_animation", "send_sticker", "send_video_note",
    "send_location", "send_contact", "send_poll", "send_dice", "send_venue",
    "send_media_group", "send_game", "send_invoice", "send_paid_media",
    "forward_message", "forward_messages",
):
    if hasattr(SafeTelegramBot, _outgoing_method):
        setattr(SafeTelegramBot, _outgoing_method,
                _track_outgoing(getattr(SafeTelegramBot, _outgoing_method)))


class SupportFormattingSource(BaseMiddleware):
    """Cache only live relay inputs, never passcodes or unrelated private admin input."""

    def __init__(self, bot, group_only, settings_file=None, threads_file=None):
        super().__init__()
        self.bot, self.group_only = bot, group_only
        self.settings_file, self.threads_file = settings_file, threads_file
        self.update_sensitive = True
        self.update_types = ["message"]

    def pre_process_message(self, message, data):
        from support_storage import SUPPORT_SETTINGS_FILE, SUPPORT_THREADS_FILE
        user = getattr(message, "from_user", None)
        if user is None:
            return
        if self.group_only:
            target = load_support_settings(self.settings_file or SUPPORT_SETTINGS_FILE)["support_chat_id"]
            if is_admin(user.id) and message.chat.id == target and message.reply_to_message:
                self.bot.remember(message)
        elif message.chat.type == "private":
            pending = load_support_threads(self.threads_file or SUPPORT_THREADS_FILE)["pending_users"]
            if user.id in pending:
                self.bot.remember(message)

    def post_process_message(self, message, data, exception):
        return None
