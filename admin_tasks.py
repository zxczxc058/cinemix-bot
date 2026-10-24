"""Private administrator notes, separate from workspace/project tasks."""

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from management_config import save_json

TASKS_FILE = Path(__file__).resolve().with_name("tasks.json")
_LOCK = threading.RLock()


def load_tasks(path=TASKS_FILE):
    with _LOCK:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("Invalid administrator tasks.")
        ids = set()
        for task in data["tasks"]:
            if (
                not isinstance(task, dict) or not isinstance(task.get("id"), str)
                or not task["id"] or task["id"] in ids
                or not isinstance(task.get("text"), str) or not task["text"].strip()
                or len(task["text"]) > 500 or type(task.get("done")) is not bool
            ):
                raise ValueError("Invalid administrator note.")
            ids.add(task["id"])
        return data


def add_task(text, path=TASKS_FILE):
    if not text.strip() or len(text) > 500:
        raise ValueError("משימה חייבת להכיל בין 1 ל־500 תווים.")
    with _LOCK:
        data = load_tasks(path)
        task = {"id": secrets.token_hex(8), "text": text.strip(), "done": False,
                "created_at": datetime.now(timezone.utc).isoformat()}
        data["tasks"].append(task)
        save_json(path, data)
        return task


def update_task(task_id, action, path=TASKS_FILE):
    if action not in {"done", "delete"}:
        raise ValueError("Unknown task action.")
    with _LOCK:
        data = load_tasks(path)
        task = next((task for task in data["tasks"] if task["id"] == task_id), None)
        if task is None:
            raise ValueError("המשימה כבר אינה קיימת. רענן את הרשימה.")
        if action == "done":
            task["done"] = True
        else:
            data["tasks"].remove(task)
        save_json(path, data)
