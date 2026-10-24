"""Short-lived authenticated relay for the independent CINEMIX web portal.

No polling, media writes, bot settings, or catalogue mutations. The Node API
checks portal entitlements on each request before issuing a signed relay header.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import urlsplit


def handle_portal_stream(handler, *, head=False):
    if urlsplit(handler.path).path != "/cinemix-stream":
        return False
    from web_streaming import _error, serve_record
    key = os.environ.get("SESSION_SECRET", "")
    ticket = handler.headers.get("X-Cinemix-Stream", "")
    try:
        if len(key) < 16 or len(ticket) > 16000:
            raise ValueError("Missing credentials")
        payload, signature = ticket.rsplit(".", 1)
        expected = hmac.new(key.encode(), f"cinemix-stream:{payload}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid signature")
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        now = int(time.time())
        if not now <= data["expires"] <= now + 120:
            raise ValueError("Expired")
        record = data["record"]
        if not isinstance(record["file_id"], str):
            raise ValueError("Invalid file")
    except (ValueError, KeyError, TypeError):
        _error(handler, 403, head)
        return True
    import web_streaming
    bot = web_streaming._BOT
    if bot is None:
        _error(handler, 503, head)
        return True
    try:
        current_bot = int(bot.token.split(":", 1)[0])
        if record.get("bot_id", current_bot) != current_bot:
            _error(handler, 404, head)
            return True
        # Existing media records only have file_id; resolve metadata at request
        # time without persisting it or exposing Telegram's download URL.
        if not isinstance(record.get("size"), int) or record["size"] <= 0:
            file = bot.get_file(record["file_id"])
            record["size"] = file.file_size
        if not isinstance(record["size"], int) or record["size"] <= 0:
            raise ValueError("Missing file size")
        record["bot_id"] = current_bot
        serve_record(handler, record, head=head)
    except Exception as exc:
        logging.warning("Portal Telegram relay unavailable (%s).", type(exc).__name__)
        _error(handler, 503, head)
    return True
