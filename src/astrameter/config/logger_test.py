import importlib
import io
import logging
import logging.handlers
import re
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from astrameter.config.logger import redact_secrets, setLogLevel

logger_module = importlib.import_module("astrameter.config.logger")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # URI userinfo (e.g. a custom MQTT broker URL).
        (
            "Connecting to mqtt://alice:s3cret@broker.example.com:1883",
            "Connecting to mqtt://***:***@broker.example.com:1883",
        ),
        # Inline key=value / key: value.
        ("PASSWORD=hunter2", "PASSWORD=***"),
        ("marstek password: hunter2 done", "marstek password: *** done"),
        ("access_token=abc.def.ghi tail", "access_token=*** tail"),
        # JSON-ish payloads (e.g. an echoed config or API response).
        (
            '{"username": "addons", "password": "xxx"}',
            '{"username": "***", "password": "***"}',
        ),
        ('{"marstek_mailbox": "me@example.com"}', '{"marstek_mailbox": "***"}'),
    ],
)
def test_redact_secrets_masks_credentials(raw: str, expected: str) -> None:
    assert redact_secrets(raw) == expected


@pytest.mark.parametrize(
    "benign",
    [
        "auth required, sending token",
        "Envoy: obtained new JWT token from Enlighten cloud",
        "Connected to MQTT broker core-mosquitto:1883",
        "CT002 consumer 60323bd11234 phase detected: A",
    ],
)
def test_redact_secrets_leaves_benign_messages_untouched(benign: str) -> None:
    assert redact_secrets(benign) == benign


@pytest.mark.parametrize(
    ("level_name", "expected_level"),
    [("info", logging.INFO), ("debug", logging.DEBUG), ("invalid", logging.WARNING)],
)
def test_set_log_level_configures_expected_level(
    level_name: str, expected_level: int
) -> None:
    with patch.object(logger_module.logging, "basicConfig") as basic_config:
        setLogLevel(level_name)

    basic_config.assert_called_once()
    assert basic_config.call_args.kwargs["level"] == expected_level


def test_set_log_level_configures_timestamped_log_output() -> None:
    with patch.object(logger_module.logging, "basicConfig") as basic_config:
        setLogLevel("info")

    basic_config.assert_called_once()
    kwargs = basic_config.call_args.kwargs

    formatter = logging.Formatter(kwargs["format"], kwargs["datefmt"])
    record = logging.LogRecord(
        name="astrameter",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    record.created = 0

    formatted = formatter.format(record)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO:astrameter:hello",
        formatted,
    )
    assert kwargs["force"] is True


@pytest.mark.parametrize(
    ("level_name", "expected"),
    [("debug", True), ("info", False), ("warning", False)],
)
def test_debug_traceback_reflects_log_level(level_name: str, expected: bool) -> None:
    setLogLevel(level_name)
    assert logger_module.debug_traceback() is expected


def test_warning_inside_except_block_includes_traceback() -> None:
    setLogLevel("warning")
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    # Copy the auto-exc-info filter from the root stream handler installed by
    # setLogLevel so this test handler sees the same behavior.
    for existing in root.handlers:
        for flt in existing.filters:
            handler.addFilter(flt)
    root.addHandler(handler)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            logging.getLogger("astrameter.test").warning("failed: %s", exc)
    finally:
        root.removeHandler(handler)

    output = buffer.getvalue()
    assert "WARNING:failed: boom" in output
    assert "Traceback (most recent call last):" in output
    assert "RuntimeError: boom" in output


def test_warning_outside_except_block_has_no_traceback() -> None:
    setLogLevel("warning")
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    for existing in root.handlers:
        for flt in existing.filters:
            handler.addFilter(flt)
    root.addHandler(handler)
    try:
        logging.getLogger("astrameter.test").warning("plain warning")
    finally:
        root.removeHandler(handler)

    output = buffer.getvalue()
    assert "WARNING:plain warning" in output
    assert "Traceback" not in output


def test_exc_info_false_opts_out_of_auto_traceback() -> None:
    setLogLevel("warning")
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    for existing in root.handlers:
        for flt in existing.filters:
            handler.addFilter(flt)
    root.addHandler(handler)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            logging.getLogger("astrameter.test").warning(
                "suppressed: %s", exc, exc_info=False
            )
    finally:
        root.removeHandler(handler)

    output = buffer.getvalue()
    assert "WARNING:suppressed: boom" in output
    assert "Traceback" not in output


