"""Every CT setting must reach the emulator it configures.

``_build_ct002`` passes ~40 settings into ``CT002`` by keyword, and from there
into the balancer and the saturation tracker. A swapped pair (say
``pace_base_step`` and ``pace_max_step``) type-checks, runs, and silently
steers the batteries wrong, so this test gives every field a distinct value and
follows it to where it is consumed.

The destination map below must cover every field of ``CtSettings``: a new
setting fails the coverage test until its destination is named here, which is
what keeps the check exhaustive rather than a snapshot of today's fields.
"""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from astrameter.config.settings import CtSettings
from astrameter.main import _build_ct002

#: Settings that ``CT002`` never sees — they are consumed elsewhere in
#: ``run_device`` (the cloud reporter) and are asserted by their own tests.
NOT_ON_THE_EMULATOR = (
    "cloud_reporting",
    "cloud_reporting_host",
    "cloud_reporting_interval",
)

#: How to read each setting back off the built emulator.
DESTINATIONS = {
    "udp_port": lambda ct002: ct002.udp_port,
    "ct_mac": lambda ct002: ct002.ct_mac,
    "wifi_rssi": lambda ct002: ct002.wifi_rssi,
    "dedupe_time_window": lambda ct002: ct002.dedupe_time_window,
    "consumer_ttl": lambda ct002: ct002.consumer_ttl,
    "debug_status": lambda ct002: ct002.debug_status,
    "active_control": lambda ct002: ct002.active_control,
    # Balancer configuration.
    "fair_distribution": lambda ct002: _cfg(ct002).fair_distribution,
    "balance_gain": lambda ct002: _cfg(ct002).balance_gain,
    "balance_deadband": lambda ct002: _cfg(ct002).balance_deadband,
    "max_correction_per_step": lambda ct002: _cfg(ct002).max_correction_per_step,
    "error_boost_threshold": lambda ct002: _cfg(ct002).error_boost_threshold,
    "error_boost_max": lambda ct002: _cfg(ct002).error_boost_max,
    "error_reduce_threshold": lambda ct002: _cfg(ct002).error_reduce_threshold,
    "max_target_step": lambda ct002: _cfg(ct002).max_target_step,
    "pace_base_step": lambda ct002: _cfg(ct002).pace_base_step,
    "pace_max_step": lambda ct002: _cfg(ct002).pace_max_step,
    "osc_damp_max": lambda ct002: _cfg(ct002).osc_damp_max,
    "osc_damp_alpha": lambda ct002: _cfg(ct002).osc_damp_alpha,
    "osc_damp_decay": lambda ct002: _cfg(ct002).osc_damp_decay,
    "osc_damp_threshold": lambda ct002: _cfg(ct002).osc_damp_threshold,
    "grid_predict_trust": lambda ct002: _cfg(ct002).grid_predict_trust,
    "concentrate_deadband": lambda ct002: _cfg(ct002).concentrate_deadband,
    "import_trim_w": lambda ct002: _cfg(ct002).import_trim_w,
    "min_efficient_power": lambda ct002: _cfg(ct002).min_efficient_power,
    "probe_min_power": lambda ct002: _cfg(ct002).probe_min_power,
    "efficiency_rotation_interval": lambda ct002: (
        _cfg(ct002).efficiency_rotation_interval
    ),
    "efficiency_fade_alpha": lambda ct002: _cfg(ct002).efficiency_fade_alpha,
    "efficiency_saturation_threshold": lambda ct002: (
        _cfg(ct002).efficiency_saturation_threshold
    ),
    "efficiency_demand_alpha": lambda ct002: _cfg(ct002).efficiency_demand_alpha,
    "min_dc_output": lambda ct002: _cfg(ct002).min_dc_output,
    # Saturation tracking.
    "saturation_detection": lambda ct002: _saturation(ct002)._enabled,
    "saturation_alpha": lambda ct002: _saturation(ct002)._alpha,
    "min_target_for_saturation": lambda ct002: _saturation(ct002)._min_target,
    "saturation_decay_factor": lambda ct002: _saturation(ct002)._decay_factor,
    "saturation_stall_timeout_seconds": lambda ct002: (
        _saturation(ct002)._stall_timeout_seconds
    ),
    "saturation_grace_seconds": lambda ct002: ct002._balancer._saturation_grace_seconds,
}


def _cfg(ct002):
    return ct002._balancer._cfg


def _saturation(ct002):
    return ct002._balancer._saturation


def boolean_fields() -> list[str]:
    return [
        field.name
        for field in fields(CtSettings)
        if isinstance(getattr(CtSettings(), field.name), bool)
    ]


def distinct_settings() -> CtSettings:
    """CtSettings where no two fields share a value, so a swap cannot hide.

    Booleans are the exception — flipping the default gives every flag with
    the same default the same value — so they are covered one at a time by
    ``test_each_boolean_setting_reaches_only_its_own_destination``.
    """
    values: dict[str, object] = {}
    for index, field in enumerate(fields(CtSettings), start=1):
        default = getattr(CtSettings(), field.name)
        if isinstance(default, bool):
            values[field.name] = not default
        elif isinstance(default, int) and not isinstance(default, bool):
            values[field.name] = 1000 + index
        elif isinstance(default, float):
            values[field.name] = 0.1001 + index / 10000
        elif isinstance(default, str):
            values[field.name] = f"value-{index}"
        elif default is None:  # consumer_ttl
            values[field.name] = 1000 + index
    return replace(CtSettings(), **values)


def test_destination_map_covers_every_ct_setting():
    """A new CT setting must declare where it lands before this test passes."""
    covered = set(DESTINATIONS) | set(NOT_ON_THE_EMULATOR)
    declared = {field.name for field in fields(CtSettings)}
    assert covered == declared


def test_every_ct_setting_reaches_its_destination():
    ct = distinct_settings()
    ct002 = _build_ct002(ct, "HME-4", "device-1", ct.debug_status, None)

    mismatches = {
        name: (getattr(ct, name), read(ct002))
        for name, read in DESTINATIONS.items()
        if read(ct002) != getattr(ct, name)
    }
    assert not mismatches, f"settings that did not arrive: {mismatches}"


def test_ct_type_and_device_id_are_passed_through():
    ct002 = _build_ct002(CtSettings(), "HME-3", "device-9", False, None)
    assert ct002.ct_type == "HME-3"
    assert ct002._device_id == "device-9"


def test_debug_status_is_passed_separately_from_the_setting():
    """The DEBUG_STATUS env escape hatch can turn it on for either backend."""
    ct002 = _build_ct002(CtSettings(debug_status=False), "HME-4", "dev", True, None)
    assert ct002.debug_status is True


@pytest.mark.parametrize("name", boolean_fields())
def test_each_boolean_setting_reaches_only_its_own_destination(name):
    """Flags cannot be told apart by value, so flip exactly one at a time.

    Two swapped flags with the same default would survive the sweep above;
    here only the flipped one may change at its destination.
    """
    defaults = CtSettings()
    flipped = replace(defaults, **{name: not getattr(defaults, name)})
    ct002 = _build_ct002(flipped, "HME-4", "device-1", flipped.debug_status, None)

    for other in boolean_fields():
        if other not in DESTINATIONS:  # not passed to the emulator at all
            continue
        expected = getattr(flipped, other)
        assert DESTINATIONS[other](ct002) == expected, (
            f"flipping {name} changed where {other} landed"
        )
