"""The add-on's advertised options and the backend that reads them must agree.

``ha_addon/config.yaml`` is what Home Assistant shows in the Configuration tab.
An option offered there but never read is a setting that silently does nothing,
and a mapping that names an option the schema does not have is a typo that can
never fire. Both directions are checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from astrameter.config.addon import (
    _CT_FIELDS,
    _GENERAL_FIELDS,
    _GLOBAL_SIGNAL_FIELDS,
    _MARSTEK_FIELDS,
    _SOURCE_SIGNAL_FIELDS,
)

CONFIG_YAML = Path(__file__).parents[3] / "ha_addon" / "config.yaml"

#: Options that do not map onto a settings field one-to-one: they select the
#: configuration source, or shape the power source / Marstek account in code.
HANDLED_IN_CODE = {
    "device_types",  # -> GeneralSettings.device_types (split on commas)
    "power_input_alias",  # -> the power source's entity ids
    "power_output_alias",  # -> ditto, and switches on calculated power
    "power_offset",  # -> SignalSettings.offsets (parsed list)
    "power_multiplier",  # -> SignalSettings.multipliers (parsed list)
    "marstek_auto_register_ct_device",  # -> MarstekSettings.enable
    "mqtt_uri",  # -> MqttInsightsConfig, instead of HA's own broker
    "custom_config",  # -> hands over to the config-file backend entirely
    "log_level",  # -> read in main() before the logger is configured
}


def schema_options() -> set[str]:
    """Option names from the add-on's ``schema:`` block.

    Hand-parsed rather than via a YAML dependency: the block is flat, one
    ``name: type`` entry per line.
    """
    lines = CONFIG_YAML.read_text(encoding="utf-8").splitlines()
    start = lines.index("schema:")
    options = set()
    for line in lines[start + 1 :]:
        if not line.startswith("  ") or not line.strip():
            break  # end of the block
        match = re.match(r"\s{2}([a-z0-9_]+):", line)
        assert match, f"unexpected schema line: {line!r}"
        options.add(match.group(1))
    return options


def mapped_options() -> set[str]:
    maps = (
        _GENERAL_FIELDS,
        _GLOBAL_SIGNAL_FIELDS,
        _SOURCE_SIGNAL_FIELDS,
        _CT_FIELDS,
        _MARSTEK_FIELDS,
    )
    return {option for field_map in maps for option in field_map.values()}


def test_the_schema_block_was_parsed():
    options = schema_options()
    assert "power_input_alias" in options
    assert "import_trim_w" in options
    assert len(options) > 40


def test_every_offered_option_is_consumed():
    """An option in the add-on UI that nothing reads would silently do nothing."""
    ignored = schema_options() - mapped_options() - HANDLED_IN_CODE
    assert not ignored, f"add-on options nothing reads: {sorted(ignored)}"


def test_every_mapped_option_exists_in_the_schema():
    """A mapping naming an option the add-on does not offer can never fire."""
    unknown = mapped_options() - schema_options()
    assert not unknown, f"options mapped but not offered: {sorted(unknown)}"


def test_no_stale_entries_in_the_handled_in_code_list():
    stale = HANDLED_IN_CODE - schema_options()
    assert not stale, f"options no longer offered: {sorted(stale)}"


def test_an_option_is_read_by_exactly_one_mapping():
    seen: dict[str, int] = {}
    for field_map in (
        _GENERAL_FIELDS,
        _GLOBAL_SIGNAL_FIELDS,
        _SOURCE_SIGNAL_FIELDS,
        _CT_FIELDS,
        _MARSTEK_FIELDS,
    ):
        for option in field_map.values():
            seen[option] = seen.get(option, 0) + 1
    duplicates = {option for option, count in seen.items() if count > 1}
    assert not duplicates, f"options read by more than one mapping: {duplicates}"
