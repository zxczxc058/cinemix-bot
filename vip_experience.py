"""Optional VIP discovery and series subscriptions on the existing JSON backend."""

import copy
import logging
import random
import threading
import time

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from bot_storage import is_admin
from media_storage import media_token
from media_genres import has_genre, genre_list
from vip_access import VIPSettingsStore, _LOCK

GENRES = (
    "🎬 אקשן", "🎭 קומדיה", "😱 אימה", "🍿 דרמה", '🚀 מד"ב',
    "🧙‍♂️ פנטזיה", "🕵️ מותחן", "🚨 פשע", "💖 רומנטיקה", "👶 ילדים",
    "🥋 ספורט", "🥋 לחימה", "📜 דוקו", "❓ אחר", "🪖 מלחמה",
)
RANDOM_LABELS = {"🎲 מה לראות היום?", "🎲 מה לראות היום (VIP)"}
TRACK_LABEL = "🔔 הסדרות שלי במעקב"
BENEFITS_LABEL = "⭐ יתרונות VIP"
WATCH_LABEL = "📌 המועדפים שלי (Watchlist)"
MESSAGES = {
    "restricted": "⭐ זמין רק למשתמשי VIP!",
    "empty": "אין כרגע תוכן זמין לבחירה.",
    "choose_genre": "🎲 בחרו ז'אנר:",
    "tracking_title": "🔔 הסדרות שלי במעקב",
    "following": "✅ הסדרה נוספה למעקב.",
    "unfollowed": "✅ המעקב הוסר.",
    "episode": "🔔 פרקים חדשים בסדרה {CONTENT_NAME}! חפשו בבוט: {COMMAND_NAME}",
    "benefits": "⭐ משתמשי VIP מקבלים תוכן VIP, סינון לפי ז'אנר ומעקב סדרות בהתאם להגדרות הבוט.",
    "revoked": "גישה ל-VIP הוסרה. התפריט הרגיל זמין עבורך.",
    "disabled": "אפשרות זו כבויה כרגע.",
    "moderated": "⛔ הפעולה אינה זמינה בזמן חסימה או השתקה.",
}
DEFAULTS = {
    "favorites": {},
    "keyboard_enabled": False,
    "genre_tagging": False,
    "random_policy": "vip",
    "tracking_policy": "vip",
    "messages": MESSAGES,
    "subscriptions": {},
    "pending_notifications": {},
}


class ExperienceStore:
    def __init__(self, vip_store=None):
        self.vip = vip_store or VIPSettingsStore()

    def snapshot(self):
        raw = self.vip.snapshot().get("experience", {})
        state = {**copy.deepcopy(DEFAULTS), **copy.deepcopy(raw)}
        state["messages"] = {**MESSAGES, **state.get("messages", {})}
        return state

    def mutate(self, action):
        with _LOCK:
            state = self.vip.load()
            current = self.snapshot()
            result = action(current)
            state["experience"] = current
            self.vip._save(state)
            return result

    def set(self, key, value):
        if key in {"random_policy", "tracking_policy"} and value not in {"vip", "public", "off"}:
            raise ValueError("Invalid access policy.")
        if key in {"keyboard_enabled", "genre_tagging"} and type(value) is not bool:
            raise ValueError("Invalid toggle.")
        if key not in {"keyboard_enabled", "genre_tagging", "random_policy", "tracking_policy"}:
            raise ValueError("Unknown setting.")
        self.mutate(lambda state: state.update({key: value}))

    def text(self, key, **variables):
        text = self.snapshot()["messages"][key]
        for name, value in variables.items():
            text = text.replace("{" + name + "}", str(value))
        if key == "restricted":
            from vip_access import vip_restriction_text
            text = vip_restriction_text(text)
        return text

    def allowed(self, policy, uid):
        return policy != "off" and (policy == "public" or self.vip.is_vip(uid))

    def subscribe(self, uid, key, enabled=True):
        def change(state):
            entries = state["subscriptions"].setdefault(str(uid), [])
            if enabled and key not in entries:
                entries.append(key)
            elif not enabled and key in entries:
                entries.remove(key)
        self.mutate(change)


