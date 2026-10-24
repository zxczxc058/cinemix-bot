"""HTML-safe identity cards for user-originated management notifications."""

from html import escape


def user_card(user_id, name, username=None, content=None, *, is_vip=False):
    handle = str(username or "").lstrip("@")
    if handle in {"לא מוגדר", "ללא"}:
        handle = ""
    card = (
        f"👤 שם מלא: {escape(str(name or 'ללא שם'))}\n"
        f"שם משתמש: {escape('@' + handle if handle else 'ללא')}\n"
        f"🆔 מזהה מספרי: {int(user_id)}\n"
        "-------------------"
    )
    if is_vip:
        card = "⭐ בקשת VIP מועדפת! — ⭐ מנוי VIP ⭐\n" + card
    if content is not None:
        card += f"\n💬 תוכן ההודעה: {escape(str(content or 'קובץ מדיה'))}"
    return card


def broken_link_text(user_id, name, username, content, is_vip=False):
    handle = str(username or "").lstrip("@")
    handle = "@" + handle if handle and handle not in {"לא מוגדר", "ללא"} else "ללא"
    title = "🚨 דיווח על קישור שגוי!"
    if is_vip:
        title = f"⭐ {title} - מנוי VIP! ⭐"
    return (
        f"{title}\n"
        f"👤 דווח על ידי: {name or 'ללא שם'} ({handle} / ID: {user_id})\n"
        f"🎬 התוכן המדווח: {content}"
    )
