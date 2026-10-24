class PasscodeMiddleware:
    def __init__(self, *args, **kwargs):
        pass
    def __getattr__(self, name):
        return lambda *a, **k: None

def load_access(*args, **kwargs):
    return {}

def save_access(*args, **kwargs):
    return None