def keyboard_for_user(uid, standard_settings, vip_store=None):
    from reply_keyboard import build_reply_keyboard
    store = ExperienceStore(vip_store)
    state = store.snapshot()
    if not store.vip.is_member(uid) or not state["keyboard_enabled"]:
        settings = copy.deepcopy(standard_settings)
        if not store.vip.is_member(uid):
            settings["buttons"] = [
                item for item in settings["buttons"]
                if not item.get("vip_only") and item["text"] not in {
                    "🎲 מה לראות היום (VIP)", TRACK_LABEL, BENEFITS_LABEL,
                    WATCH_LABEL,
                }
            ]
        return build_reply_keyboard(settings)
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, is_persistent=False, row_width=1)
    keyboard.row("🎲 מה לראות היום (VIP)")
    keyboard.row(TRACK_LABEL)
    keyboard.row(BENEFITS_LABEL)
    keyboard.row("📌 המועדפים שלי (Watchlist)")
    return keyboard


def refresh_revoked_keyboard(bot, uid, vip_store):
    from reply_keyboard import load_settings, build_reply_keyboard, remove_reply_keyboard
    try:
        # Admin editors are menu facades, not user-message transports.
        # ReplyKeyboardRemove has no InlineKeyboardMarkup.to_dict().
        getattr(bot, "raw", bot).send_message(
            uid, ExperienceStore(vip_store).text("revoked"),
            reply_markup=keyboard_for_user(uid, load_settings(), vip_store) or remove_reply_keyboard(),
        )
    except Exception:
        logging.exception("Could not refresh revoked VIP user's keyboard (user_id=%s).", uid)


class VIPRestrictionGuard(BaseMiddleware):
    """Run before authorization middleware, without changing regular-user handlers."""
    update_sensitive = True
    update_types = ["message", "callback_query"]

    def __init__(self, bot, engine_getter, vip_store=None):
        super().__init__()
        self.bot, self.engine_getter = bot, engine_getter
        self.vip = vip_store or VIPSettingsStore()

    def _blocked(self, user):
        if user is None or is_admin(user.id):
            return False
        blocked = any(self.engine_getter().state(user.id))
        # Read moderation first. Non-VIP users continue through their existing
        # restriction handlers; VIP membership can never override this result.
        return blocked and self.vip.is_member(user.id)

    def pre_process_message(self, message, data):
        if self._blocked(getattr(message, "from_user", None)):
            return CancelUpdate()

    def pre_process_callback_query(self, call, data):
        if self._blocked(call.from_user):
            self.bot.answer_callback_query(
                call.id, text=ExperienceStore(self.vip).text("moderated")[:200], show_alert=True,
            )
            return CancelUpdate()

    def post_process_message(self, message, data, exception): pass
    def post_process_callback_query(self, call, data, exception): pass


