"""Persistent aggregate search statistics for the media catalogue."""

from __future__ import annotations

import json
import threading
from pathlib import Path


MEDIA_ANALYTICS_FILE = Path(__file__).resolve().with_name("media_analytics.json")
_LOCK = threading.RLock()


def _load(path: Path) -> dict:
    try:
        from persistent_json import read_json_text
        data = json.loads(read_json_text(path))
    except FileNotFoundError:
        return {
            "total_searches": 0,
            "missing_content_reports": 0,
            "keywords": {},
        }
    except json.JSONDecodeError as exc:
        raise ValueError("קובץ סטטיסטיקות המדיה אינו JSON תקין.") from exc

    if not isinstance(data, dict):
        raise ValueError("מבנה קובץ סטטיסטיקות המדיה אינו תקין.")
    total = data.get("total_searches")
    missing = data.get("missing_content_reports")
    keywords = data.get("keywords")
    if (
        type(total) is not int or total < 0
        or type(missing) is not int or missing < 0 or missing > total
        or not isinstance(keywords, dict)
    ):
        raise ValueError("נתוני סטטיסטיקות המדיה אינם תקינים.")
    count_sum = 0
    for key, item in keywords.items():
        if (
            not isinstance(key, str)
            or not isinstance(item, dict)
            or not isinstance(item.get("keyword"), str)
            or type(item.get("count")) is not int
            or item["count"] < 1
        ):
            raise ValueError("רשימת מונחי החיפוש אינה תקינה.")
        count_sum += item["count"]
    if count_sum != total:
        raise ValueError("ספירת החיפושים אינה תואמת לרשימת המונחים.")
    return {
        "total_searches": total,
        "missing_content_reports": missing,
        "keywords": keywords,
    }


def _save(path: Path, data: dict) -> None:
    from persistent_json import write_json_document
    write_json_document(path, data)


class MediaAnalytics:
    """Thread-safe all-time counters stored independently from in-memory bot state."""

    def __init__(self, path: str | Path = MEDIA_ANALYTICS_FILE) -> None:
        self.path = Path(path)

    def _record(self, keyword: str, *, missing: bool) -> int:
        if not isinstance(keyword, str):
            raise ValueError("מונח החיפוש חייב להיות טקסט.")
        label = " ".join(keyword.split())
        if not label:
            raise ValueError("מונח החיפוש אינו יכול להיות ריק.")
        key = label.casefold()

        with _LOCK:
            data = _load(self.path)
            entry = data["keywords"].setdefault(
                key, {"keyword": label, "count": 0},
            )
            entry["count"] += 1
            data["total_searches"] += 1
            if missing:
                data["missing_content_reports"] += 1
            _save(self.path, data)
            return data["total_searches"]

    def record_search(self, keyword: str) -> int:
        return self._record(keyword, missing=False)

    def record_missing_content_request(self, keyword: str) -> int:
        """Count a successfully delivered missing-content request as a search too."""
        return self._record(keyword, missing=True)

    def snapshot(self) -> dict:
        with _LOCK:
            data = _load(self.path)
        ranked = sorted(
            data["keywords"].values(),
            key=lambda entry: (-entry["count"], entry["keyword"].casefold()),
        )
        return {
            "total_searches": data["total_searches"],
            "missing_content_reports": data["missing_content_reports"],
            "top_keywords": [
                {"keyword": entry["keyword"], "count": entry["count"]}
                for entry in ranked[:5]
            ],
        }