def test_set_log_level_installs_redacting_formatter_on_root_handlers() -> None:
    setLogLevel("debug")
    root = logging.getLogger()
    assert root.handlers
    for handler in root.handlers:
        assert isinstance(handler.formatter, logger_module._RedactingFormatter)


def test_root_logger_redacts_secrets_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setLogLevel("info")
    logging.getLogger("astrameter.test").info(
        "broker mqtt://alice:s3cret@example.com PASSWORD=hunter2"
    )
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "s3cret" not in combined
    assert "hunter2" not in combined
    assert "mqtt://***:***@example.com" in combined
    assert "PASSWORD=***" in combined


def test_redaction_covers_traceback_text() -> None:
    setLogLevel("warning")
    root = logging.getLogger()
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logger_module._RedactingFormatter("%(levelname)s:%(message)s"))
    root.addHandler(handler)
    try:
        try:
            raise RuntimeError("login failed for mqtt://bob:topsecret@host")
        except RuntimeError:
            logging.getLogger("astrameter.test").warning(
                "connect failed", exc_info=True
            )
    finally:
        root.removeHandler(handler)

    output = buffer.getvalue()
    assert "topsecret" not in output
    assert "mqtt://***:***@host" in output


# ── The optional log file ────────────────────────────────────────────────────


@pytest.fixture
def no_log_file() -> Iterator[None]:
    """Leave the root logger without a file handler, whatever a test did."""
    yield
    logger_module.set_log_file("")


def _log_file_handler() -> logging.handlers.RotatingFileHandler:
    handler = logger_module.log_file_handler()
    assert handler is not None
    return handler


def test_set_log_file_adds_a_rotating_file_handler(
    tmp_path: Path, no_log_file: None
) -> None:
    setLogLevel("info")
    path = tmp_path / "astrameter.log"

    logger_module.set_log_file(str(path))

    handler = _log_file_handler()
    assert handler.baseFilename == str(path)
    assert handler.maxBytes == logger_module.LOG_FILE_MAX_BYTES
    assert handler.backupCount == logger_module.LOG_FILE_BACKUPS
    assert handler.maxBytes > 0 and handler.backupCount > 0
    # The console keeps logging too: the file is in addition, not instead.
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logging.getLogger().handlers
    )


def test_log_file_redacts_secrets_and_attaches_tracebacks(
    tmp_path: Path, no_log_file: None
) -> None:
    """Whatever protects the console protects the file: a secret masked on
    stdout must not land in a file that gets attached to a bug report."""
    setLogLevel("info")
    path = tmp_path / "astrameter.log"
    logger_module.set_log_file(str(path))

    log = logging.getLogger("astrameter.test")
    log.info("broker mqtt://alice:s3cret@example.com PASSWORD=hunter2")
    try:
        raise RuntimeError("boom for mqtt://bob:topsecret@host")
    except RuntimeError as exc:
        log.warning("failed: %s", exc)

    text = path.read_text(encoding="utf-8")
    assert "s3cret" not in text and "hunter2" not in text
    assert "mqtt://***:***@example.com PASSWORD=***" in text
    assert "Traceback (most recent call last):" in text
    assert "topsecret" not in text
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO:", text, re.M)


def test_set_log_file_is_idempotent_and_follows_the_setting(
    tmp_path: Path, no_log_file: None
) -> None:
    """Run from the config-restart path, so it has to settle rather than add."""
    setLogLevel("info")
    first = tmp_path / "first.log"
    second = tmp_path / "second.log"

    logger_module.set_log_file(str(first))
    opened = _log_file_handler()
    logger_module.set_log_file(str(first))
    assert _log_file_handler() is opened, "an unchanged path keeps the open file"

    logger_module.set_log_file(str(second))
    assert _log_file_handler().baseFilename == str(second)
    assert (
        sum(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers)
        == 1
    )
    logging.getLogger("astrameter.test").info("after the switch")
    assert "after the switch" in second.read_text(encoding="utf-8")
    assert "after the switch" not in first.read_text(encoding="utf-8")

    logger_module.set_log_file("")
    assert logger_module.log_file_handler() is None


def test_unopenable_log_file_keeps_the_console_and_says_so(
    tmp_path: Path, no_log_file: None, capsys: pytest.CaptureFixture[str]
) -> None:
    setLogLevel("info")
    missing_dir = tmp_path / "missing" / "astrameter.log"

    logger_module.set_log_file(str(missing_dir))

    assert logger_module.log_file_handler() is None
    captured = capsys.readouterr()
    assert "Cannot open log file" in captured.out + captured.err
    assert "console only" in captured.out + captured.err
