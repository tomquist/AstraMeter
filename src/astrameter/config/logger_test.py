import importlib
import io
import logging
import re
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
def test_redact_secrets_masks_credentials(raw, expected):
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
def test_redact_secrets_leaves_benign_messages_untouched(benign):
    assert redact_secrets(benign) == benign


@pytest.mark.parametrize(
    ("level_name", "expected_level"),
    [("info", logging.INFO), ("debug", logging.DEBUG), ("invalid", logging.WARNING)],
)
def test_set_log_level_configures_expected_level(level_name, expected_level):
    with patch.object(logger_module.logging, "basicConfig") as basic_config:
        setLogLevel(level_name)

    basic_config.assert_called_once()
    assert basic_config.call_args.kwargs["level"] == expected_level


def test_set_log_level_configures_timestamped_log_output():
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
def test_debug_traceback_reflects_log_level(level_name, expected):
    setLogLevel(level_name)
    assert logger_module.debug_traceback() is expected


def test_warning_inside_except_block_includes_traceback():
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


def test_warning_outside_except_block_has_no_traceback():
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


def test_exc_info_false_opts_out_of_auto_traceback():
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


def test_set_log_level_installs_redacting_formatter_on_root_handlers():
    setLogLevel("debug")
    root = logging.getLogger()
    assert root.handlers
    for handler in root.handlers:
        assert isinstance(handler.formatter, logger_module._RedactingFormatter)


def test_root_logger_redacts_secrets_end_to_end(capsys):
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


def test_redaction_covers_traceback_text():
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
