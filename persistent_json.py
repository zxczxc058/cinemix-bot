"""PostgreSQL-backed JSON documents with local mirrors for existing backups."""

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import threading

DATA_ROOT = Path(__file__).resolve().parent
DOCUMENT_NAMES = frozenset({
    "access_settings.json", "activity.json", "admin_ui_state.json",
    "backup_state.json", "command_drafts.json", "commands.json", "config.json",
    "donation_broadcast.json", "force_subscribe_settings.json",
    "group_settings.json", "groups.json", "link_health.json",
    "live_chat_state.json", "media_analytics.json", "media_db.json",
    "missing_content_requests.json", "moderation_events.json",
    "moderation_routes.json", "night_lock_state.json", "reply_keyboard.json",
    "reports.json", "support_settings.json", "support_threads.json",
    "system_settings.json", "tasks.json", "users.json",
    "vip_media_db.json", "vip_settings.json",
    "private_inbox_delivery.json",
    "web_streams.json",
})

def _default_content(name):
    if name == "tasks.json":
        return {"tasks": []}
    if name in {"groups.json", "users.json", "media_db.json", "vip_media_db.json", "commands.json"}:
        return []
    if name == "backup_state.json":
        return {"backups": [], "last_backup": None}
    return {}

_LOCK = threading.RLock()
_connection = None

def postgres_enabled():
    mode = os.environ.get("BOT_STORAGE", "json")
    if mode not in {"json", "postgres"}:
        raise RuntimeError("BOT_STORAGE must be json or postgres.")
    return mode == "postgres"

def _managed(path):
    path = Path(path).resolve()
    return (
        postgres_enabled()
        and path.parent == DATA_ROOT
        and path.name in DOCUMENT_NAMES
    )

def _database():
    global _connection
    if _connection is None or _connection.closed:
        import psycopg
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("Persistent bot storage requires DATABASE_URL.")
        _connection = psycopg.connect(url, autocommit=True, connect_timeout=10)
    return _connection

def close_storage():
    global _connection
    with _LOCK:
        if _connection is not None:
            _connection.close()
            _connection = None

def hidden_drive_file_ids():
    if not postgres_enabled():
        return set()
    db = _database()
    if db.execute("SELECT to_regclass('cinemix_drive_movies')").fetchone()[0] is None:
        return set()
    return {row[0] for row in db.execute(
        "SELECT file_id FROM cinemix_drive_movies WHERE hidden = true"
    ).fetchall()}

def _mirror(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

def read_json_text(path):
    path = Path(path)
    # AUTO-CREATE missing local files - this is the fix
    if not _managed(path):
        if not path.exists():
            default = _default_content(path.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return path.read_text(encoding="utf-8")
    with _LOCK:
        row = _database().execute(
            "SELECT body FROM telegram_bot_documents WHERE name = %s",
            (path.name,),
        ).fetchone()
        if row is None:
            from psycopg.types.json import Jsonb
            try:
                seed = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                seed = _default_content(path.name)
                path.write_text(json.dumps(seed, ensure_ascii=False, indent=2), encoding="utf-8")
            _database().execute(
                "INSERT INTO telegram_bot_documents (name, body) VALUES (%s, %s) "
                "ON CONFLICT (name) DO NOTHING",
                (path.name, Jsonb(seed)),
            )
            row = _database().execute(
                "SELECT body FROM telegram_bot_documents WHERE name = %s",
                (path.name,),
            ).fetchone()
        return json.dumps(row[0], ensure_ascii=False)

def write_json_document(path, data):
    path = Path(path)
    with _LOCK:
        if not _managed(path):
            _mirror(path, data)
            return
        from psycopg.types.json import Jsonb
        _database().execute(
            "INSERT INTO telegram_bot_documents (name, body) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET body = EXCLUDED.body, "
            "revision = telegram_bot_documents.revision + 1, updated_at = now()",
            (path.name, Jsonb(data)),
        )
        try:
            _mirror(path, data)
        except OSError:
            logging.warning("Database saved %s; local backup mirror could not be refreshed.", path.name)

def initialize_storage():
    if not postgres_enabled():
        return 0
    from psycopg.types.json import Jsonb
    with _LOCK:
        db = _database()
        with db.transaction():
            existing = {
                row[0] for row in db.execute(
                    "SELECT name FROM telegram_bot_documents"
                ).fetchall()
            }
            for name in sorted(DOCUMENT_NAMES):
                if name in existing:
                    continue
                path = DATA_ROOT / name
                if path.exists():
                    data = json.loads(path.read_text(encoding="utf-8"))
                else:
                    data = _default_content(name)
                    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                db.execute(
                    "INSERT INTO telegram_bot_documents (name, body) VALUES (%s, %s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (name, Jsonb(data)),
                )
        rows = db.execute("SELECT name, body FROM telegram_bot_documents").fetchall()
        for name, data in rows:
            if name in DOCUMENT_NAMES:
                _mirror(DATA_ROOT / name, data)
        logging.info("Persistent bot storage loaded (%s JSON documents).", len(rows))
        return len(rows)
