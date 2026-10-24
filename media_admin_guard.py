"""Silently discard media updates from users without admin privileges."""

from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import is_admin


MEDIA_CONTENT_TYPES = frozenset({
    "photo",
    "video",
    "document",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
})


class NonAdminMediaDropMiddleware(BaseMiddleware):
    """Keep media/file processing private to the configured bot admins."""

    def __init__(self):
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message"]

    def pre_process_message(self, message, data):
        if getattr(message, "content_type", None) not in MEDIA_CONTENT_TYPES:
            return None

        user = getattr(message, "from_user", None)
        try:
            if user is not None and is_admin(user.id):
                return None
        except Exception:
            # Fail closed if the configured admin list cannot be read.
            pass

        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        return None
