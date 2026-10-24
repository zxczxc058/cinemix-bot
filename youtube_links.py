"""YouTube video links shared by regular and VIP content flows."""

import re
from urllib.parse import parse_qs, urlsplit


_YOUTUBE_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtu\.be|youtube\.com|m\.youtube\.com|youtube-nocookie\.com)/[^\s<>\"']+",
    re.IGNORECASE,
)


def valid_youtube_url(value):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return False
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password:
            return False
        host = (url.hostname or "").lower()
        parts = url.path.strip("/").split("/")
        if host in {"youtu.be", "www.youtu.be"} and len(parts) == 1:
            video_id = parts[0]
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtube-nocookie.com", "www.youtube-nocookie.com"}:
            if url.path == "/watch":
                video_id = parse_qs(url.query).get("v", [""])[0]
            elif len(parts) == 2 and parts[0] in {"shorts", "embed", "live"}:
                video_id = parts[1]
            else:
                return False
        else:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id))
    except (ValueError, TypeError):
        return False


def extract_youtube_urls(value):
    """Extract valid YouTube links from text while ignoring adjacent punctuation."""
    if not isinstance(value, str) or not value:
        return []
    matches = []
    for match in _YOUTUBE_URL_PATTERN.finditer(value):
        candidate = match.group(0).rstrip(".,;:!?)]}»”’")
        if valid_youtube_url(candidate) and candidate not in matches:
            matches.append(candidate)
    return matches


def extract_youtube_url(value):
    """Return the first valid YouTube URL embedded in text, if any."""
    return next(iter(extract_youtube_urls(value)), None)
