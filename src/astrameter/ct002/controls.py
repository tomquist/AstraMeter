"""The controls a CT002 emulator exposes to the dashboard and to MQTT.

One table defines each per-battery control: its wire name, the CT002 setter
it reaches, the bounds a value must satisfy and the scale between the wire
unit and the setter unit.  The MQTT command handlers, the dashboard write
path and the Home Assistant discovery payloads all read this table, so the
three surfaces cannot disagree on what a valid value is.

Mirrored by ``esphome/components/ct002/controls.{h,cpp}``: the bounds must
stay identical on both stacks, or a value one accepts and the other refuses
would be settable from one dashboard and then silently reverted by the next
retained MQTT replay.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol


class ControllableDevice(Protocol):
    """What a device must offer for its controls to be driven remotely."""

    def set_consumer_active(self, consumer_id: str, active: bool) -> None: ...
    def set_consumer_auto_target(self, consumer_id: str, auto: bool) -> None: ...
    def set_consumer_manual_target(self, consumer_id: str, target: float) -> None: ...
    def set_consumer_distribution_weight(
        self, consumer_id: str, weight: float
    ) -> None: ...
    def set_consumer_efficiency_window_weight(
        self, consumer_id: str, weight: float
    ) -> None: ...
    def set_consumer_min_dc_output(self, consumer_id: str, value: float) -> None: ...
    def set_active_control(self, active: bool) -> None: ...
    def force_efficiency_rotation(self) -> None: ...


@dataclass(frozen=True)
class ConsumerControl:
    """One per-battery control; ``low`` is ``None`` for a boolean switch."""

    field: str
    setter: str
    low: float | None = None
    high: float | None = None
    #: Wire unit to setter unit.  The efficiency window travels as a
    #: percentage on both MQTT and the dashboard; the setter takes 0..1.
    wire_scale: float = 1.0

    @property
    def is_switch(self) -> bool:
        return self.low is None

    def coerce(self, value: object) -> bool | float:
        """Validate a JSON value and convert it to what the setter takes."""
        if self.is_switch:
            if not isinstance(value, bool):
                raise ValueError(f"{self.field} must be true or false")
            return value
        assert self.low is not None and self.high is not None
        # bool is an int subclass, so float(True) would quietly become 1.0.
        # The firmware refuses a JSON boolean here; both stacks must agree.
        if isinstance(value, bool):
            raise ValueError(f"{self.field} must be a number")
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{self.field} must be a number") from exc
        if not math.isfinite(number) or not self.low <= number <= self.high:
            raise ValueError(
                f"{self.field} must be between {self.low:g} and {self.high:g}"
            )
        return number * self.wire_scale

    def parse(self, payload: str) -> bool | float:
        """Validate the text of an MQTT command and convert it for the setter."""
        if self.is_switch:
            parsed = parse_bool(payload)
            if parsed is None:
                raise ValueError(f"{self.field} must be true or false")
            return parsed
        try:
            number = float(payload)
        except ValueError as exc:
            raise ValueError(f"{self.field} must be a number") from exc
        return self.coerce(number)

    def apply(
        self, device: ControllableDevice, consumer_id: str, value: object
    ) -> None:
        getattr(device, self.setter)(consumer_id, value)


def parse_bool(payload: str) -> bool | None:
    token = payload.strip().lower()
    if token in ("true", "on", "1"):
        return True
    if token in ("false", "off", "0"):
        return False
    return None


CONSUMER_CONTROLS: tuple[ConsumerControl, ...] = (
    ConsumerControl("active", "set_consumer_active"),
    ConsumerControl("auto_target", "set_consumer_auto_target"),
    ConsumerControl("manual_target", "set_consumer_manual_target", -10000.0, 10000.0),
    ConsumerControl(
        "distribution_weight", "set_consumer_distribution_weight", 0.0, 10.0
    ),
    ConsumerControl(
        "efficiency_window_weight",
        "set_consumer_efficiency_window_weight",
        0.0,
        100.0,
        wire_scale=0.01,
    ),
    ConsumerControl("min_dc_output", "set_consumer_min_dc_output", 0.0, 1000.0),
)

CONSUMER_CONTROLS_BY_FIELD: dict[str, ConsumerControl] = {
    control.field: control for control in CONSUMER_CONTROLS
}


def coerce_consumer_control(field: str, value: object) -> bool | float:
    """Validate a dashboard write; raises ``ValueError`` with the user-facing
    reason (the same text the firmware's dashboard produces)."""
    return CONSUMER_CONTROLS_BY_FIELD[field].coerce(value)


def apply_device_control(device: ControllableDevice, field: str, value: object) -> None:
    """Apply a device-wide control.  ``force_rotation`` is a button and
    carries no value; ``active_control`` is a switch."""
    if field == "force_rotation":
        device.force_efficiency_rotation()
    elif field == "active_control":
        if not isinstance(value, bool):
            raise ValueError("active_control must be true or false")
        device.set_active_control(value)
    else:
        raise KeyError(field)
