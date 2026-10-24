"""Private /addcmd confirmation and durable, actionable broken-link reports."""

import html
import json
import logging
import secrets
import threading
import time
from functools import wraps


def private_command_feedback(handler):
    @wraps(handler)
    def wrapped(self, message, data):
        from admin_edit_flow import track_edit
        actor = getattr(getattr(message, "from_user", None), "id", None)
        base = (message.text or "").split(maxsplit=1)[0].split("@")[0] if message.text else ""
        if actor and message.chat.id == actor and is_admin(actor) and base in {"/addcmd", "/removeadmin"}:
            dashboard = self.registry.get(actor)
            dashboard.cancel_pending(actor)
            if base == "/addcmd":
                dashboard.bot.state.setdefault("navigation", {})[str(actor)] = {
                    "current": "adm:text_menu", "parents": {"adm:text_menu": "adm:menu"},
                    "back": "adm:text_menu",
                }
            elif str(actor) not in dashboard.bot.state["menus"]:
                dashboard.send_menu(actor)
            with dashboard.bot.feedback(message=message):
                with track_edit(dashboard, message=message):
                    return handler(self, message, data)
        return handler(self, message, data)
    return wrapped
from pathlib import Path

from telebot.handler_backends import BaseMiddleware, CancelUpdate
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_roles import OWNER_ID, change_admin, is_owner
from bot_storage import add_command, is_admin, load_bot_data
from command_archive import archive_command, capture_source
from management_config import save_json, load_config
from support_storage import load_support_settings

REPORTS_FILE = Path(__file__).resolve().with_name("reports.json")
DRAFTS_FILE = Path(__file__).resolve().with_name("command_drafts.json")
SUCCESS_ALERT = "✅ הדיווח נשלח בהצלחה למנהל!"
_LOCK = threading.RLock()


def load_reports(path=REPORTS_FILE):
    from persistent_json import read_json_text
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("reports"), dict) or not isinstance(data.get("cooldowns"), dict):
        raise ValueError("Invalid broken-link reports.")
    for rid, record in data["reports"].items():
        if (
            not isinstance(rid, str) or not isinstance(record, dict)
            or type(record.get("resolved")) is not bool
            or type(record.get("reporter_id")) is not int or record["reporter_id"] <= 0
            or type(record.get("chat_id")) is not int or record["chat_id"] >= 0
            or type(record.get("message_id")) is not int or record["message_id"] <= 0
            or not isinstance(record.get("keyword"), str)
        ):
            raise ValueError("Invalid broken-link report record.")
    if any(not isinstance(value, (int, float)) for value in data["cooldowns"].values()):
        raise ValueError("Invalid report cooldown.")
    return data


def load_drafts(path=DRAFTS_FILE):
    from persistent_json import read_json_text
    data = json.loads(read_json_text(path))
    if not isinstance(data, dict) or not isinstance(data.get("drafts"), dict):
        raise ValueError("Invalid command drafts.")
    for nonce, record in data["drafts"].items():
        if not isinstance(record, dict) or type(record.get("admin_id")) is not int or not isinstance(record.get("keyword"), str) or not isinstance(record.get("text"), str):
            raise ValueError("Invalid command draft.")
    return data


