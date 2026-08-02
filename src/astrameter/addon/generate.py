"""Turn Home Assistant add-on options into a ``config.ini``.

Pure and side-effect free: options in, INI text out.  That is what makes it
testable, which is the whole reason this moved out of ``run.sh`` — the shell
version once shipped a bug that stopped the add-on from starting at all
(#510) and nothing could have caught it.
"""

from __future__ import annotations

from .options import (
    CT,
    GENERAL,
    HOMEASSISTANT,
    HOMEASSISTANT_HEAD,
    OPTION_MAP,
    is_set,
    render,
)

MARSTEK_BASE_URL = "https://eu.hamedata.com"
MARSTEK_TIMEZONE = "Europe/Berlin"


def ct_sections(device_types: str) -> list[str]:
    """Which ``[CT00x]`` sections this install needs.

    Both are emitted when the user emulates both, so each gets its own copy of
    the CT settings.  A Shelly-only install still gets a ``[CT002]`` section:
    it is inert there, and writing it keeps the CT settings from vanishing
    when someone flips ``device_types`` back.
    """
    wanted = device_types.lower()
    has_ct002 = "ct002" in wanted
    has_ct003 = "ct003" in wanted
    if has_ct002 and has_ct003:
        return ["CT002", "CT003"]
    if has_ct003:
        return ["CT003"]
    return ["CT002"]


def _pairs(options: dict, section: str) -> list[tuple[str, str]]:
    """Every mapped key for *section* the user has actually set."""
    out: list[tuple[str, str]] = []
    for entry in OPTION_MAP:
        if entry.section != section:
            continue
        value = options.get(entry.option)
        if entry.always or is_set(value):
            out.append((entry.key, render(value if value is not None else "")))
    return out


def _grid_source(options: dict) -> list[tuple[str, str]]:
    """The grid-power keys, which differ for one sensor vs two.

    A separate export sensor switches the meter into calculate mode; with a
    single signed sensor the entity is read directly.
    """
    if is_set(options.get("power_output_alias")):
        return [
            ("POWER_CALCULATE", "True"),
            ("POWER_INPUT_ALIAS", render(options.get("power_input_alias", ""))),
            ("POWER_OUTPUT_ALIAS", render(options["power_output_alias"])),
        ]
    return [
        ("POWER_CALCULATE", "False"),
        ("CURRENT_POWER_ENTITY", render(options.get("power_input_alias", ""))),
    ]


def _marstek(options: dict) -> list[tuple[str, str]]:
    """The one-time Marstek registration section, when fully configured.

    All three of the flag, the mailbox and the password are required: a
    half-filled account only produces a warning at startup.
    """
    enabled = options.get("marstek_auto_register_ct_device") is True or (
        str(options.get("marstek_auto_register_ct_device", "")).lower() == "true"
    )
    if not enabled:
        return []
    if not is_set(options.get("marstek_mailbox")) or not is_set(
        options.get("marstek_password")
    ):
        return []
    return [
        ("ENABLE", "True"),
        ("BASE_URL", MARSTEK_BASE_URL),
        ("MAILBOX", render(options["marstek_mailbox"])),
        ("PASSWORD", render(options["marstek_password"])),
        ("TIMEZONE", MARSTEK_TIMEZONE),
    ]


def _mqtt_insights(
    options: dict,
    mqtt_service: dict | None,
    addon_slug: str | None,
) -> list[tuple[str, str]]:
    """MQTT Insights, from an explicit URL or the Supervisor's own broker."""
    pairs: list[tuple[str, str]] = []
    if is_set(options.get("mqtt_uri")):
        pairs.append(("URI", render(options["mqtt_uri"])))
    elif mqtt_service:
        pairs.extend(
            [
                ("BROKER", render(mqtt_service.get("host", ""))),
                ("PORT", render(mqtt_service.get("port", ""))),
                ("USERNAME", render(mqtt_service.get("username", ""))),
                ("PASSWORD", render(mqtt_service.get("password", ""))),
                ("TLS", render(mqtt_service.get("ssl", False))),
            ]
        )
    else:
        return []
    pairs.append(("HA_DISCOVERY", "True"))
    if addon_slug:
        # Lets discovered meter devices link back to the add-on via_device.
        pairs.append(("ADDON_SLUG", addon_slug))
    return pairs


def _render_section(name: str, pairs: list[tuple[str, str]]) -> list[str]:
    if not pairs:
        return []
    return [f"[{name}]", *(f"{key}={value}" for key, value in pairs), ""]


def generate_config(
    options: dict,
    *,
    mqtt_service: dict | None = None,
    addon_slug: str | None = None,
) -> str:
    """Build the whole ``config.ini`` from the add-on's merged options."""
    lines: list[str] = []

    general = _pairs(options, GENERAL)
    # The dashboard/health server is always on in the add-on; the watchdog
    # depends on it.
    general.append(("ENABLE_WEB_SERVER", "true"))
    lines += _render_section(GENERAL, general)

    ct_pairs = _pairs(options, CT)
    for section in ct_sections(render(options.get("device_types", ""))):
        lines += _render_section(section, ct_pairs)

    lines += _render_section("MARSTEK", _marstek(options))

    home_assistant = [
        # The add-on always talks to Home Assistant through the Supervisor
        # proxy, so these three are fixed rather than user-configurable.
        ("IP", "supervisor"),
        ("PORT", "80"),
        ("API_PATH_PREFIX", "/core"),
        *_pairs(options, HOMEASSISTANT_HEAD),
        *_grid_source(options),
        *_pairs(options, HOMEASSISTANT),
    ]
    lines += _render_section(HOMEASSISTANT, home_assistant)

    lines += _render_section(
        "MQTT_INSIGHTS", _mqtt_insights(options, mqtt_service, addon_slug)
    )

    return "\n".join(lines).rstrip("\n") + "\n"
