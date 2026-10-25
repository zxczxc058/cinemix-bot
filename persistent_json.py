"""PostgreSQL-backed JSON documents with local mirrors for existing backups.

Production uses BOT_STORAGE=postgres. Temporary/test paths remain file-backed.
Schema is created in development and applied to production by Replit Publish;
this module deliberately contains no runtime DDL.
"""

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
    """Catalogue imports must not resurrect portal-admin deletions."""
    if not postgres_enabled():
        return set()
    db = _database()
    if db.execute("SELECT to_regclass('cinemix_drive_movies')").fetchone()[0] is None:
        return set()
    return {row[0] for row in db.execute(
        "SELECT file_id FROM cinemix_drive_movies WHERE hidden = true"
    ).fetchall()}


def _mirror(path, data):
    """Atomic local export; database commits are the authoritative source."""
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
    if not _managed(path):
        return path.read_text(encoding="utf-8")
    with _LOCK:
        row = _database().execute(
            "SELECT body FROM telegram_bot_documents WHERE name = %s",
            (path.name,),
        ).fetchone()
        if row is None:
            # Missing documents may be added by a new feature. Never overwrite
            # existing database records with older bundled JSON.
            from psycopg.types.json import Jsonb
            seed = json.loads(path.read_text(encoding="utf-8"))
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
        # Commit synchronously before returning success to the administrator.
        # A DB outage must fail explicitly, never silently save only to disk.
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
    """Seed missing documents once; restore authoritative records on every boot."""
    if not postgres_enabled():
        return 0
    from psycopg.types.json import Jsonb
    with _LOCK:
        db = _database()
        # One atomic seed transaction; invalid JSON aborts the entire import.
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


def complete_cinemix_telegram_link(token, user):
    """Consume a short-lived portal link and attach the verified Telegram identity."""
    import re

    if not postgres_enabled():
        raise RuntimeError("Telegram account linking requires PostgreSQL-backed bot storage.")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return False
    user_id = getattr(user, "id", None)
    if user_id is None:
        return False
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    username = getattr(user, "username", None)
    first_name = getattr(user, "first_name", None)
    last_name = getattr(user, "last_name", None)
    with _LOCK:
        db = _database()
        link = db.execute(
            """SELECT id, device_key FROM cinemix_login_activity
               WHERE link_token_hash=%s AND link_expires_at>now()""",
            (token_hash,),
        ).fetchone()
        if link is None:
            return False
        updated = db.execute(
            """UPDATE cinemix_login_activity
               SET telegram_user_id=%s, telegram_username=%s,
                   telegram_first_name=%s, telegram_last_name=%s,
                   link_token_hash=NULL, link_expires_at=NULL
               WHERE id=%s AND link_token_hash=%s AND link_expires_at>now()
               RETURNING id""",
            (str(user_id), username, first_name, last_name, link[0], token_hash),
        ).fetchone()
        if updated is None:
            return False
        # Backfill this device's earlier code-only logins once its owner proves
        # control of the Telegram account. Future logins inherit the same link.
        db.execute(
            """UPDATE cinemix_login_activity
               SET telegram_user_id=%s, telegram_username=%s,
                   telegram_first_name=%s, telegram_last_name=%s
               WHERE device_key=%s AND telegram_user_id IS NULL""",
            (str(user_id), username, first_name, last_name, link[1]),
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        print(f"Persistent documents initialized: {initialize_storage()}")
    finally:
        close_storage()
