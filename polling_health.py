"""Thread-safe, anonymous polling readiness based on completed Telegram requests."""

from threading import Lock
from time import monotonic
import re


class PollingHealth:
    STALE_AFTER_SECONDS = 120

    def __init__(self, runtime, *, standby=False, clock=monotonic, credential_key=None):
        self.runtime = runtime
        if credential_key not in {None, "BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "DEV_BOT_TOKEN"}:
            raise ValueError("Unknown credential key name.")
        self._credential_key = credential_key
        self._bot_username = None
        self._clock = clock
        self._lock = Lock()
        self._state = "standby" if standby else "starting"
        self._last_success = None
        self._last_failure = None
        self._failure_category = None
        self._failures = 0
        self._consecutive_failures = 0

    def set_bot_identity(self, username):
        """Retain only the username returned by this process's Telegram client."""
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            raise ValueError("Telegram bot username is unavailable or invalid.")
        with self._lock:
            self._bot_username = username

    def admin_snapshot(self):
        """Private diagnostics; deliberately separate from anonymous HTTP health."""
        with self._lock:
            return {
                "runtime": self.runtime,
                "credential_key": self._credential_key,
                "bot_username": self._bot_username,
            }

    def start(self):
        with self._lock:
            self._state = "polling"

    def success(self):
        with self._lock:
            self._last_success = self._clock()
            self._consecutive_failures = 0

    def failure(self, exc):
        # Never retain exception text, URLs, Telegram payloads, or identifiers.
        code = getattr(exc, "error_code", None)
        category = {
            401: "unauthorized", 403: "forbidden", 409: "conflict",
            429: "rate_limited",
        }.get(code, "telegram_api" if isinstance(code, int) else "request_failed")
        with self._lock:
            self._last_failure = self._clock()
            self._failure_category = category
            self._failures += 1
            self._consecutive_failures += 1

    def stop(self):
        with self._lock:
            self._state = "stopped"

    def snapshot(self):
        with self._lock:
            now = self._clock()
            success_age = (
                None if self._last_success is None else max(0, now - self._last_success)
            )
            failure_age = (
                None if self._last_failure is None else max(0, now - self._last_failure)
            )
            state = self._state
            if state == "polling":
                if self._consecutive_failures:
                    state = "failed"
                elif success_age is None:
                    state = "starting"
                elif success_age >= self.STALE_AFTER_SECONDS:
                    state = "stale"
                else:
                    state = "ready"
            return {
                "runtime": self.runtime,
                "state": state,
                "ready": state == "ready",
                "last_success_age_seconds": None if success_age is None else round(success_age, 1),
                "last_failure_age_seconds": None if failure_age is None else round(failure_age, 1),
                "last_failure_category": self._failure_category,
                "failure_count": self._failures,
                "consecutive_failures": self._consecutive_failures,
                "stale_after_seconds": self.STALE_AFTER_SECONDS,
            }
