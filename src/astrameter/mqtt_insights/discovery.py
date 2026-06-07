"""Pure functions that build HA MQTT Device Discovery payloads (HA 2024.11+)."""

from __future__ import annotations

import re

from astrameter import entity_model as em
from astrameter.version_info import get_git_commit_sha

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_id(value: str) -> str:
    return _SAFE_ID_RE.sub("_", value)


def _origin() -> dict:
    sha = get_git_commit_sha()
    return {
        "name": "astrameter",
        "sw_version": sha or "unknown",
        "support_url": "https://github.com/tomquist/astrameter",
    }


def _system_availability(base_topic: str) -> dict:
    return {
        "topic": f"{base_topic}/status",
        "payload_available": "online",
        "payload_not_available": "offline",
    }


# ── Render MQTT components from the shared entity_model table ──────────────
#
# The entity inventory and its metadata (platform, device_class, unit,
# state_class, entity_category, enum options, number bounds, display name) live
# in ``astrameter.entity_model`` — the single source of truth shared with the
# native Home Assistant integration. The helpers below add only the MQTT
# plumbing (state/command topics, value_template, switch/binary payload
# conventions) so discovery never re-declares that metadata.


def _value_template(desc: em.EntityDescriptor, *, null_guard: bool = False) -> str:
    """Build a component's ``value_template`` from its descriptor field path."""
    expr = f"value_json.{desc.field}" if desc.field else "value_json"
    if desc.transform == "saturation_pct":
        return f"{{{{ ({expr} * 100) | round(1) }}}}"
    if null_guard:
        return f"{{{{ {expr} if {expr} is not none else '' }}}}"
    if desc.platform == em.NUMBER and desc.default is not None:
        return f"{{{{ {expr} | default({desc.default}) }}}}"
    return f"{{{{ {expr} }}}}"


def _render_component(
    desc: em.EntityDescriptor,
    *,
    uid_prefix: str,
    state_topic: str,
    null_guard: bool = False,
) -> dict:
    """Render one MQTT discovery component from an :class:`EntityDescriptor`.

    ``button`` entities are command-only (no state topic / template) and are
    built by their caller instead of here.
    """
    comp: dict = {
        "platform": desc.platform,
        "unique_id": f"{uid_prefix}_{desc.key}",
        "name": None if desc.primary else desc.name,
    }
    if desc.device_class:
        comp["device_class"] = desc.device_class
    if desc.state_class:
        comp["state_class"] = desc.state_class
    if desc.unit:
        comp["unit_of_measurement"] = desc.unit
    if desc.options is not None:
        comp["options"] = list(desc.options)
    if desc.platform == em.NUMBER:
        comp["min"] = desc.min
        comp["max"] = desc.max
        if desc.step is not None:
            comp["step"] = desc.step
        if desc.mode is not None:
            comp["mode"] = desc.mode
    comp["state_topic"] = state_topic
    comp["value_template"] = _value_template(desc, null_guard=null_guard)
    if desc.platform == em.SWITCH:
        comp["command_topic"] = f"{state_topic}/{desc.key}/set"
        comp["payload_on"] = "true"
        comp["payload_off"] = "false"
        comp["state_on"] = "True"
        comp["state_off"] = "False"
        comp["retain"] = True
    elif desc.platform == em.NUMBER:
        comp["command_topic"] = f"{state_topic}/{desc.key}/set"
        comp["retain"] = True
    elif desc.platform == em.BINARY_SENSOR:
        comp["payload_on"] = "True"
        comp["payload_off"] = "False"
    if desc.entity_category:
        comp["entity_category"] = desc.entity_category
    return comp


# ── CT002 consumer (per-battery) ──────────────────────────────────────────


