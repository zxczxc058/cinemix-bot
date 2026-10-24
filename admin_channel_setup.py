class AdminChannelSetup:
    def __init__(self, *args, **kwargs):
        pass
    def __getattr__(self, name):
        return lambda *a, **k: None

def __getattr__(name):
    def dummy(*args, **kwargs):
        return None
    return dummy
