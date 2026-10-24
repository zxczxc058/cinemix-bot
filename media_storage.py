"""Validated JSON persistence for Telegram media entries and resumable drafts."""

from __future__ import annotations

from media_genres import genre_text as _genre_value
from drive_links import drive_file_id_from_text, extract_drive_url

import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any, cast


MEDIA_DB_FILE = Path(__file__).resolve().with_name("media_db.json")
_LOCK = threading.RLock()
_EMPTY_DB = {"items": [], "drafts": {}}
_FILE_TYPES = {"video", "document"}
_MEDIA_TYPES = {"movie", "series"}
_TIMESTAMP_FIELDS = ("createdAt", "uploadedAt", "updatedAt")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalized_timestamps(item: dict[str, Any]) -> dict[str, str]:
    timestamps = {}
    for field in _TIMESTAMP_FIELDS:
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamps[field] = parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return timestamps


class DuplicateMediaError(ValueError):
    """Raised when a new media command would overwrite an existing one."""


def media_token(command_name: str) -> str:
    """Return a short stable callback token without exposing the command text."""
    return hashlib.sha256(command_name.encode("utf-8")).hexdigest()[:20]


def media_access_level(item: dict, *, legacy_vip=False) -> str:
    """Explicit tiers override catalogue location; old records keep their tier."""
    return item.get("access_level", "vip" if legacy_vip else "regular")


def _upload_order(index: int, file_record: dict[str, Any]) -> tuple[int, int, int]:
    message_id = file_record.get("_upload_message_id")
    if type(message_id) is int and message_id > 0:
        return (1, message_id, index)
    return (0, index, index)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    from persistent_json import write_json_document
    write_json_document(path, data)


