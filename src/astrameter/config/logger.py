import logging
import re
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)s:%(name)s:%(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Patterns for credentials that must never reach the log, regardless of how the
# app is launched (Home Assistant add-on, plain Docker, CLI, ...). Redaction
# happens on the fully-rendered line — message *and* any traceback text — so a
# secret can't slip through via an exception repr either. The add-on's run.sh
# applies the same masking to bashio's own output, which never passes through
# this Python formatter.
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


def setLogLevel(inLevel: str):
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


levels = {
    "critical": logging.CRITICAL,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

logger = logging.getLogger("astrameter")
