"""Persistent media and text presets for administrator broadcasts."""

import copy
import json
from pathlib import Path

from management_config import save_json

DEFAULT_DONATION_PRESET = {"payload": None}
MEDIA_KINDS = {"photo", "video", "document", "animation", "audio", "voice"}
MEDIA_ATTRIBUTES = {
    "photo": "photo",
    "video": "video",
    "document": "document",
    "animation": "animation",
    "audio": "audio",
    "voice": "voice",
}


def validate_payload(payload):
    if not isinstance(payload, dict) or payload.get("kind") not in MEDIA_KINDS | {"text"}:
        raise ValueError("תוכן התפוצה אינו תקין.")
    kind = payload["kind"]
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise ValueError("טקסט התפוצה אינו תקין.")
    if kind == "text":
        if not text.strip() or len(text) > 4096:
            raise ValueError("טקסט התפוצה חייב להכיל 1–4,096 תווים.")
        return {"kind": "text", "text": text}
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id or len(file_id) > 2048:
        raise ValueError("מזהה קובץ התפוצה אינו תקין.")
    if len(text) > 1024:
        raise ValueError("כיתוב המדיה חייב להיות עד 1,024 תווים.")
    return {"kind": kind, "file_id": file_id, "text": text}


def payload_from_message(message, text=None):
    """Capture a single text or Telegram media message without user metadata."""
    content_type = getattr(message, "content_type", None)
    if content_type not in MEDIA_KINDS:
        content_type = next(
            (kind for kind, attribute in MEDIA_ATTRIBUTES.items()
             if getattr(message, attribute, None)),
            None,
        )
    if content_type is None and (
        getattr(message, "text", None) is not None or text is not None
    ):
        return validate_payload({
            "kind": "text",
            "text": text if text is not None else (getattr(message, "text", None) or ""),
        })
    if content_type not in MEDIA_KINDS:
        raise ValueError("שלח טקסט, תמונה, סרטון, מסמך, GIF או הודעת אודיו.")

    media = getattr(message, MEDIA_ATTRIBUTES[content_type], None)
    if content_type == "photo" and isinstance(media, (list, tuple)):
        media = media[-1] if media else None
    file_id = getattr(media, "file_id", None)
    caption = getattr(message, "caption", None) or ""
    if getattr(message, "caption_entities", None):
        caption = getattr(message, "html_caption", None) or caption
    return validate_payload({
        "kind": content_type,
        "file_id": file_id,
        "text": caption,
    })


def load_donation_preset(path):
    path = Path(path)
    try:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
    except FileNotFoundError:
        data = copy.deepcopy(DEFAULT_DONATION_PRESET)
    if not isinstance(data, dict) or set(data) != {"payload"}:
        raise ValueError("קובץ הודעת התרומות אינו תקין.")
    payload = data["payload"]
    if payload is not None:
        data["payload"] = validate_payload(payload)
    return data


def save_donation_preset(payload, path):
    normalized = validate_payload(payload)
    save_json(path, {"payload": normalized})
    return normalized