def build_ct002_consumer_discovery(
    base_topic: str,
    device_id: str,
    consumer_id: str,
    ha_prefix: str,
    device_type: str = "",
    network_mac: str = "",
    battery_ip: str = "",
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    safe_cid = _sanitize_id(consumer_id)
    node_id = f"astrameter_ct002_{safe_dev}_{safe_cid}"
    state_topic = f"{base_topic}/ct002/{device_id}/consumer/{consumer_id}"
    avail_topic = f"{state_topic}/availability"
    uid_prefix = f"astrameter_ct002_{safe_dev}_{safe_cid}"
    meter_identifier = f"astrameter_ct002_{safe_dev}"

    # Sensors/controls come from the shared entity table. Controllable entities
    # (number/switch) each get their own command topic with ``retain: true``;
    # Home Assistant publishes the set-command retained, so on an AstraMeter
    # restart the broker redelivers it as soon as we re-subscribe and the value
    # restores itself — no local state store needed (a dedicated topic per
    # setting is required because a broker keeps only the last retained message
    # per topic). ``min_dc_output`` is gated by its presence predicate so only
    # external-inverter battery types (B2500 family) get it.
    components: dict[str, dict] = {}
    for desc in em.CT002_CONSUMER_ENTITIES:
        if not desc.present_for(device_type):
            continue
        components[desc.key] = _render_component(
            desc, uid_prefix=uid_prefix, state_topic=state_topic
        )

    # Identity string-sensors: MQTT publishes these as diagnostic sensors, while
    # the native integration promotes them to device-registry attributes — so
    # they live outside the shared entity table.
    for key, label in [
        ("device_type", "Device Type"),
        ("battery_ip", "Battery IP"),
        ("ct_type", "CT Type"),
        ("ct_mac", "CT MAC"),
    ]:
        components[key] = {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_{key}",
            "name": label,
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "entity_category": "diagnostic",
        }

    mac_slug = _sanitize_id(consumer_id).lower().replace("-", "").replace("_", "")

    device_info: dict = {
        "identifiers": [f"astrameter_consumer_{mac_slug}"],
        "name": f"AstraMeter Consumer {device_type} {mac_slug}"
        if device_type
        else f"AstraMeter Consumer {mac_slug}",
        "manufacturer": "Marstek",
        "via_device": meter_identifier,
    }
    connections: list[list[str]] = []
    if re.fullmatch(r"[0-9a-f]{12}", mac_slug):
        bt_mac = ":".join(
            mac_slug[i : i + 2] for i in range(0, len(mac_slug), 2)
        ).upper()
        connections.append(["bluetooth", bt_mac])
    if network_mac:
        connections.append(["mac", network_mac])
    if battery_ip:
        connections.append(["ip", battery_ip])
    if connections:
        device_info["connections"] = connections
    if device_type:
        device_info["model_id"] = device_type

    payload = {
        "device": device_info,
        "origin": _origin(),
        "components": components,
        "availability_mode": "all",
        "availability": [
            _system_availability(base_topic),
            {
                "topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


# ── Add-on hub device ─────────────────────────────────────────────────────


def build_addon_device_discovery(
    base_topic: str,
    addon_slug: str,
    ha_prefix: str,
) -> tuple[str, dict]:
    """Discovery for the top-level "AstraMeter" device.

    Its ``identifiers`` is the add-on slug so the ``via_device`` references on
    the per-meter devices resolve to a real, named MQTT device instead of an
    empty HA placeholder. (MQTT ``via_device`` only ever resolves within the
    MQTT identifier namespace, so it can't point at the Supervisor's own
    hassio add-on device — this is the MQTT-native stand-in for it.)

    Exposes a connectivity ``status`` sensor (driven by the system LWT topic),
    plus diagnostic ``version`` and ``consumer_count`` sensors fed from the
    retained ``{base}/bridge`` topic.
    """
    safe_slug = _sanitize_id(addon_slug)
    node_id = f"astrameter_addon_{safe_slug}"
    uid_prefix = f"astrameter_addon_{safe_slug}"
    bridge_topic = f"{base_topic}/bridge"

    components: dict[str, dict] = {
        # No availability block on purpose: this sensor IS the offline
        # indicator, so it must flip to "off" rather than going unavailable
        # when AstraMeter drops the LWT.
        "status": {
            "platform": "binary_sensor",
            "unique_id": f"{uid_prefix}_status",
            "name": "Status",
            "device_class": "connectivity",
            "state_topic": f"{base_topic}/status",
            "payload_on": "online",
            "payload_off": "offline",
            "entity_category": "diagnostic",
        },
        "version": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_version",
            "name": "Version",
            "state_topic": bridge_topic,
            "value_template": "{{ value_json.version }}",
            "entity_category": "diagnostic",
            "availability": [_system_availability(base_topic)],
        },
        "consumer_count": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_consumer_count",
            "name": "Consumer Count",
            "state_topic": bridge_topic,
            "value_template": "{{ value_json.consumer_count }}",
            "entity_category": "diagnostic",
            "availability": [_system_availability(base_topic)],
        },
    }

    payload = {
        "device": {
            "identifiers": addon_slug,
            "name": "AstraMeter",
            "manufacturer": "astrameter",
        },
        "origin": _origin(),
        "components": components,
        "state_topic": bridge_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


# ── CT002 device-level ────────────────────────────────────────────────────


def build_ct002_device_discovery(
    base_topic: str,
    device_id: str,
    ha_prefix: str,
    addon_slug: str | None = None,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_ct002_{safe_dev}"
    state_topic = f"{base_topic}/ct002/{device_id}/status"
    uid_prefix = f"astrameter_ct002_{safe_dev}"

    components: dict[str, dict] = {}
    for desc in em.CT002_DEVICE_ENTITIES:
        if desc.platform == em.BUTTON:
            # A button is command-only: it presses a JSON command onto the
            # device's ``/set`` topic (keyed by its setter), with no state.
            components[desc.key] = {
                "platform": "button",
                "unique_id": f"{uid_prefix}_{desc.key}",
                "name": desc.name,
                "command_topic": f"{base_topic}/ct002/{device_id}/set",
                "payload_press": f'{{"{desc.setter}": true}}',
                "entity_category": desc.entity_category,
            }
        else:
            components[desc.key] = _render_component(
                desc, uid_prefix=uid_prefix, state_topic=state_topic
            )

    device_info: dict = {
        "identifiers": node_id,
        "name": f"AstraMeter CT002 {device_id}",
        "manufacturer": "astrameter",
    }
    if addon_slug:
        device_info["via_device"] = addon_slug

    payload = {
        "device": device_info,
        "origin": _origin(),
        "components": components,
        "availability": [_system_availability(base_topic)],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


# ── Powermeter (grid power source) ────────────────────────────────────────


def build_powermeter_device_discovery(
    base_topic: str,
    pm_id: str,
    name: str,
    ha_prefix: str,
    addon_slug: str | None = None,
) -> tuple[str, dict]:
    """Discovery for a per-powermeter diagnostic device with an "Online" sensor.

    ``pm_id`` is the already-sanitized config section name; ``name`` is the raw
    section used as the device's display label. The sensor flips off when the
    powermeter stops delivering fresh/usable data (stale stream, disconnect, or
    — for pull meters — a failing read).
    """
    safe_pm = _sanitize_id(pm_id)
    node_id = f"astrameter_powermeter_{safe_pm}"
    uid_prefix = node_id
    state_topic = f"{base_topic}/powermeter/{pm_id}"

    # Power readings (per phase + total) plus the Online binary_sensor, from the
    # shared table. The power sensors are null-guarded: a ``null`` phase (e.g. a
    # single-phase meter has no L2/L3, or the meter is currently down) renders to
    # an empty string so Home Assistant leaves the entity untouched rather than
    # logging a parse error.
    components: dict[str, dict] = {}
    for desc in em.POWERMETER_ENTITIES:
        components[desc.key] = _render_component(
            desc,
            uid_prefix=uid_prefix,
            state_topic=state_topic,
            null_guard=desc.platform == em.SENSOR,
        )

    device_info: dict = {
        "identifiers": node_id,
        # Capital-Case the config section for a readable device label
        # (e.g. "SMA_ENERGY_METER" -> "Sma Energy Meter").
        "name": f"AstraMeter Powermeter {name.replace('_', ' ').title()}",
        "manufacturer": "astrameter",
    }
    if addon_slug:
        device_info["via_device"] = addon_slug

    payload = {
        "device": device_info,
        "origin": _origin(),
        "components": components,
        "availability": [_system_availability(base_topic)],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


# ── Shelly per-battery ────────────────────────────────────────────────────


def build_shelly_battery_discovery(
    base_topic: str,
    device_id: str,
    battery_ip: str,
    ha_prefix: str,
) -> tuple[str, dict]:
    ip_slug = _sanitize_id(battery_ip)
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_shelly_{safe_dev}_{ip_slug}"
    state_topic = f"{base_topic}/shelly/{device_id}/battery/{ip_slug}"
    avail_topic = f"{state_topic}/availability"
    uid_prefix = f"astrameter_shelly_{safe_dev}_{ip_slug}"

    components: dict[str, dict] = {
        desc.key: _render_component(
            desc, uid_prefix=uid_prefix, state_topic=state_topic
        )
        for desc in em.SHELLY_BATTERY_ENTITIES
    }

    payload = {
        "device": {
            "identifiers": node_id,
            "name": f"AstraMeter Shelly Battery {battery_ip}",
            "manufacturer": "astrameter",
            "via_device": f"astrameter_shelly_{safe_dev}",
        },
        "origin": _origin(),
        "components": components,
        "availability_mode": "all",
        "availability": [
            _system_availability(base_topic),
            {
                "topic": avail_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


# ── Shelly device-level ───────────────────────────────────────────────────


def build_shelly_device_discovery(
    base_topic: str,
    device_id: str,
    ha_prefix: str,
    addon_slug: str | None = None,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_shelly_{safe_dev}"
    state_topic = f"{base_topic}/shelly/{device_id}/status"
    uid_prefix = f"astrameter_shelly_{safe_dev}"

    components: dict[str, dict] = {
        desc.key: _render_component(
            desc, uid_prefix=uid_prefix, state_topic=state_topic
        )
        for desc in em.SHELLY_DEVICE_ENTITIES
    }

    device_info: dict = {
        "identifiers": node_id,
        "name": f"AstraMeter Shelly {device_id}",
        "manufacturer": "astrameter",
    }
    if addon_slug:
        device_info["via_device"] = addon_slug

    payload = {
        "device": device_info,
        "origin": _origin(),
        "components": components,
        "availability": [_system_availability(base_topic)],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload
