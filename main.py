"""Telegram bot entry point."""

import logging
import os
import argparse
import re
from contextlib import nullcontext
from threading import Event

import telebot
from telebot.handler_backends import BaseMiddleware
from dotenv import load_dotenv
from bot_runtime import RuntimeConfigurationError, select_bot_runtime, verify_production_bot_identity
from polling_health import PollingHealth
from bot_shutdown import install_shutdown_handlers, cleanup_services
from polling_lease import ProductionPollingLease

from admin_dashboard import AdminDashboard
from group_management import GroupManagementMiddleware, delete_later
from group_settings import load_group_settings
from night_lock import NightLockManager, NightMigrationMiddleware, load_night_state
from system_features import (
    ActivityMaintenanceMiddleware, load_system_settings, load_activity,
)
from access_control import PasscodeMiddleware, load_access
from admin_ui import AdminInputCleanupMiddleware, load_ui_state
from admin_channel_setup import AdminChannelSetup
from private_inbox import PrivateInbox, PrivateDeliveryMiddleware
from support_relay import SupportSystem, SupportOwnerRelayMiddleware
from support_storage import load_support_settings, load_support_threads
from media_catalog import (
    MediaCatalogue, VIPMediaCatalogue, VIP_MEDIA_DB_FILE,
    build_private_start_keyboard, sync_edited_media_message,
)
from media_storage import MediaRepository
from vip_access import VIPAccess, VIPSettingsStore
from live_chat import LiveChatQueue
from management_config import configure_identity, load_config, resolve_reply
from admin_tasks import load_tasks
from backup_service import load_backup_state
from force_subscribe import ForceSubscribeMiddleware, load_force_subscribe
from keepalive_server import start_keepalive_server, stop_keepalive_server
from moderation import BannedUserMiddleware, OwnerManagementCommands, load_moderation_routes
from bot_storage import (
    BotReply,
    UserRegistrationMiddleware,
    WELCOME_MESSAGE,
    build_inline_keyboard,
    format_telegram_text,
    get_reply,
    get_start_reply,
    is_admin,
    load_bot_data,
    load_group_ids,
    load_user_ids,
    update_group_membership,
)
from conversation_state import (
    ConversationStateResetMiddleware,
    clear_persisted_conversation_state,
    flush_persisted_conversation_state,
)

class IncomingUpdateLoggingMiddleware(BaseMiddleware):
    """Log incoming messages before any command or state-specific handler runs."""

    def __init__(self):
        super().__init__()
        self.update_sensitive = True
        self.update_types = ["message"]
        self.code_entry_pending = lambda chat_id: False

    def pre_process_message(self, message, data):
        user = getattr(message, "from_user", None)
        user_id = getattr(user, "id", None)
        text = getattr(message, "text", None) or getattr(message, "caption", None) or ""
        command = text.strip().split(maxsplit=1)[0].partition("@")[0].casefold() if text else ""
        if command == "/start" and "cinemix_link_" in text:
            text = "/start [portal link token redacted]"
        if getattr(message.chat, "type", None) == "private":
            try:
                if command != "/start" and self.code_entry_pending(message.chat.id):
                    text = "[access code input redacted]"
            except Exception:
                text = "[private message text omitted during state-check failure]"
        print(f"[INCOMING UPDATE] User ID: {user_id}, Text: {text}", flush=True)
        if command == "/start":
            print(f">>> RECEIVED /START FROM USER {user_id}", flush=True)

    def post_process_message(self, message, data, exception):
        return None


def open_admin_start_menu(registry, message) -> bool:
    """Open the authenticated admin's main menu, never a shared actor's menu."""
    user = getattr(message, "from_user", None)
    if (
        user is None or message.chat.type != "private"
        or not is_admin(user.id)
    ):
        return False
    dashboard = registry.get(user.id)
    if dashboard is None:
        return False
    dashboard.send_menu(message.chat.id, clear_state=True, fresh=True)
    return True


def handle_cinemix_telegram_link(bot, message) -> bool:
    """Handle the portal's one-time Telegram identity-link deep link."""
    text = getattr(message, "text", "") or ""
    match = re.fullmatch(r"/start(?:@\w+)?\s+cinemix_link_([A-Za-z0-9_-]{43})", text.strip())
    if not match:
        return False
    if getattr(message.chat, "type", None) != "private" or getattr(message, "from_user", None) is None:
        return True
    try:
        from persistent_json import complete_cinemix_telegram_link
        linked = complete_cinemix_telegram_link(match.group(1), message.from_user)
    except Exception:
        logging.exception("Could not complete CINEMIX Telegram account linking.")
        bot.reply_to(message, "לא ניתן להשלים את הקישור כרגע. חזרו לאתר וצרו קישור חדש.")
        return True
    if linked:
        bot.reply_to(message, "✅ חשבון הטלגרם אומת וחובר לחשבון CINEMIX.")
    else:
        bot.reply_to(message, "הקישור פג או שכבר נוצל. חזרו לאתר וצרו קישור חדש.")
    return True


def send_start_response(bot, message, *, commands_file=None, config_file=None) -> None:
    """Always try to deliver a usable /start response despite bad optional settings."""
    try:
        stored_reply = get_start_reply() if commands_file is None else get_start_reply(commands_file)
    except Exception:
        logging.exception("Could not load the configured /start message.")
        stored_reply = BotReply(WELCOME_MESSAGE, buttons_enabled=False)

    try:
        from management_config import CONFIG_FILE
        config_path = config_file or CONFIG_FILE
        from start_keyboard import KEY
        if message.chat.type == "private" and KEY in load_config(config_path):
            # Legacy URL extras must not override an explicitly managed list.
            stored_reply = BotReply(
                stored_reply.text, report_id=stored_reply.report_id,
                buttons_enabled=stored_reply.buttons_enabled,
            )
        reply = resolve_reply(stored_reply, config_path)
    except Exception as exc:
        logging.warning(
            "Could not resolve optional /start links (%s); sending the welcome text without buttons.",
            type(exc).__name__,
        )
        # A missing link should not suppress /start. Preserve the configured
        # welcome text, but omit buttons whose URLs may be incomplete.
        reply = BotReply(
            stored_reply.text, buttons=(), report_id=stored_reply.report_id,
            buttons_enabled=False,
        )

    text, parse_mode = format_telegram_text(reply.text)
    try:
        markup = (
            None if not reply.buttons_enabled
            else (
                build_private_start_keyboard(reply.buttons, config_path=config_path)
                if message.chat.type == "private"
                else build_inline_keyboard(reply.buttons)
            )
        )
        bot.reply_to(message, text, reply_markup=markup, parse_mode=parse_mode)
    except Exception:
        logging.exception("Could not send the configured /start message; trying a safe fallback.")
        fallback_text, fallback_mode = format_telegram_text(WELCOME_MESSAGE)
        try:
            bot.send_message(message.chat.id, fallback_text, parse_mode=fallback_mode)
        except Exception:
            logging.exception("Could not deliver the /start fallback message.")


def run_bot(runtime_mode="development") -> None:
    load_dotenv()
    try:
        runtime = select_bot_runtime(runtime_mode, os.environ)
    except RuntimeConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    polling_health = PollingHealth(
        runtime.mode, standby=runtime.token is None,
        credential_key=runtime.credential_key,
    )
    if runtime.token is None:
        logging.warning(
            "Development standby: DEV_BOT_TOKEN is not configured. "
            "Telegram polling, webhook changes, storage and background jobs are disabled."
        )
        server = start_keepalive_server(
            response_body=b"Development standby: Telegram polling disabled. Configure DEV_BOT_TOKEN.\n",
            polling_health=polling_health,
        )
        restore_signals = install_shutdown_handlers()
        try:
            Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            cleanup_services([
                ("keep-alive HTTP server", lambda: stop_keepalive_server(server)),
                ("signal handlers", restore_signals),
            ])
        return
    logging.info("Selected Telegram runtime: %s.", runtime.mode)
    # Own the production session before storage recovery or Telegram IO. The
    # replacement VM must serve liveness while the previous VM still owns it;
    # otherwise publishing cannot promote it and retire the old VM.
    lease = (
        ProductionPollingLease(runtime.token, wait_on_busy=True)
        if runtime.mode == "production" else ProductionPollingLease(runtime.token)
        if runtime.mode in {"production", "preview"} else nullcontext()
    )
    begin_shutdown = lease.begin_shutdown if runtime.mode in {"production", "preview"} else None
    restore_signals = install_shutdown_handlers(before_stop=begin_shutdown)
    server = None
    try:
        if runtime.mode == "production":
            server = start_keepalive_server(polling_health=polling_health)
        with lease:
            try:
                if server is None:
                    _run_active_runtime(runtime, polling_health, begin_shutdown)
                else:
                    _run_active_runtime(runtime, polling_health, begin_shutdown, server=server)
            finally:
                # Stop the staging HTTP worker before releasing ownership,
                # just as the SDK and background workers stop before handoff.
                if server is not None:
                    polling_health.stop()
                    stop_keepalive_server(server)
                    server = None
    finally:
        if server is not None:
            polling_health.stop()
            stop_keepalive_server(server)
        restore_signals()