class VIPExperience:
    def __init__(self, bot, standard, exclusive, vip_store, engine):
        self.bot = bot
        self.catalogues = {"standard": standard, "vip": exclusive}
        self.store = ExperienceStore(vip_store)
        self.engine = engine
        self._stop = threading.Event()
        self._worker = None

    def restricted(self, uid):
        return not is_admin(uid) and any(self.engine.state(uid))

    def items(self, uid):
        values = []
        for tier, catalogue in self.catalogues.items():
            for item in catalogue.repository.list_media():
                if self.item_allowed(uid, tier, item):
                    values.append((tier, item))
        return values

    def item_allowed(self, uid, tier, item):
        from media_storage import media_access_level
        return (
            media_access_level(item, legacy_vip=tier == "vip") == "regular"
            or self.store.vip.is_vip(uid)
        )

    def key(self, tier, item):
        return tier + ":" + media_token(item["command_name"])

    def resolve(self, key):
        tier, _, token = key.partition(":")
        catalogue = self.catalogues.get(tier)
        return (catalogue, catalogue.repository.get_by_token(token)) if catalogue else (None, None)

    def register_handlers(self):
        self.bot.message_handler(
            func=self.matches, content_types=["text"],
        )(self.handle_message)
        self.bot.callback_query_handler(
            func=lambda c: bool(c.data) and c.data.startswith("vx:")
        )(self.handle_callback)

    def matches(self, message):
        text = (getattr(message, "text", None) or "").strip()
        user = getattr(message, "from_user", None)
        if not text or not user or message.chat.type != "private":
            return False
        if getattr(self, "live_chat", None) and self.live_chat.is_active_user(user.id):
            return False
        if is_admin(user.id):
            registry = getattr(self, "registry", None)
            if registry and registry.get(user.id).pending.get(user.id):
                return False
            if any(catalogue.has_pending_admin_input(message) for catalogue in self.catalogues.values()):
                return False
        return text in RANDOM_LABELS | {TRACK_LABEL, BENEFITS_LABEL, WATCH_LABEL} or text.split(maxsplit=1)[0].split("@")[0] == "/random"

    def handle_message(self, message):
        uid = message.from_user.id
        if self.restricted(uid):
            return
        text = (message.text or "").strip()
        if text == "🎲 מה לראות היום (VIP)" and not self.store.vip.is_vip(uid):
            self.bot.send_message(uid, self.store.text("restricted"))
            return
        if text == WATCH_LABEL:
            self.show_favorites(uid)
        elif text == TRACK_LABEL:
            self.show_subscriptions(uid)
        elif text == BENEFITS_LABEL:
            self.bot.send_message(uid, self.store.text("benefits" if self.store.vip.is_vip(uid) else "restricted"))
        else:
            args = text.split(maxsplit=1)
            genre = args[1] if args[0].split("@")[0] == "/random" and len(args) > 1 else None
            policy = self.store.snapshot()["random_policy"]
            if genre is not None:
                if not self.store.allowed(policy, uid):
                    self.bot.send_message(uid, self.store.text("disabled" if policy == "off" else "restricted"))
                    return
                matched = next((g for g in GENRES if has_genre(g, genre)), None)
                if matched is None:
                    matched = next((
                        g for _, item in self.items(uid)
                        for g in genre_list(item.get("genre")) if has_genre(g, genre)
                    ), None)
                if matched is None:
                    self.bot.send_message(uid, self.store.text("empty"))
                    return
                self.random_item(uid, matched)
            elif self.store.allowed(policy, uid):
                keyboard = InlineKeyboardMarkup(row_width=2)
                keyboard.row(InlineKeyboardButton("🎲 הכל", callback_data="vx:random:all"))
                for index in range(0, len(GENRES), 2):
                    keyboard.row(*[
                        InlineKeyboardButton(GENRES[i], callback_data=f"vx:random:{i}")
                        for i in range(index, min(index + 2, len(GENRES)))
                    ])
                self.bot.send_message(uid, self.store.text("choose_genre"), reply_markup=keyboard)
            else:
                # No filter menu for ordinary users; never include VIP-exclusive media.
                self.random_item(uid)

    def random_item(self, uid, genre=None):
        candidates = [(tier, item) for tier, item in self.items(uid)
                      if genre is None or has_genre(item.get("genre"), genre)]
        if not candidates:
            self.bot.send_message(uid, self.store.text("empty"))
            return
        tier, item = random.choice(candidates)
        self.catalogues[tier]._show_delivery_options(uid, item)

    def follow_markup(self, uid, tier, item, keyboard):
        policy = self.store.snapshot()["tracking_policy"]
        if item["type"] == "series" and self.store.allowed(policy, uid):
            keyboard.row(InlineKeyboardButton("🔔 עקוב אחרי סדרה", callback_data="vx:follow:" + self.key(tier, item)))
        if self.store.vip.is_vip(uid):
            key = self.key(tier, item)
            saved = key in self.store.snapshot()["favorites"].get(str(uid), [])
            keyboard.row(InlineKeyboardButton(
                "❌ הסר מהמועדפים" if saved else "📌 שמור במועדפים",
                callback_data=("vx:unsave:" if saved else "vx:save:") + key,
            ))
        return keyboard

    def show_favorites(self, uid, page=0):
        if not self.store.vip.is_vip(uid):
            self.bot.send_message(uid, self.store.text("restricted"))
            return
        entries = []
        for key in self.store.snapshot()["favorites"].get(str(uid), []):
            catalogue, item = self.resolve(key)
            if item and self.item_allowed(uid, key.partition(":")[0], item):
                entries.append((key, catalogue, item))
        page = max(0, min(int(page), max(0, (len(entries) - 1)//10)))
        markup = InlineKeyboardMarkup(row_width=1)
        for key, catalogue, item in entries[page*10:(page+1)*10]:
            markup.row(InlineKeyboardButton(item["title"][:64], callback_data=catalogue._user_callback("item", media_token(item["command_name"]))))
            markup.row(InlineKeyboardButton("❌ הסר מהמועדפים", callback_data="vx:unsave:" + key))
        if page:
            markup.row(InlineKeyboardButton("⬅️ הקודם", callback_data=f"vx:favorites:{page-1}"))
        if (page+1)*10 < len(entries):
            markup.row(InlineKeyboardButton("➡️ הבא", callback_data=f"vx:favorites:{page+1}"))
        self.bot.send_message(uid, WATCH_LABEL + ("" if entries else "\nהרשימה ריקה."),
                              reply_markup=markup if markup.keyboard else None)

    def show_subscriptions(self, uid, page=0):
        state = self.store.snapshot()
        if not self.store.allowed(state["tracking_policy"], uid):
            self.bot.send_message(uid, self.store.text("disabled" if state["tracking_policy"] == "off" else "restricted"))
            return
        entries = []
        for key in state["subscriptions"].get(str(uid), []):
            catalogue, item = self.resolve(key)
            if item and self.item_allowed(uid, key.partition(":")[0], item):
                entries.append((key, item))
        page = max(0, min(page, max(0, (len(entries)-1)//10)))
        keyboard = InlineKeyboardMarkup(row_width=1)
        for key, item in entries[page*10:(page+1)*10]:
            keyboard.row(InlineKeyboardButton("❌ הסר מעקב: " + item["title"][:35], callback_data="vx:unfollow:" + key))
        if page:
            keyboard.row(InlineKeyboardButton("⬅️ הקודם", callback_data=f"vx:list:{page-1}"))
        if (page+1)*10 < len(entries):
            keyboard.row(InlineKeyboardButton("➡️ הבא", callback_data=f"vx:list:{page+1}"))
        self.bot.send_message(uid, self.store.text("tracking_title") + "\n" + "\n".join(
            "• " + item["title"] for _, item in entries[page*10:(page+1)*10]
        ), reply_markup=keyboard if keyboard.keyboard else None)

    def handle_callback(self, call):
        # Acknowledge immediately; re-check membership/policy even on old buttons.
        self.bot.answer_callback_query(call.id)
        uid = call.from_user.id
        if (
            self.restricted(uid) or call.message is None or call.message.chat.type != "private"
            or call.message.chat.id != uid
        ):
            return
        try:
            _, action, value = call.data.split(":", 2)
            state = self.store.snapshot()
            if action in {"save", "unsave", "favorites", "trailer"}:
                if not self.store.vip.is_vip(uid):
                    self.bot.send_message(uid, self.store.text("restricted"))
                    return
                if action == "favorites":
                    self.show_favorites(uid, int(value))
                    return
                catalogue, item = self.resolve(value)
                if not item or not self.item_allowed(uid, value.partition(":")[0], item):
                    self.bot.send_message(uid, self.store.text("empty"))
                    return
                if action == "trailer":
                    from management_config import is_link_input
                    if item.get("trailer_url") and is_link_input(item["trailer_url"]):
                        self.bot.send_message(uid, item["trailer_url"])
                    else:
                        self.bot.send_message(uid, "טריילר אינו זמין כרגע.")
                    return
                def change(current):
                    entries = current["favorites"].setdefault(str(uid), [])
                    if action == "save" and value not in entries:
                        entries.append(value)
                    elif action == "unsave" and value in entries:
                        entries.remove(value)
                self.store.mutate(change)
                self.show_favorites(uid)
                return
            policy = state["random_policy"] if action == "random" else state["tracking_policy"]
            if not self.store.allowed(policy, uid):
                self.bot.send_message(uid, self.store.text("disabled" if policy == "off" else "restricted"))
                return
            if action == "random":
                if value != "all" and not 0 <= int(value) < len(GENRES):
                    return
                self.random_item(uid, None if value == "all" else GENRES[int(value)])
            elif action == "list":
                self.show_subscriptions(uid, int(value))
            elif action in {"follow", "unfollow"}:
                catalogue, item = self.resolve(value)
                if not item or item["type"] != "series":
                    self.bot.send_message(uid, self.store.text("empty"))
                    return
                if not self.item_allowed(uid, value.partition(":")[0], item):
                    self.bot.send_message(uid, self.store.text("restricted"))
                    return
                self.store.subscribe(uid, value, enabled=action == "follow")
                self.bot.send_message(uid, self.store.text("following" if action == "follow" else "unfollowed"))
                if action == "unfollow":
                    self.show_subscriptions(uid)
        except Exception:
            logging.exception("VIP experience callback failed.")

    def content_updated(self, tier, before, after):
        if after["type"] != "series":
            return
        previous_ids = {entry["file_id"] for entry in (before or {}).get("telegram_files", [])}
        new_ids = {entry["file_id"] for entry in after["telegram_files"]} - previous_ids
        if not before or not new_ids:
            return
        key = self.key(tier, after)
        def enqueue(state):
            for uid, entries in state["subscriptions"].items():
                if key in entries:
                    event = f"{uid}:{key}:{media_token('|'.join(sorted(new_ids)))}"
                    state["pending_notifications"].setdefault(event, {
                        "uid": int(uid), "key": key, "title": after["title"],
                        "command": after["command_name"],
                    })
        self.store.mutate(enqueue)

    def rename_series(self, tier, old_name, item):
        old_key = tier + ":" + media_token(old_name)
        new_key = self.key(tier, item)
        if old_key == new_key:
            return
        def move(state):
            for uid, entries in state["favorites"].items():
                state["favorites"][uid] = list(dict.fromkeys(
                    new_key if key == old_key else key for key in entries
                ))
            for uid, entries in state["subscriptions"].items():
                state["subscriptions"][uid] = list(dict.fromkeys(
                    new_key if key == old_key else key for key in entries
                ))
            for record in state["pending_notifications"].values():
                if record["key"] == old_key:
                    record.update(key=new_key, title=item["title"], command=item["command_name"])
        self.store.mutate(move)

    def deliver_pending(self):
        for event, record in self.store.snapshot()["pending_notifications"].items():
            if self._stop.is_set():
                return
            uid = record["uid"]
            state = self.store.snapshot()
            catalogue, item = self.resolve(record["key"])
            entitled = (
                self.store.allowed(state["tracking_policy"], uid)
                and record["key"] in state["subscriptions"].get(str(uid), [])
                and item is not None
                and self.item_allowed(uid, record["key"].partition(":")[0], item)
            )
            if entitled and self.restricted(uid):
                continue
            if entitled:
                try:
                    self.bot.send_message(uid, self.store.text(
                        "episode", CONTENT_NAME=record["title"], COMMAND_NAME=record["command"],
                    ))
                except Exception:
                    logging.exception("Episode notification failed (user_id=%s); retained for retry.", uid)
                    continue
                self._stop.wait(.05)
            self.store.mutate(lambda current: current["pending_notifications"].pop(event, None))

    def start(self):
        def worker():
            while not self._stop.is_set():
                try:
                    self.deliver_pending()
                except Exception:
                    logging.exception("Series notification worker failed.")
                self._stop.wait(30)
        self._worker = threading.Thread(target=worker, name="series-notifications", daemon=True)
        self._worker.start()

    def stop(self):
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)
