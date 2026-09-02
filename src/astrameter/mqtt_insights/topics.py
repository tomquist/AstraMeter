"""The MQTT topic tree, defined once.

Three layers have to agree on every one of these strings: the publisher
(``service.py``), the Home Assistant discovery payloads that tell HA where to
read them (``discovery.py``), and the command parser that reverses the two
``.set`` shapes back into a device and a field.  Each shape is spelled here and
nowhere else so it cannot drift between them.  ``docs/mqtt-insights.md`` is the
human-readable reference for the same tree.
"""

from __future__ import annotations

from dataclasses import dataclass

_COMMAND_SUFFIX = "/set"


def system_status_topic(base_topic: str) -> str:
    """Retained ``online``/``offline`` for AstraMeter itself (also the LWT)."""
    return f"{base_topic}/status"


def bridge_topic(base_topic: str) -> str:
    """Retained state of the top-level AstraMeter hub device."""
    return f"{base_topic}/bridge"


def ct002_consumer_topic(base_topic: str, device_id: str, consumer_id: str) -> str:
    """Retained per-consumer state of one CT002 meter."""
    return f"{base_topic}/ct002/{device_id}/consumer/{consumer_id}"


def ct002_status_topic(base_topic: str, device_id: str) -> str:
    """Retained device-level state of one CT002 meter."""
    return f"{base_topic}/ct002/{device_id}/status"


def shelly_battery_topic(base_topic: str, device_id: str, ip_slug: str) -> str:
    """Retained per-battery state of one Shelly meter."""
    return f"{base_topic}/shelly/{device_id}/battery/{ip_slug}"


def shelly_status_topic(base_topic: str, device_id: str) -> str:
    """Retained device-level state of one Shelly meter."""
    return f"{base_topic}/shelly/{device_id}/status"


def powermeter_topic(base_topic: str, pm_id: str) -> str:
    """Retained health state of one configured powermeter."""
    return f"{base_topic}/powermeter/{pm_id}"


def availability_topic(state_topic: str) -> str:
    """Per-entity availability beside a state topic."""
    return f"{state_topic}/availability"


def consumer_command_topic(
    base_topic: str, device_id: str, consumer_id: str, field: str
) -> str:
    """Retained command topic for one per-consumer setting (scalar payload)."""
    return f"{base_topic}/ct002/{device_id}/consumer/{consumer_id}/{field}/set"


def device_command_topic(base_topic: str, device_id: str) -> str:
    """Retained device-level command topic (JSON object payload)."""
    return f"{base_topic}/ct002/{device_id}/set"


def consumer_command_filter(base_topic: str) -> str:
    """Subscription wildcard covering every consumer command topic."""
    return f"{base_topic}/ct002/+/consumer/+/+/set"


def device_command_filter(base_topic: str) -> str:
    """Subscription wildcard covering every device-level command topic."""
    return f"{base_topic}/ct002/+/set"


@dataclass(frozen=True, slots=True)
class ConsumerCommandTopic:
    device_id: str
    consumer_id: str
    field: str


@dataclass(frozen=True, slots=True)
class DeviceCommandTopic:
    device_id: str


@dataclass(frozen=True, slots=True)
class MalformedCommandTopic:
    """Fits the ``{base}/ct002/…/set`` frame but carries no ``<cid>/<field>``."""


ParsedCommandTopic = ConsumerCommandTopic | DeviceCommandTopic | MalformedCommandTopic


def parse_command_topic(base_topic: str, topic: str) -> ParsedCommandTopic | None:
    """Reverse a command topic, or ``None`` when it is not one of ours.

    The two shapes the subscriptions above deliver:
      consumer: ``{base}/ct002/<dev>/consumer/<cid>/<field>/set`` (scalar)
      device:   ``{base}/ct002/<dev>/set``                        (JSON)
    """
    prefix = f"{base_topic}/ct002/"
    if not topic.startswith(prefix) or not topic.endswith(_COMMAND_SUFFIX):
        return None
    middle = topic[len(prefix) : -len(_COMMAND_SUFFIX)]
    device_id, sep, rest = middle.partition("/consumer/")
    # Every id is one topic level, matching the "+" in the subscription
    # filters above -- so a segment holding a "/" is not a topic we publish.
    if "/" in device_id:
        return None
    if not sep:
        return DeviceCommandTopic(device_id=device_id)
    consumer_id, sep, field = rest.rpartition("/")
    if not sep:
        return MalformedCommandTopic()
    if "/" in consumer_id:
        return None
    return ConsumerCommandTopic(
        device_id=device_id, consumer_id=consumer_id, field=field
    )
