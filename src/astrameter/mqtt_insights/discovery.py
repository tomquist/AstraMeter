"""Pure functions that build HA MQTT Device Discovery payloads (HA 2024.11+)."""

from __future__ import annotations

import re
from functools import partial

from astrameter.ct002.balancer import CONTROL_QUALITY_STATES, _needs_dc_output_floor
from astrameter.ct002.controls import CONSUMER_CONTROLS_BY_FIELD
from astrameter.version_info import get_git_commit_sha

from .topics import (
    availability_topic,
    bridge_topic,
    consumer_command_topic,
    ct002_consumer_topic,
    ct002_status_topic,
    device_command_topic,
    powermeter_topic,
    shelly_battery_topic,
    shelly_status_topic,
    system_status_topic,
)

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


def _absent_as_unknown(key: str) -> str:
    """Value template mapping a JSON ``null`` onto Home Assistant's "unknown".

    A control-quality figure is null until there is something to report. A
    plain ``value_json.<key>`` renders that as the string "None", which HA
    stores as a state and any "below X" automation then fires on.
    """
    return f"{{{{ value_json.{key} if value_json.{key} is not none else 'unknown' }}}}"


def _availability(topic: str) -> dict:
    return {
        "topic": topic,
        "payload_available": "online",
        "payload_not_available": "offline",
    }


# Home Assistant keeps an entity that merely stops appearing in a device's
# discovery payload; a component that is empty apart from ``platform`` removes
# it.  ``last_seen`` changed on every poll and filled the logbook (issue #576);
# HA's own ``last_reported`` carries the same information.
RETIRED_COMPONENTS: dict[str, str] = {"last_seen": "sensor"}


def _device_info(identifier: str, name: str) -> dict:
    return {
        "identifiers": identifier,
        "name": name,
        "manufacturer": "astrameter",
    }


def _device_discovery(
    ha_prefix: str,
    node_id: str,
    device: dict,
    components: dict[str, dict],
    base_topic: str,
    state_topic: str,
    *,
    avail_topic: str | None = None,
    via_device: str | None = None,
) -> tuple[str, dict]:
    """Assemble one device-based discovery message.

    Every device is unavailable while AstraMeter itself is offline; a battery
    additionally has its own availability topic, and both must say online.
    """
    if via_device:
        device["via_device"] = via_device
    payload: dict = {
        "device": device,
        "origin": _origin(),
        "components": components,
    }
    if avail_topic is None:
        payload["availability"] = [_availability(system_status_topic(base_topic))]
    else:
        payload["availability_mode"] = "all"
        payload["availability"] = [
            _availability(system_status_topic(base_topic)),
            _availability(avail_topic),
        ]
    payload["state_topic"] = state_topic
    return f"{ha_prefix}/device/{node_id}/config", payload


def _sensor(
    uid_prefix: str,
    state_topic: str,
    key: str,
    name: str | None,
    *,
    platform: str = "sensor",
    device_class: str | None = None,
    options: list[str] | None = None,
    state_class: str | None = None,
    unit: str | None = None,
    template: str | None = None,
    payload_on: str | None = None,
    payload_off: str | None = None,
    category: str | None = None,
    availability: list[dict] | None = None,
) -> dict:
    """One read-only entity. A ``None`` *name* marks the device's primary
    entity; every other ``None`` drops its field from the payload."""
    optional = {
        "device_class": device_class,
        "options": options,
        "state_class": state_class,
        "unit_of_measurement": unit,
        "state_topic": state_topic,
        "value_template": template,
        "payload_on": payload_on,
        "payload_off": payload_off,
        "entity_category": category,
        "availability": availability,
    }
    return {
        "platform": platform,
        "unique_id": f"{uid_prefix}_{key}",
        "name": name,
        **{k: v for k, v in optional.items() if v is not None},
    }


def _power_sensors(
    uid_prefix: str, state_topic: str, sensors: list[tuple[str, str | None, str]]
) -> dict[str, dict]:
    """Power sensors for (key, name, value template); a ``None`` name marks
    the device's primary entity."""
    return {
        key: _sensor(
            uid_prefix,
            state_topic,
            key,
            name,
            device_class="power",
            state_class="measurement",
            unit="W",
            template=template,
        )
        for key, name, template in sensors
    }


