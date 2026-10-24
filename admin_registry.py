"""Dispatch by authenticated actor, without sharing administrator input state."""

import logging
from telebot.handler_backends import BaseMiddleware, CancelUpdate

from bot_storage import ADMIN_USER_ID, is_admin


class AdminRegistry:
    def __init__(self, owner_dashboard, factory):
        self.owner = owner_dashboard
        self.factory = factory
        self.items = {ADMIN_USER_ID: owner_dashboard}

    def get(self, user_id):
        if not is_admin(user_id):
            return None
        if user_id not in self.items:
            dashboard = self.factory(user_id)
            dashboard.management.backup = self.owner.management.backup
            dashboard.management._blast_lock = self.owner.management._blast_lock
            self.items[user_id] = dashboard
        return self.items[user_id]

    def has_pending_input(self, message):
        user = getattr(message, "from_user", None)
        dashboard = self.get(user.id) if user else None
        return dashboard is not None and dashboard.has_pending_input(message)

    def has_active_private_ui(self, user_id):
        """Keep keyword commands from stealing input only while the admin UI is active."""
        dashboard = self.get(user_id)
        if dashboard is None:
            return False
        if dashboard.pending.get(user_id):
            return True
        return str(user_id) in dashboard.bot.state.get("menus", {})

    def register_entrypoints(self, bot, support):
        """Reserve admin navigation before support and pending-input handlers."""
        def matches(message):
            user = getattr(message, "from_user", None)
            text = (getattr(message, "text", None) or "").strip()
            if not user or not is_admin(user.id) or not text:
                return False
            if message.chat.type == "private" and self.has_pending_input(message):
                return False
            command = text.split(maxsplit=1)[0].split("@", 1)[0]
            private = getattr(getattr(message, "chat", None), "type", None) == "private"
            return (
                text == "הגדרות"
                or command in {"/help", "/users"}
                or (private and command == "/admin")
            )

        def open_menu(message):
            actor = message.from_user.id
            dashboard = self.get(actor)
            dashboard.cancel_pending(actor)
            if dashboard.on_admin_navigation:
                dashboard.on_admin_navigation(actor, message.chat.id)
            else:
                support.cancel_user(actor, message.chat.id)
            dashboard.send_menu(message.chat.id, clear_state=False, fresh=True)

        bot.message_handler(func=matches, content_types=["text"])(open_menu)

    def register_handlers(self, bot):
        def callback(call):
            dashboard = None
            try:
                dashboard = self.get(call.from_user.id if call.from_user else None)
                (dashboard or self.owner).handle_callback(call)
            except Exception:
                logging.exception("Unhandled admin callback dispatch failure.")
                try:
                    bot.answer_callback_query(call.id)
                except Exception:
                    logging.debug("Could not acknowledge a failed admin callback.", exc_info=True)
                if dashboard and call.message:
                    try:
                        dashboard._rerender_callback_menu(
                            call.message.chat.id, call.data or "",
                        )
                    except Exception:
                        logging.exception("Could not restore the admin menu after dispatch failure.")
        bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("adm:"))(callback)
        bot.message_handler(func=self.has_pending_input, content_types=["text"])(
            lambda message: self.get(message.from_user.id).handle_pending_input(message)
        )
        bot.message_handler(
            func=lambda message: self.get(message.from_user.id) is not None and self.get(message.from_user.id).management.has_photo_input(message),
            content_types=["photo"],
        )(lambda message: self.get(message.from_user.id).management.handle_photo(message))


class AdminEditingMiddleware(BaseMiddleware):
    """Run active private editors before command, code and support consumers."""

    def __init__(self, registry):
        super().__init__()
        self.registry = registry
        self.update_types = ["message"]
        self.update_sensitive = True

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        if message.chat.type != "private" or user is None or not is_admin(user.id):
            return None
        command = (getattr(message, "text", None) or "").strip().split(maxsplit=1)
        if command and command[0].split("@", 1)[0].lower() in {"/start", "/cancel", "/logout", "/sync"}:
            return None
        dashboard = self.registry.get(user.id)
        handler = None
        if dashboard.has_pending_input(message):
            handler = dashboard.handle_pending_input
        elif dashboard.has_media_input(message):
            handler = dashboard.handle_media_input
        elif (dashboard.pending.get(user.id)
              and dashboard.pending_chat_id in {None, message.chat.id}):
            # Unsupported payloads still belong to this wizard. Let its
            # validation handle them rather than forwarding them as tickets.
            handler = dashboard.handle_pending_input
        if handler is None:
            return None
        try:
            handler(message)
        except Exception:
            # Never turn a failed management input into a support request.
            logging.exception("Private admin editor failed; input remains isolated.")
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        pass
