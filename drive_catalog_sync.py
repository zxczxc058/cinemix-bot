"""Admin-initiated, additive Drive scans using the bot's existing OAuth bridge."""

import copy
from datetime import datetime, timezone
import logging
import re
import threading
from urllib.parse import parse_qs, urlsplit

from bot_storage import is_admin
from media_storage import MEDIA_DB_FILE, MediaRepository, _LOCK, _validated_media

FOLDER_MIME = "application/vnd.google-apps.folder"
VIDEO_EXT = re.compile(r"\.(mp4|mkv|m4v|mov|avi|webm|mpeg|mpg|ts|ogv)$", re.I)
SEASON = re.compile(r"(?:עונה\s*|season[\s._-]*|(?<![A-Za-z0-9])S)(\d{1,3})(?!\d)", re.I)
EPISODE = re.compile(r"(?:פרק\s*|episode[\s._-]*|(?<![A-Za-z0-9])E|(?<=\d)E)(\d{1,4})(?!\d)", re.I)
_jobs_lock = threading.Lock()
_jobs = set()
_cancel_events = {}


def cancel_user_sync(user_id):
    """Logging out must not let an old account's in-flight scan import later."""
    with _jobs_lock:
        event = _cancel_events.get(user_id)
        if event is not None:
            event.set()


def folder_reference(value="root"):
    value = value.strip()
    if re.fullmatch(r"[\w-]{1,200}", value, re.ASCII):
        return value, None
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname != "drive.google.com" or parsed.username or parsed.password:
        raise ValueError("שלח מזהה תיקייה או קישור תיקיית Google Drive תקין.")
    match = re.search(r"/folders/([\w-]+)", parsed.path)
    query = parse_qs(parsed.query)
    folder_id = match.group(1) if match else query.get("id", [""])[0]
    resource_key = query.get("resourcekey", [None])[0]
    if not re.fullmatch(r"[\w-]{1,200}", folder_id, re.ASCII) or (
        resource_key and not re.fullmatch(r"[\w-]{1,200}", resource_key, re.ASCII)
    ):
        raise ValueError("שלח מזהה תיקייה או קישור תיקיית Google Drive תקין.")
    return folder_id, resource_key


def sync_drive_content(drive, user_id, parent_folder_id="root", resource_key=None, cancelled=None):
    """Iterative recursion, pagination and deduplication; never returns partial success."""
    if cancelled is not None and cancelled.is_set():
        raise ValueError("הסנכרון בוטל.")
    info = drive.oauth_operation("folder_info", user_id, folder_id=parent_folder_id,
                                 resource_key=resource_key)
    if info.get("mimeType") != FOLDER_MIME or info.get("trashed"):
        raise ValueError("הקישור חייב להפנות לתיקיית Drive פעילה.")
    # Keep the selected folder's name too (the command may target a season directly).
    path = (info["name"],)
    ancestors = (parent_folder_id,)
    season = SEASON.search(info["name"])
    if season and not info["name"][:season.start()].strip(" ._-–—|") and info.get("parents"):
        parent = drive.oauth_operation("folder_info", user_id, folder_id=info["parents"][0])
        path = (parent["name"], *path)
        ancestors = (info["parents"][0], *ancestors)
    pending = [(parent_folder_id, path, resource_key, ancestors)]
    scheduled = {parent_folder_id}
    visited, files = set(), {}
    while pending:
        if cancelled is not None and cancelled.is_set():
            raise ValueError("הסנכרון בוטל.")
        folder_id, path, key, ancestors = pending.pop()
        if folder_id in visited:
            continue
        visited.add(folder_id)
        if len(visited) > 5000:
            raise ValueError("הסריקה גדולה מדי. הפעל /sync על תיקייה ממוקדת יותר.")
        token, tokens = None, set()
        for _ in range(100):
            if cancelled is not None and cancelled.is_set():
                raise ValueError("הסנכרון בוטל.")
            page = drive.oauth_operation("list_folder", user_id, folder_id=folder_id,
                                         page_token=token, resource_key=key)
            if page.get("incompleteSearch") or not isinstance(page.get("files", []), list):
                raise ValueError("Drive החזיר סריקה חלקית. לא נשמרו שינויים.")
            for item in page.get("files", []):
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or
                    not re.fullmatch(r"[\w-]{1,200}", item["id"], re.ASCII) or
                    not isinstance(item.get("name"), str) or not item["name"].strip()):
                    raise ValueError("Drive החזיר פריט לא תקין. לא נשמרו שינויים.")
                if item.get("mimeType") == FOLDER_MIME:
                    if item["id"] not in scheduled:
                        scheduled.add(item["id"])
                        if len(scheduled) > 5000:
                            raise ValueError("הסריקה גדולה מדי. הפעל /sync על תיקייה ממוקדת יותר.")
                        pending.append((item["id"], (*path, item["name"]), item.get("resourceKey"),
                                        (*ancestors, item["id"])))
                elif str(item.get("mimeType", "")).lower().startswith("video/") or VIDEO_EXT.search(item["name"]):
                    files.setdefault(item["id"], {**item, "folder_id": folder_id,
                                                  "folder_path": path, "folder_resource_key": key,
                                                  "folder_ancestor_ids": ancestors})
                    if len(files) > 100000:
                        raise ValueError("הסריקה גדולה מדי. הפעל /sync על תיקייה ממוקדת יותר.")
            token = page.get("nextPageToken")
            if not token:
                break
            if token in tokens:
                raise ValueError("Drive חזר על אותו עמוד. לא נשמרו שינויים.")
            tokens.add(token)
        else:
            raise ValueError("הסריקה חרגה ממגבלת העמודים. לא נשמרו שינויים.")
    return list(files.values())


