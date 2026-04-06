import importlib
import io
import logging
import re
from unittest.mock import patch

import pytest

from astrameter.config.logger import setLogLevel

logger_module = importlib.import_module("astrameter.config.logger")


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
