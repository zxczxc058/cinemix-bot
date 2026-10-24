"""Readable transfer sizes without rounding early progress down to 0.00GB."""


def transfer_size(size):
    if size >= 1024**3:
        return f"{size / 1024**3:.2f}GB"
    return f"{size / 1024**2:.1f}MB"