def _run_active_runtime(runtime, polling_health, begin_shutdown=None, *, server=None):
    owns_server = server is None
    if owns_server:
        server = start_keepalive_server(polling_health=polling_health)
    try:
        _run_polling_bot(runtime, polling_health, begin_shutdown)
    except Exception as exc:
        polling_health.failure(exc)
        raise
    finally:
        polling_health.stop()
        if owns_server:
            stop_keepalive_server(server)


def capture_bot_identity(bot, polling_health, identity=None):
    """Reuse production verification, or query the separate development client."""
    if identity is None:
        identity = bot.get_me()
    polling_health.set_bot_identity(identity.username)


def _run_polling_bot(runtime, polling_health, begin_shutdown=None):
    from persistent_json import initialize_storage
    initialize_storage()
    # Validate persistent data before accepting Telegram updates.
    load_bot_data()
    load_user_ids()
    load_group_ids()
    load_group_settings()
    load_system_settings()
    load_activity()
    load_night_state()
    load_access()
    load_ui_state()
    load_support_settings()
    load_support_threads()
    load_config()
    load_tasks()
    load_backup_state()
    load_force_subscribe()
    load_moderation_routes()
    VIPSettingsStore().load()
    MediaRepository(VIP_MEDIA_DB_FILE).list_media()
    # Clear abandoned wizard/input states from prior processes once, while
    # retaining saved users, access grants, reports, and committed media.
    flush_persisted_conversation_state()

    from safe_bot import SafeTelegramBot
    bot = SafeTelegramBot(
        runtime.token, use_class_middlewares=True, polling_health=polling_health,
        shutdown_callback=begin_shutdown,
    )
    # Cover initialization failures as well as interrupted polling. WorkerThread
    # instances are already running as soon as TeleBot construction returns.
    try:
        _configure_and_poll_bot(bot, runtime, polling_health)
    finally:
        bot.stop_bot()


