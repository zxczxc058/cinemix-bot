"""Admin guide, private to-do UI, backups, and the interactive broadcast wizard."""

import copy
import logging
import secrets
import threading
from collections import deque
from pathlib import Path

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_guide import build_admin_commands_help, build_guide_pages
from admin_menus import BROADCAST_MENU, build_menu
from admin_tasks import add_task, load_tasks, update_task
from backup_service import BackupService, load_backup_state
from broadcasts import broadcast_markup, send_broadcast, send_payload
from broadcast_presets import (
    DEFAULT_DONATION_PRESET,
    load_donation_preset,
    payload_from_message,
    save_donation_preset,
    validate_payload,
)
from bot_storage import ADMIN_USER_ID, is_admin
from group_management import delete_later
from management_config import DEFAULT_CONFIG, is_link_input, load_config, save_json
from moderation import load_moderation_routes
from private_admin_protection import send_broadcast_completion


class ManagementAdmin:
    def __init__(self, dashboard):
        self.d = dashboard
        root = self.d.commands_file.parent
        self.config_file = root / "config.json"
        self.tasks_file = root / "tasks.json"
        self.donation_preset_file = root / "donation_broadcast.json"
        defaults = {
            "config.json": DEFAULT_CONFIG, "tasks.json": {"tasks": []},
            "donation_broadcast.json": DEFAULT_DONATION_PRESET,
            "backup_state.json": {"last_success_week": None, "active": None},
            "moderation_routes.json": {"messages": {}},
        }
        for name, data in defaults.items():
            if not (root / name).exists():
                save_json(root / name, copy.deepcopy(data))
        load_config(self.config_file)
        load_tasks(self.tasks_file)
        load_backup_state(root / "backup_state.json")
        load_moderation_routes(root / "moderation_routes.json")
        self.backup = BackupService(self.d.raw_bot, root, self.d.support_admin.path)
        self._blast_lock = threading.Lock()
        self._consumed_broadcasts = deque(maxlen=1024)
        from admin_accounts import AdminAccounts
        self.accounts = AdminAccounts(self.d)

    def _markup(self, rows):
        markup = InlineKeyboardMarkup(row_width=1)
        for label, action in rows:
            markup.add(InlineKeyboardButton(label, callback_data=action))
        return markup

    def _pending(self, chat_id, action, **extra):
        self.d.pending[ADMIN_USER_ID] = {"action": action, **extra}
        self.d.pending_chat_id = chat_id

    def _private(self, chat_id):
        if chat_id < 0:
            self.d.bot.send_message(chat_id, "מדריך ומשימות אישיות זמינים רק בפרטי עם הבוט.")
            return False
        return True

    def _guide(self, chat_id, page):
        if not self._private(chat_id):
            return
        self.d.cancel_pending(ADMIN_USER_ID)
        pages = build_guide_pages()
        page = max(0, min(page, len(pages) - 1))
        rows = []
        if page:
            rows.append(("⬅️ לעמוד הקודם", f"adm:guide:{page - 1}"))
        if page < len(pages) - 1:
            rows.append(("➡️ לעמוד הבא", f"adm:guide:{page + 1}"))
        rows.append(("🔙 חזרה לתפריט ראשי", "adm:menu"))
        self.d.bot.send_message(chat_id, pages[page], parse_mode="HTML", reply_markup=self._markup(rows))

    def _tasks_menu(self, chat_id):
        if not self._private(chat_id):
            return
        self.d.cancel_pending(ADMIN_USER_ID)
        self.d.bot.send_message(chat_id, "📝 משימות ופתקים", reply_markup=self._markup([
            ("📋 צפייה במשימות פתוחות", "adm:todo_list:0"),
            ("➕ הוספת משימה חדשה", "adm:todo_add"),
            ("🔙 חזרה לתפריט ראשי", "adm:menu"),
        ]))

    def _tasks_list(self, chat_id, page=0):
        if not self._private(chat_id):
            return
        self.d.cancel_pending(ADMIN_USER_ID)
        tasks = load_tasks(self.tasks_file)["tasks"]
        opened = [task for task in tasks if not task["done"]]
        pages = max(1, (len(opened) + 5) // 6)
        page = max(0, min(page, pages - 1))
        selected = opened[page * 6:page * 6 + 6]
        body = f"📝 משימות פתוחות: {len(opened)} | בוצעו: {len(tasks) - len(opened)}\n"
        markup = InlineKeyboardMarkup()
        for index, task in enumerate(selected, start=page * 6 + 1):
            body += f"\n{index}. {task['text']}\n"
            markup.row(
                InlineKeyboardButton(f"✅ סמן {index} כבוצע", callback_data=f"adm:todo_done:{task['id']}"),
                InlineKeyboardButton(f"❌ מחק {index}", callback_data=f"adm:todo_delete:{task['id']}"),
            )
        if not selected:
            body += "\nאין משימות פתוחות."
        if page:
            markup.add(InlineKeyboardButton("⬅️ הקודם", callback_data=f"adm:todo_list:{page - 1}"))
        if page + 1 < pages:
            markup.add(InlineKeyboardButton("➡️ הבא", callback_data=f"adm:todo_list:{page + 1}"))
        markup.add(InlineKeyboardButton("➕ הוספת משימה", callback_data="adm:todo_add"))
        markup.add(InlineKeyboardButton("🔙 חזרה", callback_data="adm:todo_menu"))
        self.d.bot.send_message(chat_id, body, reply_markup=markup)

    def _broadcast_menu(self, chat_id):
        self.d.cancel_pending(ADMIN_USER_ID)
        self.d.bot.send_message(
            chat_id, "📢 שידור הודעה — בחר פעולה:",
            reply_markup=self._markup(BROADCAST_MENU + [
                ("🔙 חזרה לתפריט ראשי", "adm:menu"),
            ]),
        )

    def _broadcast_prompt(self, chat_id, text):
        self.d.bot.send_message(
            chat_id, text,
            reply_markup=self._markup([("🔙 חזרה לתפריט שידור", "adm:broadcast")]),
        )

    def _start_manual_broadcast(self, chat_id):
        if not self._private(chat_id):
            return
        self._target_menu(chat_id, {
            "buttons": [], "nonce": secrets.token_hex(8),
            "broadcast_kind": "manual", "resume_action": "broadcast_manual_content",
        })

    def _target_menu(self, chat_id, state):
        state["action"] = "broadcast_target"
        state["nonce"] = secrets.token_hex(8)
        state.pop("preview_message_id", None)
        self.d.pending[ADMIN_USER_ID] = state
        self.d.pending_chat_id = chat_id
        nonce = state["nonce"]
        self.d.bot.send_message(
            chat_id, "📢 למי לשלוח את השידור? בחר יעד לפני התצוגה המקדימה:",
            reply_markup=self._markup([
                ("👤 משתמשים בלבד", f"adm:broadcast_target:{nonce}:users"),
                ("👥 קבוצות בלבד", f"adm:broadcast_target:{nonce}:groups"),
                ("🌐 גם וגם (משתמשים וקבוצות)", f"adm:broadcast_target:{nonce}:both"),
                ("🔙 חזרה לתפריט שידור", "adm:broadcast"),
                ("🏠 חזרה לתפריט ראשי", "adm:menu"),
            ]),
        )

    def start_broadcast_callback(self, call):
        """Consume valid send clicks before generic/deferred callback feedback."""
        parts = (call.data or "").split(":")
        if len(parts) != 3 or parts[0] != "adm" or parts[1] not in {
            "broadcast_confirm", "blast_send",
        }:
            return False
        actor = getattr(call.from_user, "id", None)
        message = call.message
        if not is_admin(actor) or message is None or message.chat.id != actor or actor <= 0:
            return False
        if (actor, parts[2]) in self._consumed_broadcasts:
            self.d.bot.acknowledge_callback_query(
                call.id, text="השידור כבר אושר. אין צורך ללחוץ שוב.", show_alert=True,
            )
            return True
        state = self.d.pending.get(ADMIN_USER_ID)
        expected = "broadcast_confirmation" if parts[1] == "broadcast_confirm" else "blast_buttons"
        if (
            not state or state.get("action") != expected
            or self.d.pending_chat_id != actor or state.get("nonce") != parts[2]
            or state.get("target") not in {"users", "groups", "both"}
            or (parts[1] == "broadcast_confirm"
                and state.get("preview_message_id") != message.message_id)
        ):
            return False
        try:
            self._send_interactive(actor, state, actor, callback_id=call.id)
        except (ValueError, OSError) as exc:
            self.d.bot.acknowledge_callback_query(call.id, text=str(exc)[:200], show_alert=True)
            self.d.bot.answer_callback_query(call.id, text=str(exc)[:200], show_alert=True)
        return True

    def _start_donation_edit(self, chat_id):
        if not self._private(chat_id):
            return
        preset = load_donation_preset(self.donation_preset_file)["payload"]
        current = "לא נשמרה עדיין הודעת תרומות קבועה." if preset is None else "הודעת תרומות קבועה קיימת ותוחלף רק לאחר שמירה."
        self._pending(chat_id, "donation_broadcast_edit")
        self._broadcast_prompt(
            chat_id,
            f"{current}\nשלח טקסט או מדיה לשמירה כהודעת התרומות הקבועה. לביטול שלח „ביטול”.",
        )

    def _confirmation_markup(self, nonce):
        return self._markup([
            ("✅ אישור ושליחה", f"adm:broadcast_confirm:{nonce}"),
            ("❌ ביטול", f"adm:broadcast_cancel:{nonce}"),
        ])

    def _show_broadcast_preview(self, chat_id, state):
        if not self._private(chat_id):
            raise ValueError("תפוצה זמינה בפרטי בלבד.")
        if state.get("target") not in {"users", "groups", "both"}:
            state["resume_action"] = "broadcast_confirmation"
            return self._target_menu(chat_id, state)
        if load_config(self.config_file)["ADMIN_CHAT_ID"] is None:
            raise ValueError("הגדר קבוצת ניהול עם /set_admin לפני התפוצה.")
        payload = validate_payload(state["payload"])
        from management_config import resolve_text
        resolve_text(payload["text"], self.config_file)
        was_input = state.get("source_message") is not None
        state["payload"] = payload
        previous_action = state.get("action")
        state["action"] = "broadcast_confirmation"
        state["preview_message_id"] = None
        state["broadcast_kind"] = state.get("broadcast_kind", "manual")
        markup = self._confirmation_markup(state["nonce"])
        try:
            preview = send_payload(
                self.d.raw_bot, chat_id, payload, markup, self.config_file,
            )
            self.d.bot.register_external_menu(
                chat_id, preview, payload.get("text", ""), markup,
            )
        except Exception as exc:
            state["action"] = previous_action or "broadcast_manual_content"
            state.pop("preview_message_id", None)
            logging.warning("Broadcast preview failed (%s).", type(exc).__name__)
            raise ValueError("לא ניתן להציג את התצוגה המקדימה. בדוק את המדיה ונסה שוב.") from exc
        state["preview_message_id"] = preview.message_id
        state["source_message"] = None
        self.d.pending[ADMIN_USER_ID] = state
        if was_input:
            from admin_edit_flow import record_draft_commit
            record_draft_commit()
            from admin_ui import suppress_input_feedback
            suppress_input_feedback()
        return preview

    def _preview_saved_donation(self, chat_id):
        payload = load_donation_preset(self.donation_preset_file)["payload"]
        if payload is None:
            raise ValueError("עדיין לא נשמרה הודעת תרומות. בחר „עריכת הודעת התרומות הקבועה” תחילה.")
        self._target_menu(chat_id, {
            "payload": payload, "buttons": [], "source_message": None,
            "broadcast_kind": "donation", "resume_action": "broadcast_confirmation",
        })

    def _blast_buttons(self, chat_id, state):
        state["action"] = "blast_buttons"
        rows = [
            ("➕ הוספת כפתור קישור", "adm:blast_link"),
            ("💬 הוספת כפתור תמיכה", "adm:blast_support"),
            ("👁️ תצוגה מקדימה", "adm:blast_preview"),
            ("📤 אישור ושליחה", f"adm:blast_send:{state['nonce']}"),
            ("🔙 ביטול וחזרה", "adm:menu"),
        ]
        self.d.bot.send_message(
            chat_id, f"התוכן נשמר. כפתורים: {len(state['buttons'])}.\nאפשר לשלוח גם בלי כפתורים. שום דבר לא נשלח עד לאישור.",
            reply_markup=self._markup(rows),
        )

    def run_plain(self, chat_id, text, include_groups, source_message=None):
        self._target_menu(chat_id, {
            "payload": validate_payload({"kind": "text", "text": text}), "buttons": [],
            "source_message": source_message, "broadcast_kind": "manual",
            "resume_action": "broadcast_confirmation",
        })

    def _completion(self, chat_id, result, source_message=None):
        message = (
            f"✅ השידור הושלם. נשלחו {result['sent'] + result['groups_sent']} הודעות; "
            f"נכשלו {result['failed'] + result['blocked'] + result['groups_failed']}.\n"
            f"פרטיים: {result['sent']}; קבוצות: {result['groups_sent']}.\n"
            f"חסמו את הבוט: {result['blocked']}; כשלים: {result['failed'] + result['groups_failed']}."
        )
        if result["report_delivered"]:
            message += "\nדוח מפורט נשלח לקבוצת הניהול."
        else:
            message += "\n⚠️ הדוח לא הגיע לקבוצת הניהול. אין לשלוח שוב את התפוצה שכבר הופצה."
        send_broadcast_completion(self.d.bot, chat_id, message, source_message)

    def _send_interactive(self, chat_id, state, initiator_id=None, return_to_broadcast=False,
                          callback_id=None):
        # Network/storage validation and delivery belong to the worker. The
        # preview has already validated the payload; the engine validates again
        # before its first recipient to catch intervening setting changes.
        if state.get("target") not in {"users", "groups", "both"}:
            raise ValueError("יש לבחור את יעד השידור לפני השליחה.")
        if not self._blast_lock.acquire(blocking=False):
            raise ValueError("תפוצה כבר מתבצעת. המתן לסיומה.")
        try:
            snapshot = copy.deepcopy(state)
            if callback_id is not None:
                self.d.bot.acknowledge_callback_query(
                    callback_id, text="🚀 השידור יצא לדרך ברקע!", show_alert=True,
                )
            self.d.cancel_pending(ADMIN_USER_ID)
            self._consumed_broadcasts.append((initiator_id or chat_id, state["nonce"]))
        except Exception:
            self._blast_lock.release()
            raise

        def work():
            try:
                # Cosmetic cleanup must never prevent recipient delivery.
                if snapshot.get("preview_message_id"):
                    try:
                        self.d.raw_bot.edit_message_reply_markup(
                            chat_id=chat_id, message_id=snapshot["preview_message_id"],
                            reply_markup=self._markup([
                                ("🔙 חזרה לתפריט שידור", "adm:broadcast"),
                                ("🏠 חזרה לתפריט ראשי", "adm:menu"),
                            ]),
                        )
                    except Exception as exc:
                        logging.warning("Broadcast preview cleanup failed (%s).", type(exc).__name__)
                result = send_broadcast(
                    self.d.raw_bot, self.d.users_file, self.d.groups_file,
                    self.d.support_admin.path, self.config_file,
                    snapshot["payload"], snapshot["buttons"],
                    initiator_id=initiator_id if initiator_id is not None else chat_id,
                    include_users=snapshot["target"] != "groups",
                    include_groups=snapshot["target"] != "users",
                )
                self._completion(chat_id, result, snapshot.get("source_message"))
            except Exception as exc:
                logging.warning("Interactive broadcast failed (%s).", type(exc).__name__)
                self.d.bot.send_message(chat_id, "התפוצה לא הושלמה. בדוק את קבוצת הניהול לפני ניסיון נוסף.")
            finally:
                self._blast_lock.release()
        try:
            threading.Thread(target=work, name="interactive-broadcast", daemon=True).start()
        except Exception:
            self._blast_lock.release()
            self._consumed_broadcasts.remove((initiator_id or chat_id, snapshot["nonce"]))
            self.d.pending[ADMIN_USER_ID] = snapshot
            self.d.pending_chat_id = chat_id
            raise ValueError("לא ניתן להתחיל את משימת השידור. התחל תצוגה מקדימה חדשה.")

    def handle_callback(self, action, call):
        controls = getattr(self.d, "restriction_controls", None)
        if controls and controls.handle_callback(self.d, action, call):
            return True
        if action == "commands_help":
            if call.message.chat.id <= 0:
                self.d.bot.send_message(call.message.chat.id, "פקודות הניהול זמינות בפרטי בלבד.")
                return True
            from admin_menus import build_menu
            self.d.cancel_pending(ADMIN_USER_ID)
            self.d.bot.send_message(
                call.message.chat.id, build_admin_commands_help(), parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=build_menu([("🔙 חזרה לתפריט ראשי", "adm:menu")]),
            )
            return True
        if self.accounts.handle_callback(action, call):
            return True
        recognized = (
            action in {"guide", "backup", "broadcast", "log_chat"}
            or action.startswith("broadcast_")
            or action.startswith("todo_") or action.startswith("blast_")
        )
        if not recognized:
            return False
        if not is_admin(call.from_user.id) or call.message is None:
            return True
        chat_id = call.message.chat.id
        parts = call.data.split(":")
        try:
            if action == "log_chat":
                if not self._private(chat_id):
                    return True
                self._pending(chat_id, "log_chat_id")
                self.d.bot.prompt(chat_id, "שלח את מזהה קבוצת הגיבוי, למשל ‎-100123456789. הוסף את הבוט לקבוצה ואפשר לו לשלוח קבצים. לביטול שלח „ביטול”.")
            elif action == "guide":
                self._guide(chat_id, int(parts[2]) if len(parts) > 2 else 0)
            elif action == "backup":
                self.d.cancel_pending(ADMIN_USER_ID)
                self.backup.manual_config()
                self.d.bot.answer_callback_query(
                    call.id, text="✅ גיבוי הנתונים נשלח בהצלחה לקבוצת הגיבוי!", show_alert=True,
                )
            elif action == "broadcast":
                self._broadcast_menu(chat_id)
            elif action in {"broadcast_manual", "broadcast_plain"}:
                self._start_manual_broadcast(chat_id)
            elif action == "broadcast_preset":
                if not self._private(chat_id):
                    return True
                self._preview_saved_donation(chat_id)
            elif action == "broadcast_donation_edit":
                self._start_donation_edit(chat_id)
            elif action == "broadcast_target":
                state = self.d.pending.get(ADMIN_USER_ID)
                if (
                    chat_id != call.from_user.id or chat_id <= 0
                    or not state or state.get("action") != "broadcast_target"
                    or self.d.pending_chat_id != chat_id or len(parts) != 4
                    or parts[2] != state.get("nonce") or parts[3] not in {"users", "groups", "both"}
                ):
                    return True
                state["target"] = parts[3]
                resume = state.pop("resume_action", "broadcast_manual_content")
                if resume == "broadcast_confirmation":
                    self._show_broadcast_preview(chat_id, state)
                else:
                    state["action"] = resume
                    label = {
                        "users": "משתמשים בלבד",
                        "groups": "קבוצות בלבד",
                        "both": "גם וגם (משתמשים וקבוצות)",
                    }[state["target"]]
                    self._broadcast_prompt(
                        chat_id,
                        f"בחרת {label}. כעת שלח/הקלד את ההודעה שברצונך לשדר:\n"
                        "ניתן לשלוח טקסט או מדיה. השידור יישלח רק לאחר תצוגה מקדימה "
                        "ואישור מפורש. לביטול שלח „ביטול”.",
                    )
            elif action in {"broadcast_confirm", "broadcast_cancel"}:
                state = self.d.pending.get(ADMIN_USER_ID)
                if not self._private(chat_id) or chat_id != call.from_user.id:
                    return True
                if (
                    not state or state.get("action") != "broadcast_confirmation"
                    or self.d.pending_chat_id != chat_id
                    or len(parts) != 3 or parts[2] != state.get("nonce")
                    or state.get("preview_message_id") != getattr(call.message, "message_id", None)
                ):
                    # An old button must never authorize a different payload.
                    # Rebuild the current draft and require a fresh click.
                    if state and state.get("action") in {
                        "broadcast_confirmation", "broadcast_manual_content",
                    } and state.get("payload"):
                        state = dict(state)
                        state["nonce"] = secrets.token_hex(8)
                        self.d.cancel_pending(ADMIN_USER_ID)
                        self.d.pending_chat_id = chat_id
                        self._show_broadcast_preview(chat_id, state)
                    else:
                        self.d.cancel_pending(ADMIN_USER_ID)
                        try:
                            # After a process restart the original preview
                            # itself can recover text/media, but never send it
                            # until a newly generated confirmation is pressed.
                            payload = payload_from_message(call.message)
                            buttons = [
                                {"text": button.text, "url": button.url}
                                for row in getattr(getattr(call.message, "reply_markup", None), "keyboard", [])
                                for button in row if getattr(button, "url", None)
                            ]
                        except (ValueError, AttributeError, TypeError):
                            self._start_manual_broadcast(chat_id)
                        else:
                            self.d.pending_chat_id = chat_id
                            self._show_broadcast_preview(chat_id, {
                                "action": "broadcast_confirmation", "payload": payload,
                                "buttons": buttons, "nonce": secrets.token_hex(8),
                                "broadcast_kind": "manual", "source_message": None,
                            })
                    return True
                if action == "broadcast_cancel":
                    self.d.cancel_pending(ADMIN_USER_ID)
                    self.d.bot.send_message(chat_id, "השידור בוטל ולא נשלח.")
                    self._broadcast_menu(chat_id)
                else:
                    self._send_interactive(
                        chat_id, state, call.from_user.id, return_to_broadcast=True, callback_id=call.id,
                    )
            elif action.startswith("todo_"):
                if not self._private(chat_id):
                    return True
                if action == "todo_menu":
                    self._tasks_menu(chat_id)
                elif action == "todo_list":
                    self._tasks_list(chat_id, int(parts[2]) if len(parts) > 2 else 0)
                elif action == "todo_add":
                    self._pending(chat_id, "todo_add")
                    self.d.bot.prompt(chat_id, "שלח משימה או פתק (עד 500 תווים), או ״ביטול״.")
                elif action == "todo_done":
                    update_task(parts[2], "done", self.tasks_file)
                    self.d.bot.send_message(chat_id, "המשימה סומנה כבוצעה.")
                    self._tasks_list(chat_id)
                elif action == "todo_delete":
                    task = next((task for task in load_tasks(self.tasks_file)["tasks"] if task["id"] == parts[2]), None)
                    if not task:
                        raise ValueError("המשימה אינה קיימת.")
                    self.d.bot.send_message(chat_id, f"למחוק לצמיתות את המשימה?\n{task['text']}", reply_markup=self._markup([
                        ("❌ כן, מחק", f"adm:todo_confirm:{task['id']}"),
                        ("🔙 לא, חזרה", "adm:todo_list:0"),
                    ]))
                elif action == "todo_confirm":
                    update_task(parts[2], "delete", self.tasks_file)
                    self.d.bot.send_message(chat_id, "המשימה נמחקה.")
                    self._tasks_list(chat_id)
            elif action == "blast_begin":
                self._target_menu(chat_id, {
                    "buttons": [], "broadcast_kind": "manual", "resume_action": "blast_content",
                })
            else:
                state = self.d.pending.get(ADMIN_USER_ID)
                if not state or state["action"] != "blast_buttons" or self.d.pending_chat_id != chat_id:
                    raise ValueError("הפעולה התיישנה. התחל תפוצה חדשה.")
                if action == "blast_link":
                    if len(state["buttons"]) >= 8:
                        raise ValueError("אפשר עד 8 כפתורים.")
                    state["action"] = "blast_label"
                    self.d.bot.prompt(chat_id, "שלח את שם כפתור הקישור (עד 64 תווים), או ״ביטול״.")
                elif action == "blast_support":
                    if len(state["buttons"]) >= 8:
                        raise ValueError("אפשר עד 8 כפתורים.")
                    if not any(button.get("support") for button in state["buttons"]):
                        state["buttons"].append({"text": "💬 פנייה למנהל / תמיכה", "support": True})
                    self._blast_buttons(chat_id, state)
                elif action == "blast_preview":
                    markup = broadcast_markup(state["buttons"], self.config_file)
                    preview = send_payload(self.d.raw_bot, chat_id, state["payload"], markup, self.config_file)
                    delete_later(self.d.raw_bot, chat_id, preview.message_id, 5)
                    self._blast_buttons(chat_id, state)
                elif action == "blast_send":
                    if len(parts) < 3 or parts[2] != state["nonce"]:
                        raise ValueError("אישור השליחה התיישן.")
                    self._send_interactive(chat_id, state, call.from_user.id, callback_id=call.id)
        except (ValueError, OSError, IndexError) as exc:
            self.d.bot.send_message(chat_id, str(exc))
        except Exception as exc:
            logging.warning("Administrative management action failed (%s).", type(exc).__name__)
            self.d.bot.send_message(chat_id, "הפעולה לא הושלמה. בדוק הרשאות וקישורים ונסה שוב.")
        return True

    def handle_input(self, action, state, message, reply_text):
        health = getattr(self.d, "drive_checker", None)
        if health and health.handle_input(self.d, action, state, reply_text):
            return True
        controls = getattr(self.d, "restriction_controls", None)
        if controls and controls.handle_input(self.d, action, state, message, reply_text):
            return True
        if self.accounts.handle_input(action, message):
            return True
        chat_id = message.chat.id
        try:
            if action == "log_chat_id":
                if not self._private(chat_id):
                    return True
                from command_archive import configure_log_chat
                configure_log_chat(self.d.raw_bot, message.text, self.config_file)
                self.d.cancel_pending(self.d.actor_id)
                self.d.bot.send_message(chat_id, "✅ קבוצת הגיבוי הוגדרה בהצלחה!")
                self.d.send_menu(chat_id)
            elif action == "todo_add":
                if not self._private(chat_id):
                    return True
                add_task(message.text, self.tasks_file)
                self.d.cancel_pending(ADMIN_USER_ID)
                self.d.bot.send_message(chat_id, "המשימה נשמרה.")
                self._tasks_list(chat_id)
            elif action == "broadcast_target":
                self._target_menu(chat_id, state)
            elif action in {"broadcast_manual_content", "broadcast"}:
                if not self._private(chat_id):
                    return True
                state["payload"] = validate_payload({"kind": "text", "text": reply_text})
                state["source_message"] = message
                self._show_broadcast_preview(chat_id, state)
            elif action == "donation_broadcast_edit":
                if not self._private(chat_id):
                    return True
                payload = payload_from_message(message, text=reply_text)
                save_donation_preset(payload, self.donation_preset_file)
                self.d.cancel_pending(ADMIN_USER_ID)
                self.d.bot.send_message(chat_id, "✅ הודעת התרומות הקבועה נשמרה.")
                self._broadcast_menu(chat_id)
            elif action == "blast_content":
                if len(reply_text) > 4096:
                    raise ValueError("טקסט תפוצה ארוך מדי.")
                state["payload"] = {"kind": "text", "text": reply_text}
                state["source_message"] = message
                self._blast_buttons(chat_id, state)
            elif action == "blast_label":
                label = message.text.strip()
                if not 1 <= len(label) <= 64:
                    raise ValueError("שם כפתור חייב להכיל 1–64 תווים.")
                state["label"] = label
                state["action"] = "blast_url"
                self.d.bot.prompt(chat_id, "שלח URL או {channel_link}, {main_group_link}, {support_link}. ״ביטול״ מבטל.")
            elif action == "blast_url":
                url = message.text.strip()
                if not is_link_input(url):
                    raise ValueError("שלח קישור http/https תקין או משתנה קישור מוכר.")
                state["buttons"].append({"text": state.pop("label"), "url": url})
                self._blast_buttons(chat_id, state)
            else:
                return False
        except (ValueError, OSError) as exc:
            self.d.bot.send_message(chat_id, str(exc))
        return True

    def has_photo_input(self, message):
        return self.has_media_input(message, photo_only=True)

    def has_media_input(self, message, photo_only=False):
        user = getattr(message, "from_user", None)
        state = self.d.pending.get(ADMIN_USER_ID, {})
        if user is None or not is_admin(user.id) or self.d.pending_chat_id != message.chat.id:
            return False
        if message.chat.id <= 0 or message.chat.id != user.id:
            return False
        kinds = ("photo",) if photo_only else (
            "photo", "video", "document", "animation", "audio", "voice",
        )
        has_media = any(getattr(message, kind, None) for kind in kinds)
        return has_media and state.get("action") in {
            "blast_content", "broadcast_manual_content", "donation_broadcast_edit",
        }

    def handle_photo(self, message):
        return self.handle_media(message)

    def handle_media(self, message):
        if not self.has_media_input(message):
            return
        content = getattr(message, "caption", None) or getattr(message, "text", None) or ""
        if content.strip() == "ביטול":
            self.d.cancel_pending(ADMIN_USER_ID)
            self._broadcast_menu(message.chat.id)
            return
        state = self.d.pending[ADMIN_USER_ID]
        try:
            payload = payload_from_message(message)
            if state["action"] == "blast_content":
                state["payload"] = payload
                state["source_message"] = message
                self._blast_buttons(message.chat.id, state)
            elif state["action"] == "broadcast_manual_content":
                state["payload"] = payload
                state["source_message"] = message
                self._show_broadcast_preview(message.chat.id, state)
            elif state["action"] == "donation_broadcast_edit":
                save_donation_preset(payload, self.donation_preset_file)
                self.d.cancel_pending(ADMIN_USER_ID)
                self.d.bot.send_message(message.chat.id, "✅ הודעת התרומות הקבועה נשמרה.")
                self._broadcast_menu(message.chat.id)
        except (ValueError, OSError) as exc:
            self.d.bot.send_message(message.chat.id, str(exc))
