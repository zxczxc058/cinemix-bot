"""Per-chat custom replies, stored in config.json under each signed chat ID."""

from management_config import CONFIG_FILE, _LOCK, load_config, save_json

MAX_COMMANDS_PER_GROUP = 50
MAX_KEYWORD_LENGTH = 80
MAX_RESPONSE_LENGTH = 3000


def _chat_id(chat_id):
    if type(chat_id) is not int or chat_id >= 0 or abs(chat_id) >= 2**52:
        raise ValueError("פקודות מותאמות זמינות רק בקבוצה או בסופר־קבוצה.")
    return str(chat_id)


def _normalized(keyword):
    return " ".join(keyword.replace("_", " ").split()).casefold()


def list_group_commands(chat_id, config_file=CONFIG_FILE):
    key = _chat_id(chat_id)
    return dict(load_config(config_file)["group_commands"].get(key, {}))


def save_group_command(chat_id, keyword, response, config_file=CONFIG_FILE):
    key = _chat_id(chat_id)
    if (
        not isinstance(keyword, str) or not keyword.strip()
        or len(keyword.strip()) > MAX_KEYWORD_LENGTH
        or any(character.isspace() for character in keyword.strip())
    ):
        raise ValueError("שם הפקודה חייב להיות מילה אחת, עד 80 תווים (אפשר להשתמש בקו תחתון).")
    if not isinstance(response, str) or not response.strip() or len(response) > MAX_RESPONSE_LENGTH:
        raise ValueError("תוכן הפקודה חייב להכיל 1–3000 תווים.")
    keyword = keyword.strip()
    response = response.strip()
    with _LOCK:
        config = load_config(config_file)
        commands = config["group_commands"].setdefault(key, {})
        existing = next(
            (saved for saved in commands if _normalized(saved) == _normalized(keyword)),
            None,
        )
        if existing is None and len(commands) >= MAX_COMMANDS_PER_GROUP:
            raise ValueError(f"אפשר לשמור עד {MAX_COMMANDS_PER_GROUP} פקודות בכל קבוצה.")
        if existing is not None:
            del commands[existing]
        commands[keyword] = response
        save_json(config_file, config)
    return keyword


def delete_group_command(chat_id, keyword, config_file=CONFIG_FILE):
    key = _chat_id(chat_id)
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("צריך לציין את שם הפקודה למחיקה.")
    with _LOCK:
        config = load_config(config_file)
        commands = config["group_commands"].get(key, {})
        existing = next(
            (saved for saved in commands if _normalized(saved) == _normalized(keyword.strip())),
            None,
        )
        if existing is None:
            raise ValueError("הפקודה לא נמצאה בקבוצה הזו.")
        del commands[existing]
        if not commands:
            config["group_commands"].pop(key, None)
        save_json(config_file, config)
    return existing


def match_group_command(chat_id, text, config_file=CONFIG_FILE):
    if not isinstance(text, str) or not text.strip():
        return None
    commands = list_group_commands(chat_id, config_file)
    candidate = text.strip()
    for keyword, response in commands.items():
        if candidate == keyword:
            return keyword, response
    return None
