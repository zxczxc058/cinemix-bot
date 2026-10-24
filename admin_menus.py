def __getattr__(name):
    def dummy(*args, **kwargs):
        return None
    return dummy

def build_me(*args, **kwargs):
    return None