def _clean_title(value):
    return re.sub(r"\s*[|•]\s*(?:מבית|CINEMIX).*", "", VIDEO_EXT.sub("", value), flags=re.I).strip()


def _season_details(file):
    path = file["folder_path"]
    name = file["name"]
    # Directory season markers take precedence over fallback filename inference.
    for index in range(len(path) - 1, -1, -1):
        match = SEASON.search(path[index])
        if match:
            base = path[index][:match.start()].strip(" ._-–—|")
            if not base and index:
                base = path[index - 1]
            if base and int(match.group(1)) > 0:
                return _clean_title(base), int(match.group(1))
    match = SEASON.search(name)
    if match or EPISODE.search(name):
        base = name[:match.start()].strip(" ._-–—|") if match else ""
        base = base or (path[-1] if path else "")
        if base:
            return _clean_title(base), int(match.group(1)) if match else 1
    return None


def scan_candidates(files):
    """Keep movies as direct files and seasons as real folder-backed series."""
    candidates, seasons = [], {}
    natural = lambda value: re.sub(r"\d+", lambda match: match.group().zfill(8), value.casefold())
    for file in sorted(files, key=lambda item: natural("/".join((*item["folder_path"], item["name"])))):
        details = _season_details(file)
        if details:
            series, season = details
            folder_id = file["folder_id"]
            title = f"{series} עונה {season}"
            link = f"https://drive.google.com/drive/folders/{folder_id}"
            if file.get("folder_resource_key"):
                link += f"?resourcekey={file['folder_resource_key']}"
            kind = "series"
            key = (series.casefold(), season)
            episode_match = EPISODE.search(file["name"]) or re.match(r"^(\d{1,3})(?:[ ._-]|$)", file["name"]) or next(
                (match for name in reversed(file["folder_path"]) if (match := EPISODE.search(name))), None)
            episode = int(episode_match.group(1)) if episode_match else (
                len(seasons[key]["drive_episode_file_ids"]) + 1 if key in seasons else 1)
            episode_details = {"season_number": season, "episode_number": max(1, episode)}
            if file.get("resourceKey"):
                episode_details["resource_key"] = file["resourceKey"]
            if key in seasons:
                candidate = seasons[key]
                candidate["drive_episode_file_ids"].append(file["id"])
                candidate["drive_episode_details"][file["id"]] = episode_details
                candidate["_drive_parent_ids"] = list(dict.fromkeys([
                    *candidate["_drive_parent_ids"], *file.get("folder_ancestor_ids", [])]))
                if link not in candidate["drive_folder_urls"]:
                    candidate["drive_folder_urls"].append(link)
                continue
        else:
            title, kind = _clean_title(file["name"]), "movie"
            link = f"https://drive.google.com/file/d/{file['id']}/view"
            if file.get("resourceKey"):
                link += f"?resourcekey={file['resourceKey']}"
        now = datetime.now(timezone.utc).isoformat()
        candidate = {
            "command_name": title or file["id"], "type": kind, "title": title or file["id"],
            "drive_url": link, "telegram_files": [], "access_level": "regular",
            "createdAt": file.get("createdTime") or now,
            "uploadedAt": file.get("createdTime") or now,
        }
        if details:
            candidate["drive_episode_file_ids"] = [file["id"]]
            candidate["drive_folder_urls"] = [link]
            candidate["drive_episode_details"] = {file["id"]: episode_details}
            candidate["_drive_parent_ids"] = list(file.get("folder_ancestor_ids", []))
            candidate["_drive_series_title"] = series
            seasons[key] = candidate
        candidates.append(candidate)
    return candidates