def _duration_sensor(uid_prefix: str, state_topic: str, key: str, name: str) -> dict:
    return _sensor(
        uid_prefix,
        state_topic,
        key,
        name,
        device_class="duration",
        unit="s",
        template=f"{{{{ value_json.{key} }}}}",
        category="diagnostic",
    )


def build_retirement_payload(payload: dict) -> dict:
    """Copy of a discovery *payload* that also removes retired components."""
    components = dict(payload["components"])
    for key, platform in RETIRED_COMPONENTS.items():
        components[key] = {"platform": platform}
    return {**payload, "components": components}


# Per-consumer controllable entities each use their own command topic with
# ``retain: true``.  Home Assistant then publishes the set-command retained,
# so on an AstraMeter restart the broker redelivers it as soon as we
# re-subscribe and the value restores itself — no local state store needed.
# A dedicated topic per setting is required because a broker keeps only the
# last retained message per topic.


def _bounds(field: str) -> tuple[float, float]:
    """The range a number entity offers: the same one the command handler
    enforces, so Home Assistant never offers a value that would be refused."""
    control = CONSUMER_CONTROLS_BY_FIELD[field]
    assert control.low is not None and control.high is not None
    return control.low, control.high


def _number_control(
    uid_prefix: str,
    state_topic: str,
    base_topic: str,
    device_id: str,
    consumer_id: str,
    field: str,
    name: str,
    *,
    template: str,
    mode: str,
    unit: str | None = None,
    device_class: str | None = None,
    step: float | None = None,
) -> dict:
    low, high = _bounds(field)
    optional = {
        "unit_of_measurement": unit,
        "device_class": device_class,
    }
    return {
        "platform": "number",
        "unique_id": f"{uid_prefix}_{field}",
        "name": name,
        **{k: v for k, v in optional.items() if v is not None},
        "min": low,
        "max": high,
        **({"step": step} if step is not None else {}),
        "mode": mode,
        "state_topic": state_topic,
        "value_template": template,
        "command_topic": consumer_command_topic(
            base_topic, device_id, consumer_id, field
        ),
        "retain": True,
        "entity_category": "config",
    }


def _switch_control(
    uid_prefix: str,
    state_topic: str,
    base_topic: str,
    device_id: str,
    consumer_id: str,
    field: str,
    name: str,
    *,
    category: str | None = None,
) -> dict:
    return {
        "platform": "switch",
        "unique_id": f"{uid_prefix}_{field}",
        "name": name,
        "state_topic": state_topic,
        "command_topic": consumer_command_topic(
            base_topic, device_id, consumer_id, field
        ),
        "value_template": f"{{{{ value_json.{field} }}}}",
        "payload_on": "true",
        "payload_off": "false",
        "state_on": "True",
        "state_off": "False",
        "retain": True,
        **({"entity_category": category} if category is not None else {}),
    }


