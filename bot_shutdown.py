"""Graceful process shutdown for Telegram polling and background services."""

import logging
import signal


def install_shutdown_handlers(bot=None, before_stop=None):
    """Terminate on VM stop/restart, allowing the caller's finally to run."""
    previous = {}

    def shutdown(signum, frame):
        logging.info("Shutdown signal received (%s); stopping Telegram polling.", signum)
        if before_stop is not None:
            before_stop()
        if bot is not None:
            try:
                bot.stop_polling()
            except Exception:
                logging.exception("Could not stop polling during shutdown.")
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, shutdown)

    def restore():
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def stop_interrupted_polling(bot, exception):
    """Recover the SDK's local worker when an exception skips its teardown.

    pyTelegramBotAPI 4.37 keeps polling_thread in __threaded_polling's frame,
    not on the bot. SystemExit bypasses that method's normal stop/clear code.
    Only touch this bot's exact SDK frame, never unrelated named threads.
    """
    import telebot

    code = telebot.TeleBot._TeleBot__threaded_polling.__code__
    traceback = exception.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code is code and frame.f_locals.get("self") is bot:
            worker = frame.f_locals.get("polling_thread")
            if worker is not None:
                worker.stop()
                # stop() alone cannot wake a worker waiting after an exception.
                worker.clear_exceptions()
                worker.join()
        traceback = traceback.tb_next


def cleanup_services(services):
    """A failing service must not prevent the other services from stopping."""
    for name, stop in services:
        try:
            stop()
        except Exception:
            logging.exception("Could not stop %s during shutdown.", name)