def _configure_and_poll_bot(bot, runtime, polling_health):
    identity = verify_production_bot_identity(bot, runtime.mode)
    capture_bot_identity(bot, polling_health, identity)
    from web_streaming import configure_stream_bot
    configure_stream_bot(bot)
    if identity is not None:
        logging.info("%s Telegram bot verified: @%s.", runtime.mode.capitalize(), identity.username)
    from media_admin_guard import NonAdminMediaDropMiddleware
    bot.setup_middleware(NonAdminMediaDropMiddleware())
    incoming_logger = IncomingUpdateLoggingMiddleware()
    bot.setup_middleware(incoming_logger)
    # VIP users must not redeem codes or reach menus ahead of moderation.
    # Existing regular-user moderation handlers remain unchanged.
    from vip_experience import VIPRestrictionGuard, VIPExperience
    bot.setup_middleware(VIPRestrictionGuard(bot, lambda: engine))
    from private_admin_protection import PrivateAdminProtection
    bot.setup_middleware(PrivateAdminProtection(bot))
    force_subscribe = ForceSubscribeMiddleware(bot)
    from allowed_chats import AllowedChats
    allowed_chats = AllowedChats(bot)
    bot.chat_allowlist = allowed_chats
    # Security runs before channel setup, moderation, greetings and command handlers.
    bot.setup_middleware(allowed_chats)
    bot.setup_middleware(ConversationStateResetMiddleware())
    bot.setup_middleware(UserRegistrationMiddleware())
    bot.setup_middleware(force_subscribe)
    from anti_spam import GroupAntiSpamMiddleware
    bot.setup_middleware(GroupAntiSpamMiddleware(bot))
    allowed_chats.register_handlers()
    support = SupportSystem(bot)
    night_manager = NightLockManager(bot)
    from admin_registry import AdminRegistry
    from command_reports import CommandReports
    from restriction_engine import RestrictionEngine
    from restriction_controls import RestrictionControls
    from restriction_middleware import RestrictionMiddleware
    from drive_checker import DriveChecker
    from safe_bot import SupportFormattingSource
    dashboard = AdminDashboard(bot, night_manager=night_manager)
    dashboard.vip_admin.store = VIPSettingsStore()

    def create_dashboard(actor_id):
        child = AdminDashboard(bot, night_manager=night_manager, actor_id=actor_id)
        child.on_admin_navigation = admin_navigation
        child.media_manager = media_catalogue
        child.vip_media_manager = vip_media_catalogue
        child.vip_admin.store = vip_access.store
        child.restriction_controls = controls
        child.drive_checker = checker
        return child

    registry = AdminRegistry(dashboard, create_dashboard)
    core = OwnerManagementCommands(bot)
    reports = CommandReports(bot, registry, dashboard.commands_file, dashboard.support_admin.path)
    engine = RestrictionEngine(bot, core)
    inbox = PrivateInbox(bot, engine, dashboard.commands_file)
    inbox.admin_registry = registry
    controls = RestrictionControls(engine, registry)
    checker = DriveChecker(bot, registry, dashboard.commands_file, engine.path, core.support_file)
    bot.restriction_service = engine
    dashboard.restriction_controls = controls
    dashboard.drive_checker = checker

    media_catalogue = None
    vip_media_catalogue = None
    vip_access = VIPAccess(
        bot,
        store=dashboard.vip_admin.store,
        media_repository=MediaRepository(VIP_MEDIA_DB_FILE),
        request_service=inbox.missing_content,
    )
    # Persist missing VIP defaults (including vip_code_enabled) on startup.
    vip_access.store.load()
    live_chat = LiveChatQueue(
        bot,
        config_file=engine.path,
        support_settings_file=support.settings_file,
        moderation_markup=engine.markup,
        vip_store=vip_access.store,
    )
    live_chat.load()
    bot.live_chat_queue = live_chat
    vip_access.live_chat = live_chat
    inbox.live_chat = live_chat
    inbox.missing_content.live_chat = live_chat
    support.live_chat_queue = live_chat
    support.admin_relay = inbox.relay_support_request
    support.admin_inbox = inbox

    def admin_navigation(user_id, chat_id):
        # Reset each state independently: one damaged optional draft must not
        # prevent /start or the main-menu button from navigating.
        operations = []
        if media_catalogue is not None:
            operations.append(("standard media wizard", lambda: media_catalogue.pause(user_id)))
        if vip_media_catalogue is not None:
            operations.append(("VIP media wizard", lambda: vip_media_catalogue.pause(user_id)))
        # A navigation action also exits an unfinished VIP-code prompt. The
        # user can reopen it from the menu if they still want to enter a code.
        operations.extend([
            ("VIP code prompt", lambda: vip_access.store.remove_pending(user_id)),
            ("support prompt", lambda: support.cancel_user(user_id, chat_id)),
            ("purchase inquiry", lambda: purchase_inquiry.cancel_user(user_id)),
        ])
        from command_reports import _LOCK, load_drafts
        from management_config import save_json

        def clear_report_draft():
            with _LOCK:
                drafts = load_drafts(reports.drafts_file)
                drafts["drafts"] = {
                    key: value for key, value in drafts["drafts"].items()
                    if value["admin_id"] != user_id
                }
                save_json(reports.drafts_file, drafts)

        operations.append(("report draft", clear_report_draft))
        for label, operation in operations:
            try:
                operation()
            except Exception:
                logging.exception("Could not clear %s during menu navigation.", label)

    dashboard.on_admin_navigation = admin_navigation

    media_catalogue = MediaCatalogue(bot, registry, inbox)
    media_catalogue.cancel_support_request = support.cancel_user
    dashboard.media_manager = media_catalogue
    vip_media_catalogue = VIPMediaCatalogue(
        bot,
        registry,
        inbox,
        repository=vip_access.media_repository,
        vip_access=vip_access,
    )
    vip_access.catalogue = vip_media_catalogue
    dashboard.vip_media_manager = vip_media_catalogue
    experience = VIPExperience(bot, media_catalogue, vip_media_catalogue, vip_access.store, engine)
    experience.registry = registry
    experience.live_chat = live_chat
    media_catalogue.experience = experience
    vip_media_catalogue.experience = experience
    media_catalogue.access_service = vip_access
    vip_access.standard_media_repository = media_catalogue.repository

    def channel_navigation(actor, chat_id):
        registry.get(actor).cancel_pending(actor)
        admin_navigation(actor, chat_id)

    channel_setup = AdminChannelSetup(bot, engine.path, channel_navigation)

    def reset_user_conversation(user_id, chat_id):
        admin_navigation(user_id, chat_id)
        clear_persisted_conversation_state(user_id)
        if is_admin(user_id):
            try:
                registry.get(user_id).cancel_pending(user_id)
            except Exception:
                logging.exception("Could not clear admin input state.")

    # Register reset commands before wizard and pending-input handlers.
    @bot.message_handler(commands=["start"])
    def send_welcome(message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        print(f">>> EXECUTING /START HANDLER FOR USER {user_id}", flush=True)
        if user_id is not None:
            # State cleanup must never prevent the welcome response. Existing
            # users may have stale or malformed wizard data unlike new users.
            try:
                reset_user_conversation(user_id, message.chat.id)
            except Exception:
                logging.exception(
                    "Could not reset prior conversation before /start (user_id=%s).",
                    user_id,
                )
        if handle_cinemix_telegram_link(bot, message):
            return
        try:
            if open_admin_start_menu(registry, message):
                logging.info("/start admin menu dispatched (user_id=%s).", user_id)
                return
        except Exception:
            logging.exception(
                "Could not open the /start admin menu; falling back to welcome (user_id=%s).",
                user_id,
            )
        try:
            if passcode_gate.start_response(message):
                logging.info("/start authorization/welcome dispatched (user_id=%s).", user_id)
                return
            send_start_response(bot, message)
            logging.info("/start welcome dispatched (user_id=%s).", user_id)
        except Exception:
            logging.exception("Could not send /start welcome (user_id=%s).", user_id)
    @bot.message_handler(commands=["sync"])
    def sync_google_catalog(message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        if user_id is None or not is_admin(user_id) or message.chat.type != "private":
            return
        try:
            from drive_catalog_sync import DriveCatalogSync
            from drive_uploads import DriveUploads
            current = registry.get(user_id)
            parts = (message.text or "").split(maxsplit=1)
            DriveCatalogSync(bot, DriveUploads(bot, current.management.config_file)).start(
                user_id, parts[1] if len(parts) > 1 else "root",
            )
        except ValueError:
            bot.send_message(user_id, "הפעל /sync או /sync ואחריו קישור לתיקיית Google Drive.")
        except Exception:
            logging.warning("Could not start Drive catalogue sync.")
            bot.send_message(user_id, "לא ניתן להתחיל את הסנכרון כרגע. נסה שוב בהמשך.")

    @bot.message_handler(commands=["reset_google_oauth"])
    def reset_google_connections(message: telebot.types.Message) -> None:
        from bot_storage import ADMIN_USER_ID
        user_id = message.from_user.id if message.from_user is not None else None
        if user_id != ADMIN_USER_ID or message.chat.type != "private":
            return
        try:
            from drive_uploads import DriveUploads
            current = registry.get(user_id)
            DriveUploads(bot, current.management.config_file).reset_all_oauth(user_id)
            current.cancel_pending(user_id)
            bot.send_message(user_id,
                "כל חיבורי Google ואסימוני OAuth השמורים אופסו. קישורי התחברות קודמים אינם תקפים. "
                "התוכן והקבצים נשמרו. לחץ על התחברות/החלפת חשבון Google ואשר מחדש גישה ל-Drive.")
        except Exception:
            logging.warning("Google OAuth global reset could not complete.")
            try:
                bot.send_message(user_id, "איפוס חיבורי Google לא הושלם. נסה שוב בהמשך.")
            except Exception:
                logging.warning("Could not deliver Google reset status.")

    @bot.message_handler(commands=["logout"])
    def logout_google_account(message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        if user_id is None or not is_admin(user_id) or message.chat.type != "private":
            return
        try:
            from drive_uploads import DriveUploads
            current = registry.get(user_id)
            DriveUploads(bot, current.management.config_file).logout(user_id)
            current.cancel_pending(user_id)
            bot.send_message(message.chat.id,
                "החשבון נותק בהצלחה! כעת ניתן ללחוץ על התחברות ולבחור כל חשבון Google שתרצה.")
        except Exception:
            logging.warning("Google logout could not complete.")
            try:
                bot.send_message(message.chat.id, "הניתוק לא הושלם. נסה שוב בהמשך.")
            except Exception:
                logging.warning("Could not deliver Google logout status.")

    @bot.message_handler(commands=["cancel"])
    def cancel_conversation(message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        if user_id is None:
            return
        reset_user_conversation(user_id, message.chat.id)
        if is_admin(user_id) and message.chat.type == "private":
            current = registry.get(user_id)
            current.bot.hide_menu(message.chat.id)
            current.send_menu(message.chat.id, clear_state=True, fresh=True)
        elif message.chat.type == "private":
            send_welcome(message)
        else:
            # The state is cleared for group commands too; keep groups quiet.
            return

    from admin_registry import AdminEditingMiddleware
    bot.setup_middleware(AdminEditingMiddleware(registry))
    bot.setup_middleware(channel_setup)
    bot.setup_middleware(RestrictionMiddleware(engine, registry, support))
    bot.setup_middleware(BannedUserMiddleware(bot))
    bot.setup_middleware(core)
    bot.setup_middleware(reports)
    bot.setup_middleware(NightMigrationMiddleware(night_manager))
    bot.setup_middleware(SupportFormattingSource(bot, group_only=True))
    from admin_group_commands import AdminGroupCommandMiddleware
    bot.setup_middleware(AdminGroupCommandMiddleware(
        bot, media_catalogue.repository, vip_media_catalogue.repository,
    ))
    bot.setup_middleware(SupportOwnerRelayMiddleware(support))
    from daily_command_limiter import DailyCommandLimiter, DailyLimiterMiddleware, is_private_daily_request
    from bot_storage import match_reply
    from group_commands import match_group_command
    from reply_keyboard import get_button_trigger

    daily_limiter = DailyCommandLimiter(
        engine.path, vip_check=vip_access.store.is_vip,
    )

    def eligible_daily_search(message):
        user = getattr(message, "from_user", None)
        text = (getattr(message, "text", None) or "").strip()
        chat_type = getattr(getattr(message, "chat", None), "type", None)
        if not user or is_admin(user.id) or chat_type not in {"private", "group", "supergroup"}:
            return False
        if chat_type == "private":
            return is_private_daily_request(message, get_button_trigger(text))
        # Unknown group messages must remain completely silent.
        return bool(
            match_group_command(message.chat.id, text, engine.path) is not None
            or match_reply(text, dashboard.commands_file) is not None
            or media_catalogue.repository.match_exact(text) is not None
            or vip_media_catalogue.repository.match_exact(text) is not None
        )

    group_daily_limiter = DailyLimiterMiddleware(
        bot, daily_limiter,
        eligible=lambda message: message.chat.type in {"group", "supergroup"} and eligible_daily_search(message),
    )
    bot.setup_middleware(group_daily_limiter)
    bot.setup_middleware(GroupManagementMiddleware(bot))
    passcode_gate = PasscodeMiddleware(
        bot, vip_store=vip_access.store, support_pending=support.has_pending,
    )
    incoming_logger.code_entry_pending = (
        lambda chat_id: chat_id in passcode_gate.awaiting_code_input
        or chat_id in passcode_gate.optional_code_input
    )
    from purchase_inquiry import PurchaseInquiryMiddleware
    from system_features import load_system_settings

    def clear_purchase_code_state(user_id, chat_id):
        with passcode_gate._attempt_lock:
            passcode_gate.awaiting_code_input.discard(chat_id)
        vip_access.store.remove_pending(user_id)

    purchase_inquiry = PurchaseInquiryMiddleware(
        inbox, clear_code_state=clear_purchase_code_state,
        cancel_support=support.cancel_user,
        allow_inquiry=lambda user_id: (
            is_admin(user_id) or not load_system_settings(
                dashboard.advanced.system_file,
            )["maintenance_enabled"]
        ),
    )
    vip_access.purchase_inquiry = purchase_inquiry
    # Charge support/media requests before their middleware consumes them.
    # Credential inputs and reset commands remain isolated from the quota.
    bot.setup_middleware(DailyLimiterMiddleware(
        bot, daily_limiter,
        eligible=lambda message: message.chat.type == "private" and eligible_daily_search(message),
        skip=lambda message: (
            live_chat.is_active_user(message.from_user.id)
            or (
                not support.has_pending(message)
                and message.from_user.id not in purchase_inquiry.pending
                and (
                    message.chat.id in passcode_gate.awaiting_code_input
                    or message.chat.id in passcode_gate.optional_code_input
                    or vip_access.store.has_pending(message.from_user.id)
                )
            )
        ),
        catalog_prefixes=(media_catalogue.user_prefix, vip_media_catalogue.user_prefix),
        request_callbacks={
            ("vx", "random"),
            (media_catalogue.user_prefix, "report"),
            (vip_media_catalogue.user_prefix, "report"),
            ("vip", "contact"), ("vip", "purchase"),
        },
    ))
    bot.setup_middleware(purchase_inquiry)
    bot.setup_middleware(passcode_gate)
    force_subscribe.awaiting_access_code = lambda message: (
        message.chat.id in passcode_gate.awaiting_code_input
        or vip_access.store.has_pending(message.from_user.id)
    )
    from reply_keyboard import ReplyKeyboardMiddleware
    bot.setup_middleware(SupportFormattingSource(bot, group_only=False))
    bot.setup_middleware(ActivityMaintenanceMiddleware(bot))
    bot.setup_middleware(PrivateDeliveryMiddleware(
        inbox,
        awaiting_code=lambda user_id: (
            user_id in passcode_gate.awaiting_code_input
            or vip_access.store.has_pending(user_id)
        ),
        known_content=lambda message: (
            media_catalogue.matches_private_media_command(message)
            or vip_media_catalogue.matches_private_media_command(message)
            or experience.matches(message)
        ),
    ))
    bot.setup_middleware(ReplyKeyboardMiddleware(bot))
    bot.setup_middleware(AdminInputCleanupMiddleware(bot, registry))

    def switch_admin_to_support(user_id):
        current = registry.get(user_id)
        if current:
            if media_catalogue is not None:
                media_catalogue.pause(user_id)
            if vip_media_catalogue is not None:
                vip_media_catalogue.pause(user_id)
            current.cancel_pending(user_id)
            current.bot.hide_menu(user_id)

    support.on_admin_open = switch_admin_to_support
    channel_setup.register_handlers()
    # Navigation must precede /help's user welcome, support relays and wizards.
    registry.register_entrypoints(bot, support)
    experience.register_handlers()

    from reply_keyboard import get_button_trigger, send_button_response
    from reply_keyboard_admin import input_active

    @bot.message_handler(
        func=lambda message: message.chat.type == "private"
        and not input_active(message, registry)
        and get_button_trigger(message.text or "") is not None,
        content_types=["text"],
    )
    def handle_reply_keyboard_button(message):
        """Dispatch keyboard actions before any wizard can consume their text."""
        import copy
        trigger = get_button_trigger(message.text or "")
        if trigger is None or message.from_user is None:
            return
        reset_user_conversation(message.from_user.id, message.chat.id)
        if send_button_response(bot, message):
            return
        routed = copy.copy(message)
        routed.text = trigger
        routed.reply_keyboard_triggered = True
        command = trigger.split(maxsplit=1)[0].partition("@")[0]
        if command == "/start":
            send_welcome(routed)
        elif command == "/cancel":
            cancel_conversation(routed)
        elif command in {"/admin", "/help", "/users"} or trigger == "הגדרות":
            if is_admin(message.from_user.id):
                open_admin_dashboard(routed)
            elif command == "/help":
                send_welcome(routed)
            else:
                inbox.handle_message(routed)
        elif media_catalogue.matches_private_media_command(routed):
            media_catalogue.handle_private_media_command(routed)
        elif vip_media_catalogue.matches_private_media_command(routed):
            vip_media_catalogue.handle_private_media_command(routed)
        else:
            inbox.handle_message(routed)

    @bot.my_chat_member_handler()
    def track_group_membership(update: telebot.types.ChatMemberUpdated) -> None:
        if update.chat.type == "private":
            passcode_gate.handle_private_membership(update)
            return
        allowed_chats.membership(update)
        update_group_membership(update)

    @bot.message_handler(commands=["help"])
    def send_help(message: telebot.types.Message) -> None:
        send_welcome(message)

    @bot.message_handler(commands=["users"])
    def open_admin_dashboard(message: telebot.types.Message) -> None:
        user_id = message.from_user.id if message.from_user is not None else None
        if not is_admin(user_id):
            inbox.handle_message(message)
            return
        current = registry.get(user_id)
        current.cancel_pending(user_id)
        support.cancel_user(user_id, message.chat.id)
        current.send_menu(message.chat.id, clear_state=False, fresh=True)

    @bot.message_handler(commands=["set_support_chat"])
    def configure_support_group(message: telebot.types.Message) -> None:
        current = registry.get(message.from_user.id) if message.from_user else None
        if current:
            current.support_admin.set_chat(message)
        else:
            inbox.handle_message(message)

    # Media report input must take priority over the generic support-request
    # text state; starting a media report also cancels any older support draft.
    vip_access.register_handlers()
    live_chat.register_handlers()
    media_catalogue.register_handlers(bot)
    vip_media_catalogue.register_handlers(bot)

    edited_media_types = ["text", "photo", "video", "document", "animation", "audio"]

    @bot.edited_message_handler(content_types=edited_media_types)
    def sync_edited_user_media(message):
        sync_edited_media_message(message, (media_catalogue, vip_media_catalogue))

    @bot.edited_channel_post_handler(content_types=edited_media_types)
    def sync_edited_channel_media(message):
        sync_edited_media_message(
            message, (media_catalogue, vip_media_catalogue), allow_channel_post=True,
        )

    @bot.channel_post_handler(content_types=edited_media_types)
    def sync_new_channel_media(message):
        sync_edited_media_message(
            message, (media_catalogue, vip_media_catalogue), allow_channel_post=True,
        )

    support.register_handlers()
    reports.register_handlers()
    bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("mod:"))(controls.action)
    inbox.register_missing_content_callback()
    bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("health:"))(checker.callback)
    force_subscribe.register_handlers()
    registry.register_handlers(bot)

    @bot.callback_query_handler(func=lambda call: True)
    def recover_unrecognized_button(call):
        # Registered feature handlers above retain their own popup/ack logic.
        # Old or malformed buttons must not leave Telegram's spinner hanging.
        try:
            bot.answer_callback_query(
                call.id, text="הכפתור אינו זמין עוד. פתחו את התפריט המעודכן.",
                show_alert=True,
            )
        except Exception:
            logging.warning("Could not acknowledge an unrecognized inline button.")
        logging.warning(
            "Unrecognized inline callback prefix: %s",
            (call.data or "").partition(":")[0][:32],
        )
        if call.message and call.message.chat.type == "private" and call.from_user:
            current = registry.get(call.from_user.id)
            if current is not None:
                current.cancel_pending(call.from_user.id)
                current.send_menu(call.message.chat.id, clear_state=True)

    @bot.message_handler(
        func=lambda message: bool(message.text),
        content_types=["text"],
    )
    def reply_to_keyword(message: telebot.types.Message) -> None:
        text = message.text or ""
        user_id = message.from_user.id if message.from_user is not None else None
        if text.strip() == "הגדרות":
            if is_admin(user_id):
                current = registry.get(user_id)
                current.cancel_pending(user_id)
                current.send_menu(message.chat.id, clear_state=False, fresh=True)
            else:
                inbox.handle_message(message)
            return
        if text.strip() == "משתמשים":
            if is_admin(user_id):
                current = registry.get(user_id)
                current.cancel_pending(user_id)
                current.send_statistics_menu(message.chat.id)
            else:
                inbox.handle_message(message)
            return

        inbox.handle_message(message)

    inbox.register_media_handler()

    logging.info("Removing any existing webhook before starting polling.")
    bot.remove_webhook()
    configure_identity(bot)
    allowed_chats.bootstrap()
    logging.info("Starting Telegram bot polling.")
    restore_signals = install_shutdown_handlers(bot, before_stop=bot.begin_shutdown)
    from drive_sync_monitor import DriveSyncMonitor
    from drive_uploads import DriveUploads
    drive_monitor = DriveSyncMonitor(DriveUploads(bot, dashboard.management.config_file))
    try:
        drive_monitor.start()
        night_manager.start()
        dashboard.management.backup.start()
        engine.start()
        checker.start()
        support.start()
        live_chat.start()
        experience.start()
        inbox._fallback_delivery.start()
        # pyTelegramBotAPI's infinity_polling retries failures internally by
        # calling polling(non_stop=True); interval=0 avoids added polling delay.
        bot.infinity_polling(
            skip_pending=False,
            interval=0,
            allowed_updates=["message", "edited_message", "channel_post", "edited_channel_post",
                             "callback_query", "my_chat_member"],
        )
    finally:
        bot.begin_shutdown()
        cleanup_services([
            ("Drive catalogue monitor", drive_monitor.stop),
            ("Telegram polling", bot.stop_polling),
            ("live chat queue", live_chat.stop),
            ("series notifications", experience.stop),
            ("fallback delivery queue", inbox._fallback_delivery.stop),
            ("support reminders", support.stop),
            ("night scheduler", night_manager.stop),
            ("backup scheduler", dashboard.management.backup.stop),
            ("moderation engine", engine.stop),
            ("link checker", checker.stop),
            ("signal handlers", restore_signals),
        ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", choices=("development", "production"), default="development",
        help="Development uses only DEV_BOT_TOKEN; production requires a published deployment.",
    )
    run_bot(parser.parse_args().runtime)
