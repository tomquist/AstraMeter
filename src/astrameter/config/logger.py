import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Size the optional log file so a DEBUG run lasts long enough to catch a
# steering problem — issue #655's reporter got ten minutes out of the add-on
# UI's 10,000-line download — without eating a small host's disk: at DEBUG
# a line is ~150 bytes and there are ~15 a second, so one file holds a good
# two hours and the set about a working day.
LOG_FILE_MAX_BYTES = 20 * 1024 * 1024
LOG_FILE_BACKUPS = 2

# Patterns for credentials that must never reach the log, regardless of how the
# app is launched (Home Assistant add-on, plain Docker, CLI, ...). Redaction
# happens on the fully-rendered line — message *and* any traceback text — so a
# secret can't slip through via an exception repr either. On the Home Assistant
# add-on this is the only masking layer — the add-on reads its own options and
# talks to the Supervisor from Python, so every log line goes through here.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credentials in a URI userinfo: scheme://user:pass@host -> scheme://***:***@host
    (
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"),
        r"\1***:***@",
    ),
    # JSON-ish "<...key...>": "<value>" for sensitive keys.
    (
        re.compile(
            r'("[A-Za-z0-9_]*'
            r'(?:password|passwd|secret|token|api[_-]?key|username|mailbox)"'
            r'\s*:\s*")[^"]*"',
            re.IGNORECASE,
        ),
        r'\1***"',
    ),
    # Inline key=value / key: value for sensitive keys (incl. prefixed names
    # such as access_token or marstek_password).
    (
        re.compile(
            r"([A-Za-z0-9_]*"
            r"(?:password|passwd|secret|token|api[_-]?key|username|mailbox))"
            r"(\s*[=:]\s*)\S+",
            re.IGNORECASE,
        ),
        r"\1\2***",
    ),
)


def redact_secrets(text: str) -> str:
    """Mask passwords, tokens and other credentials in a log line."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class _RedactingFormatter(logging.Formatter):
    """Formatter that strips credentials from the fully-rendered log line."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


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


def debug_traceback() -> bool:
    """Return ``True`` only when the logger is at DEBUG level.

    Use as ``exc_info=debug_traceback()`` on an ``except``-block log call to
    emit a one-line message at the normal level while still including the full
    traceback when the user runs with ``LOG_LEVEL = DEBUG``. Passing ``False``
    also opts the record out of the auto-exc-info filter above.
    """
    return logger.isEnabledFor(logging.DEBUG)


levels = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}


def setLogLevel(inLevel: str) -> None:
    level = levels.get(inLevel.lower())
    if level is None:
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format=_LOG_FORMAT,
        datefmt=_LOG_DATEFMT,
        force=True,
    )
    _install_redacting_formatter()
    _install_auto_exc_info_filter()


def _install_redacting_formatter() -> None:
    """Swap every root handler's formatter for one that masks credentials."""
    for handler in logging.getLogger().handlers:
        handler.setFormatter(_RedactingFormatter(_LOG_FORMAT, _LOG_DATEFMT))


def _install_auto_exc_info_filter() -> None:
    """Attach the auto-exc-info filter to every root handler exactly once."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _AutoExcInfoFilter) for f in handler.filters):
            handler.addFilter(_AutoExcInfoFilter())


class _LogFileHandler(RotatingFileHandler):
    """The file handler :func:`set_log_file` owns, told apart from any other."""


def log_file_handler() -> RotatingFileHandler | None:
    """The file the root logger currently also writes to, if any."""
    for handler in logging.getLogger().handlers:
        if isinstance(handler, _LogFileHandler):
            return handler
    return None


def set_log_file(path: str) -> None:
    """Also write the log to *path*; an empty *path* stops doing so.

    Called after :func:`setLogLevel` at start-up and again on every config
    restart with whatever ``LOG_FILE`` now says, so it settles the root logger
    rather than adding to it: an unchanged path keeps the open file, a new one
    replaces it, and an empty one takes the file handler away. The file takes
    the same redacting formatter and auto-exc-info filter as the console, so
    nothing masked on stdout reaches the file unmasked. A file that cannot be
    opened is reported and the log stays on the console — a bad path must not
    take the service down.
    """
    root = logging.getLogger()
    path = path.strip()
    current = log_file_handler()
    if current is not None:
        if path and current.baseFilename == os.path.abspath(path):
            return
        root.removeHandler(current)
        current.close()
        logger.info("Stopped logging to %s", current.baseFilename)
    if not path:
        return
    try:
        handler = _LogFileHandler(
            path,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(
            "Cannot open log file %s (%s); logging to the console only",
            path,
            exc,
            exc_info=False,
        )
        return
    root.addHandler(handler)
    _install_redacting_formatter()
    _install_auto_exc_info_filter()
    logger.info(
        "Also logging to %s (rotated at %d MB, %d older files kept)",
        handler.baseFilename,
        LOG_FILE_MAX_BYTES // (1024 * 1024),
        LOG_FILE_BACKUPS,
    )


logger = logging.getLogger("astrameter")
