import logging
import sys


class _AutoExcInfoFilter(logging.Filter):
    """Attach the active traceback to WARNING+ records logged from except blocks.

    When a log call happens while ``sys.exc_info()`` is set (i.e. inside an
    ``except`` block) and the caller did not pass ``exc_info`` explicitly, we
    attach the current exception so the traceback is emitted. Call sites that
    want the old terse behavior can opt out by passing ``exc_info=False``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is None and record.levelno >= logging.WARNING:
            exc = sys.exc_info()
            if exc[0] is not None:
                record.exc_info = exc
        return True


def setLogLevel(inLevel: str):
    level = levels.get(inLevel.lower())
    if level is None:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    _install_auto_exc_info_filter()


def _install_auto_exc_info_filter() -> None:
    """Attach the auto-exc-info filter to every root handler exactly once."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _AutoExcInfoFilter) for f in handler.filters):
            handler.addFilter(_AutoExcInfoFilter())


levels = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

logger = logging.getLogger("astrameter")