def _validated_media(item: object) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("כל פריט מדיה חייב להיות אובייקט JSON.")
    item = cast(dict[str, Any], item)
    command_name = item.get("command_name")
    media_type = item.get("type")
    title = item.get("title")
    drive_url = item.get("drive_url")
    telegram_files = item.get("telegram_files")
    from youtube_links import valid_youtube_url
    youtube_urls = item.get("youtube_urls", [])
    if not isinstance(youtube_urls, list) or any(not valid_youtube_url(url) for url in youtube_urls):
        raise ValueError("קישורי YouTube אינם תקינים.")
    youtube_urls = list(dict.fromkeys(youtube_urls))
    if not isinstance(command_name, str) or not command_name.strip():
        raise ValueError("חסר שם פקודה למדיה.")
    if media_type not in _MEDIA_TYPES:
        raise ValueError("סוג המדיה חייב להיות movie או series.")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("חסר שם להצגת התוכן.")
    if drive_url is not None and not isinstance(drive_url, str):
        raise ValueError("קישור Drive חייב להיות טקסט או null.")
    if not isinstance(telegram_files, list):
        raise ValueError("telegram_files חייבת להיות רשימה.")
    files = []
    for file in telegram_files:
        if not isinstance(file, dict):
            raise ValueError("קובץ מדיה חייב לכלול file_id ו-file_type תקינים.")
        file = cast(dict[str, Any], file)
        if (
            not isinstance(file.get("file_id"), str)
            or not file["file_id"].strip()
            or file.get("file_type") not in _FILE_TYPES
        ):
            raise ValueError("קובץ מדיה חייב לכלול file_id ו-file_type תקינים.")
        caption = file.get("caption")
        caption_entities = file.get("caption_entities", [])
        file_size = file.get("file_size")
        if caption is not None and not isinstance(caption, str):
            raise ValueError("כיתוב קובץ המדיה חייב להיות טקסט או null.")
        if file_size is not None and (
            isinstance(file_size, bool) or not isinstance(file_size, int) or file_size <= 0
        ):
            raise ValueError("גודל קובץ המדיה אינו תקין.")
        if not isinstance(caption_entities, list) or any(
            not isinstance(entity, dict) for entity in caption_entities
        ):
            raise ValueError("ישויות העיצוב של כיתוב המדיה אינן תקינות.")
        normalized_file = {
            "file_id": file["file_id"].strip(),
            "file_type": file["file_type"],
            "caption": caption,
            "caption_entities": copy.deepcopy(caption_entities),
        }
        caption_link_text = " ".join([
            caption or "",
            *(entity["url"] for entity in caption_entities if isinstance(entity.get("url"), str)),
        ])
        file_drive_url = extract_drive_url(
            file.get("drive_url") or file.get("driveUrl") or file.get("google_drive_url")
        ) or extract_drive_url(caption_link_text)
        explicit_file_drive_id = file.get("drive_file_id")
        file_drive_id = (
            drive_file_id_from_text(file_drive_url)
            or (
                explicit_file_drive_id.strip()
                if isinstance(explicit_file_drive_id, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{10,200}", explicit_file_drive_id.strip())
                else None
            )
        )
        if file_drive_url:
            normalized_file["drive_url"] = file_drive_url
        if file_drive_id:
            normalized_file["drive_file_id"] = file_drive_id
        if file_size is not None:
            normalized_file["file_size"] = file_size
        files.append(normalized_file)
    normalized_drive_url = extract_drive_url(drive_url) or (
        drive_url.strip()
        if isinstance(drive_url, str) and drive_url.strip()
        else None
    )
    explicit_drive_file_id = item.get("drive_file_id")
    normalized_drive_file_id = (
        drive_file_id_from_text(normalized_drive_url)
        or (
            explicit_drive_file_id.strip()
            if isinstance(explicit_drive_file_id, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{10,200}", explicit_drive_file_id.strip())
            else None
        )
    )
    if normalized_drive_url is None and normalized_drive_file_id:
        normalized_drive_url = f"https://drive.google.com/file/d/{normalized_drive_file_id}/view"
    if not files and normalized_drive_url is None and not youtube_urls:
        raise ValueError("יש להוסיף קובץ Telegram או קישור Drive.")
    if "access_level" in item and item["access_level"] not in {"regular", "vip"}:
        raise ValueError("רמת הגישה חייבת להיות regular או vip.")
    episode_drive_ids = item.get("drive_episode_file_ids")
    drive_account_id = item.get("drive_account_id")
    if drive_account_id is not None and (type(drive_account_id) is not int or drive_account_id <= 0):
        raise ValueError("חשבון Drive אינו תקין.")
    if episode_drive_ids is not None and (
        not isinstance(episode_drive_ids, list) or
        any(not isinstance(value, str) or not re.fullmatch(r"[\w-]{1,200}", value, re.ASCII)
            for value in episode_drive_ids)
    ):
        raise ValueError("מזהי פרקי Drive אינם תקינים.")
    drive_folders = item.get("drive_folder_urls")
    if drive_folders is not None and (
        not isinstance(drive_folders, list) or
        any(not isinstance(value, str) or not extract_drive_url(value) or
            "/folders/" not in value for value in drive_folders)
    ):
        raise ValueError("קישורי תיקיות Drive אינם תקינים.")
    drive_details = item.get("drive_episode_details")
    if drive_details is not None and (
        not isinstance(drive_details, dict) or
        any(not isinstance(key, str) or not re.fullmatch(r"[\w-]{1,200}", key, re.ASCII) or
            not isinstance(value, dict) or
            any(type(value.get(field)) is not int or value[field] < 1
                for field in ("season_number", "episode_number"))
            for key, value in drive_details.items())
    ):
        raise ValueError("מספור פרקי Drive אינו תקין.")
    return {
        "command_name": command_name.strip(),
        "type": media_type,
        "title": title.strip(),
        **({"poster_url": item["poster_url"]} if "poster_url" in item else {}),
        **({"poster_file_id": item["poster_file_id"]} if "poster_file_id" in item else {}),
        "drive_url": normalized_drive_url,
        **({"drive_file_id": normalized_drive_file_id} if normalized_drive_file_id else {}),
        "telegram_files": files,
        **({"drive_episode_file_ids": list(dict.fromkeys(episode_drive_ids))}
           if episode_drive_ids is not None else {}),
        **({"drive_account_id": drive_account_id} if drive_account_id is not None else {}),
        **({"drive_folder_urls": list(dict.fromkeys(drive_folders))}
           if drive_folders is not None else {}),
        **({"drive_episode_details": copy.deepcopy(drive_details)}
           if drive_details is not None else {}),
        **({"youtube_urls": youtube_urls} if "youtube_urls" in item else {}),
        **({"trailer_url": item["trailer_url"]} if isinstance(item.get("trailer_url"), str) else {}),
        **({"command_trigger": command_name.strip(), "display_title": title.strip()}
           if "command_trigger" in item or "display_title" in item else {}),
        **({"genre": _genre_value(item["genre"])} if item.get("genre") is not None else {}),
        **({"access_level": item["access_level"]} if "access_level" in item else {}),
        **_normalized_timestamps(item),
    }


class MediaRepository:
    """Thread-safe store for media records and administrator wizard drafts."""

    def __init__(self, path: str | Path = MEDIA_DB_FILE):
        self.path = Path(path)

    def _load_locked(self) -> dict[str, Any]:
        try:
            from persistent_json import read_json_text
            data = json.loads(read_json_text(self.path))
        except FileNotFoundError:
            _atomic_write(self.path, copy.deepcopy(_EMPTY_DB))
            return copy.deepcopy(_EMPTY_DB)
        except json.JSONDecodeError as exc:
            raise ValueError(f"מאגר המדיה פגום: {self.path.name}") from exc
        if not isinstance(data, dict):
            raise ValueError("media_db.json חייב להכיל אובייקט JSON.")
        items = data.get("items", [])
        drafts = data.get("drafts", {})
        if not isinstance(items, list) or not isinstance(drafts, dict):
            raise ValueError("מבנה media_db.json אינו תקין.")
        normalized = [_validated_media(item) for item in items]
        names = [item["command_name"] for item in normalized]
        if len(names) != len(set(names)):
            raise ValueError("נמצאו שמות פקודה כפולים במאגר המדיה.")
        if any(not str(key).isdigit() or not isinstance(value, dict) for key, value in drafts.items()):
            raise ValueError("טיוטות המדיה במאגר אינן תקינות.")
        return {"items": normalized, "drafts": drafts}

    def _save_locked(self, data: dict[str, Any]) -> None:
        _atomic_write(self.path, data)

    def list_media(self) -> list[dict[str, Any]]:
        with _LOCK:
            return copy.deepcopy(self._load_locked()["items"])

    def get(self, command_name: str) -> dict[str, Any] | None:
        name = command_name.strip()
        with _LOCK:
            for item in self._load_locked()["items"]:
                if item["command_name"] == name:
                    return copy.deepcopy(item)
        return None

    def match_exact(self, text: str) -> dict[str, Any] | None:
        return self.get(text.strip())

    def get_by_token(self, token: str) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token:
            return None
        with _LOCK:
            matches = [
                item for item in self._load_locked()["items"]
                if media_token(item["command_name"]) == token
            ]
            return copy.deepcopy(matches[0]) if len(matches) == 1 else None

    def save(self, item: dict[str, Any]) -> dict[str, Any]:
        normalized = _validated_media(item)
        now = _utc_timestamp()
        normalized.setdefault("createdAt", now)
        normalized.setdefault("uploadedAt", normalized["createdAt"])
        normalized["updatedAt"] = now
        with _LOCK:
            data = self._load_locked()
            if any(existing["command_name"] == normalized["command_name"] for existing in data["items"]):
                raise DuplicateMediaError("כבר קיימת מדיה עם שם הפקודה הזה.")
            data["items"].append(normalized)
            self._save_locked(data)
        return copy.deepcopy(normalized)

    def update(self, current_command_name: str, **changes: Any) -> dict[str, Any]:
        allowed = {"command_name", "title", "drive_url", "telegram_files", "genre", "access_level", "trailer_url", "youtube_urls", "poster_url", "poster_file_id"}
        if not changes or changes.keys() - allowed:
            raise ValueError("שדות העריכה אינם נתמכים.")
        with _LOCK:
            data = self._load_locked()
            index = next(
                (
                    i for i, item in enumerate(data["items"])
                    if item["command_name"] == current_command_name
                ),
                None,
            )
            if index is None:
                raise ValueError("המדיה שנבחרה כבר אינה קיימת.")
            current = data["items"][index]
            candidate = {**current, **changes}
            if "drive_url" in changes:
                candidate.pop("drive_file_id", None)
                if changes["drive_url"] != current.get("drive_url"):
                    candidate.pop("drive_account_id", None)
                    if current["type"] == "series":
                        for derived in ("drive_folder_urls", "drive_episode_file_ids", "drive_episode_details"):
                            candidate.pop(derived, None)
            if set(changes) != {"genre"}:
                candidate["updatedAt"] = _utc_timestamp()
            if "telegram_files" in changes and changes["telegram_files"] != current["telegram_files"]:
                candidate["uploadedAt"] = candidate["updatedAt"]
            if "title" in changes:
                candidate["display_title"] = changes["title"]
                candidate["command_trigger"] = candidate["command_name"]
            if "command_name" in changes:
                candidate["command_name"] = str(changes["command_name"]).strip()
                if (
                    candidate["command_name"] != current_command_name
                    and any(item["command_name"] == candidate["command_name"] for item in data["items"])
                ):
                    raise DuplicateMediaError("כבר קיימת מדיה עם שם הפקודה החדש.")
                if current["title"] == current_command_name and "display_title" not in current:
                    candidate["title"] = candidate["command_name"]
            updated = _validated_media(candidate)
            data["items"][index] = updated
            self._save_locked(data)
            return copy.deepcopy(updated)

    def delete(self, command_name: str) -> bool:
        with _LOCK:
            data = self._load_locked()
            remaining = [item for item in data["items"] if item["command_name"] != command_name]
            if len(remaining) == len(data["items"]):
                return False
            data["items"] = remaining
            self._save_locked(data)
            return True

    def get_draft(self, actor_id: int) -> dict[str, Any] | None:
        with _LOCK:
            draft = self._load_locked()["drafts"].get(str(actor_id))
            return copy.deepcopy(draft) if draft is not None else None

    def save_draft(self, actor_id: int, draft: dict[str, Any]) -> None:
        if type(actor_id) is not int or actor_id <= 0 or not isinstance(draft, dict):
            raise ValueError("טיוטת המדיה אינה תקינה.")
        with _LOCK:
            data = self._load_locked()
            data["drafts"][str(actor_id)] = copy.deepcopy(draft)
            self._save_locked(data)

    def update_draft(self, actor_id: int, **changes: Any) -> dict[str, Any] | None:
        with _LOCK:
            data = self._load_locked()
            current = data["drafts"].get(str(actor_id))
            if current is None:
                raise ValueError("תהליך המדיה הסתיים או אינו קיים.")
            updated = {**current, **copy.deepcopy(changes)}
            data["drafts"][str(actor_id)] = updated
            self._save_locked(data)
            return copy.deepcopy(updated)

    def append_draft_file(
        self,
        actor_id: int,
        file_record: dict[str, Any],
        message_id: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically append an upload, retaining Telegram message order for albums."""
        if type(actor_id) is not int or actor_id <= 0 or not isinstance(file_record, dict):
            raise ValueError("קובץ המדיה אינו תקין.")
        if message_id is not None and (type(message_id) is not int or message_id <= 0):
            message_id = None
        with _LOCK:
            data = self._load_locked()
            current = data["drafts"].get(str(actor_id))
            if current is None or current.get("stage") not in {"files", "replace_files", "append_files"}:
                raise ValueError("שלב העלאת המדיה הסתיים או אינו קיים.")

            files = list(current.get("telegram_files", []))
            if message_id is not None and any(
                file.get("_upload_message_id") == message_id
                for file in files if isinstance(file, dict)
            ):
                return copy.deepcopy(current), False

            record = copy.deepcopy(file_record)
            if message_id is not None:
                record["_upload_message_id"] = message_id
            files.append(record)
            indexed_files = list(enumerate(files))
            indexed_files.sort(key=lambda pair: _upload_order(pair[0], pair[1]))
            files = [file for _index, file in indexed_files]

            updated = {**current, "telegram_files": files, "active": True}
            if current.get("action") in {"add", "append_files"} and message_id is not None:
                stored_ids = current.get("uploaded_message_ids", [])
                uploaded_ids = list(stored_ids) if isinstance(stored_ids, list) else []
                if message_id not in uploaded_ids:
                    uploaded_ids.append(message_id)
                updated["uploaded_message_ids"] = uploaded_ids
            data["drafts"][str(actor_id)] = updated
            self._save_locked(data)
            return copy.deepcopy(updated), True

    def delete_draft(self, actor_id: int) -> bool:
        with _LOCK:
            data = self._load_locked()
            removed = data["drafts"].pop(str(actor_id), None) is not None
            if removed:
                self._save_locked(data)
            return removed

    def clear_drafts(self, actor_id: int | None = None) -> int:
        """Clear unfinished upload conversations without touching saved media."""
        with _LOCK:
            data = self._load_locked()
            if actor_id is None:
                count = len(data["drafts"])
                if count:
                    data["drafts"] = {}
                    self._save_locked(data)
                return count
            removed = data["drafts"].pop(str(actor_id), None) is not None
            if removed:
                self._save_locked(data)
            return int(removed)

    def commit_draft(self, actor_id: int) -> dict[str, Any]:
        with _LOCK:
            data = self._load_locked()
            draft = data["drafts"].get(str(actor_id))
            if not draft:
                raise ValueError("טיוטת המדיה אינה קיימת.")
            if draft.get("action") in {"replace_files", "append_files"}:
                target = draft.get("target_command_name")
                index = next(
                    (i for i, item in enumerate(data["items"]) if item["command_name"] == target),
                    None,
                )
                if index is None:
                    raise ValueError("המדיה לעריכה כבר אינה קיימת.")
                current = data["items"][index]
                files = draft.get("telegram_files", [])
                youtube_urls = list(dict.fromkeys(
                    current.get("youtube_urls", []) + draft.get("youtube_urls", [])
                ))
                if draft.get("action") == "append_files":
                    if current["type"] != "series":
                        raise ValueError("ניתן להוסיף פרקים לסדרה בלבד.")
                    files = list(current["telegram_files"])
                    known = {file["file_id"] for file in files}
                    for file in draft.get("telegram_files", []):
                        if file["file_id"] not in known:
                            files.append(file)
                            known.add(file["file_id"])
                    if len(files) == len(current["telegram_files"]) and youtube_urls == current.get("youtube_urls", []):
                        raise ValueError("כל הפרקים שנשלחו כבר קיימים בסדרה.")
                now = _utc_timestamp()
                candidate = {**current, "telegram_files": files, "uploadedAt": now, "updatedAt": now}
                if "trailer_url" in draft:
                    candidate["trailer_url"] = draft["trailer_url"]
                if youtube_urls:
                    candidate["youtube_urls"] = youtube_urls
                if "access_level" in draft:
                    candidate["access_level"] = draft["access_level"]
                updated = _validated_media(candidate)
                data["items"][index] = updated
            else:
                candidate = _validated_media(draft)
                if any(item["command_name"] == candidate["command_name"] for item in data["items"]):
                    raise DuplicateMediaError("כבר קיימת מדיה עם שם הפקודה הזה.")
                now = _utc_timestamp()
                candidate.setdefault("createdAt", now)
                candidate.setdefault("uploadedAt", candidate["createdAt"])
                candidate["updatedAt"] = now
                data["items"].append(candidate)
                updated = candidate
            data["drafts"].pop(str(actor_id), None)
            self._save_locked(data)
            return copy.deepcopy(updated)
