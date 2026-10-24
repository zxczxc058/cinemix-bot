"""Fail-closed production polling ownership on a dedicated PostgreSQL session."""

import hashlib
import logging
import os
import threading

from telebot import apihelper


class PollingLeaseError(RuntimeError):
    """Credential-free startup/ownership failure."""

class PollingLeaseBusy(PollingLeaseError):
    """A healthy previous deployment still owns polling."""


def lease_key(token):
    # Bot identity survives token rotation; never send the credential to Postgres.
    identity = token.partition(":")[0]
    digest = hashlib.sha256(f"telegram-production-poller:{identity}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


class ProductionPollingLease:
    """No tables, writes, TTL, reconnect, or shared storage connection.

    Session loss releases PostgreSQL's lock. A watchdog and a guard at TeleBot's
    HTTP boundary terminate the entire process on loss (including worker threads).
    An in-flight Telegram request cannot be recalled, but no new requests may
    begin once loss is detected.
    """

    def __init__(self, token, *, connect=None, fatal_exit=None, interval=1.0,
                 wait_on_busy=False):
        self.key = lease_key(token)
        self._connect = connect
        self._fatal_exit = fatal_exit if fatal_exit is not None else os._exit
        self._interval = interval
        self._wait_on_busy = wait_on_busy
        self._connection = None
        self._pid = None
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._request = None
        self._lost = False
        self._requests_stopped = threading.Event()

    def acquire(self):
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise PollingLeaseError("Production polling lease requires DATABASE_URL.")
        if self._connect is None:
            import psycopg
            connect = psycopg.connect
        else:
            connect = self._connect
        try:
            self._connection = connect(
                url, autocommit=True, connect_timeout=10,
                application_name="telegram-polling-lease",
                options="-c statement_timeout=5000",
                keepalives=1, keepalives_idle=5, keepalives_interval=1,
                keepalives_count=3, tcp_user_timeout=5000,
            )
            row = self._connection.execute(
                "SELECT pg_try_advisory_lock(%s), pg_backend_pid()", (self.key,),
            ).fetchone()
            if not row or not row[0]:
                raise PollingLeaseBusy(
                    "Production polling lease is already owned by another instance; "
                    "this instance will not contact Telegram."
                )
            self._pid = row[1]
            self._verify()
        except BaseException as exc:
            if isinstance(exc, PollingLeaseBusy):
                # No lease or Telegram guard was acquired. Close this failed
                # attempt without poisoning the replacement's shutdown flags.
                self._connection.close()
                self._connection = None
                raise
            self.close()
            if isinstance(exc, (PollingLeaseError, SystemExit, KeyboardInterrupt)):
                raise
            raise PollingLeaseError(
                "Could not acquire production polling lease; Telegram startup is disabled."
            ) from None
        logging.info("Production polling lease acquired.")

    def _verify(self):
        if self._connection is None or self._connection.closed:
            raise PollingLeaseError("Production polling lease connection is closed.")
        # Check both the backend and its actual lock, not just connection health.
        # A transaction-pooling URL is unsuitable for a session-scoped lease.
        row = self._connection.execute(
            """SELECT pg_backend_pid() = %s AND EXISTS (
                SELECT 1 FROM pg_locks
                WHERE locktype = 'advisory' AND pid = pg_backend_pid()
                  AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
                  AND classid = %s::oid AND objid = %s::oid
                  AND objsubid = 1 AND granted
            )""",
            (self._pid, self.key >> 32, self.key & 0xffffffff),
        ).fetchone()
        if not row or not row[0]:
            raise PollingLeaseError("Production polling lease ownership was lost.")

    def assert_owned(self):
        with self._mutex:
            if self._stop.is_set() or self._lost:
                raise PollingLeaseError("Production polling lease is not active.")
            try:
                self._verify()
            except Exception:
                self._lost = True
                logging.critical(
                    "Production polling lease lost; terminating all Telegram workers."
                )
                # Do not run graceful cleanup that may send Telegram requests, or
                # reconnect. OS process exit closes every DB socket and worker.
                self._fatal_exit(1)
                raise PollingLeaseError("Production polling lease was lost.") from None

    def _watch(self):
        while not self._stop.wait(self._interval):
            try:
                self.assert_owned()
            except PollingLeaseError:
                return

    def __enter__(self):
        announced = False
        while True:
            try:
                self.acquire()
                break
            except PollingLeaseBusy:
                if not self._wait_on_busy:
                    raise
                if not announced:
                    logging.info(
                        "Replacement VM is in HTTP-only standby; waiting for "
                        "the previous production poller to release ownership."
                    )
                    announced = True
                if self._requests_stopped.wait(self._interval):
                    raise PollingLeaseError("Polling handoff cancelled during shutdown.")
        try:
            self._request = vars(apihelper._make_request).get(
                "_lease_original_request", apihelper._make_request,
            )
            request = self._request

            def guarded_request(*args, **kwargs):
                if self._requests_stopped.is_set():
                    raise PollingLeaseError("Telegram requests are disabled during shutdown.")
                self.assert_owned()
                if self._requests_stopped.is_set():
                    raise PollingLeaseError("Telegram requests are disabled during shutdown.")
                return request(*args, **kwargs)

            guarded_request._lease_original_request = request
            apihelper._make_request = guarded_request
            self._thread = threading.Thread(
                target=self._watch, name="production-polling-lease", daemon=True,
            )
            self._thread.start()
        except BaseException:
            self.close()
            raise
        return self

    def begin_shutdown(self):
        """Block new Telegram IO without releasing ownership or its watchdog."""
        self._requests_stopped.set()

    def close(self):
        self.begin_shutdown()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=12)
        with self._mutex:
            if self._request is not None:
                # Keep the closed guard installed until process exit. A worker
                # whose stop/join timed out must not send after we release the
                # session. A later explicit lease can install a new guard.
                self._request = None
            if self._connection is not None:
                try:
                    # Closing the dedicated session releases the lock even when
                    # startup fails; no document storage is involved.
                    self._connection.close()
                except Exception:
                    logging.error("Could not close production polling lease connection.")
                finally:
                    self._connection = None

    def __exit__(self, exc_type, exc, traceback):
        self.close()
