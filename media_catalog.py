"""Private admin CRUD and user-facing delivery for movies and series."""

from __future__ import annotations

import logging
import json
import copy
import re
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from admin_menus import MEDIA_MENU, VIP_MEDIA_MENU, build_menu
from bot_storage import build_inline_keyboard, is_admin, match_reply
from media_analytics import MEDIA_ANALYTICS_FILE, MediaAnalytics
from media_storage import DuplicateMediaError, MediaRepository, media_token
from drive_links import drive_file_id_from_text, extract_drive_url
from youtube_links import extract_youtube_url, extract_youtube_urls
from media_genres import genre_list, genre_text, has_genre


USER_PAGE_SIZE = 12
ADMIN_PAGE_SIZE = 15
MAX_NAME_LENGTH = 100
VIP_MEDIA_DB_FILE = Path(__file__).resolve().with_name("vip_media_db.json")


def build_private_start_keyboard(start_buttons=(), *, enabled=True, config_path=None):
    """Load the complete welcome keyboard; never append hidden hardcoded buttons."""
    from start_keyboard import build_keyboard
    from management_config import CONFIG_FILE
    return build_keyboard(start_buttons, enabled=enabled, path=config_path or CONFIG_FILE)


def _clean_name(value: str) -> str:
    return " ".join(value.split()).strip()


def _valid_drive_url(value: str) -> bool:
    return extract_drive_url(value) is not None


def _poster_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or len(value) > 2048:
        raise ValueError("שלח כתובת תמונה תקינה המתחילה ב-https:// או http://.")
    return value


def _message_drive_url(message, text: str = "") -> str | None:
    values = [
        text,
        getattr(message, "caption", None) or "",
        getattr(message, "text", None) or "",
    ]
    for entity in [
        *(getattr(message, "entities", None) or []),
        *(getattr(message, "caption_entities", None) or []),
    ]:
        value = entity if isinstance(entity, dict) else (
            entity.to_dict() if hasattr(entity, "to_dict") else {}
        )
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            values.append(value["url"])
    return extract_drive_url(" ".join(values))


def _message_youtube_urls(message, text: str = "") -> list[str]:
    values = [text, getattr(message, "caption", None) or "", getattr(message, "text", None) or ""]
    for entity in [
        *(getattr(message, "entities", None) or []),
        *(getattr(message, "caption_entities", None) or []),
    ]:
        value = entity if isinstance(entity, dict) else (
            entity.to_dict() if hasattr(entity, "to_dict") else {}
        )
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            values.append(value["url"])
    return list(dict.fromkeys(
        url for value in values for url in extract_youtube_urls(value)
    ))


def _button(label: str, callback: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=callback)


def _caption_entities(message) -> list[dict]:
    entities = getattr(message, "caption_entities", None) or []
    serialized = []
    for entity in entities:
        if isinstance(entity, dict):
            value = entity
        elif hasattr(entity, "to_dict"):
            value = entity.to_dict()
        else:
            continue
        try:
            # Round-trip through JSON so the on-disk record contains only JSON
            # values, not Telegram library objects.
            serialized.append(json.loads(json.dumps(value, ensure_ascii=False)))
        except (TypeError, ValueError):
            logging.warning("Skipping a non-serializable Telegram caption entity.")
    return serialized


def sync_edited_media_message(message, catalogues, *, allow_channel_post=False) -> bool:
    """Apply an edited Telegram Drive URL only to one unambiguous bot media record."""
    author = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    is_channel_post = getattr(chat, "type", None) == "channel"
    if author is not None:
        if not is_admin(getattr(author, "id", None)):
            return False
    elif not (allow_channel_post and is_channel_post):
        return False

    body = getattr(message, "caption", None) or getattr(message, "text", None) or ""
    entity_urls = []
    for entity in [
        *(getattr(message, "entities", None) or []),
        *(getattr(message, "caption_entities", None) or []),
    ]:
        value = entity if isinstance(entity, dict) else (
            entity.to_dict() if hasattr(entity, "to_dict") else {}
        )
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            entity_urls.append(value["url"])
    link_text = " ".join([body, *entity_urls])
    drive_url = extract_drive_url(link_text)
    youtube_urls = _message_youtube_urls(message, link_text)
    if not drive_url and not youtube_urls:
        return False

    file_ids = set()
    for media_type in ("video", "document", "animation", "audio"):
        media = getattr(message, media_type, None)
        file_id = getattr(media, "file_id", None)
        if isinstance(file_id, str) and file_id:
            file_ids.add(file_id)
    photos = getattr(message, "photo", None) or []
    if photos:
        file_id = getattr(photos[-1], "file_id", None)
        if isinstance(file_id, str) and file_id:
            file_ids.add(file_id)

    matching_records = []
    all_records = [
        (catalogue, record)
        for catalogue in catalogues
        for record in catalogue.repository.list_media()
    ]
    if file_ids:
        matching_records = [
            (catalogue, record)
            for catalogue, record in all_records
            if any(file.get("file_id") in file_ids for file in record.get("telegram_files", []))
        ]

    if not matching_records:
        normalized_body = re.sub(r"https?://\S+", " ", body, flags=re.IGNORECASE)
        def normalize_title(value: str) -> str:
            value = re.sub(
                r"^(?:שם הסרט|שם|כותרת|title|movie)\s*[:：—–-]\s*",
                "",
                value.strip(),
                flags=re.IGNORECASE,
            )
            value = re.sub(r"^[^\w]+", "", value, flags=re.UNICODE)
            return " ".join(value.casefold().split())

        candidate_lines = {
            normalize_title(line.strip(" \t•|:—–-"))
            for line in normalized_body.splitlines()
        }
        candidate_lines.discard("")
        matching_records = []
        for catalogue, record in all_records:
            names = {
                normalize_title(str(record.get(key, "")))
                for key in ("command_name", "title", "display_title")
                if record.get(key)
            }
            if candidate_lines.intersection(names):
                matching_records.append((catalogue, record))

    if len(matching_records) != 1:
        return False

    catalogue, record = matching_records[0]
    changes = {}
    if drive_url:
        changes["drive_url"] = drive_url
        if file_ids:
            telegram_files = copy.deepcopy(record.get("telegram_files", []))
            updated_file = False
            for file in telegram_files:
                if file.get("file_id") in file_ids:
                    file["drive_url"] = drive_url
                    file["drive_file_id"] = drive_file_id_from_text(drive_url)
                    updated_file = True
            if updated_file:
                changes["telegram_files"] = telegram_files
                if record.get("type") == "series" and (
                    len(telegram_files) > 1 or "/folders/" in (record.get("drive_url") or "")
                ):
                    # Editing one episode must not replace the whole show's
                    # season folder or another episode's default Drive link.
                    changes.pop("drive_url", None)
    if youtube_urls:
        changes["youtube_urls"] = list(dict.fromkeys(
            [*record.get("youtube_urls", []), *youtube_urls]
        ))
        changes["trailer_url"] = youtube_urls[0]
    catalogue.repository.update(record["command_name"], **changes)
    logging.info("Synced media links from an edited Telegram media post.")
    return True