def _drive_identity(url):
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname != "drive.google.com":
        return None
    match = re.search(r"/(?:file/d|folders)/([\w-]+)", parsed.path)
    return match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]


def _identities(item):
    from drive_links import extract_drive_url
    identities = set(item.get("drive_episode_file_ids", []))
    for folder in item.get("drive_folder_urls", []):
        identity = _drive_identity(folder)
        if identity:
            identities.add(identity)
    for record in [item, *item.get("telegram_files", [])]:
        if record.get("drive_file_id"):
            identities.add(record["drive_file_id"])
        for value in (record.get("drive_url"), extract_drive_url(record.get("caption") or "")):
            identity = _drive_identity(value)
            if identity:
                identities.add(identity)
    return identities


def _merge_series_sources(item, candidate):
    before = copy.deepcopy(item)
    if candidate.get("drive_account_id") and not item.get("drive_account_id"):
        item["drive_account_id"] = candidate["drive_account_id"]
    for field in ("drive_episode_file_ids", "drive_folder_urls"):
        item[field] = list(dict.fromkeys([*item.get(field, []), *candidate.get(field, [])]))
    item["drive_episode_details"] = {
        **item.get("drive_episode_details", {}), **candidate.get("drive_episode_details", {}),
    }
    return item != before


def save_scan_media(files, regular=None, vip=None, user_id=None):
    """One short repository transaction per store; no network IO under its lock.

    Use the repositories' persistence adapter, not raw SQL, so revision handling
    and JSON/Postgres storage modes stay identical to normal admin edits.
    """
    regular = regular or MediaRepository()
    vip = vip or MediaRepository(MEDIA_DB_FILE.with_name("vip_media_db.json"))
    from persistent_json import hidden_drive_file_ids
    hidden = hidden_drive_file_ids()
    candidates = scan_candidates([file for file in files if file["id"] not in hidden])
    added = linked = 0
    with _LOCK:
        stores = [(regular, regular._load_locked()), (vip, vip._load_locked())]
        dirty = set()
        by_identity, by_title, by_series_base, names, vip_series = {}, {}, {}, set(), set()
        def series_key(title):
            return re.sub(r"[\W_]+", "", _clean_title(title).casefold())
        def index_item(index, item):
            for identity in _identities(item):
                by_identity.setdefault(identity, (index, item))
            key = (item["type"], item["title"].strip().casefold())
            by_title.setdefault(key, []).append((index, item))
            if item["type"] == "series" and not SEASON.search(item["title"]):
                by_series_base.setdefault(series_key(item["title"]), []).append((index, item))
            if item["type"] == "series" and (index == 1 or item.get("access_level") == "vip"):
                marker = SEASON.search(item["title"])
                vip_series.add(series_key(item["title"][:marker.start()] if marker else item["title"]))
            names.add(item["command_name"])
        for index, (_, data) in enumerate(stores):
            for item in data["items"]:
                index_item(index, item)
        for candidate in candidates:
            if user_id is not None:
                candidate["drive_account_id"] = user_id
            identity = _drive_identity(candidate["drive_url"])
            owned = by_identity.get(identity)
            if not owned and candidate["type"] == "series":
                episode_owners = { (owner[0], owner[1]["command_name"]): owner
                    for file_id in candidate["drive_episode_file_ids"]
                    if (owner := by_identity.get(file_id)) }
                if len(episode_owners) == 1:
                    owner = next(iter(episode_owners.values()))
                    if owner[1]["type"] == "series":
                        owned = owner
                if not owned and any(owner[0] == 1 or owner[1].get("access_level") == "vip"
                                     for owner in episode_owners.values()):
                    continue
            if not owned and candidate["type"] == "series":
                # A series may point at its parent folder while new seasons
                # live in descendants. Resolve real folder identity first.
                owned = next((by_identity[parent] for parent in reversed(candidate["_drive_parent_ids"])
                              if parent in by_identity and by_identity[parent][1]["type"] == "series"), None)
            if owned:
                index, item = owned
                if user_id is not None and item.get("drive_account_id") not in (None, user_id):
                    continue
                # Refresh the additive episode identity map without replacing
                # season text, trailers, VIP tier, keyboard or existing sources.
                if candidate["type"] == "series" and item["type"] == "series":
                    if _merge_series_sources(item, candidate):
                        for file_id in _identities(item):
                            by_identity.setdefault(file_id, (index, item))
                        dirty.add(index)
                continue
            matches = by_title.get((candidate["type"], candidate["title"].casefold()), [])
            if not matches and candidate["type"] == "series":
                matches = by_series_base.get(series_key(candidate["_drive_series_title"]), [])
            if matches:
                # Ambiguous regular/VIP title matches must never become public.
                if len(matches) != 1:
                    continue
                index, item = matches[0]
                if user_id is not None and item.get("drive_account_id") not in (None, user_id):
                    continue
                if candidate["type"] == "series" and item.get("drive_url"):
                    if _merge_series_sources(item, candidate):
                        for file_id in _identities(item):
                            by_identity.setdefault(file_id, (index, item))
                        dirty.add(index)
                        linked += 1
                    continue
                # A manually chosen Drive source is authoritative; never replace it.
                if not item.get("drive_url"):
                    item["drive_url"] = candidate["drive_url"]
                    if user_id is not None:
                        item["drive_account_id"] = user_id
                    if candidate["type"] == "series":
                        _merge_series_sources(item, candidate)
                    normalized = _validated_media(item)
                    item.update(normalized)
                    for file_id in _identities(item):
                        by_identity.setdefault(file_id, (index, item))
                    dirty.add(index)
                    linked += 1
                continue
            if candidate["command_name"] in names:
                candidate["command_name"] += f" [{identity}]"
            destination = 1 if candidate["type"] == "series" and series_key(candidate["_drive_series_title"]) in vip_series else 0
            if destination == 1:
                candidate["access_level"] = "vip"
            imported = _validated_media(candidate)
            stores[destination][1]["items"].append(imported)
            index_item(destination, imported)
            dirty.add(destination)
            added += 1
        for index in sorted(dirty):
            repository, data = stores[index]
            repository._save_locked(copy.deepcopy(data))
    return {"scanned": len(files), "added": added, "linked": linked}


