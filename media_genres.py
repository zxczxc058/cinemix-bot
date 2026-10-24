"""Shared parsing for legacy single genres and multi-genre content."""

import re


def genre_key(value):
    label = re.sub(r"^[^\w]+", "", value.strip()).casefold()
    return "מותחן" if label == "מתח" else label


def _canonical_genre_label(value):
    prefix = re.match(r"^[^\w]+", value)
    core = re.sub(r"^[^\w]+", "", value)
    if core.casefold() == "מתח":
        return f"{prefix.group(0) if prefix else ''}מותחן"
    return value


def genre_list(value):
    """Accept comma-separated text or arrays, retaining display labels."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) for v in value):
        raise ValueError("שלח ז'אנרים כטקסט מופרד בפסיקים.")
    result, seen = [], set()
    for entry in value:
        for part in re.split(r"[,،/]+", entry):
            label = " ".join(part.split())
            key = genre_key(label)
            if not key:
                continue
            label = _canonical_genre_label(label)
            if len(label) > 100:
                raise ValueError("שם ז'אנר חייב להיות באורך עד 100 תווים.")
            if key not in seen:
                result.append(label)
                seen.add(key)
    return result


def genre_text(value):
    return ", ".join(genre_list(value))


def has_genre(value, target):
    return genre_key(target) in {genre_key(label) for label in genre_list(value)}
