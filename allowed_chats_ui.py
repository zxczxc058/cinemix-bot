"""Single-column, private administrator chat approval menus."""

import html
from telebot import types

from allowed_chats import AllowedChats
from management_config import load_config


def service_for(dashboard):
    service = getattr(dashboard.raw_bot, "chat_allowlist", None)
    return service if isinstance(service, AllowedChats) else None


def show_list(dashboard, page=0):
    service = service_for(dashboard)
    config = load_config(service.config_file)
    records = [(int(key), record) for key, record in config["managed_chats"].items()
               if record["present"]]
    records.sort(key=lambda item: (item[1]["title"], item[0]))
    size = 8
    page = max(0, min(page, max(0, (len(records) - 1) // size)))
    rows = []
    for cid, record in records[page * size:(page + 1) * size]:
        status = "🟢 פעיל" if cid in config["allowed_chats"] else "🔴 מוקפא"
        rows.append([types.InlineKeyboardButton(f"{status} — {record['title'][:40]}",
                     callback_data=f"adm:chat_detail:{cid}:{page}")])
    if page:
        rows.append([types.InlineKeyboardButton("הקודם", callback_data=f"adm:chat_list:{page - 1}")])
    if (page + 1) * size < len(records):
        rows.append([types.InlineKeyboardButton("הבא", callback_data=f"adm:chat_list:{page + 1}")])
    rows.extend([
        [types.InlineKeyboardButton("🔙 חזרה לניהול קבוצות", callback_data="adm:group_menu")],
    ])
    text = ("🛡️ ניהול קבוצות וערוצים\n\n"
            "בחרו צ׳אט לשינוי הסטטוס או להסרת הבוט.\n"
            "🔴 מוקפא — הבוט לא מגיב לפקודות ולא מבצע ניהול.\n"
            "🟢 פעיל — הצ׳אט מאושר להפעלת הבוט.\n\n"
            f"צ׳אטים רשומים שהבוט נמצא בהם: {len(records)}")
    if not records:
        text += "\nאין כרגע קבוצות או ערוצים רשומים. הוסיפו את הבוט כדי להציג בקשת אישור."
    dashboard.bot.send_message(dashboard.actor_id, text, reply_markup=types.InlineKeyboardMarkup(rows))


def show_detail(dashboard, chat_id, page):
    service = service_for(dashboard)
    config = load_config(service.config_file)
    record = config["managed_chats"].get(str(chat_id))
    if not record or not record["present"]:
        raise ValueError("הבוט כבר אינו נמצא בצ׳אט הזה.")
    active = chat_id in config["allowed_chats"]
    rows = [
        [types.InlineKeyboardButton("🔴 הקפאת פעילות" if active else "✅ אישור הפעלה",
                                   callback_data=f"adm:chat_toggle:{chat_id}:{page}")],
        [types.InlineKeyboardButton("🚪 הסרת הבוט מהצ׳אט", callback_data=f"adm:chat_leave:{chat_id}:{page}")],
        [types.InlineKeyboardButton("🔙 חזרה", callback_data=f"adm:chat_list:{page}")],
    ]
    dashboard.bot.send_message(
        dashboard.actor_id, f"🛡️ {html.escape(record['title'])}\n"
        f"מזהה: <code>{chat_id}</code>\nסוג: {record['type']}\n"
        f"מצב: {'🟢 פעיל' if active else '🔴 מוקפא'}",
        parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup(rows))


def handle(dashboard, action, call):
    if action not in {"chat_list", "chat_detail", "chat_toggle", "chat_leave", "chat_leave_confirm"}:
        return False
    service = service_for(dashboard)
    if service is None or call.message.chat.type != "private" or call.message.chat.id != call.from_user.id:
        dashboard.bot.answer_callback_query(call.id, text="פעולה זו זמינה בתפריט הפרטי בלבד.", show_alert=True)
        return True
    parts = call.data.split(":")
    try:
        if action == "chat_list":
            show_list(dashboard, int(parts[2]))
        else:
            cid, page = int(parts[2]), int(parts[3])
            record = load_config(service.config_file)["managed_chats"].get(str(cid))
            if not record or not record["present"]:
                raise ValueError("הצ׳אט אינו רשום או שהבוט יצא ממנו.")
            if action == "chat_detail":
                show_detail(dashboard, cid, page)
            elif action == "chat_toggle":
                active = not service.is_active(cid)
                title = service.set_active(cid, active)
                show_detail(dashboard, cid, page)
                dashboard.bot.answer_callback_query(call.id, show_alert=True,
                    text=(f"✅ הקבוצה {title} הופעלה בהצלחה!" if active
                          else f"🔴 פעילות הבוט בצ׳אט {title} הוקפאה.")[:200])
            elif action == "chat_leave":
                dashboard.bot.send_message(dashboard.actor_id,
                    f"האם להסיר את הבוט מהצ׳אט {html.escape(record['title'])}?\n"
                    "הבוט ייצא מהצ׳אט. כדי להחזירו יהיה צורך להוסיף ולאשר אותו מחדש.",
                    parse_mode="HTML", reply_markup=types.InlineKeyboardMarkup([
                        [types.InlineKeyboardButton("🚪 אישור יציאה", callback_data=f"adm:chat_leave_confirm:{cid}:{page}")],
                        [types.InlineKeyboardButton("❌ ביטול", callback_data=f"adm:chat_detail:{cid}:{page}")],
                    ]))
            else:
                title = service.leave(cid)
                show_list(dashboard, page)
                dashboard.bot.answer_callback_query(call.id, text=f"✅ הבוט יצא מהצ׳אט {title}."[:200], show_alert=True)
    except (ValueError, IndexError) as exc:
        dashboard.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)
    except Exception:
        import logging
        logging.exception("Private chat approval action failed.")
        dashboard.bot.answer_callback_query(call.id, text="הפעולה נכשלה. הצ׳אט נשאר מוגן; בדקו הרשאות ונסו שוב.", show_alert=True)
    return True
