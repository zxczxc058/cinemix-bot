"""Private administrator input steps for bottom-keyboard buttons."""

INPUT_ACTIONS = {
    "reply_keyboard_add", "reply_keyboard_edit",
    "reply_keyboard_content", "reply_keyboard_content_edit",
}


def input_active(message, registry):
    user = getattr(message, "from_user", None)
    if user is None or message.chat.type != "private":
        return False
    dashboard = registry.get(user.id)
    if dashboard is None:
        return False
    state = dashboard.pending.get(user.id) or {}
    return state.get("action") in INPUT_ACTIONS


def handle_input(dashboard, message, state, response_text):
    from reply_keyboard import load_settings, save_settings

    action = state["action"]
    user_id, chat_id = message.from_user.id, message.chat.id
    text = (message.text or "").strip()
    settings = load_settings()
    index = state.get("index")
    if index is not None and not 0 <= index < len(settings["buttons"]):
        dashboard.cancel_pending(user_id)
        dashboard._send_reply_keyboard_menu(chat_id)
        return
    if action in {"reply_keyboard_add", "reply_keyboard_edit"}:
        if not text or len(text) > 64:
            dashboard.bot.send_message(chat_id, "שם הכפתור חייב להכיל 1–64 תווים.")
            return
        token = text.split(maxsplit=1)[0].partition("@")[0].casefold()
        if text in {"הגדרות", "משתמשים", "ביטול"} or token in {
            "/start", "/cancel", "/help", "/admin", "/users",
        }:
            dashboard.bot.send_message(chat_id, "שם זה שמור לניווט במערכת. בחר שם אחר.")
            return
        if any(
            button["text"] == text and i != index
            for i, button in enumerate(settings["buttons"])
        ):
            dashboard.bot.send_message(chat_id, "כפתור עם הטקסט הזה כבר קיים.")
            return
        if action == "reply_keyboard_add":
            if len(settings["buttons"]) >= 40:
                dashboard.bot.send_message(chat_id, "ניתן להגדיר עד 40 כפתורים.")
                return
            dashboard.pending[user_id] = {
                "action": "reply_keyboard_content", "label": text,
            }
            dashboard._send_prompt(
                chat_id, f"שם הכפתור: {text}\nשלח כעת את תוכן התגובה או הקישור של הכפתור.",
            )
            return
        button = dict(settings["buttons"][index])
        # Renaming a legacy button must not disconnect its existing command.
        if "response" not in button:
            button["trigger"] = button.get("trigger", button.get("command", button["text"]))
        button["text"] = text
        settings["buttons"][index] = button
    else:
        if not isinstance(response_text, str) or not response_text.strip() or len(response_text) > 4096:
            dashboard.bot.send_message(chat_id, "שלח תוכן או קישור באורך 1–4096 תווים.")
            return
        if action == "reply_keyboard_content":
            if any(button["text"] == state["label"] for button in settings["buttons"]):
                dashboard.bot.send_message(chat_id, "כפתור עם הטקסט הזה כבר קיים.")
                return
            settings["buttons"].append({"text": state["label"], "response": response_text})
            index = len(settings["buttons"]) - 1
        else:
            button = dict(settings["buttons"][index])
            button["response"] = response_text
            settings["buttons"][index] = button
    settings["shown_to"] = []
    try:
        save_settings(settings)
    except (OSError, ValueError) as exc:
        dashboard.bot.send_message(chat_id, f"לא ניתן לשמור את הכפתור: {exc}")
        return
    dashboard.cancel_pending(user_id)
    dashboard._send_reply_keyboard_item_menu(
        chat_id, index, notice="✅ הכפתור נשמר בהצלחה!",
    )
