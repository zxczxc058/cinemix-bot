"""Private admin controls for the shared, persisted group blacklist."""

from group_settings import (
    add_blacklist_entry, blacklist_key, load_group_settings, remove_blacklist_entry,
)

PAGE_SIZE = 20


def show_menu(dashboard, chat_id, page=0):
    entries = load_group_settings(dashboard.group_settings_file)["blacklist"]
    last_page = max(0, (len(entries) - 1) // PAGE_SIZE)
    page = min(max(0, page), last_page)
    buttons = [("➕ הוספת מילה / ביטוי / קישור", "adm:group_blacklist_add")]
    for entry in entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        buttons.append((
            f"🗑️ הסרה: {entry[:40]}",
            f"adm:group_blacklist_remove:{blacklist_key(entry)}:{page}",
        ))
    if page:
        buttons.append(("⬅️ עמוד קודם", f"adm:group_blacklist:{page - 1}"))
    if page < last_page:
        buttons.append(("➡️ עמוד הבא", f"adm:group_blacklist:{page + 1}"))
    dashboard._send_submenu(
        chat_id,
        f"⛔ ניהול רשימה שחורה\n\nפריטים שמורים: {len(entries)}\n"
        f"עמוד {page + 1}/{last_page + 1}\n"
        "ההגנה חלה בכל הקבוצות הפעילות, גם כשהגנת הקישורים כבויה.\n"
        "התאמה לפי הכלת הטקסט, ללא הבחנה בין אותיות גדולות וקטנות.\n"
        "מנהלי קבוצות ומנהלי הבוט פטורים. להסרת פריט לחץ עליו.",
        buttons, back_action="adm:group_settings",
    )


def handle_callback(dashboard, call, action, parts):
    if call.message.chat.type != "private":
        dashboard.bot.answer_callback_query(
            call.id, text="ניהול הרשימה השחורה זמין בפרטי בלבד.", show_alert=True,
        )
        return
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    if action == "group_blacklist_add":
        dashboard.pending[user_id] = {"action": "group_blacklist_add"}
        dashboard._send_prompt(
            chat_id, "שלח מילה, ביטוי או קישור אחד להוספה לרשימה השחורה.\n"
            "לביטול שלח „ביטול”.",
        )
    elif action == "group_blacklist_remove" and len(parts) == 4:
        remove_blacklist_entry(parts[2], dashboard.group_settings_file)
        show_menu(dashboard, chat_id, int(parts[3]) if parts[3].isdigit() else 0)
    else:
        show_menu(dashboard, chat_id, int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0)


def handle_input(dashboard, message, text):
    try:
        add_blacklist_entry(text, dashboard.group_settings_file)
    except (ValueError, OSError) as exc:
        dashboard._send_prompt(message.chat.id, f"לא ניתן להוסיף את הפריט: {exc}")
        return
    dashboard.cancel_pending(message.from_user.id)
    show_menu(dashboard, message.chat.id)