class CommandReports(BaseMiddleware):
    COMMANDS = frozenset({"/addcmd", "/removeadmin"})

    def __init__(self, bot, registry, commands_file, support_file,
                 reports_file=REPORTS_FILE, drafts_file=DRAFTS_FILE):
        super().__init__()
        self.bot, self.registry = bot, registry
        self.commands_file, self.support_file = commands_file, support_file
        self.reports_file, self.drafts_file = reports_file, drafts_file
        self.update_sensitive = True
        self.update_types = ["message"]

    def _clean_group_input(self, message):
        if message.chat.type in {"group", "supergroup"}:
            try:
                self.bot.delete_message(message.chat.id, message.message_id)
            except Exception as exc:
                logging.warning("Command input cleanup failed (%s).", type(exc).__name__)

    @private_command_feedback
    def pre_process_message(self, message, data):
        pieces = (message.text or "").strip().split(maxsplit=1)
        if not pieces:
            return
        base, _, mention = pieces[0].partition("@")
        if base not in self.COMMANDS or not is_admin(message.from_user.id):
            return
        if mention and mention.casefold() != self.bot.get_me().username.casefold():
            return CancelUpdate()
        actor = message.from_user.id
        dashboard = self.registry.get(actor)
        args = pieces[1] if len(pieces) > 1 else ""
        try:
            if base == "/removeadmin":
                if not is_owner(actor) or message.chat.type != "private":
                    raise ValueError("הסרת מנהלים בפקודה זמינה רק למנהל הראשי בפרטי.")
                if not args.isdigit():
                    raise ValueError("שלח /removeadmin USER_ID.")
                change_admin(int(args), False, dashboard.management.config_file)
                dashboard.bot.send_message(actor, "הרשאות המנהל הוסרו.")
            else:
                keyword, separator, text = args.partition("|")
                keyword, text = keyword.strip(), text.strip()
                if not separator or not 1 <= len(keyword) <= 128 or keyword.startswith("/") or not text or len(text) > 4096:
                    raise ValueError("שלח /addcmd שם הפקודה | התגובה או הקישור.")
                if any(key.casefold() == keyword.casefold() for key in load_bot_data(self.commands_file)["responses"]):
                    raise ValueError("הפקודה כבר קיימת. השתמש בתפריט עריכת פקודה.")
                dashboard.cancel_pending(actor)
                if dashboard.on_admin_navigation:
                    dashboard.on_admin_navigation(actor, actor)
                nonce = secrets.token_hex(8)
                with _LOCK:
                    drafts = load_drafts(self.drafts_file)
                    # One current draft per administrator, no accumulating abandoned drafts.
                    drafts["drafts"] = {key: value for key, value in drafts["drafts"].items() if value["admin_id"] != actor}
                    drafts["drafts"][nonce] = {"admin_id": actor, "keyword": keyword, "text": text}
                    source = capture_source(message)
                    if source and message.chat.id != actor:
                        drafts["drafts"][nonce]["command_sources"] = [source]
                    save_json(self.drafts_file, drafts)
                    from admin_edit_flow import record_draft_commit
                    record_draft_commit()
                keyboard = InlineKeyboardMarkup()
                keyboard.row(
                    InlineKeyboardButton("כן, הוסף כפתור", callback_data=f"cmd:choice:{nonce}:yes"),
                    InlineKeyboardButton("לא, ללא כפתור", callback_data=f"cmd:choice:{nonce}:no"),
                )
                keyboard.add(InlineKeyboardButton("ביטול", callback_data="adm:menu"))
                dashboard.bot.send_message(actor, f"פקודה: {keyword}\nהאם ברצונך להוסיף לתגובה זו כפתור דיווח על קישור תקול?", reply_markup=keyboard)
        except (ValueError, OSError) as exc:
            dashboard.bot.send_message(actor, str(exc))
        finally:
            self._clean_group_input(message)
        return CancelUpdate()

    def post_process_message(self, message, data, exception):
        return None

    def choice(self, call):
        if not is_admin(call.from_user.id) or call.message is None or call.message.chat.id != call.from_user.id:
            self.bot.answer_callback_query(call.id, text="הפעולה אינה מורשית.", show_alert=True)
            return
        parts = (call.data or "").split(":")
        dashboard = self.registry.get(call.from_user.id)
        try:
            if len(parts) != 4 or parts[3] not in {"yes", "no"}:
                raise ValueError("בחירה לא תקינה.")
            if not dashboard.bot.adopt_menu(call.message):
                raise ValueError("התפריט התיישן.")
            with _LOCK:
                drafts = load_drafts(self.drafts_file)
                draft = drafts["drafts"].get(parts[2])
                if not draft or draft["admin_id"] != call.from_user.id:
                    raise ValueError("הפעולה התיישנה או כבר הושלמה.")
                if not add_command(draft["keyword"], draft["text"], self.commands_file, report_enabled=parts[3] == "yes"):
                    raise ValueError("הפקודה כבר קיימת; לא בוצעה דריסה.")
                del drafts["drafts"][parts[2]]
                save_json(self.drafts_file, drafts)
            notice = archive_command(self.bot, draft["keyword"], self.commands_file,
                                     dashboard.management.config_file, call.from_user.id, draft, created=True)
            self.bot.answer_callback_query(call.id, text=notice[:200], show_alert=True)
            dashboard.send_menu(call.message.chat.id)
        except (ValueError, OSError) as exc:
            self.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)

    def _disable_report_button(self, call):
        """Remove only this report action; retain unrelated URL buttons."""
        if call.message.chat.id != call.from_user.id:
            return
        markup = getattr(call.message, "reply_markup", None)
        rows = [
            [button for button in row if button.callback_data != call.data]
            for row in markup.keyboard
        ] if markup else []
        rows = [row for row in rows if row]
        try:
            self.bot.edit_message_reply_markup(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=InlineKeyboardMarkup(rows) if rows else None,
            )
        except Exception as exc:
            # Durable per-message deduplication still prevents repeated reports.
            logging.warning("Could not remove used report button (%s).", type(exc).__name__)

    @staticmethod
    def _report_action_keyboard(engine, user_id: int, report_id: str, *, include_resolve=True):
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.row(InlineKeyboardButton(
            "💬 פתח שיחה עם המשתמש", url=f"tg://user?id={user_id}",
        ))
        keyboard.row(
            InlineKeyboardButton(
                "✅ הועלה", callback_data=f"report:status:uploaded:{report_id}",
            ),
            InlineKeyboardButton(
                "❌ לא זמין", callback_data=f"report:status:unavailable:{report_id}",
            ),
        )
        if engine:
            keyboard = engine.markup(user_id, keyboard)
        else:
            keyboard.row(
                InlineKeyboardButton("🔊 השתק משתמש", callback_data=f"mod:mute:{user_id}"),
                InlineKeyboardButton("🚫 חסום משתמש", callback_data=f"mod:ban:{user_id}"),
            )
        if include_resolve:
            keyboard.add(InlineKeyboardButton(
                "✅ סמן כטופל", callback_data=f"report:resolve:{report_id}",
            ))
        return keyboard

    def report(self, call):
        parts = (call.data or "").split(":")
        if (
            len(parts) not in {3, 4} or call.message is None
            or (len(parts) == 4 and parts[1] != "status")
        ):
            self.bot.answer_callback_query(call.id, text="דיווח לא תקין.", show_alert=True)
            return
        try:
            config_path = Path(self.commands_file).parent / "config.json"
            support_target = load_support_settings(self.support_file)["support_chat_id"]
            target = load_config(config_path)["ADMIN_CHAT_ID"] if config_path.exists() else support_target
            from admin_notifications import resolve_user_management_chat
            target = resolve_user_management_chat(
                support_target, config_file=config_path,
            )
            engine = getattr(self.bot, "restriction_service", None)
            if target is None and parts[1] != "resolve":
                raise ValueError("קבוצת הניהול טרם הוגדרה. נסה שוב מאוחר יותר.")
            with _LOCK:
                reports = load_reports(self.reports_file)
                if len(parts) == 4:
                    result, report_id = parts[2], parts[3]
                    record = reports["reports"].get(report_id)
                    if (
                        result not in {"uploaded", "unavailable"}
                        or not is_admin(call.from_user.id) or not record
                        or call.message.chat.id != record["chat_id"]
                        or call.message.message_id != record["message_id"]
                    ):
                        raise ValueError("אין הרשאה לעדכן דיווח זה.")
                    if record.get("admin_status"):
                        self.bot.answer_callback_query(
                            call.id, text="הדיווח כבר עודכן.", show_alert=True,
                        )
                        return
                    response = load_config(config_path)["missing_content_messages"][result]
                    response = response.replace("{CONTENT_NAME}", record["keyword"])
                    from bot_storage import format_telegram_text

                    response_text, parse_mode = format_telegram_text(response)
                    self.bot.send_message(
                        record["reporter_id"],
                        response_text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                    )
                    record["admin_status"] = result
                    save_json(self.reports_file, reports)
                    self.bot.answer_callback_query(call.id, text="המשתמש עודכן.")
                    return
                if parts[1] == "resolve":
                    record = reports["reports"].get(parts[2])
                    if (
                        not is_admin(call.from_user.id) or not record
                        or call.message.chat.id != record["chat_id"]
                        or record["chat_id"] not in {target, support_target}
                        or call.message.message_id != record["message_id"]
                    ):
                        raise ValueError("אין הרשאה לעדכן דיווח זה.")
                    target = record["chat_id"]
                    if not record["resolved"]:
                        body = call.message.html_text if getattr(call.message, "entities", None) else html.escape(call.message.text or "")
                        try:
                            markup = self._report_action_keyboard(
                                engine, record["reporter_id"], parts[2],
                                include_resolve=False,
                            )
                            self.bot.edit_message_text(body + "\n\n✅ טופל", chat_id=target, message_id=record["message_id"], parse_mode="HTML", reply_markup=markup)
                        except Exception as exc:
                            if "message is not modified" not in str(getattr(exc, "description", "")).lower():
                                raise
                        record["resolved"] = True
                        save_json(self.reports_file, reports)
                        if engine:
                            engine.track(target, record["message_id"], record["reporter_id"], markup)
                    self.bot.answer_callback_query(call.id, text="הדיווח סומן כטופל.")
                    return
                if parts[1] != "open":
                    raise ValueError("פעולה לא תקינה.")
                source_key = f"{call.from_user.id}:{call.message.chat.id}:{call.message.message_id}:{parts[2]}"
                if any(record.get("source_key") == source_key for record in reports["reports"].values()):
                    self._disable_report_button(call)
                    self.bot.answer_callback_query(call.id, text="הדיווח מהודעה זו כבר נשלח.")
                    return
                keyword = next((
                    key for key, value in load_bot_data(self.commands_file)["responses"].items()
                    if value.get("report_enabled") and value.get("report_id") == parts[2]
                ), None)
                if keyword is None:
                    raise ValueError("הפקודה אינה קיימת או שכפתור הדיווח בוטל.")
                cooldown = f"{call.from_user.id}:{parts[2]}"
                now = time.time()
                rid = secrets.token_hex(8)
                user = call.from_user
                name = " ".join(filter(None, [user.first_name, getattr(user, "last_name", None)]))
                from vip_access import VIPSettingsStore

                is_vip = VIPSettingsStore(
                    config_path.with_name("vip_settings.json"),
                ).is_member(user.id)
                if is_vip:
                    body = (
                        "⭐ 🚨 דיווח על תקלה בקישור דרייב/מדיה - מנוי VIP! ⭐\n"
                        f"- תוכן: {html.escape(keyword)}\n"
                        f"- משתמש: ⭐ {html.escape(name or 'ללא שם')} "
                        f"(ID: {user.id}) - סטטוס: VIP · ⭐ מנוי VIP ⭐"
                    )
                else:
                    body = (
                        "🚨 דיווח על תקלה בקישור דרייב/מדיה!\n"
                        f"- תוכן: {html.escape(keyword)}\n"
                        f"- משתמש: {html.escape(name or 'ללא שם')} (ID: {user.id})"
                    )
                from user_notification_card import user_card, broken_link_text
                from admin_notifications import resolve_user_management_chat
                target = resolve_user_management_chat(support_target, config_file=config_path)
                if target is None:
                    raise ValueError("לא הוגדרה קבוצת ניהול.")
                body = user_card(
                    user.id, name, getattr(user, "username", None),
                    broken_link_text(user.id, name, getattr(user, "username", None), keyword, is_vip),
                )
                keyboard = self._report_action_keyboard(
                    engine, user.id, rid,
                )
                try:
                    sent = self.bot.send_message(
                        target, body,
                        parse_mode="HTML", reply_markup=keyboard,
                    )
                except Exception as exc:
                    logging.error("Broken-link report delivery with buttons failed (%s); retrying without buttons.", type(exc).__name__)
                    sent = self.bot.send_message(
                        target, body,
                        parse_mode="HTML",
                    )
                reports["reports"][rid] = {
                    "keyword": keyword, "reporter_id": user.id, "chat_id": target,
                    "message_id": sent.message_id, "resolved": False, "created_at": now,
                    "source_key": source_key, "admin_status": None,
                }
                reports["cooldowns"][cooldown] = now
                try:
                    save_json(self.reports_file, reports)
                except Exception:
                    self.bot.delete_message(target, sent.message_id)
                    raise
            self._disable_report_button(call)
            self.bot.answer_callback_query(call.id, text=SUCCESS_ALERT, show_alert=False)
            engine = getattr(self.bot, "restriction_service", None)
            if engine is not None:
                engine.track(target, sent.message_id, user.id, keyboard)
        except (ValueError, OSError) as exc:
            self.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)
        except Exception as exc:
            logging.warning("Broken-link reporting failed (%s).", type(exc).__name__)
            self.bot.answer_callback_query(call.id, text="לא ניתן להעביר את הדיווח כרגע. נסה שוב מאוחר יותר.", show_alert=True)

    def register_handlers(self):
        self.bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("cmd:choice:"))(self.choice)
        self.bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("report:"))(self.report)
