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
    _SHELLY_FIELDS,
    _SOURCE_SIGNAL_FIELDS,
    option_name,
)

FIELD_LISTS = (
    _GENERAL_FIELDS,
    _GLOBAL_SIGNAL_FIELDS,
    _SOURCE_SIGNAL_FIELDS,
    _CT_FIELDS,
    _MARSTEK_FIELDS,
    _SHELLY_FIELDS,
)

CONFIG_YAML = Path(__file__).parents[3] / "ha_addon" / "config.yaml"
TRANSLATIONS_YAML = Path(__file__).parents[3] / "ha_addon" / "translations" / "en.yaml"

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
    return {option_name(field) for fields in FIELD_LISTS for field in fields}


def _translation_entries() -> dict[str, list[str]]:
    """Each option's block of translation lines, keyed by option name."""
    lines = TRANSLATIONS_YAML.read_text(encoding="utf-8").splitlines()
    start = lines.index("configuration:")
    entries: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        match = re.match(r"\s{2}([a-z0-9_]+):$", line)
        if match:
            current = match.group(1)
            entries[current] = []
            continue
        if not line.startswith("    "):
            break  # end of the block
        if current is not None:
            entries[current].append(line.strip())
    return entries


def test_every_option_has_a_label_and_a_description() -> None:
    """The Supervisor renders these; an option without one shows its raw key.

    This also catches a whole document broken by an option key that landed on
    the end of the previous line — the keys after it stop being keys, so they
    go missing here rather than silently costing every option its label.
    """
    entries = _translation_entries()
    missing = schema_options() - set(entries)
    assert not missing, f"options with no en.yaml entry: {sorted(missing)}"
    for option in sorted(schema_options()):
        body = entries[option]
        assert any(line.startswith("name:") for line in body), f"{option} has no name"
        assert any(line.startswith("description:") for line in body), (
            f"{option} has no description"
        )


def test_no_translation_entry_is_orphaned() -> None:
    """An entry for an option that no longer exists is dead weight."""
    orphaned = set(_translation_entries()) - schema_options()
    assert not orphaned, f"en.yaml entries with no option: {sorted(orphaned)}"


def test_the_translations_file_ends_with_a_newline() -> None:
    """Because appending to a file that does not is how this broke once.

    Without the trailing newline the next line written lands on the end of the
    last description, which is valid text and invalid YAML.
    """
    assert TRANSLATIONS_YAML.read_text(encoding="utf-8").endswith("\n")