class MediaCatalogue:
    def __init__(
        self, bot, registry, inbox, repository: MediaRepository | None = None, *,
        admin_prefix="adm:media", user_prefix="catalog", vip_access=None,
    ):
        self.bot = bot
        self.registry = registry
        self.inbox = inbox
        self.repository = repository or MediaRepository()
        self.admin_prefix = admin_prefix
        self.user_prefix = user_prefix
        self.vip_access = vip_access
        self.analytics = getattr(inbox, "analytics", None) or MediaAnalytics(
            self.repository.path.with_name(MEDIA_ANALYTICS_FILE.name),
        )

    def _admin_callback(self, callback: str) -> str:
        if self.admin_prefix == "adm:media":
            return callback
        if callback == "adm:media":
            return "adm:vip_media"
        if callback.startswith("adm:media_"):
            return self.admin_prefix + callback[len("adm:media"):]
        return callback

    def _user_callback(self, action: str, *parts) -> str:
        suffix = ":".join(str(part) for part in parts)
        return f"{self.user_prefix}:{action}" + (f":{suffix}" if suffix else "")

    def register_handlers(self, bot) -> None:
        bot.message_handler(
            func=self.has_pending_admin_input,
            content_types=["text"],
        )(self.handle_admin_text)
        bot.message_handler(
            func=self.has_pending_admin_input,
            content_types=[
                "video", "document", "photo", "animation", "audio",
                "voice", "video_note", "sticker",
            ],
        )(self.handle_admin_file)
        bot.message_handler(
            func=self.matches_private_media_command,
            content_types=["text"],
        )(self.handle_private_media_command)
        bot.message_handler(
            func=self.matches_group_media_command,
            content_types=["text"],
        )(self.handle_group_media_command)
        bot.callback_query_handler(
            func=lambda call: bool(call.data) and call.data.startswith(f"{self.user_prefix}:")
        )(self.handle_user_callback)

    def pause(self, actor_id: int | None) -> None:
        if actor_id is None or not is_admin(actor_id):
            return
        draft = self.repository.get_draft(actor_id)
        if draft and draft.get("active", True):
            self.repository.update_draft(actor_id, active=False)

    def cancel_from_message(self, message) -> bool:
        if not self.has_pending_admin_input(message):
            return False
        actor, chat_id = message.from_user.id, message.chat.id
        self.repository.delete_draft(actor)
        self._show_media_menu(self.registry.get(actor), chat_id, "תהליך המדיה בוטל.")
        return True

    def has_pending_admin_input(self, message) -> bool:
        user = getattr(message, "from_user", None)
        if (
            user is None or not is_admin(user.id)
            or getattr(getattr(message, "chat", None), "type", None) != "private"
            or message.chat.id != user.id
        ):
            return False
        draft = self.repository.get_draft(user.id)
        return bool(draft and draft.get("active", True))

    def matches_private_media_command(self, message) -> bool:
        user = getattr(message, "from_user", None)
        text = getattr(message, "text", None)
        return bool(
            user and text and message.chat.type == "private"
            and self.repository.match_exact(text) is not None
            and match_reply(text) is None
        )

    def matches_group_media_command(self, message) -> bool:
        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        text = getattr(message, "text", None)
        return bool(
            user and not getattr(user, "is_bot", False) and text
            and getattr(chat, "type", None) in {"group", "supergroup"}
            and self.repository.match_exact(text) is not None
            and match_reply(text) is None
        )

    def handle_private_media_command(self, message) -> None:
        user = getattr(message, "from_user", None)
        if user is None:
            return
        item = self.repository.match_exact(message.text or "")
        if item is None:
            return
        if not self._check_item_access(user.id, message.chat.id, item):
            return
        if is_admin(user.id):
            # The manager handles content exclusively through the inline menu.
            return
        self._record_media_search(item["command_name"])
        self._show_delivery_options(message.chat.id, item)

    def handle_group_media_command(self, message) -> None:
        if getattr(getattr(message, "chat", None), "type", None) not in {"group", "supergroup"}:
            return
        item = self.repository.match_exact(getattr(message, "text", "") or "")
        if item is not None:
            user = getattr(message, "from_user", None)
            if (
                user is not None
                and not self._check_item_access(user.id, message.chat.id, item)
            ):
                return
            self._record_media_search(item["command_name"])
            self._show_delivery_options(message.chat.id, item, group=True)

    def _record_media_search(self, keyword: str) -> None:
        try:
            self.analytics.record_search(keyword)
        except Exception as exc:
            logging.error("Media search analytics could not be saved (%s).", type(exc).__name__)

    def handle_admin_action(self, dashboard, action: str, call, parts: list[str]) -> bool:
        if action.startswith("vip_media_"):
            action = "media_" + action[len("vip_media_"):]
            parts = list(parts)
            if len(parts) > 1:
                parts[1] = action
        actor = getattr(getattr(call, "from_user", None), "id", None)
        message = getattr(call, "message", None)
        if (
            not isinstance(actor, int) or not is_admin(actor) or message is None
            or message.chat.type != "private" or message.chat.id != actor
        ):
            dashboard.bot.answer_callback_query(
                call.id, text="ניהול מדיה זמין למנהלים בצ'אט הפרטי בלבד.", show_alert=True,
            )
            return True

        chat_id = message.chat.id
        if action == "media_add_movie":
            self._start_add(dashboard, chat_id, actor, "movie")
        elif action == "media_add_series":
            self._start_add(dashboard, chat_id, actor, "series")
        elif action == "media_list":
            self._send_media_summary(dashboard, chat_id, 0)
        elif action == "media_summary":
            self._send_media_summary(dashboard, chat_id, _page(parts))
        elif action == "media_edit_list":
            self._send_admin_picker(dashboard, chat_id, "edit", _page(parts))
        elif action == "media_delete_list":
            self._send_admin_picker(dashboard, chat_id, "delete", _page(parts))
        elif action in {"media_resume", "media_discard", "media_cancel"}:
            self._handle_draft_control(dashboard, chat_id, actor, action)
        elif action == "media_skip_drive":
            self._skip_drive(dashboard, chat_id, actor)
        elif action == "media_finish":
            self._finish_draft(dashboard, chat_id, actor)
        elif action == "media_access" and len(parts) == 3:
            draft = self.repository.get_draft(actor)
            if draft and draft.get("active", True) and draft.get("stage") == "access_level" and parts[2] in {"regular", "vip"}:
                self.repository.update_draft(
                    actor, access_level=parts[2],
                    stage="append_files" if draft.get("action") == "append_files" else "name",
                )
                self._prompt_for_stage(dashboard, chat_id, actor)
        elif action == "media_genre" and len(parts) == 3:
            from vip_experience import GENRES
            draft = self.repository.get_draft(actor)
            try:
                index = int(parts[2])
                if not 0 <= index < len(GENRES):
                    raise ValueError("Invalid genre.")
                if draft and draft.get("action") == "add" and draft.get("stage") == "genres":
                    selected = genre_list(draft.get("genre"))
                    label = GENRES[index]
                    if has_genre(selected, label):
                        selected = [g for g in selected if not has_genre([g], label)]
                    else:
                        selected.append(label)
                    self.repository.update_draft(actor, genre=genre_text(selected))
                    self._prompt_for_stage(dashboard, chat_id, actor)
            except ValueError:
                logging.warning("Invalid genre-selection callback.")
        elif action == "media_genres_done":
            draft = self.repository.get_draft(actor)
            if draft and draft.get("action") == "add" and draft.get("stage") == "genres":
                if not genre_list(draft.get("genre")):
                    self._send_admin_draft_message(actor, chat_id, "בחר לפחות ז'אנר אחד או הקלד ז'אנרים מופרדים בפסיקים.")
                else:
                    self.repository.update_draft(actor, stage="files")
                    self._finish_draft(dashboard, chat_id, actor)
        elif action == "media_edit_pick" and len(parts) == 3:
            self._show_edit_options(dashboard, chat_id, parts[2])
        elif action in {"media_edit_name", "media_edit_drive", "media_edit_files", "media_append_files"} and len(parts) == 3:
            self._begin_edit(dashboard, chat_id, actor, action, parts[2])
        elif action in {"media_add_trailer", "media_trailer_skip"}:
            draft = self.repository.get_draft(actor)
            if draft:
                if action == "media_add_trailer" and draft.get("stage") in {"files", "replace_files", "append_files"}:
                    self.repository.update_draft(actor, trailer_return_stage=draft["stage"], stage="new_trailer", active=True)
                elif action == "media_trailer_skip" and draft.get("stage") == "new_trailer":
                    self.repository.update_draft(actor, stage=draft.get("trailer_return_stage", "files"), active=True)
                self._prompt_for_stage(dashboard, chat_id, actor)
        elif action == "media_title" and self.repository.get_draft(actor):
            self.repository.update_draft(actor, stage="display_title", active=True)
            self._prompt_for_stage(dashboard, chat_id, actor)
        elif action == "media_skip_poster":
            draft = self.repository.get_draft(actor)
            if draft and draft.get("stage") == "poster":
                self.repository.update_draft(actor, poster_url=None, poster_file_id=None, stage="drive")
                self._prompt_for_stage(dashboard, chat_id, actor)
        elif action == "media_delete_poster" and len(parts) == 3:
            item = self.repository.get_by_token(parts[2])
            if item:
                self.repository.update(item["command_name"], poster_url=None, poster_file_id=None)
                # An editor opened before deletion must not restore the poster.
                draft = self.repository.get_draft(actor)
                if draft and draft.get("stage") == "edit_poster" and draft.get("target_command_name") == item["command_name"]:
                    self.repository.delete_draft(actor)
                self._show_edit_options(dashboard, chat_id, parts[2], prefix="✅ התמונה נמחקה.")
        elif action in {"media_edit_title", "media_edit_trailer", "media_edit_genre", "media_edit_poster"} and len(parts) == 3:
            item = self.repository.get_by_token(parts[2])
            if item:
                stage = action.removeprefix("media_")
                self.repository.save_draft(actor, {
                    "action": stage,
                    "stage": stage,
                    "chat_id": chat_id, "active": True, "target_command_name": item["command_name"],
                })
                self._prompt_for_stage(dashboard, chat_id, actor)
            else:
                self._send_admin_picker(dashboard, chat_id, "edit", 0, notice="המדיה שנבחרה כבר אינה קיימת.")
        elif action == "media_delete_pick" and len(parts) == 3:
            self._confirm_delete_menu(dashboard, chat_id, parts[2])
        elif action == "media_delete_confirm" and len(parts) == 3:
            self._delete_item(dashboard, chat_id, parts[2])
        elif action == "media_delete_cancel":
            self._send_admin_picker(dashboard, chat_id, "delete", 0)
        else:
            dashboard.bot.answer_callback_query(call.id, text="הפעולה אינה זמינה.")
        return True

    def _start_add(self, dashboard, chat_id: int, actor: int, media_type: str) -> None:
        existing = self.repository.get_draft(actor)
        if existing:
            self.repository.update_draft(actor, active=False)
            label = "סרט" if existing.get("type") == "movie" else "סדרה"
            self._screen(
                dashboard, chat_id,
                f"יש תהליך {label} שלא הסתיים. אפשר להמשיך אותו או למחוק את הטיוטה ולהתחיל מחדש.",
                [
                    ("▶️ המשך בתהליך הקודם", "adm:media_resume"),
                    ("🗑️ מחיקת הטיוטה", "adm:media_discard"),
                ],
                back="adm:media",
            )
            return
        self.repository.save_draft(actor, {
            "action": "add",
            "stage": "access_level" if media_type == "series" else "name",
            "type": media_type,
            "title": "",
            "command_name": "",
            "drive_url": None,
            "telegram_files": [],
            "bot_message_ids": [],
            "uploaded_message_ids": [],
            "active": True,
        })
        self._prompt_for_stage(dashboard, chat_id, actor)

    def _handle_draft_control(self, dashboard, chat_id: int, actor: int, action: str) -> None:
        if action == "media_discard" or action == "media_cancel":
            self.repository.delete_draft(actor)
            self._show_media_menu(dashboard, chat_id, "הטיוטה בוטלה.")
            return
        draft = self.repository.get_draft(actor)
        if not draft:
            self._show_media_menu(dashboard, chat_id, "לא נמצאה טיוטת מדיה להמשך.")
            return
        self.repository.update_draft(actor, active=True)
        self._prompt_for_stage(dashboard, chat_id, actor)

    def _prompt_for_stage(self, dashboard, chat_id: int, actor: int) -> None:
        draft = self.repository.get_draft(actor)
        if not draft:
            self._show_media_menu(dashboard, chat_id, "לא נמצאה טיוטת מדיה.")
            return
        stage = draft.get("stage")
        if stage == "access_level":
            self._screen(
                dashboard, chat_id,
                "בחר את רמת הגישה לסדרה כולה (כולל הפרקים הקיימים והחדשים):",
                [
                    ("🔓 סדרה רגילה (פתוח לכולם)", "adm:media_access:regular"),
                    ("⭐ סדרת VIP (בלעדי למנויים)", "adm:media_access:vip"),
                ],
                back="adm:media_cancel",
            )
        elif stage == "name":
            self._screen(dashboard, chat_id, "שלב 1: שלח את מילת ההפעלה המדויקת שהמשתמש יקליד.", [], back="adm:media_cancel")
        elif stage in {"poster", "edit_poster"}:
            self._screen(
                dashboard, chat_id, "שלח תמונה בצ'אט לפוסטר (אפשר גם קישור לתמונה).",
                [("⏭️ דילוג", "adm:media_skip_poster")] if stage == "poster" else [],
                back="adm:media_cancel",
            )
        elif stage == "drive":
            self._screen(
                dashboard, chat_id, "שלח קישור Google Drive או YouTube (youtube.com / youtu.be), או לחץ על דילוג.",
                [
                    ("⏭️ דילוג", "adm:media_skip_drive"),
                    ("✏️ הגדר שם להצגה (בנפרד מהפקודה)", "adm:media_title"),
                ], back="adm:media_cancel",
            )
        elif stage in {"files", "replace_files", "append_files"}:
            count = len(draft.get("telegram_files", []))
            label = "הגדרת סרט" if draft.get("type") == "movie" else "הגדרת סדרה"
            status = (
                f"התקבל פרק {count}. שלח פרק נוסף או לחץ סיום."
                if draft.get("type") == "series" and count
                else f"התקבלו {count} קבצים. שלח קובץ נוסף או לחץ סיום."
            )
            self._screen(
                dashboard, chat_id, (status if count else "שלח קובץ וידאו או קישור YouTube.")
                + f"\nאפשר להוסיף קישורי YouTube לסרט או לפרקים. קישורים שנקלטו: {len(draft.get('youtube_urls', []))}. בסיום לחץ ✅ סיום."
                + "\nטריילר YouTube הוא רשות: ניתן להוסיף או לשנות אותו בכפתור למטה.",
                [("🎬 הוספת/עריכת טריילר (רשות)", "adm:media_add_trailer"),
                 ("✅ סיום והגדרת " + ("סרט" if draft.get("type") == "movie" else "סדרה"), "adm:media_finish")],
                back="adm:media_cancel",
            )
        elif stage == "edit_name":
            self._screen(dashboard, chat_id, "שלח את שם הפקודה החדש.", [], back="adm:media_cancel")
        elif stage == "edit_drive":
            self._screen(dashboard, chat_id, "שלח קישור Drive חדש, או שלח „ללא” להסרת הקישור.", [], back="adm:media_cancel")
        elif stage == "edit_genre":
            self._screen(
                dashboard, chat_id, "שלח את הז'אנר החדש או מספר ז'אנרים מופרדים בפסיקים (למשל: אקשן, דרמה, קומדיה).",
                [], back=f"adm:media_edit_pick:{media_token(draft['target_command_name'])}",
            )
        elif stage == "genres":
            from vip_experience import GENRES
            self._screen(
                dashboard, chat_id,
                "בחר מספר ז'אנרים ולחץ „סיימתי”, או הקלד ז'אנרים מופרדים בפסיקים.\n"
                "נבחרו: " + (draft.get("genre") or "ללא"),
                [
                    (("✅ " if has_genre(draft.get("genre"), genre) else "") + genre,
                     f"adm:media_genre:{index}")
                    for index, genre in enumerate(GENRES)
                ] + [("✅ סיימתי / Done", "adm:media_genres_done")],
                back="adm:media_cancel",
            )
        elif stage == "new_trailer":
            self._screen(dashboard, chat_id, "שלח קישור YouTube לטריילר (youtube.com / youtu.be), או „ללא” להסרה.",
                         [("⏭️ דילוג / שמירת הקיים", "adm:media_trailer_skip")], back="adm:media_trailer_skip")
        elif stage in {"display_title", "edit_title", "edit_trailer"}:
            prompt = (
                "שלח קישור YouTube לטריילר, או „ללא” להסרה."
                if stage == "edit_trailer" else (
                    "שלב 2: שלח את הכותרת שתוצג למשתמשים. מילת ההפעלה נשארת נפרדת וללא שינוי."
                    if stage == "display_title" else "שלח את שם התוכן להצגה. מילת ההפעלה לא תשתנה."
                )
            )
            self._screen(dashboard, chat_id, prompt, [], back="adm:media_cancel")

    def _skip_drive(self, dashboard, chat_id: int, actor: int) -> None:
        draft = self.repository.get_draft(actor)
        if not draft or draft.get("stage") != "drive":
            dashboard.bot.answer_callback_query(
                _current_callback_id(dashboard),
                text="שלב הדילוג כבר הסתיים.",
                show_alert=True,
            )
            return
        self.repository.update_draft(actor, drive_url=None, stage="files")
        self._prompt_for_stage(dashboard, chat_id, actor)

    def _finish_draft(self, dashboard, chat_id: int, actor: int) -> None:
        draft = self.repository.get_draft(actor)
        if not draft or draft.get("stage") not in {"files", "replace_files", "append_files"}:
            self._show_media_menu(dashboard, chat_id, "שלב ההעלאה כבר הסתיים.")
            return
        if not draft.get("telegram_files") and not (
            draft.get("youtube_urls") and draft.get("stage") in {"files", "append_files"}
        ):
            dashboard.bot.answer_callback_query(
                _current_callback_id(dashboard), text="יש להעלות לפחות קובץ אחד לפני הסיום.", show_alert=True,
            )
            return
        experience = getattr(self, "experience", None)
        if (
            experience is not None and draft.get("action") == "add"
            and experience.store.snapshot()["genre_tagging"]
            and not draft.get("genre")
        ):
            self.repository.update_draft(actor, stage="genres")
            self._prompt_for_stage(dashboard, chat_id, actor)
            return
        was_replacing = draft.get("action") in {"replace_files", "append_files"}
        was_adding = draft.get("action") == "add"
        clean_upload = was_adding or draft.get("action") == "append_files"
        bot_message_ids = (
            list(draft.get("bot_message_ids", []))
            if clean_upload else []
        )
        uploaded_message_ids = (
            list(draft.get("uploaded_message_ids", []))
            if clean_upload else []
        )
        try:
            previous = (
                self.repository.get(draft.get("target_command_name", ""))
                if was_replacing else None
            )
            saved = self.repository.commit_draft(actor)
        except (OSError, ValueError) as exc:
            logging.warning("Media draft could not be committed (%s).", type(exc).__name__)
            dashboard.bot.answer_callback_query(
                _current_callback_id(dashboard), text=str(exc)[:190], show_alert=True,
            )
            return
        if experience is not None:
            try:
                experience.content_updated("vip" if self.vip_access is not None else "standard", previous, saved)
            except Exception:
                logging.exception("Could not enqueue new-episode notifications.")
        if clean_upload:
            self._delete_completed_bot_messages(
                actor, chat_id, bot_message_ids,
            )
            self._delete_uploaded_admin_messages(
                actor, chat_id, uploaded_message_ids,
            )
            if was_adding:
                self._send_content_announcement(saved)
        kind = "סרט" if saved["type"] == "movie" else "סדרה"
        if was_adding:
            details = (
                "✅ הסדרה והפרקים נשמרו בהצלחה במערכת!"
                if saved["type"] == "series"
                else "✅ התוכן נשמר בהצלחה במערכת!"
            )
        else:
            details = (
                f"✅ קובצי {kind} עודכנו בהצלחה\n"
                f"שם: {saved['title']}\n"
                f"פקודה: {saved['command_name']}\n"
                f"קבצי Telegram: {len(saved['telegram_files'])}\n"
                f"קישור Drive: {'קיים' if saved['drive_url'] else 'לא הוגדר'}"
            )
        self._screen(
            dashboard, chat_id, details,
            [(
                "⭐ חזרה להגדרות VIP" if self.vip_access is not None
                else "🎬 חזרה לניהול סרטים וסדרות",
                self._admin_callback("adm:media"),
            )],
            back="adm:menu",
        )

    def _send_content_announcement(self, item: dict) -> None:
        """Send a forward-ready new-content announcement to the configured admin chat."""
        try:
            from admin_notifications import resolve_admin_chat

            target = resolve_admin_chat(config_file=self.inbox.engine.path)
            if type(target) is not int or target >= 0:
                raise ValueError("ADMIN_CHAT_ID אינו מוגדר.")
            content_name = escape(item["title"])
            command_name = escape(item["command_name"])
            content_type = "סרט" if item["type"] == "movie" else "סדרה"
            text = (
                "🎬 <b>תוכן חדש עלה לבוט!</b>\n\n"
                f"📌 <b>שם התוכן:</b> {content_name}\n"
                f"📁 <b>סוג:</b> {content_type}\n\n"
                "💡 <b>איך מקבלים את התוכן?</b>\n"
                f"שלחו בבוט או בקבוצה את המילה: <code>{command_name}</code>\n\n"
                "🍿 צפייה מהנה!"
            )
            from media_storage import media_access_level
            if media_access_level(item, legacy_vip=self.vip_access is not None) == "vip":
                text += "\n\n⭐ זמין רק למשתמשי VIP!"
            self.bot.send_message(
                target, text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logging.warning(
                "New-content announcement delivery failed (%s).",
                type(exc).__name__,
            )

    def _handle_text_input(self, message, draft) -> None:
        actor, chat_id = message.from_user.id, message.chat.id
        raw_text = getattr(message, "text", "") or ""
        text = _clean_name(raw_text)
        drive_url_from_message = _message_drive_url(message, raw_text)
        dashboard = self.registry.get(actor)
        if text.casefold() in {"ביטול", "/cancel"}:
            self.repository.delete_draft(actor)
            self._show_media_menu(dashboard, chat_id, "הפעולה בוטלה.")
            return
        stage = draft.get("stage")
        try:
            if stage in {"poster", "edit_poster"}:
                poster = None if stage == "poster" and text in {"דילוג", "דלג"} else _poster_url(raw_text.strip())
                if stage == "poster":
                    self.repository.update_draft(actor, poster_url=poster, poster_file_id=None, stage="drive")
                    self._prompt_for_stage(dashboard, chat_id, actor)
                else:
                    saved = self.repository.update(draft["target_command_name"], poster_url=poster, poster_file_id=None)
                    self.repository.delete_draft(actor)
                    self._delete_admin_input(message)
                    self._show_edit_options(dashboard, chat_id, media_token(saved["command_name"]), prefix="✅ התמונה עודכנה.")
            elif stage == "name":
                if not text or len(text) > MAX_NAME_LENGTH:
                    self._send_admin_draft_message(
                        actor, chat_id, f"שלח שם באורך 1–{MAX_NAME_LENGTH} תווים.",
                    )
                    return
                self.repository.update_draft(
                    actor, command_name=text, command_trigger=text, title=text,
                    display_title=text, stage="display_title", active=True,
                )
                if draft.get("action") != "add":
                    self._delete_admin_input(message)
                self._prompt_for_stage(dashboard, chat_id, actor)
            elif stage == "new_trailer":
                trailer_url = extract_youtube_url(text)
                if text != "ללא" and not trailer_url:
                    raise ValueError("שלח קישור YouTube תקין לטריילר.")
                self.repository.update_draft(
                    actor, trailer_url="" if text == "ללא" else trailer_url,
                    **({"youtube_urls": [trailer_url]} if trailer_url else {}),
                    stage=draft.get("trailer_return_stage", "files"),
                )
                self._delete_admin_input(message)
                self._prompt_for_stage(dashboard, chat_id, actor)
                return
            elif stage == "genres":
                selected = genre_text(text)
                if not selected:
                    raise ValueError("שלח לפחות ז'אנר אחד.")
                self.repository.update_draft(actor, genre=selected, stage="files")
                self._finish_draft(dashboard, chat_id, actor)
            elif stage in {"display_title", "edit_title", "edit_trailer", "edit_genre"}:
                if stage == "edit_genre":
                    selected = genre_text(text)
                    if not selected:
                        raise ValueError("שלח לפחות ז'אנר אחד.")
                    updated = self.repository.update(draft["target_command_name"], genre=selected)
                elif stage == "edit_trailer":
                    from management_config import is_link_input
                    value = None if text == "ללא" else extract_youtube_url(text)
                    if text != "ללא" and (not value or not is_link_input(value)):
                        raise ValueError("שלח קישור YouTube תקין.")
                    updated = self.repository.update(
                        draft["target_command_name"],
                        trailer_url=value or "",
                        youtube_urls=[value] if value else [],
                    )
                else:
                    if not text or len(text) > MAX_NAME_LENGTH:
                        raise ValueError("שם ההצגה ארוך מדי או ריק.")
                    if stage == "display_title":
                        self.repository.update_draft(actor, title=text, display_title=text, stage="poster")
                        self._delete_admin_input(message)
                        self._prompt_for_stage(dashboard, chat_id, actor)
                        return
                    updated = self.repository.update(draft["target_command_name"], title=text)
                self.repository.delete_draft(actor)
                self._delete_admin_input(message)
                self._show_edit_options(dashboard, chat_id, media_token(updated["command_name"]), prefix="✅ עודכן.")
            elif stage == "drive":
                if text.casefold() in {"ללא", "דילוג", "skip"}:
                    drive_url = None
                elif drive_url_from_message:
                    drive_url = drive_url_from_message
                else:
                    trailer_urls = _message_youtube_urls(message, raw_text)
                    if trailer_urls:
                        self.repository.update_draft(
                            actor, youtube_urls=trailer_urls,
                            trailer_url=trailer_urls[0], stage="files", active=True,
                        )
                        self._prompt_for_stage(dashboard, chat_id, actor)
                        return
                    self._send_admin_draft_message(
                        actor, chat_id, "שלח קישור Google Drive או קישור וידאו YouTube תקין, או לחץ דילוג.",
                    )
                    return
                trailer_urls = _message_youtube_urls(message, raw_text)
                changes = {"drive_url": drive_url, "stage": "files", "active": True}
                if trailer_urls:
                    changes["youtube_urls"] = trailer_urls
                    changes["trailer_url"] = trailer_urls[0]
                self.repository.update_draft(actor, **changes)
                if draft.get("action") != "add":
                    self._delete_admin_input(message)
                self._prompt_for_stage(dashboard, chat_id, actor)
            elif stage in {"files", "append_files"}:
                trailer_urls = _message_youtube_urls(message, raw_text)
                if not drive_url_from_message and not trailer_urls:
                    self._send_admin_draft_message(
                        actor, chat_id, "שלח קישור Drive או YouTube תקין, או העלה קובץ.",
                    )
                    return
                changes = {"active": True}
                if drive_url_from_message:
                    changes["drive_url"] = drive_url_from_message
                if trailer_urls:
                    changes["youtube_urls"] = list(dict.fromkeys(
                        [*draft.get("youtube_urls", []), *trailer_urls]
                    ))
                    changes["trailer_url"] = trailer_urls[0]
                self.repository.update_draft(actor, **changes)
                self._prompt_for_stage(dashboard, chat_id, actor)
            elif stage == "edit_name":
                if not text or len(text) > MAX_NAME_LENGTH:
                    self._send_admin_draft_message(
                        actor, chat_id, f"שלח שם באורך 1–{MAX_NAME_LENGTH} תווים.",
                    )
                    return
                target = draft.get("target_command_name")
                updated = self.repository.update(target, command_name=text)
                experience = getattr(self, "experience", None)
                if experience is not None:
                    experience.rename_series(
                        "vip" if self.vip_access is not None else "standard", target, updated,
                    )
                self.repository.delete_draft(actor)
                self._delete_admin_input(message)
                self._show_edit_options(dashboard, chat_id, media_token(updated["command_name"]), prefix="✅ שם הפקודה עודכן.")
            elif stage == "edit_drive":
                target = draft.get("target_command_name")
                if text.casefold() in {"ללא", "הסר"}:
                    drive_url = None
                elif drive_url_from_message:
                    drive_url = drive_url_from_message
                else:
                    self._send_admin_draft_message(
                        actor, chat_id, "שלח קישור https://drive.google.com תקין, או „ללא”.",
                    )
                    return
                updated = self.repository.update(target, drive_url=drive_url)
                self.repository.delete_draft(actor)
                self._delete_admin_input(message)
                self._show_edit_options(dashboard, chat_id, media_token(updated["command_name"]), prefix="✅ קישור Drive עודכן.")
            else:
                self._send_admin_draft_message(
                    actor, chat_id, "השלב הנוכחי אינו מקבל טקסט. השתמש בכפתורים.",
                )
        except (OSError, ValueError) as exc:
            logging.warning("Media text input could not be saved (%s).", type(exc).__name__)
            self._send_admin_draft_message(actor, chat_id, f"לא ניתן לשמור את העדכון: {exc}")

    def handle_admin_text(self, message) -> None:
        if not self.has_pending_admin_input(message):
            return
        actor = message.from_user.id
        draft = self.repository.get_draft(actor)
        self._track_admin_upload_message(actor, message)
        self._handle_text_input(message, draft)

    def handle_admin_file(self, message) -> None:
        if not self.has_pending_admin_input(message):
            return
        actor, chat_id = message.from_user.id, message.chat.id
        draft = self.repository.get_draft(actor)
        if not draft:
            return
        if draft.get("stage") in {"poster", "edit_poster"}:
            photos = getattr(message, "photo", None) or []
            photo = photos[-1] if photos else None
            file_id = getattr(photo, "file_id", None)
            if not isinstance(file_id, str) or not file_id.strip():
                self._send_admin_draft_message(actor, chat_id, "שלח תמונה רגילה בצ'אט לפוסטר, או השתמש בדילוג.")
                return
            try:
                if draft["stage"] == "poster":
                    self._track_admin_upload_message(actor, message)
                    self.repository.update_draft(actor, poster_file_id=file_id, poster_url=None, stage="drive")
                    self._prompt_for_stage(self.registry.get(actor), chat_id, actor)
                else:
                    saved = self.repository.update(
                        draft["target_command_name"], poster_file_id=file_id, poster_url=None,
                    )
                    self.repository.delete_draft(actor)
                    self._delete_admin_input(message)
                    self._show_edit_options(self.registry.get(actor), chat_id, media_token(saved["command_name"]), prefix="✅ התמונה עודכנה.")
            except (OSError, ValueError) as exc:
                logging.warning("Poster upload could not be saved (%s).", type(exc).__name__)
                self._send_admin_draft_message(actor, chat_id, "לא ניתן לשמור את התמונה. נסה שוב.")
            return
        if draft.get("stage") not in {"files", "replace_files", "append_files"}:
            self._track_admin_upload_message(actor, message)
            self._send_admin_draft_message(
                actor, chat_id, "קבצים מתקבלים רק בשלב העלאת המדיה.",
            )
            return
        video = getattr(message, "video", None)
        document = getattr(message, "document", None)
        if video and getattr(video, "file_id", None):
            record = {
                "file_id": video.file_id,
                "file_type": "video",
                "caption": getattr(message, "caption", None),
                "caption_entities": _caption_entities(message),
            }
            if isinstance(getattr(video, "file_size", None), int) and not isinstance(video.file_size, bool) and video.file_size > 0:
                record["file_size"] = video.file_size
        elif document and getattr(document, "file_id", None):
            record = {
                "file_id": document.file_id,
                "file_type": "document",
                "caption": getattr(message, "caption", None),
                "caption_entities": _caption_entities(message),
            }
            if isinstance(getattr(document, "file_size", None), int) and not isinstance(document.file_size, bool) and document.file_size > 0:
                record["file_size"] = document.file_size
        else:
            self._track_admin_upload_message(actor, message)
            self._send_admin_draft_message(
                actor, chat_id,
                "העלאת תמונות או מדיה מסוג זה אינה נתמכת. שלח קובץ וידאו או מסמך.",
            )
            return
        try:
            caption_entities = _caption_entities(message)
            caption_urls = " ".join(
                [getattr(message, "caption", None) or "", *(
                    entity.get("url") or "" for entity in caption_entities if isinstance(entity, dict)
                )]
            )
            caption_drive = extract_drive_url(caption_urls)
            if caption_drive:
                record["drive_url"] = caption_drive
                record["drive_file_id"] = drive_file_id_from_text(caption_drive)
                self.repository.update_draft(actor, drive_url=caption_drive, active=True)
            caption_youtube_urls = _message_youtube_urls(message, caption_urls)
            if caption_youtube_urls:
                current_draft = self.repository.get_draft(actor) or draft
                self.repository.update_draft(
                    actor,
                    youtube_urls=list(dict.fromkeys([
                        *current_draft.get("youtube_urls", []), *caption_youtube_urls,
                    ])),
                    trailer_url=caption_youtube_urls[0],
                    active=True,
                )
            message_id = getattr(message, "message_id", None)
            _updated, appended = self.repository.append_draft_file(
                actor, record, message_id=message_id,
            )
        except (OSError, ValueError) as exc:
            logging.warning("Telegram media draft could not be saved (%s).", type(exc).__name__)
            self._send_admin_draft_message(
                actor, chat_id, "לא הצלחתי לשמור את הקובץ; הוא לא נמחק. נסה שוב.",
            )
            return
        if not appended:
            return
        if draft.get("action") not in {"add", "append_files"}:
            self._delete_admin_input(message)
        self._prompt_for_stage(self.registry.get(actor), chat_id, actor)

    def _track_admin_upload_message(self, actor: int, message) -> None:
        draft = self.repository.get_draft(actor)
        message_id = getattr(message, "message_id", None)
        if (
            not draft or draft.get("action") not in {"add", "append_files"} or not draft.get("active", True)
            or not is_admin(actor) or getattr(getattr(message, "chat", None), "type", None) != "private"
            or getattr(message.chat, "id", None) != actor
            or type(message_id) is not int or message_id <= 0
        ):
            return
        stored_ids = draft.get("uploaded_message_ids", [])
        uploaded_ids = list(stored_ids) if isinstance(stored_ids, list) else []
        if message_id in uploaded_ids:
            return
        try:
            uploaded_ids.append(message_id)
            self.repository.update_draft(
                actor, uploaded_message_ids=uploaded_ids, active=True,
            )
        except (OSError, ValueError) as exc:
            logging.warning(
                "Media admin message ID %s could not be tracked (%s).",
                message_id, type(exc).__name__,
            )

    def _track_bot_message(self, actor: int, sent_message) -> None:
        message_id = getattr(sent_message, "message_id", None)
        if type(message_id) is not int or message_id <= 0:
            return
        draft = self.repository.get_draft(actor)
        if not draft or draft.get("action") not in {"add", "append_files"} or not draft.get("active", True):
            return
        message_ids = list(draft.get("bot_message_ids", []))
        if message_id in message_ids:
            return
        try:
            message_ids.append(message_id)
            self.repository.update_draft(
                actor, bot_message_ids=message_ids, active=True,
            )
        except (OSError, ValueError) as exc:
            logging.warning(
                "Media bot message ID %s could not be tracked (%s: %s).",
                message_id, type(exc).__name__, exc,
            )

    def _send_admin_draft_message(
        self, actor: int, chat_id: int, text: str, **kwargs,
    ):
        sent = self.bot.send_message(chat_id, text, **kwargs)
        self._track_bot_message(actor, sent)
        return sent

    def _delete_completed_bot_messages(
        self, actor: int, chat_id: int, message_ids: list,
    ) -> bool:
        if not is_admin(actor) or chat_id != actor:
            logging.warning("Refusing media bot-message cleanup outside the admin's private chat.")
            return False
        succeeded = True
        for message_id in message_ids:
            if type(message_id) is not int or message_id <= 0:
                continue
            try:
                deleted = self.bot.delete_message(
                    chat_id=chat_id, message_id=message_id,
                )
                if deleted is False:
                    succeeded = False
                    logging.warning(
                        "Completed media bot-message cleanup returned false (message=%s).",
                        message_id,
                    )
            except Exception as exc:
                succeeded = False
                logging.warning(
                    "Completed media bot-message cleanup failed (message=%s, error=%s: %s).",
                    message_id, type(exc).__name__, exc,
                )
        return succeeded

    def _delete_uploaded_admin_messages(
        self, actor: int, chat_id: int, message_ids: list,
    ) -> None:
        if not is_admin(actor) or chat_id != actor:
            return
        for message_id in message_ids:
            if type(message_id) is not int or message_id <= 0:
                continue
            try:
                self.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass

    def _delete_admin_input(self, message) -> None:
        protection = getattr(self.bot, "private_admin_protection", None)
        try:
            if protection is not None:
                protection.delete_private_admin_input(message)
        except Exception as exc:
            logging.warning("Media admin input cleanup failed (%s).", type(exc).__name__)

    def _begin_edit(self, dashboard, chat_id: int, actor: int, action: str, token: str) -> None:
        item = self.repository.get_by_token(token)
        if item is None:
            self._send_admin_picker(dashboard, chat_id, "edit", 0, notice="המדיה שנבחרה כבר אינה קיימת.")
            return
        if self.repository.get_draft(actor):
            self.pause(actor)
            self._screen(
                dashboard, chat_id, "יש תהליך מדיה פתוח. אפשר להמשיך אותו או לבטל אותו לפני העריכה.",
                [
                    ("▶️ המשך בתהליך", "adm:media_resume"),
                    ("❌ ביטול התהליך", "adm:media_cancel"),
                ],
                back="adm:media",
            )
            return
        if action == "media_append_files" and item["type"] != "series":
            self._show_edit_options(dashboard, chat_id, token)
            return
        if action == "media_append_files":
            stage = "access_level"
        elif action == "media_edit_name":
            stage = "edit_name"
        elif action == "media_edit_drive":
            stage = "edit_drive"
        else:
            stage = "replace_files"
        self.repository.save_draft(actor, {
            "action": "append_files" if action == "media_append_files" else ("replace_files" if stage == "replace_files" else "edit"),
            "stage": stage,
            "target_command_name": item["command_name"],
            "type": item["type"],
            "title": item["title"],
            "command_name": item["command_name"],
            "drive_url": item["drive_url"],
            "telegram_files": [] if stage in {"replace_files", "access_level"} else item["telegram_files"],
            **({"access_level": item["access_level"]} if "access_level" in item else {}),
            "active": True,
        })
        self._prompt_for_stage(dashboard, chat_id, actor)

    def _send_admin_picker(self, dashboard, chat_id: int, mode: str, page: int, notice: str | None = None) -> None:
        items = sorted(self.repository.list_media(), key=lambda item: item["title"].casefold())
        page_count = max(1, (len(items) + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        current = items[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
        buttons = [
            (
                f"{'✏️' if mode == 'edit' else '🗑️'} {item['title'][:45]}",
                f"adm:media_{'edit' if mode == 'edit' else 'delete'}_pick:{media_token(item['command_name'])}",
            )
            for item in current
        ]
        if page:
            buttons.append(("⬅️ הקודם", f"adm:media_{mode}_list:{page - 1}"))
        if page + 1 < page_count:
            buttons.append(("➡️ הבא", f"adm:media_{mode}_list:{page + 1}"))
        title = "בחירת מדיה לעריכה" if mode == "edit" else "בחירת מדיה למחיקה"
        if notice:
            title = notice + "\n\n" + title
        if items:
            title += f"\nעמוד {page + 1} מתוך {page_count}"
        else:
            title += "\nאין עדיין סרטים או סדרות."
        self._screen(
            dashboard, chat_id, title, buttons,
            back="adm:media", back_label="🔙 חזרה לניהול סרטים וסדרות",
        )

    def _send_media_summary(self, dashboard, chat_id: int, page: int) -> None:
        items = sorted(self.repository.list_media(), key=lambda item: item["title"].casefold())
        page_count = max(1, (len(items) + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        current = items[page * ADMIN_PAGE_SIZE:(page + 1) * ADMIN_PAGE_SIZE]
        lines = [f"📋 סרטים וסדרות במאגר — עמוד {page + 1} מתוך {page_count}"]
        for item in current:
            kind = "סרט" if item["type"] == "movie" else "סדרה"
            lines.append(
                f"• {item['title']} ({kind}) — {len(item['telegram_files'])} קבצים — "
                f"Drive: {'כן' if item['drive_url'] else 'לא'}"
            )
        if not items:
            lines.append("אין עדיין תכנים במאגר.")
        buttons = []
        if page:
            buttons.append(("⬅️ הקודם", f"adm:media_summary:{page - 1}"))
        if page + 1 < page_count:
            buttons.append(("➡️ הבא", f"adm:media_summary:{page + 1}"))
        self._screen(
            dashboard, chat_id, "\n".join(lines), buttons,
            back="adm:media", back_label="🔙 חזרה לניהול סרטים וסדרות",
        )

    def _show_edit_options(self, dashboard, chat_id: int, token: str, prefix: str | None = None) -> None:
        item = self.repository.get_by_token(token)
        if item is None:
            self._send_admin_picker(dashboard, chat_id, "edit", 0, notice="המדיה שנבחרה כבר אינה קיימת.")
            return
        title = (prefix + "\n" if prefix else "") + f"✏️ עריכת {item['title']}"
        self._screen(
            dashboard, chat_id, title,
            [
                ("✏️ שינוי שם הפקודה", f"adm:media_edit_name:{token}"),
                ("✏️ שינוי שם להצגה", f"adm:media_edit_title:{token}"),
                ("🎭 עריכת ז'אנר", f"adm:media_edit_genre:{token}"),
                ("🖼️ הוספת/עדכון תמונה", f"adm:media_edit_poster:{token}"),
                ("🗑️ מחיקת תמונה", f"adm:media_delete_poster:{token}"),
                ("🎬 הגדרת קישור טריילר", f"adm:media_edit_trailer:{token}"),
                ("🔗 עדכון קישור Drive", f"adm:media_edit_drive:{token}"),
                ("🎞️ החלפת קובצי Telegram", f"adm:media_edit_files:{token}"),
                *([("➕ הוספת פרקים לסדרה", f"adm:media_append_files:{token}")] if item["type"] == "series" else []),
            ],
            back="adm:media_edit_list", back_label="🔙 חזרה לרשימת המדיה",
        )

    def _confirm_delete_menu(self, dashboard, chat_id: int, token: str) -> None:
        item = self.repository.get_by_token(token)
        if item is None:
            self._send_admin_picker(dashboard, chat_id, "delete", 0, notice="המדיה שנבחרה כבר אינה קיימת.")
            return
        self._screen(
            dashboard, chat_id, f"⚠️ האם למחוק את {item['title']} לצמיתות?",
            [
                ("✅ אישור מחיקה", f"adm:media_delete_confirm:{token}"),
                ("❌ ביטול", "adm:media_delete_cancel"),
            ],
            back="adm:media_delete_list", back_label="🔙 חזרה לרשימה",
        )

    def _delete_item(self, dashboard, chat_id: int, token: str) -> None:
        item = self.repository.get_by_token(token)
        if item is None or not self.repository.delete(item["command_name"]):
            self._send_admin_picker(dashboard, chat_id, "delete", 0, notice="המדיה כבר אינה קיימת.")
            return
        self._screen(
            dashboard, chat_id, f"✅ {item['title']} נמחק מהמאגר.",
            [("🗑️ המשך למחיקת מדיה", "adm:media_delete_list")],
            back="adm:media",
        )

    def _show_media_menu(self, dashboard, chat_id: int, notice: str | None = None) -> None:
        if self.vip_access is not None:
            title = "🎬 הוספת וניהול סרטים/סדרות VIP"
            if notice:
                title = notice + "\n\n" + title
            self._screen(
                dashboard, chat_id, title, VIP_MEDIA_MENU,
                back="adm:vip_menu", back_label="🔙 חזרה להגדרות VIP",
            )
            return
        text = "🎬 ניהול סרטים וסדרות"
        if notice:
            text = notice + "\n\n" + text
        self._screen(dashboard, chat_id, text, MEDIA_MENU, back="adm:menu", back_label="🔙 חזרה לתפריט הראשי")

    def _screen(self, dashboard, chat_id: int, text: str, buttons: list[tuple[str, str]], *, back: str, back_label="🔙 חזרה") -> None:
        transformed = [
            (label, self._admin_callback(callback))
            for label, callback in buttons
        ]
        markup = build_menu(transformed)
        markup.add(_button(back_label, self._admin_callback(back)))
        sent = dashboard.bot.send_message(chat_id, text, reply_markup=markup)
        self._track_bot_message(chat_id, sent)

    def _show_delivery_options(self, chat_id: int, item: dict, *, group=False) -> None:
        if not group and not self._check_item_access(chat_id, chat_id, item):
            return
        token = media_token(item["command_name"])
        buttons = [
            (
                "📁 לדרייב" if group else "📁 לצפייה/הורדה בדרייב (לחצו כאן)",
                self._user_callback("drive", token),
            ),
            (
                "🎬 לצפייה ישירה" if group else "🎬 לצפייה ישירה בטלגרם (לחצו כאן)",
                self._user_callback("telegram", token),
            ),
        ]
        keyboard = InlineKeyboardMarkup(row_width=1)
        for label, callback in buttons:
            keyboard.add(_button(label, callback))
        for index, url in enumerate(item.get("youtube_urls", []), start=1):
            if item["type"] == "movie":
                continue
            label = f"▶️ פרק {index} ב-YouTube"
            keyboard.add(InlineKeyboardButton(label, url=url))
        from management_config import is_link_input
        if item.get("trailer_url") and is_link_input(item["trailer_url"]):
            keyboard.add(InlineKeyboardButton("🎬 טריילר", url=item["trailer_url"]))
        experience = getattr(self, "experience", None)
        if experience is not None and not group:
            experience.follow_markup(
                chat_id, "vip" if self.vip_access is not None else "standard", item, keyboard,
            )
        caption = f"איך תרצו לקבל את התוכן?\n\n{item['title']}"
        poster = item.get("poster_file_id") or item.get("poster_url")
        if poster:
            try:
                self.bot.send_photo(chat_id, poster, caption=caption[:1024], reply_markup=keyboard)
                return
            except Exception as exc:
                logging.warning("Poster delivery failed; using text (%s).", type(exc).__name__)
        self.bot.send_message(chat_id, caption, reply_markup=keyboard)

    def _show_user_list(self, chat_id: int, page: int) -> None:
        experience = getattr(self, "experience", None)
        # Keep legacy catalogue contents in their original lists. Explicitly
        # tagged series follow their access tier, irrespective of storage file.
        entries = [(self, item) for item in self.repository.list_media()
                   if self._item_allowed(chat_id, item)]
        if experience is not None:
            for catalogue in experience.catalogues.values():
                if catalogue is self:
                    continue
                entries.extend(
                    (catalogue, item) for item in catalogue.repository.list_media()
                    if item.get("access_level") == ("vip" if self.vip_access else "regular")
                    and catalogue._item_allowed(chat_id, item)
                )
        entries.sort(key=lambda entry: entry[1]["title"].casefold())
        items = [item for _, item in entries]
        page_count = max(1, (len(items) + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        current = entries[page * USER_PAGE_SIZE:(page + 1) * USER_PAGE_SIZE]
        keyboard = InlineKeyboardMarkup(row_width=1)
        for catalogue, item in current:
            icon = "🎬" if item["type"] == "movie" else "📺"
            keyboard.add(_button(
                f"{icon} {item['title'][:55]}",
                catalogue._user_callback("item", media_token(item["command_name"])),
            ))
        nav = []
        if page:
            nav.append(_button("⬅️ הקודם", self._user_callback("list", page - 1)))
        if page + 1 < page_count:
            nav.append(_button("➡️ הבא", self._user_callback("list", page + 1)))
        if nav:
            keyboard.row(*nav)
        keyboard.add(_button("❓ שאלות נפוצות", self._user_callback("faq", 0)))
        keyboard.add(_button("📞 פנייה לצוות הניהול", "support:open"))
        text = "⭐ סרטים וסדרות VIP" if self.vip_access is not None else "🎬 סרטים וסדרות זמינים"
        if not items:
            text += "\n\nאין כרגע תכנים זמינים."
        else:
            text += f"\nעמוד {page + 1} מתוך {page_count}"
        self.bot.send_message(chat_id, text, reply_markup=keyboard)

    def _show_faq_list(self, chat_id: int, page: int) -> None:
        try:
            from telegram_faq_bot.faq import load_faqs
            faqs = load_faqs()
        except (OSError, ValueError) as exc:
            logging.warning("FAQ list could not be read (%s).", type(exc).__name__)
            self.bot.send_message(chat_id, "שאלות נפוצות עדיין לא הוגדרו.")
            return
        page_count = max(1, (len(faqs) + USER_PAGE_SIZE - 1) // USER_PAGE_SIZE)
        page = max(0, min(page, page_count - 1))
        keyboard = InlineKeyboardMarkup(row_width=1)
        for faq in faqs[page * USER_PAGE_SIZE:(page + 1) * USER_PAGE_SIZE]:
            token = media_token(faq.question)
            keyboard.add(_button(
                f"❓ {faq.question[:55]}",
                self._user_callback("faq_item", token),
            ))
        navigation = []
        if page:
            navigation.append(_button("⬅️ הקודם", self._user_callback("faq", page - 1)))
        if page + 1 < page_count:
            navigation.append(_button("➡️ הבא", self._user_callback("faq", page + 1)))
        if navigation:
            keyboard.row(*navigation)
        keyboard.add(_button("🎬 סרטים וסדרות", self._user_callback("list", 0)))
        keyboard.add(_button("📞 פנייה לצוות הניהול", "support:open"))
        self.bot.send_message(chat_id, "❓ שאלות נפוצות — בחרו שאלה:", reply_markup=keyboard)

    def _show_faq_answer(self, chat_id: int, token: str) -> None:
        try:
            from telegram_faq_bot.faq import load_faqs
            faq = next((item for item in load_faqs() if media_token(item.question) == token), None)
        except (OSError, ValueError):
            faq = None
        if faq is None:
            self.bot.send_message(chat_id, "השאלה אינה זמינה כרגע.")
            return
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(_button("🔙 חזרה לשאלות נפוצות", self._user_callback("faq", 0)))
        self.bot.send_message(chat_id, f"{faq.question}\n\n{faq.answer}", reply_markup=keyboard)

    def _message_config_file(self):
        configured = getattr(getattr(self.inbox, "engine", None), "path", None)
        if configured is not None:
            return configured
        return self.repository.path.with_name("config.json")

    def _drive_unavailable_text(self):
        from system_messages import message_text
        return message_text("drive_unavailable", self._message_config_file())

    def _answer_user_callback(self, call, text=None, show_alert=False) -> None:
        options = {}
        if text is not None:
            options["text"] = str(text)[:200]
        if show_alert:
            options["show_alert"] = True
        try:
            self.bot.answer_callback_query(call.id, **options)
        except Exception as exc:
            logging.warning(
                "Media callback acknowledgement failed (%s).",
                type(exc).__name__,
            )

    def handle_user_callback(self, call) -> None:
        message = getattr(call, "message", None)
        actor = getattr(getattr(call, "from_user", None), "id", None)
        if message is None or actor is None:
            self._answer_user_callback(
                call, text="הפעולה זמינה בצ'אט הפרטי בלבד.", show_alert=True,
            )
            return
        parts = (call.data or "").split(":")
        if not parts or parts[0] != self.user_prefix:
            self._answer_user_callback(
                call, text="הפעולה אינה זמינה.", show_alert=True,
            )
            return
        action = parts[1] if len(parts) > 1 else ""
        chat = getattr(message, "chat", None)
        chat_type = getattr(chat, "type", None)
        private_chat = chat_type == "private" and chat.id == actor
        group_chat = chat_type in {"group", "supergroup"}
        if not private_chat and not (group_chat and action in {"drive", "telegram", "report"}):
            self._answer_user_callback(
                call, text="הפעולה זמינה בצ'אט הפרטי בלבד.", show_alert=True,
            )
            return
        chat_id = chat.id
        try:
            if action == "report":
                try:
                    from management_config import load_config

                    popup_text = load_config(self.inbox.engine.path)[
                        "link_error_popup_text"
                    ]
                except Exception as exc:
                    logging.warning(
                        "Link-error popup text could not be loaded (%s).",
                        type(exc).__name__,
                    )
                    popup_text = "תודה על הדיווח, נעדכן בקרוב! 🙏"
                self._answer_user_callback(
                    call, text=popup_text, show_alert=True,
                )
            elif action != "drive":
                self._answer_user_callback(call)
            item = (
                self.repository.get_by_token(parts[2])
                if len(parts) == 3 and action in {"item", "drive", "telegram"}
                else None
            )
            if item is not None and not self._item_allowed(actor, item):
                if action == "drive":
                    self._answer_user_callback(call)
                self._check_item_access(actor, chat_id, item)
                return
            if action == "list":
                if not private_chat:
                    return
                self._show_user_list(actor, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
            elif action == "faq":
                if not private_chat:
                    return
                self._show_faq_list(actor, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
            elif action == "faq_item" and len(parts) == 3:
                if not private_chat:
                    return
                self._show_faq_answer(actor, parts[2])
            elif action == "item" and len(parts) == 3:
                if not private_chat:
                    return
                item = self.repository.get_by_token(parts[2])
                if item is None:
                    from system_messages import send_system_message
                    send_system_message(self.bot, actor, "media_unavailable", self._message_config_file())
                else:
                    self._record_media_search(item["command_name"])
                    self._show_delivery_options(actor, item)
            elif action == "drive" and len(parts) == 3:
                item = self.repository.get_by_token(parts[2])
                if item is None or not item["drive_url"]:
                    self._answer_user_callback(
                        call, text=self._drive_unavailable_text(), show_alert=True,
                    )
                else:
                    self._answer_user_callback(call)
                    report_keyboard = None
                    if private_chat:
                        report_keyboard = InlineKeyboardMarkup(row_width=1)
                        report_keyboard.add(_button(
                            "⚠️ הקישור לא עובד? לדיווח ופנייה לניהול",
                            self._user_callback("report", parts[2]),
                        ))
                    self.bot.send_message(
                        chat_id, f"📁 {item['title']}\n{item['drive_url']}",
                        disable_web_page_preview=True,
                        reply_markup=report_keyboard,
                    )
            elif action == "drive":
                self._answer_user_callback(
                    call, text=self._drive_unavailable_text(), show_alert=True,
                )
            elif action == "telegram" and len(parts) == 3:
                item = self.repository.get_by_token(parts[2])
                if item is None or not item["telegram_files"]:
                    from system_messages import send_system_message
                    send_system_message(self.bot, chat_id, "file_unavailable", self._message_config_file())
                else:
                    self._send_telegram_files(chat_id, item)
            elif action == "report" and len(parts) == 3:
                item = self.repository.get_by_token(parts[2])
                reported_content = item["title"] if item is not None else f"מזהה מדיה: {parts[2]}"
                requests = getattr(self.inbox, "missing_content", None)
                if requests is None or not requests.submit_link_error(
                    call.from_user, reported_content,
                ):
                    logging.warning("Media link-error report could not be delivered.")
                else:
                    try:
                        current = getattr(call.message, "reply_markup", None)
                        keyboard = InlineKeyboardMarkup()
                        for row in getattr(current, "keyboard", []):
                            remaining = [
                                button for button in row
                                if getattr(button, "callback_data", None) != call.data
                            ]
                            if remaining:
                                keyboard.row(*remaining)
                        self.bot.edit_message_reply_markup(
                            call.message.chat.id, call.message.message_id,
                            reply_markup=keyboard if keyboard.keyboard else None,
                        )
                    except Exception as exc:
                        logging.warning("Delivered report button could not be removed (%s).",
                                        type(exc).__name__)
        except Exception as exc:
            logging.error(
                "Media user callback failed (action=%s, actor=%s, error=%s, code=%s).",
                action, actor, type(exc).__name__, getattr(exc, "error_code", None),
            )
            # Link-error feedback is a callback popup only. Never turn a
            # delivery/configuration failure into an extra private chat post.
            if action != "report":
                try:
                    self.bot.send_message(
                        actor,
                        "לא ניתן להציג את התוכן כרגע. נסו שוב מאוחר יותר.",
                    )
                except Exception as send_exc:
                    logging.warning(
                        "Could not send media-action error to the user (%s).",
                        type(send_exc).__name__,
                    )

    def _access_service(self):
        service = self.vip_access or getattr(self, "access_service", None)
        if service is None:
            from vip_access import VIPAccess
            service = VIPAccess(
                self.bot, store=getattr(getattr(self.inbox, "missing_content", None), "vip_store", None),
            )
        return service

    def _item_allowed(self, uid: int, item: dict) -> bool:
        from media_storage import media_access_level
        return (
            media_access_level(item, legacy_vip=self.vip_access is not None) == "regular"
            or self._access_service().store.is_vip(uid)
        )

    def _check_item_access(self, uid: int, chat_id: int, item: dict) -> bool:
        if self._item_allowed(uid, item):
            return True
        self._access_service().show_prompt(chat_id, item)
        return False

    def _send_telegram_files(self, chat_id: int, item: dict) -> None:
        for index, media_file in enumerate(item["telegram_files"], start=1):
            try:
                kwargs = {}
                caption = media_file.get("caption")
                if caption is not None:
                    kwargs["caption"] = caption
                entities = media_file.get("caption_entities", [])
                if entities:
                    kwargs["caption_entities"] = [
                        MessageEntity.de_json(json.dumps(entity, ensure_ascii=False))
                        for entity in entities
                    ]
                if media_file["file_type"] == "video":
                    self.bot.send_video(chat_id, media_file["file_id"], **kwargs)
                else:
                    self.bot.send_document(chat_id, media_file["file_id"], **kwargs)
            except Exception as exc:
                logging.warning("Telegram media delivery failed (%s).", type(exc).__name__)
                self.bot.send_message(chat_id, f"לא ניתן לשלוח את קובץ {index} כרגע.")


class VIPMediaCatalogue(MediaCatalogue):
    """VIP-only content with isolated storage and callback namespaces."""

    def __init__(self, bot, registry, inbox, repository, vip_access):
        super().__init__(
            bot,
            registry,
            inbox,
            repository,
            admin_prefix="adm:vip_media",
            user_prefix="vipcatalog",
            vip_access=vip_access,
        )


def _page(parts: list[str]) -> int:
    return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0


def _current_callback_id(dashboard):
    # The dashboard wrapper captures this callback ID for a fresh popup.
    from admin_ui import _feedback
    feedback = _feedback.get()
    call = feedback.get("call") if feedback else None
    return call.id if call else None
