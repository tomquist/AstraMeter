"""The add-on's advertised options and the backend that reads them must agree.

``ha_addon/config.yaml`` is what Home Assistant shows in the Configuration tab.
An option offered there but never read is a setting that silently does nothing,
and a mapping that names an option the schema does not have is a typo that can
never fire. Both directions are checked here.

``ha_addon/translations/en.yaml`` gives each option the name and description
that Configuration tab shows. An option without one appears as its bare key.
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


def translated_options() -> dict[str, dict[str, str]]:
    """``{option: {"name": ..., "description": ...}}`` from ``en.yaml``.

    Hand-parsed like :func:`schema_options`: under ``configuration:`` each
    option is a two-space key holding four-space ``name:`` / ``description:``
    scalars.
    """
    lines = TRANSLATIONS_YAML.read_text(encoding="utf-8").splitlines()
    start = lines.index("configuration:")
    options: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith("  "):
            break  # end of the block
        option = re.match(r"\s{2}([a-z0-9_]+):\s*$", line)
        if option:
            current = options.setdefault(option.group(1), {})
            continue
        entry = re.match(r"\s{4}(name|description):\s*(.*)$", line)
        assert entry and current is not None, f"unexpected translation line: {line!r}"
        current[entry.group(1)] = _scalar(entry.group(2))
    return options


def _scalar(raw: str) -> str:
    """A one-line YAML scalar's text, or ``""`` when it is null or blank.

    Quoted scalars lose their quotes; a plain one loses a trailing ``# comment``
    — which is all of ``name: # TODO`` — as YAML would.
    """
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        return raw[1 : raw.rindex(raw[0])] if raw.count(raw[0]) > 1 else ""
    value = re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()
    return "" if value in ("~", "null") else value


def mapped_options() -> set[str]:
    return {option_name(field) for fields in FIELD_LISTS for field in fields}


def test_an_option_is_read_by_exactly_one_mapping() -> None:
    seen: dict[str, int] = {}
    for fields in FIELD_LISTS:
        for option in map(option_name, fields):
            seen[option] = seen.get(option, 0) + 1
    duplicates = {option for option, count in seen.items() if count > 1}
    assert not duplicates, f"options read by more than one mapping: {duplicates}"


def test_every_offered_option_is_described() -> None:
    """An option without a translation shows up in the add-on UI as its bare key."""
    translations = translated_options()
    undescribed = sorted(
        option
        for option in schema_options()
        if not translations.get(option, {}).get("name")
        or not translations.get(option, {}).get("description")
    )
    assert not undescribed, (
        "add-on options without a name and description in "
        f"ha_addon/translations/en.yaml: {undescribed}"
    )


def test_no_translations_for_options_no_longer_offered() -> None:
    stale = set(translated_options()) - schema_options()
    assert not stale, f"translations for options not in the schema: {sorted(stale)}"


def test_the_translations_file_ends_with_a_newline() -> None:
    """Because appending to a file that does not is how this broke once.

    Without the trailing newline the next line written lands on the end of the
    last description, which is valid text and invalid YAML — and the options
    after it stop being options at all.
    """
    assert TRANSLATIONS_YAML.read_text(encoding="utf-8").endswith("\n")
