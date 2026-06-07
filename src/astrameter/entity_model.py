"""Shared, declarative entity catalog for AstraMeter.

Single source of truth for the entity inventory exposed by **both** MQTT
Insights discovery (`astrameter.mqtt_insights.discovery`) and the native Home
Assistant integration (`custom_components.astrameter`). Each device kind is a
table of :class:`EntityDescriptor`s carrying the platform, metadata
(device_class / unit / state_class / entity_category / number bounds / enum
options), the display name (``primary`` ⇒ HA's ``name=None`` primary entity),
the **payload field path** into the CT002/Shelly event ``data`` dict (the same
shape MQTT Insights publishes as ``value_json``), an optional value transform
and default, the control **setter** id, and an optional **presence predicate**.

This module is intentionally **import-light**: it must not import
``homeassistant`` or ``aiomqtt`` (only the clean ``ct002.balancer`` helper for
the DC-output-floor presence rule), so the same table is usable from the
dependency-light native integration and from the MQTT backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from astrameter.ct002.balancer import _needs_dc_output_floor

# Platforms (mirror Home Assistant's domain strings).
SENSOR = "sensor"
BINARY_SENSOR = "binary_sensor"
SWITCH = "switch"
NUMBER = "number"
BUTTON = "button"

# Device kinds.
CT002_DEVICE = "ct002_device"
CT002_CONSUMER = "ct002_consumer"
SHELLY_DEVICE = "shelly_device"
SHELLY_BATTERY = "shelly_battery"
POWERMETER = "powermeter"


@dataclass(frozen=True)
class EntityDescriptor:
    """One entity in the catalog, backend-agnostic."""

    key: str
    platform: str
    # Metadata
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    entity_category: str | None = None  # "diagnostic" | "config"
    options: tuple[str, ...] | None = None  # enum sensor options
    # Number bounds
    min: float | None = None
    max: float | None = None
    step: float | None = None
    mode: str | None = None  # "box" | "slider"
    # Naming. ``primary`` ⇒ HA primary entity (name=None); else ``name`` label.
    primary: bool = False
    name: str | None = None
    # Value extraction from the event ``data`` dict (dotted path), with an
    # optional transform and default when the field is missing/None.
    field: str | None = None
    transform: str | None = None  # e.g. "saturation_pct"
    default: Any = None
    # Control setter id (CT002 method selector); None for read-only entities.
    setter: str | None = None
    # Presence predicate over the device_type; None ⇒ always present.
    requires: Callable[[str], bool] | None = None

    def present_for(self, device_type: str) -> bool:
        return self.requires is None or self.requires(device_type)


def _power(
    key: str, *, primary: bool = False, name: str | None = None, field_: str
) -> EntityDescriptor:
    return EntityDescriptor(
        key=key,
        platform=SENSOR,
        device_class="power",
        unit="W",
        state_class="measurement",
        primary=primary,
        name=name,
        field=field_,
    )


# ── CT002 device-level ────────────────────────────────────────────────────
CT002_DEVICE_ENTITIES: tuple[EntityDescriptor, ...] = (
    EntityDescriptor(
        key="smooth_target",
        platform=SENSOR,
        device_class="power",
        unit="W",
        state_class="measurement",
        primary=True,
        field="smooth_target",
    ),
    EntityDescriptor(
        key="active_control",
        platform=BINARY_SENSOR,
        device_class="running",
        name="Active Control",
        field="active_control",
    ),
    EntityDescriptor(
        key="consumer_count",
        platform=SENSOR,
        entity_category="diagnostic",
        name="Consumer Count",
        field="consumer_count",
    ),
    EntityDescriptor(
        key="force_rotation",
        platform=BUTTON,
        entity_category="config",
        name="Force Rotation",
        setter="force_rotation",
    ),
)


# ── CT002 consumer (per-battery) ──────────────────────────────────────────
CT002_CONSUMER_ENTITIES: tuple[EntityDescriptor, ...] = (
    _power("grid_power_total", primary=True, field_="grid_power.total"),
    _power("grid_power_l1", name="Grid Power L1", field_="grid_power.l1"),
    _power("grid_power_l2", name="Grid Power L2", field_="grid_power.l2"),
    _power("grid_power_l3", name="Grid Power L3", field_="grid_power.l3"),
    _power("target_l1", name="Target L1", field_="target.l1"),
    _power("target_l2", name="Target L2", field_="target.l2"),
    _power("target_l3", name="Target L3", field_="target.l3"),
    _power("reported_power", name="Reported Power", field_="reported_power"),
    _power("last_target", name="Last Target", field_="last_target"),
    EntityDescriptor(
        key="saturation",
        platform=SENSOR,
        unit="%",
        name="Saturation",
        field="saturation",
        transform="saturation_pct",
    ),
    EntityDescriptor(
        key="phase",
        platform=SENSOR,
        device_class="enum",
        options=("A", "B", "C"),
        entity_category="diagnostic",
        name="Phase",
        field="phase",
    ),
    EntityDescriptor(
        key="last_seen",
        platform=SENSOR,
        device_class="timestamp",
        entity_category="diagnostic",
        name="Last Seen",
        field="last_seen",
    ),
    EntityDescriptor(
        key="poll_interval",
        platform=SENSOR,
        device_class="duration",
        unit="s",
        entity_category="diagnostic",
        name="Poll Interval",
        field="poll_interval",
    ),
    EntityDescriptor(
        key="manual_target",
        platform=NUMBER,
        device_class="power",
        unit="W",
        min=-10000,
        max=10000,
        mode="box",
        entity_category="config",
        name="Manual Target",
        field="manual_target",
        default=0,
        setter="manual_target",
    ),
    EntityDescriptor(
        key="auto_target",
        platform=SWITCH,
        entity_category="config",
        name="Auto Target",
        field="auto_target",
        default=True,
        setter="auto_target",
    ),
    EntityDescriptor(
        key="active",
        platform=SWITCH,
        name="Active",
        field="active",
        default=True,
        setter="active",
    ),
    EntityDescriptor(
        key="distribution_weight",
        platform=NUMBER,
        min=0,
        max=10,
        step=0.1,
        mode="slider",
        entity_category="config",
        name="Distribution Weight",
        field="distribution_weight",
        default=1.0,
        setter="distribution_weight",
    ),
    EntityDescriptor(
        key="min_dc_output",
        platform=NUMBER,
        device_class="power",
        unit="W",
        min=0,
        max=1000,
        step=1,
        mode="box",
        entity_category="config",
        name="Min DC Output",
        field="min_dc_output",
        default=0,
        setter="min_dc_output",
        requires=_needs_dc_output_floor,
    ),
)

# Identity fields that MQTT exposes as string sensors but the native
# integration promotes to device-registry attributes (model_id / connections).
CT002_CONSUMER_IDENTITY_FIELDS: tuple[str, ...] = (
    "device_type",
    "battery_ip",
    "ct_type",
    "ct_mac",
)


# ── Powermeter health (grid-power source) ─────────────────────────────────
POWERMETER_ENTITIES: tuple[EntityDescriptor, ...] = (
    _power("grid_power_total", primary=True, field_="grid_power.total"),
    _power("grid_power_l1", name="Power L1", field_="grid_power.l1"),
    _power("grid_power_l2", name="Power L2", field_="grid_power.l2"),
    _power("grid_power_l3", name="Power L3", field_="grid_power.l3"),
    EntityDescriptor(
        key="online",
        platform=BINARY_SENSOR,
        device_class="connectivity",
        entity_category="diagnostic",
        name="Online",
        field="online",
    ),
)


# ── Shelly device-level ───────────────────────────────────────────────────
SHELLY_DEVICE_ENTITIES: tuple[EntityDescriptor, ...] = (
    EntityDescriptor(
        key="battery_count",
        platform=SENSOR,
        entity_category="diagnostic",
        name="Battery Count",
        field="battery_count",
    ),
)


# ── Shelly per-battery ────────────────────────────────────────────────────
SHELLY_BATTERY_ENTITIES: tuple[EntityDescriptor, ...] = (
    _power("grid_power_total", primary=True, field_="grid_power.total"),
    _power("grid_power_l1", name="Grid Power L1", field_="grid_power.l1"),
    _power("grid_power_l2", name="Grid Power L2", field_="grid_power.l2"),
    _power("grid_power_l3", name="Grid Power L3", field_="grid_power.l3"),
    EntityDescriptor(
        key="active",
        platform=BINARY_SENSOR,
        device_class="connectivity",
        entity_category="diagnostic",
        name="Active",
        field="active",
    ),
    EntityDescriptor(
        key="last_seen",
        platform=SENSOR,
        device_class="timestamp",
        entity_category="diagnostic",
        name="Last Seen",
        field="last_seen",
    ),
    EntityDescriptor(
        key="poll_interval",
        platform=SENSOR,
        device_class="duration",
        unit="s",
        entity_category="diagnostic",
        name="Poll Interval",
        field="poll_interval",
    ),
)


ENTITIES_BY_KIND: dict[str, tuple[EntityDescriptor, ...]] = {
    CT002_DEVICE: CT002_DEVICE_ENTITIES,
    CT002_CONSUMER: CT002_CONSUMER_ENTITIES,
    SHELLY_DEVICE: SHELLY_DEVICE_ENTITIES,
    SHELLY_BATTERY: SHELLY_BATTERY_ENTITIES,
    POWERMETER: POWERMETER_ENTITIES,
}


def extract_value(data: dict[str, Any], desc: EntityDescriptor) -> Any:
    """Resolve a descriptor's value from an event ``data`` dict.

    Follows the dotted ``field`` path, applies the descriptor ``default`` when
    the value is missing/None, and applies any declared transform.
    """
    if desc.field is None:
        return None
    value: Any = data
    for part in desc.field.split("."):
        if not isinstance(value, dict):
            value = None
            break
        value = value.get(part)
    if value is None:
        value = desc.default
    if value is None:
        return None
    if desc.transform == "saturation_pct":
        return round(float(value) * 100, 1)
    return value
