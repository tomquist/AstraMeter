"""Fidelity guard: entity_model must match discovery.py's emitted entities.

The native HA integration builds entities from ``entity_model`` while MQTT
Insights builds from ``discovery``. These tests assert the two agree on the
entity set and core metadata per device kind, so they can't silently drift
(until ``discovery.py`` is refactored to render directly from the table).
"""

from __future__ import annotations

from astrameter import entity_model as em
from astrameter.mqtt_insights import discovery

_BASE = "astrameter"
_PREFIX = "homeassistant"

# B2500 type so the conditional min_dc_output entity is present on both sides.
_CT002_TYPE = "HMA-1"


def _discovery_components(kind: str) -> dict[str, dict]:
    if kind == em.CT002_DEVICE:
        _, payload = discovery.build_ct002_device_discovery(_BASE, "dev1", _PREFIX)
    elif kind == em.CT002_CONSUMER:
        _, payload = discovery.build_ct002_consumer_discovery(
            _BASE, "dev1", "aabbccddeeff", _PREFIX, device_type=_CT002_TYPE
        )
    elif kind == em.SHELLY_DEVICE:
        _, payload = discovery.build_shelly_device_discovery(_BASE, "dev1", _PREFIX)
    elif kind == em.SHELLY_BATTERY:
        _, payload = discovery.build_shelly_battery_discovery(
            _BASE, "dev1", "192.168.1.50", _PREFIX
        )
    elif kind == em.POWERMETER:
        _, payload = discovery.build_powermeter_device_discovery(
            _BASE, "sma", "SMA_ENERGY_METER", _PREFIX
        )
    else:  # pragma: no cover
        raise AssertionError(kind)
    return payload["components"]


def _model_descriptors(kind: str) -> dict[str, em.EntityDescriptor]:
    out = {}
    for desc in em.ENTITIES_BY_KIND[kind]:
        if desc.present_for(_CT002_TYPE):
            out[desc.key] = desc
    return out


def test_entity_keys_match_discovery() -> None:
    for kind in em.ENTITIES_BY_KIND:
        comps = _discovery_components(kind)
        model = _model_descriptors(kind)
        # The MQTT identity string-sensors are intentionally promoted to device
        # attributes natively, so exclude them from the key comparison.
        comp_keys = set(comps) - set(em.CT002_CONSUMER_IDENTITY_FIELDS)
        assert comp_keys == set(model), (
            f"{kind}: model {set(model)} != discovery {comp_keys}"
        )


def test_entity_metadata_matches_discovery() -> None:
    for kind in em.ENTITIES_BY_KIND:
        comps = _discovery_components(kind)
        for key, desc in _model_descriptors(kind).items():
            comp = comps[key]
            assert comp["platform"] == desc.platform, (kind, key, "platform")
            assert comp.get("device_class") == desc.device_class, (
                kind,
                key,
                "device_class",
            )
            assert comp.get("unit_of_measurement") == desc.unit, (kind, key, "unit")
            assert comp.get("state_class") == desc.state_class, (
                kind,
                key,
                "state_class",
            )
            assert comp.get("entity_category") == desc.entity_category, (
                kind,
                key,
                "entity_category",
            )
            # Primary entity ⇒ name is None in discovery; else label matches.
            if desc.primary:
                assert comp.get("name") is None, (kind, key, "primary name")
            else:
                assert comp.get("name") == desc.name, (kind, key, "name")
            # Number bounds
            if desc.platform == em.NUMBER:
                assert comp.get("min") == desc.min, (kind, key, "min")
                assert comp.get("max") == desc.max, (kind, key, "max")
                assert comp.get("step") == desc.step, (kind, key, "step")
                assert comp.get("mode") == desc.mode, (kind, key, "mode")
            # Enum options
            if desc.options is not None:
                assert tuple(comp.get("options", ())) == desc.options, (
                    kind,
                    key,
                    "options",
                )


def test_min_dc_output_presence_predicate() -> None:
    # Present for B2500 family, absent for Venus.
    consumer = {d.key for d in em.CT002_CONSUMER_ENTITIES if d.present_for("HMA-1")}
    venus = {d.key for d in em.CT002_CONSUMER_ENTITIES if d.present_for("VNS-3")}
    assert "min_dc_output" in consumer
    assert "min_dc_output" not in venus


def test_extract_value_paths_and_transforms() -> None:
    data = {
        "grid_power": {"total": 123.0, "l1": 1.0, "l2": None, "l3": 3.0},
        "saturation": 0.4567,
        "active": False,
        "manual_target": None,
    }
    by_key = {d.key: d for d in em.CT002_CONSUMER_ENTITIES}
    assert em.extract_value(data, by_key["grid_power_total"]) == 123.0
    assert em.extract_value(data, by_key["grid_power_l2"]) is None
    # saturation transform: *100 round 1
    assert em.extract_value(data, by_key["saturation"]) == 45.7
    # default applied when missing
    assert em.extract_value(data, by_key["manual_target"]) == 0
    assert em.extract_value(data, by_key["active"]) is False
