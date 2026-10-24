"""Private administration of the two welcome profiles; reuse button CRUD."""

from bot_storage import is_admin
from management_config import _LOCK, load_config, save_json
from start_buttons_admin import StartButtonsAdmin
from system_messages import message_text, update_message
from welcome_profiles import PROFILES, button_key, send_welcome


def menu(d, chat_id, profile):
    d.cancel_pending(d.actor_id)
    enabled = load_config(d.management.config_file).get(
        f"start_{profile}_main_keyboard_enabled", True,
    )
    from bot_storage import get_start_reply
    inline_enabled = load_config(d.management.config_file).get(
        f"start_{profile}_inline_enabled",
        True if profile in {"before", "first"} else get_start_reply(d.commands_file).buttons_enabled,
    )
    d._send_submenu(
        chat_id, f"👋 הודעת {PROFILES[profile]}\n"
        "הטקסט והכפתורים נפרדים מהפרופיל האחר ונשמרים מייד.\n"
        + ("" if profile == "before" else "המקלדת התחתונה משתמשת בכפתורי התפריט הראשי שהוגדרו."),
        [
            ("✏️ עריכת הטקסט", f"adm:welcome_text:{profile}"),
            ("🔘 ניהול כפתורי Inline", f"adm:{profile}_inline"),
            (f"🔘 כפתורי Inline — {'מוצגים' if inline_enabled else 'מוסתרים'}", f"adm:welcome_inline:{profile}"),
            ("👁️ תצוגה מקדימה", f"adm:welcome_preview:{profile}"),
        ] + (
            [("🗑️ הסר כפתורים / ללא כפתור", "adm:welcome_no_buttons:first")]
            if profile == "first" else []
        ) + ([] if profile == "before" else [
            (f"🎹 מקלדת ראשית — {'מוצגת' if enabled else 'מוסתרת'}", f"adm:welcome_keyboard:{profile}"),
            ("🎹 עריכת כפתורי המקלדת הראשית", "adm:reply_keyboard_menu"),
        ]) + (
            [("🔄 עריכת הודעה למשתמש מאומת חוזר", "adm:welcome_profile:returning")]
            if profile == "first" else []
        ), back_action="adm:welcome_profile:first" if profile == "returning" else "adm:start_menu",
    )


def handle_callback(d, action, call):
    if not (action.startswith("welcome_") or action.startswith(("before_inline", "first_inline", "returning_inline"))):
        return False
    if call.message.chat.type != "private" or not is_admin(call.from_user.id):
        return True
    for profile in PROFILES:
        if action.startswith(f"{profile}_inline"):
            return StartButtonsAdmin(d, profile).handle_callback(action, call)
    parts = call.data.split(":")
    profile = parts[2] if len(parts) == 3 else None
    if profile not in PROFILES:
        d._send_start_menu(call.message.chat.id)
        return True
    chat_id = call.message.chat.id
    d.cancel_pending(call.from_user.id)
    if d.on_admin_navigation:
        d.on_admin_navigation(call.from_user.id, chat_id)
    if action == "welcome_profile":
        menu(d, chat_id, profile)
    elif action == "welcome_no_buttons" and profile == "first":
        with _LOCK:
            config = load_config(d.management.config_file)
            config[button_key("first")] = []
            config["start_first_inline_enabled"] = False
            save_json(d.management.config_file, config)
        menu(d, chat_id, profile)
    elif action == "welcome_text":
        d.pending_chat_id = chat_id
        d.pending[call.from_user.id] = {
            "action": "welcome_profile_text", "profile": profile,
            "return_to": f"adm:welcome_profile:{profile}",
        }
        key = "access_prompt" if profile == "before" else f"{profile}_welcome"
        current = message_text(key, d.management.config_file, d.commands_file)
        d._send_prompt(chat_id, f"נוסח נוכחי:\n{current}\n\nשלח את הטקסט החדש (HTML/Markdown), או ביטול.")
    elif action in {"welcome_keyboard", "welcome_inline"}:
        from bot_storage import get_start_reply
        key = f"start_{profile}_" + ("main_keyboard_enabled" if action == "welcome_keyboard" else "inline_enabled")
        default = True if action == "welcome_keyboard" or profile in {"before", "first"} else get_start_reply(d.commands_file).buttons_enabled
        with _LOCK:
            config = load_config(d.management.config_file)
            config[key] = not config.get(key, default)
            save_json(d.management.config_file, config)
        menu(d, chat_id, profile)
    elif action == "welcome_preview":
        # Preview rendering must not mark the administrator as a welcomed user.
        send_welcome(d.raw_bot, chat_id, profile, d.management.config_file,
                     d.commands_file, preview=True)
        menu(d, chat_id, profile)
    return True


def handle_input(d, action, state, message, text, *, button_text=None):
    if action == "start_inline_input" and state.get("welcome_profile") in PROFILES:
        return StartButtonsAdmin(d, state["welcome_profile"]).handle_input(
            action, state, message, button_text if button_text is not None else message.text,
        )
    if action != "welcome_profile_text":
        return False
    if message.chat.type != "private" or not is_admin(message.from_user.id):
        return True
    profile = state["profile"]
    try:
        key = "access_prompt" if profile == "before" else f"{profile}_welcome"
        update_message(key, text, d.management.config_file, d.commands_file)
        menu(d, message.chat.id, profile)
    except ValueError as exc:
        d._send_prompt(message.chat.id, str(exc))
    return True