class DriveCatalogSync:
    def __init__(self, bot, drive, regular=None, vip=None):
        self.bot, self.drive, self.regular, self.vip = bot, drive, regular, vip

    def start(self, user_id, folder="root"):
        if not is_admin(user_id):
            raise PermissionError("אין לך הרשאה לבצע סנכרון.")
        folder_id, key = folder_reference(folder)
        with _jobs_lock:
            if user_id in _jobs:
                self.bot.send_message(user_id, "⏳ סנכרון Drive כבר מתבצע. ניתן להמשיך להשתמש בבוט.")
                return False
            _jobs.add(user_id)
            cancelled = threading.Event()
            _cancel_events[user_id] = cancelled
        try:
            message = self.bot.send_message(user_id, "מתחיל סנכרון מלא של סרטים וסדרות מ-Google Drive…\nניתן להמשיך להשתמש בבוט בזמן הסריקה.")
            worker = threading.Thread(target=self._run, args=(user_id, folder_id, key, message.message_id, cancelled),
                                      name=f"drive-catalog-sync-{user_id}", daemon=True)
            worker.start()
        except Exception:
            with _jobs_lock:
                _jobs.discard(user_id)
                _cancel_events.pop(user_id, None)
            raise
        return True

    def _run(self, user_id, folder_id, key, message_id, cancelled=None):
        try:
            files = sync_drive_content(self.drive, user_id, folder_id, key, cancelled)
            if cancelled is not None and cancelled.is_set():
                raise ValueError("הסנכרון בוטל.")
            result = save_scan_media(files, self.regular, self.vip, user_id=user_id)
            text = (f"✅ הסנכרון הושלם בהצלחה!\nנסרקו {result['scanned']} קבצי סרטים ופרקים.\n"
                    f"נוספו {result['added']} רשומות; עודכנו {result['linked']} קישורי Drive.\n"
                    "תוכן קיים לא נמחק. הקטלוג באתר יתעדכן במחזור הרענון הקרוב.")
        except Exception as exc:
            logging.warning("Drive catalogue sync failed (%s).", type(exc).__name__)
            text = "⚠️ הסנכרון לא הושלם. בדוק את חיבור Google והרשאות התיקייה ונסה שוב. תוכן קיים לא נמחק."
            if cancelled is not None and cancelled.is_set():
                text = "סנכרון Drive בוטל בעקבות ניתוק החשבון. תוכן קיים לא נמחק."
        try:
            self.bot.edit_message_text(text, chat_id=user_id, message_id=message_id)
        except Exception:
            try:
                self.bot.send_message(user_id, text)
            except Exception:
                logging.warning("Could not deliver Drive sync status.")
        finally:
            with _jobs_lock:
                _jobs.discard(user_id)
                _cancel_events.pop(user_id, None)
