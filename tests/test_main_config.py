import configparser
from collections import OrderedDict
from io import StringIO


def test_main_config_parser_allows_percent_in_marstek_password():
    cfg = configparser.ConfigParser(dict_type=OrderedDict, interpolation=None)
    cfg.read_file(
        StringIO(
            """
[MARSTEK]
ENABLE = True
MAILBOX = user@example.com
PASSWORD = abc%def/123
""".strip()
        )
    )

    assert cfg.get("MARSTEK", "PASSWORD") == "abc%def/123"
