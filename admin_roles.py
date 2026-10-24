"""The immutable owner delegates bot administration; only the owner manages roles."""

OWNER_ID = 6390241278


def is_owner(user_id):
    return user_id == OWNER_ID


def admin_ids(path=None):
    from management_config import CONFIG_FILE, load_config
    return set(load_config(path or CONFIG_FILE)["admin_ids"])


def is_authorized_admin(user_id):
    if is_owner(user_id):
        return True
    if type(user_id) is not int:
        return False
    return user_id in admin_ids()


def change_admin(user_id, add, path=None):
    from management_config import CONFIG_FILE, _LOCK, load_config, save_json
    if type(user_id) is not int or not 0 < user_id < 2**52:
        raise ValueError("יש להזין Telegram ID מספרי חיובי.")
    if not add and is_owner(user_id):
        raise ValueError("לא ניתן להסיר את המנהל הראשי.")
    path = path or CONFIG_FILE
    with _LOCK:
        config = load_config(path)
        ids = set(config["admin_ids"])
        ids.add(user_id) if add else ids.discard(user_id)
        config["admin_ids"] = sorted(ids)
        save_json(path, config)


class ActorPending(dict):
    """Legacy owner-keyed workflows get an isolated map for each acting admin."""

    def __init__(self, actor_id):
        super().__init__()
        self.actor_id = actor_id

    def _key(self, key):
        return self.actor_id if key == OWNER_ID else key

    def __setitem__(self, key, value):
        return super().__setitem__(self._key(key), value)

    def __getitem__(self, key):
        return super().__getitem__(self._key(key))

    def __contains__(self, key):
        return super().__contains__(self._key(key))

    def get(self, key, default=None):
        return super().get(self._key(key), default)

    def pop(self, key, default=None):
        return super().pop(self._key(key), default)
