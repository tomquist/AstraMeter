import importlib
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
