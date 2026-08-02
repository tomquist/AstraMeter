"""The option table must match the add-on manifest exactly.

An option that exists in the Home Assistant UI but is missing from
:data:`OPTION_MAP` silently does nothing — the user sets it, the service never
sees it, and nothing anywhere says so.  These tests turn that into a failure at
the point the manifest changes.
"""

from __future__ import annotations

import os
import re

from .options import HANDLED_SEPARATELY, OPTION_MAP, is_set, render

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
CONFIG_YAML = os.path.join(_REPO_ROOT, "ha_addon", "config.yaml")

_ENTRY = re.compile(r"^  (?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<value>.*?)\s*$")


def _scalar(raw: str) -> object:
    """The Python value a YAML scalar denotes, for the subset used here."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    return raw


def read_block(block: str, path: str = CONFIG_YAML) -> dict[str, object]:
    """The ``key: value`` pairs directly under a top-level *block*.

    Both blocks this needs (``options:`` and ``schema:``) are flat maps, so a
    few lines of parsing beat adding a YAML dependency the runtime does not
    otherwise need.
    """
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")

    out: dict[str, object] = {}
    inside = False
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if not line[0].isspace():
            inside = line.rstrip() == f"{block}:"
            continue
        if not inside:
            continue
        match = _ENTRY.match(line)
        # Loudly, rather than dropping it: a silently unparsed entry would
        # make the drift test below pass for an option nothing reads.
        assert match, (
            f"{path}: cannot parse {line!r} in the '{block}:' block. "
            "read_block only understands flat 'key: value' entries."
        )
        out[match["key"]] = _scalar(match["value"])
    return out


def addon_defaults() -> dict[str, object]:
    """What the Supervisor merges into a fresh install's options.json."""
    return read_block("options")


def test_config_yaml_parses():
    schema = read_block("schema")
    assert "power_input_alias" in schema
    assert "custom_config" in schema
    # Nothing from a sibling block leaked in.
    assert "12345/udp" not in schema
    assert "slug" not in schema

    defaults = addon_defaults()
    assert defaults["device_types"] == "shellypro3em"
    assert defaults["wait_for_next_message"] is True
    assert defaults["throttle_interval"] == 0
    assert defaults["grid_predict_trust"] == 0.5


def test_every_addon_option_is_accounted_for():
    """No option in the add-on UI may be silently ignored."""
    mapped = {entry.option for entry in OPTION_MAP}
    known = mapped | HANDLED_SEPARATELY
    missing = sorted(set(read_block("schema")) - known)
    assert not missing, (
        f"ha_addon/config.yaml options with no effect: {missing}. "
        "Add them to OPTION_MAP, or to HANDLED_SEPARATELY if the generator "
        "consumes them some other way."
    )


def test_no_stale_entries():
    """And nothing may claim to map an option the add-on no longer offers."""
    schema = set(read_block("schema"))
    mapped = {entry.option for entry in OPTION_MAP}
    assert not sorted(mapped - schema)
    assert not sorted(HANDLED_SEPARATELY - schema)


def test_option_map_has_no_duplicates():
    options = [entry.option for entry in OPTION_MAP]
    assert len(options) == len(set(options))
    assert not ({entry.option for entry in OPTION_MAP} & HANDLED_SEPARATELY)


def test_is_set_matches_bashio_has_value():
    # bashio::config.has_value treats only null and "" as unset — a zero or a
    # false is a value the user chose and must still be written.
    assert not is_set(None)
    assert not is_set("")
    assert is_set(0)
    assert is_set(False)
    assert is_set("x")


def test_render_uses_yaml_spelling_for_booleans():
    assert render(True) == "true"
    assert render(False) == "false"
    assert render(0.5) == "0.5"
    # Multi-line paste into a text field must not break the INI file.
    assert render("1.0\r\n") == "1.0"