def build_ct002_consumer_discovery(
    base_topic: str,
    device_id: str,
    consumer_id: str,
    ha_prefix: str,
    device_type: str = "",
    efficiency_rotation: bool = False,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_ct002_{safe_dev}_{_sanitize_id(consumer_id)}"
    state_topic = ct002_consumer_topic(base_topic, device_id, consumer_id)
    uid_prefix = node_id
    meter_identifier = f"astrameter_ct002_{safe_dev}"

    sensor = partial(_sensor, uid_prefix, state_topic)
    duration = partial(_duration_sensor, uid_prefix, state_topic)
    control = (uid_prefix, state_topic, base_topic, device_id, consumer_id)
    number = partial(_number_control, *control)
    switch = partial(_switch_control, *control)

    components = _power_sensors(
        uid_prefix,
        state_topic,
        [
            ("grid_power_total", None, "{{ value_json.grid_power.total }}"),
            ("grid_power_l1", "Grid Power L1", "{{ value_json.grid_power.l1 }}"),
            ("grid_power_l2", "Grid Power L2", "{{ value_json.grid_power.l2 }}"),
            ("grid_power_l3", "Grid Power L3", "{{ value_json.grid_power.l3 }}"),
            ("target_l1", "Target L1", "{{ value_json.target.l1 }}"),
            ("target_l2", "Target L2", "{{ value_json.target.l2 }}"),
            ("target_l3", "Target L3", "{{ value_json.target.l3 }}"),
            ("reported_power", "Reported Power", "{{ value_json.reported_power }}"),
            ("last_target", "Last Target", "{{ value_json.last_target }}"),
        ],
    )

    components["saturation"] = sensor(
        "saturation",
        "Saturation",
        unit="%",
        template="{{ (value_json.saturation * 100) | round(1) }}",
    )

    # The phase options must cover every phase a consumer payload can carry, or
    # Home Assistant drops the state and logs "Ignoring invalid option" on every
    # poll (issue #580): "D" is combined/whole-home mode on newer Marstek
    # firmware.  Inspection reporters ("0") never reach this topic — their polls
    # fire no event — so "0" is deliberately absent.
    components["phase"] = sensor(
        "phase",
        "Phase",
        device_class="enum",
        options=["A", "B", "C", "D"],
        template="{{ value_json.phase }}",
        category="diagnostic",
    )

    for key, label in [
        ("device_type", "Device Type"),
        ("battery_ip", "Battery IP"),
        ("ct_type", "CT Type"),
        ("ct_mac", "CT MAC"),
    ]:
        components[key] = sensor(
            key,
            label,
            template=f"{{{{ value_json.{key} }}}}",
            category="diagnostic",
        )

    components["poll_interval"] = duration("poll_interval", "Poll Interval")

    # Answer interval — how often this battery actually gets a reply.  Equal to
    # the poll interval unless DEDUPE_TIME_WINDOW is suppressing replies, which
    # is exactly when the difference is worth seeing.
    components["answer_interval"] = duration("answer_interval", "Answer Interval")

    components["manual_target"] = number(
        "manual_target",
        "Manual Target",
        unit="W",
        device_class="power",
        mode="box",
        template="{{ value_json.manual_target | default(0) }}",
    )

    # Auto target: on = automatic control, off = manual override.
    components["auto_target"] = switch("auto_target", "Auto Target", category="config")

    # No ``entity_category`` on purpose: Active is this consumer's primary
    # control, not a setting tucked into the device's configuration section.
    components["active"] = switch("active", "Active")

    # Distribution weight — relative fair-share weight across batteries.
    # 1.0 is neutral; raise it on a larger battery (or lower it on a smaller
    # one) to bias the split, e.g. 1.5 vs 1.0 for a ~60:40 distribution.
    components["distribution_weight"] = number(
        "distribution_weight",
        "Distribution Weight",
        step=0.1,
        mode="slider",
        template="{{ value_json.distribution_weight | default(1.0) }}",
    )

    # Efficiency window weight — how much of the efficiency rotation window this
    # battery participates in, as a percentage.  100 % is neutral (full
    # participation); 0 % skips the battery for efficiency (parked while
    # limiting); intermediate values give it proportionally less active time.
    # Surfaced as a percentage; the internal value is a 0-1 fraction.  Only
    # meaningful when efficiency rotation is enabled (``min_efficient_power >
    # 0``); without it every battery stays active, so don't surface the entity.
    if efficiency_rotation:
        components["efficiency_window_weight"] = number(
            "efficiency_window_weight",
            "Efficiency Window Weight",
            unit="%",
            step=5,
            mode="slider",
            template=(
                "{{ (value_json.efficiency_window_weight | default(1.0)) * 100 }}"
            ),
        )

    # Min DC Output — minimum discharge (W) to keep a DC battery's external
    # inverter from switching off at 0 W.  Only surfaced for batteries where it
    # has an effect (no built-in inverter, no AC input — the B2500 family);
    # Venus/Jupiter/unknown types don't get this entity.
    if _needs_dc_output_floor(device_type):
        components["min_dc_output"] = number(
            "min_dc_output",
            "Min DC Output",
            unit="W",
            device_class="power",
            step=1,
            mode="box",
            template="{{ value_json.min_dc_output | default(0) }}",
        )

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

    return _device_discovery(
        ha_prefix,
        node_id,
        device_info,
        components,
        base_topic,
        state_topic,
        avail_topic=availability_topic(state_topic),
    )


def build_addon_device_discovery(
    base_topic: str,
    addon_slug: str,
    ha_prefix: str,
) -> tuple[str, dict]:
    """The top-level "AstraMeter" hub device the per-meter devices link to.

    MQTT ``via_device`` only resolves within the MQTT identifier namespace, so
    the hub is an MQTT device of its own rather than the Supervisor's add-on
    device.
    """
    safe_slug = _sanitize_id(addon_slug)
    node_id = f"astrameter_addon_{safe_slug}"
    uid_prefix = node_id
    bridge = bridge_topic(base_topic)
    system_availability = [_availability(system_status_topic(base_topic))]

    components: dict[str, dict] = {
        # No availability block on purpose: this sensor IS the offline
        # indicator, so it must flip to "off" rather than going unavailable
        # when AstraMeter drops the LWT.
        "status": _sensor(
            uid_prefix,
            system_status_topic(base_topic),
            "status",
            "Status",
            platform="binary_sensor",
            device_class="connectivity",
            payload_on="online",
            payload_off="offline",
            category="diagnostic",
        ),
        "version": _sensor(
            uid_prefix,
            bridge,
            "version",
            "Version",
            template="{{ value_json.version }}",
            category="diagnostic",
            availability=system_availability,
        ),
        "consumer_count": _sensor(
            uid_prefix,
            bridge,
            "consumer_count",
            "Consumer Count",
            template="{{ value_json.consumer_count }}",
            category="diagnostic",
            availability=system_availability,
        ),
    }

    payload = {
        "device": _device_info(addon_slug, "AstraMeter"),
        "origin": _origin(),
        "components": components,
        "state_topic": bridge,
    }

    topic = f"{ha_prefix}/device/{node_id}/config"
    return topic, payload


def build_ct002_device_discovery(
    base_topic: str,
    device_id: str,
    ha_prefix: str,
    addon_slug: str | None = None,
    efficiency_rotation: bool = False,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_ct002_{safe_dev}"
    state_topic = ct002_status_topic(base_topic, device_id)
    uid_prefix = node_id
    sensor = partial(_sensor, uid_prefix, state_topic)

    components: dict[str, dict] = {
        "smooth_target": sensor(
            "smooth_target",
            None,  # primary
            device_class="power",
            state_class="measurement",
            unit="W",
            template="{{ value_json.smooth_target }}",
        ),
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
            "command_topic": device_command_topic(base_topic, device_id),
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
        "consumer_count": sensor(
            "consumer_count",
            "Consumer Count",
            template="{{ value_json.consumer_count }}",
            category="diagnostic",
        ),
        # How well the loop is holding the grid at zero, and how it misses when
        # it doesn't — the one entity that answers "is this working?" without
        # reading the balancer internals. Verdict plus a 0-100 score so it can
        # be trended and alerted on. Both come from the balancer's
        # ControlQualityTracker; the states are its documented vocabulary.
        "control_quality": sensor(
            "control_quality",
            "Control Quality",
            device_class="enum",
            options=list(CONTROL_QUALITY_STATES),
            template="{{ value_json.control_quality }}",
            category="diagnostic",
        ),
        # The score is null while the loop has nothing to be scored on
        # (idle / warming up). Mapping that to HA's "unknown" keeps a
        # "score below X" automation from firing on an absent reading —
        # a plain `value_json.…` would render the string "None".
        "control_quality_score": sensor(
            "control_quality_score",
            "Control Quality Score",
            state_class="measurement",
            unit="%",
            template=_absent_as_unknown("control_quality_score"),
            category="diagnostic",
        ),
        # The evidence behind the verdict. It names no cause on purpose, so
        # these are what a user acts on: a high crossing rate beside
        # "off_target" points at a loop overshooting past zero, a near-zero one
        # at a loop that never gets there. Same absence rule as the score.
        "control_quality_error": sensor(
            "control_quality_error",
            "Control Quality Mean Error",
            device_class="power",
            state_class="measurement",
            unit="W",
            template=_absent_as_unknown("control_quality_error_w"),
            category="diagnostic",
        ),
        "control_quality_in_band": sensor(
            "control_quality_in_band",
            "Control Quality Time In Band",
            state_class="measurement",
            unit="%",
            template=_absent_as_unknown("control_quality_in_band_pct"),
            category="diagnostic",
        ),
        "control_quality_crossings": sensor(
            "control_quality_crossings",
            "Control Quality Zero Crossings",
            state_class="measurement",
            unit="/min",
            template=_absent_as_unknown("control_quality_crossings_per_min"),
            category="diagnostic",
        ),
    }

    # The Force Rotation button only does anything when efficiency rotation is
    # enabled (``min_efficient_power > 0``); without it the balancer keeps every
    # battery active and there's nothing to rotate, so don't surface the button.
    if efficiency_rotation:
        components["force_rotation"] = {
            "platform": "button",
            "unique_id": f"{uid_prefix}_force_rotation",
            "name": "Force Rotation",
            "command_topic": device_command_topic(base_topic, device_id),
            "payload_press": '{"force_rotation": true}',
            "entity_category": "config",
        }

    return _device_discovery(
        ha_prefix,
        node_id,
        _device_info(node_id, f"AstraMeter CT002 {device_id}"),
        components,
        base_topic,
        state_topic,
        via_device=addon_slug,
    )


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
    state_topic = powermeter_topic(base_topic, pm_id)

    # A ``null`` phase (a single-phase meter, or a meter that is down) renders
    # to an empty string so Home Assistant leaves the entity untouched rather
    # than logging a parse error.
    def reading(field: str) -> str:
        return (
            f"{{{{ value_json.grid_power.{field} "
            f"if value_json.grid_power.{field} is not none else '' }}}}"
        )

    components = _power_sensors(
        uid_prefix,
        state_topic,
        [
            ("grid_power_total", None, reading("total")),
            ("grid_power_l1", "Power L1", reading("l1")),
            ("grid_power_l2", "Power L2", reading("l2")),
            ("grid_power_l3", "Power L3", reading("l3")),
        ],
    )

    components["online"] = _sensor(
        uid_prefix,
        state_topic,
        "online",
        "Online",
        platform="binary_sensor",
        device_class="connectivity",
        template="{{ value_json.online }}",
        payload_on="True",
        payload_off="False",
        category="diagnostic",
    )

    return _device_discovery(
        ha_prefix,
        node_id,
        # Capital-Case the config section for a readable device label
        # (e.g. "SMA_ENERGY_METER" -> "Sma Energy Meter").
        _device_info(
            node_id, f"AstraMeter Powermeter {name.replace('_', ' ').title()}"
        ),
        components,
        base_topic,
        state_topic,
        via_device=addon_slug,
    )


def build_shelly_battery_discovery(
    base_topic: str,
    device_id: str,
    battery_ip: str,
    ha_prefix: str,
) -> tuple[str, dict]:
    ip_slug = _sanitize_id(battery_ip)
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_shelly_{safe_dev}_{ip_slug}"
    state_topic = shelly_battery_topic(base_topic, device_id, ip_slug)
    uid_prefix = node_id

    components = _power_sensors(
        uid_prefix,
        state_topic,
        [
            ("grid_power_total", None, "{{ value_json.grid_power.total }}"),
            ("grid_power_l1", "Grid Power L1", "{{ value_json.grid_power.l1 }}"),
            ("grid_power_l2", "Grid Power L2", "{{ value_json.grid_power.l2 }}"),
            ("grid_power_l3", "Grid Power L3", "{{ value_json.grid_power.l3 }}"),
        ],
    )

    components["active"] = _sensor(
        uid_prefix,
        state_topic,
        "active",
        "Active",
        platform="binary_sensor",
        device_class="connectivity",
        template="{{ value_json.active }}",
        payload_on="True",
        payload_off="False",
        category="diagnostic",
    )

    components["poll_interval"] = _duration_sensor(
        uid_prefix, state_topic, "poll_interval", "Poll Interval"
    )

    return _device_discovery(
        ha_prefix,
        node_id,
        _device_info(node_id, f"AstraMeter Shelly Battery {battery_ip}"),
        components,
        base_topic,
        state_topic,
        avail_topic=availability_topic(state_topic),
        via_device=f"astrameter_shelly_{safe_dev}",
    )


def build_shelly_device_discovery(
    base_topic: str,
    device_id: str,
    ha_prefix: str,
    addon_slug: str | None = None,
) -> tuple[str, dict]:
    safe_dev = _sanitize_id(device_id)
    node_id = f"astrameter_shelly_{safe_dev}"
    state_topic = shelly_status_topic(base_topic, device_id)
    uid_prefix = node_id

    components: dict[str, dict] = {
        "battery_count": _sensor(
            uid_prefix,
            state_topic,
            "battery_count",
            "Battery Count",
            template="{{ value_json.battery_count }}",
            category="diagnostic",
        ),
    }

    return _device_discovery(
        ha_prefix,
        node_id,
        _device_info(node_id, f"AstraMeter Shelly {device_id}"),
        components,
        base_topic,
        state_topic,
        via_device=addon_slug,
    )
