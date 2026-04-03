"""Pure functions that build HA MQTT Device Discovery payloads (HA 2024.11+)."""

from __future__ import annotations

import re

from b2500_meter.version_info import get_git_commit_sha

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_id(value: str) -> str:
    return _SAFE_ID_RE.sub("_", value)


def _origin() -> dict:
    sha = get_git_commit_sha()
    return {
        "name": "b2500-meter",
        "sw_version": sha or "unknown",
        "support_url": "https://github.com/tomquist/b2500-meter",
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
    node_id = f"b2500_meter_ct002_{safe_dev}_{safe_cid}"
    state_topic = f"{base_topic}/ct002/{device_id}/consumer/{consumer_id}"
    avail_topic = f"{state_topic}/availability"
    uid_prefix = f"b2500_meter_ct002_{safe_dev}_{safe_cid}"

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
        "command_topic": f"{state_topic}/set",
        "command_template": '{"manual_target": {{ value }}}',
        "entity_category": "config",
    }

    # Auto target switch (on = automatic control, off = manual override)
    components["auto_target"] = {
        "platform": "switch",
        "unique_id": f"{uid_prefix}_auto_target",
        "name": "Auto Target",
        "state_topic": state_topic,
        "command_topic": f"{state_topic}/set",
        "value_template": "{{ value_json.auto_target }}",
        "payload_on": '{"auto_target": true}',
        "payload_off": '{"auto_target": false}',
        "state_on": "True",
        "state_off": "False",
        "entity_category": "config",
    }

    # Active switch
    components["active"] = {
        "platform": "switch",
        "unique_id": f"{uid_prefix}_active",
        "name": "Active",
        "state_topic": state_topic,
        "command_topic": f"{state_topic}/set",
        "value_template": "{{ value_json.active }}",
        "payload_on": '{"active": true}',
        "payload_off": '{"active": false}',
        "state_on": "True",
        "state_off": "False",
        "optimistic": True,
    }

    # Build identifiers: include hame_energy_<mac> for matching real devices
    mac_slug = _sanitize_id(consumer_id).lower().replace("-", "").replace("_", "")
    identifiers = [node_id, f"hame_energy_{mac_slug}"]

    device_info: dict = {
        "identifiers": identifiers,
        "name": f"HAME Energy {device_type} {mac_slug}"
        if device_type
        else f"HAME Energy {mac_slug}",
        "manufacturer": "HAME Energy",
    }
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


# ── CT002 device-level ────────────────────────────────────────────────────


def build_ct002_device_discovery(
    base_topic: str,
    device_id: str,
    ha_prefix: str,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"b2500_meter_ct002_{safe_dev}"
    state_topic = f"{base_topic}/ct002/{device_id}/status"
    uid_prefix = f"b2500_meter_ct002_{safe_dev}"

    components: dict[str, dict] = {
        "smooth_target": {
            "platform": "sensor",
            "unique_id": f"{uid_prefix}_smooth_target",
            "name": None,  # primary
            "device_class": "power",
            "unit_of_measurement": "W",
            "state_topic": state_topic,
            "value_template": "{{ value_json.smooth_target }}",
        },
        "active_control": {
            "platform": "binary_sensor",
            "unique_id": f"{uid_prefix}_active_control",
            "name": "Active Control",
            "device_class": "running",
            "state_topic": state_topic,
            "value_template": "{{ value_json.active_control }}",
            "payload_on": "True",
            "payload_off": "False",
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

    payload = {
        "device": {
            "identifiers": node_id,
            "name": f"CT002 {device_id}",
            "manufacturer": "b2500-meter",
        },
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
    node_id = f"b2500_meter_shelly_{safe_dev}_{ip_slug}"
    state_topic = f"{base_topic}/shelly/{device_id}/battery/{ip_slug}"
    avail_topic = f"{state_topic}/availability"
    uid_prefix = f"b2500_meter_shelly_{safe_dev}_{ip_slug}"

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

    payload = {
        "device": {
            "identifiers": node_id,
            "name": f"Shelly Battery {battery_ip}",
            "manufacturer": "b2500-meter",
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
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"b2500_meter_shelly_{safe_dev}"
    state_topic = f"{base_topic}/shelly/{device_id}/status"
    uid_prefix = f"b2500_meter_shelly_{safe_dev}"

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

    payload = {
        "device": {
            "identifiers": node_id,
            "name": f"Shelly {device_id}",
            "manufacturer": "b2500-meter",
        },
        "origin": _origin(),
        "components": components,
        "availability": [_system_availability(base_topic)],
        "state_topic": state_topic,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload
