"""Pure functions that build HA MQTT Device Discovery payloads (HA 2024.11+)."""

from __future__ import annotations

import re

from astrameter.ct002.balancer import _needs_dc_output_floor
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


# ── CT002 consumer (per-battery) ──────────────────────────────────────────


def build_ct002_consumer_discovery(
    base_topic: str,
    device_id: str,
    consumer_id: str,
    ha_prefix: str,
    device_type: str = "",
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    safe_cid = _sanitize_id(consumer_id)
    node_id = f"astrameter_ct002_{safe_dev}_{safe_cid}"
    state_topic = f"{base_topic}/ct002/{device_id}/consumer/{consumer_id}"
    avail_topic = f"{state_topic}/availability"
    uid_prefix = f"astrameter_ct002_{safe_dev}_{safe_cid}"
    meter_identifier = f"astrameter_ct002_{safe_dev}"

    components: dict[str, dict] = {}

    # Power sensors
    for key, label, tmpl in [
        ("grid_power_total", "Grid Power", "{{ value_json.grid_power.total }}"),
        ("grid_power_l1", "Grid Power L1", "{{ value_json.grid_power.l1 }}"),
        ("grid_power_l2", "Grid Power L2", "{{ value_json.grid_power.l2 }}"),
        ("grid_power_l3", "Grid Power L3", "{{ value_json.grid_power.l3 }}"),
        ("target_l1", "Target L1", "{{ value_json.target.l1 }}"),
        ("target_l2", "Target L2", "{{ value_json.target.l2 }}"),
        ("target_l3", "Target L3", "{{ value_json.target.l3 }}"),
        ("reported_power", "Reported Power", "{{ value_json.reported_power }}"),
        ("last_target", "Last Target", "{{ value_json.last_target }}"),
    ]:
        comp: dict = {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_{key}",
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": tmpl,
        }
        if key == "grid_power_total":
            comp["name"] = None  # primary entity
        else:
            comp["name"] = label
        components[key] = comp

    # Saturation
    components["saturation"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_saturation",
        "name": "Saturation",
        "unit_of_measurement": "%",
        "state_topic": state_topic,
        "value_template": "{{ (value_json.saturation * 100) | round(1) }}",
    }

    # Phase sensor (enum)
    components["phase"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_phase",
        "name": "Phase",
        "device_class": "enum",
        "options": ["A", "B", "C"],
        "state_topic": state_topic,
        "value_template": "{{ value_json.phase }}",
        "entity_category": "diagnostic",
    }

    # Diagnostic sensors
    for key, label, tmpl in [
        ("device_type", "Device Type", "{{ value_json.device_type }}"),
        ("battery_ip", "Battery IP", "{{ value_json.battery_ip }}"),
        ("ct_type", "CT Type", "{{ value_json.ct_type }}"),
        ("ct_mac", "CT MAC", "{{ value_json.ct_mac }}"),
    ]:
        comp = {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_{key}",
            "name": label,
            "state_topic": state_topic,
            "value_template": tmpl,
            "entity_category": "diagnostic",
        }
        components[key] = comp

    # Last seen (timestamp)
    components["last_seen"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_last_seen",
        "name": "Last Seen",
        "device_class": "timestamp",
        "state_topic": state_topic,
        "value_template": "{{ value_json.last_seen }}",
        "entity_category": "diagnostic",
    }

    # Poll interval (EMA-smoothed seconds between consecutive polls)
    components["poll_interval"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_poll_interval",
        "name": "Poll Interval",
        "device_class": "duration",
        "unit_of_measurement": "s",
        "state_topic": state_topic,
        "value_template": "{{ value_json.poll_interval }}",
        "entity_category": "diagnostic",
    }

    # Per-consumer controllable entities each use their own command topic with
    # ``retain: true``.  Home Assistant then publishes the set-command retained,
    # so on an AstraMeter restart the broker redelivers it as soon as we
    # re-subscribe and the value restores itself — no local state store needed.
    # A dedicated topic per setting is required because a broker keeps only the
    # last retained message per topic.

    # Manual target number
    components["manual_target"] = {
        "platform": "number",
        "unique_id": f"{uid_prefix}_manual_target",
        "name": "Manual Target",
        "unit_of_measurement": "W",
        "device_class": "power",
        "min": -10000,
        "max": 10000,
        "mode": "box",
        "state_topic": state_topic,
        "value_template": "{{ value_json.manual_target | default(0) }}",
        "command_topic": f"{state_topic}/manual_target/set",
        "retain": True,
        "entity_category": "config",
    }

    # Auto target switch (on = automatic control, off = manual override)
    components["auto_target"] = {
        "platform": "switch",
        "unique_id": f"{uid_prefix}_auto_target",
        "name": "Auto Target",
        "state_topic": state_topic,
        "command_topic": f"{state_topic}/auto_target/set",
        "value_template": "{{ value_json.auto_target }}",
        "payload_on": "true",
        "payload_off": "false",
        "state_on": "True",
        "state_off": "False",
        "retain": True,
        "entity_category": "config",
    }

    # Active switch
    components["active"] = {
        "platform": "switch",
        "unique_id": f"{uid_prefix}_active",
        "name": "Active",
        "state_topic": state_topic,
        "command_topic": f"{state_topic}/active/set",
        "value_template": "{{ value_json.active }}",
        "payload_on": "true",
        "payload_off": "false",
        "state_on": "True",
        "state_off": "False",
        "retain": True,
    }

    # Distribution weight number — relative fair-share weight across batteries.
    # 1.0 is neutral; raise it on a larger battery (or lower it on a smaller
    # one) to bias the split, e.g. 1.5 vs 1.0 for a ~60:40 distribution.
    components["distribution_weight"] = {
        "platform": "number",
        "unique_id": f"{uid_prefix}_distribution_weight",
        "name": "Distribution Weight",
        "min": 0,
        "max": 10,
        "step": 0.1,
        "mode": "slider",
        "state_topic": state_topic,
        "value_template": "{{ value_json.distribution_weight | default(1.0) }}",
        "command_topic": f"{state_topic}/distribution_weight/set",
        "retain": True,
        "entity_category": "config",
    }

    # Min DC Output number — minimum discharge (W) to keep a DC battery's
    # external inverter from switching off at 0 W.  Only surfaced for batteries
    # where it has an effect (no built-in inverter, no AC input — the B2500
    # family); Venus/Jupiter/unknown types don't get this entity.
    if _needs_dc_output_floor(device_type):
        components["min_dc_output"] = {
            "platform": "number",
            "unique_id": f"{uid_prefix}_min_dc_output",
            "name": "Min DC Output",
            "unit_of_measurement": "W",
            "device_class": "power",
            "min": 0,
            "max": 1000,
            "step": 1,
            "mode": "box",
            "state_topic": state_topic,
            "value_template": "{{ value_json.min_dc_output | default(0) }}",
            "command_topic": f"{state_topic}/min_dc_output/set",
            "retain": True,
            "entity_category": "config",
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
    # No device ``connections`` are advertised: HA treats a connection as a
    # global cross-integration identity and merges devices that share one, so
    # advertising the battery's MAC folded this consumer into the battery device
    # (owned by e.g. hm2mqtt) depending on MQTT registration order (issue #438).
    # Identify solely via the namespaced ``identifiers`` + ``via_device``.
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

    components: dict[str, dict] = {
        "smooth_target": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_smooth_target",
            "name": None,  # primary
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": "{{ value_json.smooth_target }}",
        },
        # Active Control switch — on (default) computes per-battery targets; off
        # falls back to relay mode. The command is published retained so an "off"
        # choice survives an AstraMeter restart (the broker redelivers it on
        # reconnect, like the per-consumer settings).
        "active_control": {
            "platform": "switch",
            "unique_id": f"{uid_prefix}_active_control",
            "name": "Active Control",
            "state_topic": state_topic,
            "value_template": "{{ value_json.active_control }}",
            "command_topic": f"{base_topic}/ct002/{device_id}/set",
            "command_template": (
                '{"active_control": {{ "true" if value == "ON" else "false" }}}'
            ),
            "payload_on": "ON",
            "payload_off": "OFF",
            "state_on": "True",
            "state_off": "False",
            "retain": True,
            "entity_category": "config",
        },
        "consumer_count": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_consumer_count",
            "name": "Consumer Count",
            "state_topic": state_topic,
            "value_template": "{{ value_json.consumer_count }}",
            "entity_category": "diagnostic",
        },
        "force_rotation": {
            "platform": "button",
            "unique_id": f"{uid_prefix}_force_rotation",
            "name": "Force Rotation",
            "command_topic": f"{base_topic}/ct002/{device_id}/set",
            "payload_press": '{"force_rotation": true}',
            "entity_category": "config",
        },
    }

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

    components: dict[str, dict] = {}

    # Latest readings (per phase + total). ``grid_power_total`` is the device's
    # primary entity. A ``null`` phase (e.g. a single-phase meter has no L2/L3,
    # or the meter is currently down) renders to an empty string so Home
    # Assistant leaves the entity untouched rather than logging a parse error.
    for key, label, field in [
        ("grid_power_total", None, "total"),
        ("grid_power_l1", "Power L1", "l1"),
        ("grid_power_l2", "Power L2", "l2"),
        ("grid_power_l3", "Power L3", "l3"),
    ]:
        components[key] = {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_{key}",
            "name": label,
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": (
                f"{{{{ value_json.grid_power.{field} "
                f"if value_json.grid_power.{field} is not none else '' }}}}"
            ),
        }

    components["online"] = {
        "platform": "binary_sensor",
        "unique_id": f"{uid_prefix}_online",
        "name": "Online",
        "device_class": "connectivity",
        "state_topic": state_topic,
        "value_template": "{{ value_json.online }}",
        "payload_on": "True",
        "payload_off": "False",
        "entity_category": "diagnostic",
    }

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

    components: dict[str, dict] = {}

    for key, label, tmpl in [
        ("grid_power_total", "Grid Power", "{{ value_json.grid_power.total }}"),
        ("grid_power_l1", "Grid Power L1", "{{ value_json.grid_power.l1 }}"),
        ("grid_power_l2", "Grid Power L2", "{{ value_json.grid_power.l2 }}"),
        ("grid_power_l3", "Grid Power L3", "{{ value_json.grid_power.l3 }}"),
    ]:
        comp: dict = {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_{key}",
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": tmpl,
        }
        if key == "grid_power_total":
            comp["name"] = None
        else:
            comp["name"] = label
        components[key] = comp

    components["active"] = {
        "platform": "binary_sensor",
        "unique_id": f"{uid_prefix}_active",
        "name": "Active",
        "device_class": "connectivity",
        "state_topic": state_topic,
        "value_template": "{{ value_json.active }}",
        "payload_on": "True",
        "payload_off": "False",
        "entity_category": "diagnostic",
    }

    components["last_seen"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_last_seen",
        "name": "Last Seen",
        "device_class": "timestamp",
        "state_topic": state_topic,
        "value_template": "{{ value_json.last_seen }}",
        "entity_category": "diagnostic",
    }

    # Poll interval (EMA-smoothed seconds between consecutive polls)
    components["poll_interval"] = {
        "platform": "sensor",
        "unique_id": f"{uid_prefix}_poll_interval",
        "name": "Poll Interval",
        "device_class": "duration",
        "unit_of_measurement": "s",
        "state_topic": state_topic,
        "value_template": "{{ value_json.poll_interval }}",
        "entity_category": "diagnostic",
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
        "battery_count": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_battery_count",
            "name": "Battery Count",
            "state_topic": state_topic,
            "value_template": "{{ value_json.battery_count }}",
            "entity_category": "diagnostic",
        },
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
