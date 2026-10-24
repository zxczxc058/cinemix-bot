"""Extract and canonicalize Google Drive links from Telegram text/captions."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


_URL_RE = re.compile(r"https://(?:drive\.google\.com|docs\.google\.com)/[^\s<>\"']+", re.IGNORECASE)
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TRAILING_PUNCTUATION = ".,;:!?)]}»"


def _candidate_urls(value: object):
    if not isinstance(value, str):
        return
    for match in _URL_RE.finditer(value):
        yield match.group(0).rstrip(_TRAILING_PUNCTUATION)


def _file_id(url) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "drive.google.com" or parsed.username or parsed.password:
        return None
    match = re.search(r"/file/d/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
    candidate = match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]
    return candidate if isinstance(candidate, str) and _FILE_ID_RE.fullmatch(candidate) else None


def drive_file_id_from_text(value: object) -> str | None:
    """Return the first direct file ID from a Drive URL pasted in text."""
    for url in _candidate_urls(value) or ():
        file_id = _file_id(url)
        if file_id:
            return file_id
    return None


def extract_drive_url(value: object) -> str | None:
    """Return a safe, canonical Drive URL from a Telegram message or caption."""
    for url in _candidate_urls(value) or ():
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {"drive.google.com", "docs.google.com"}:
            continue
        if parsed.username or parsed.password:
            continue
        file_id = _file_id(url)
        if file_id:
            query = parse_qs(parsed.query)
            resource_key = query.get("resourcekey", [None])[0]
            return urlunsplit((
                "https", "drive.google.com", f"/file/d/{file_id}/view",
                urlencode({"resourcekey": resource_key}) if resource_key else "", "",
            ))
        folder_match = re.search(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        if folder_match:
            query = parse_qs(parsed.query)
            resource_key = query.get("resourcekey", [None])[0]
            return urlunsplit((
                "https", "drive.google.com", f"/drive/folders/{folder_match.group(1)}",
                urlencode({"resourcekey": resource_key}) if resource_key else "", "",
            ))
        return url
    return None
