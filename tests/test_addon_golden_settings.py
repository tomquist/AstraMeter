"""The add-on's options must keep meaning what they meant in the add-on 2.x.

The fixture was produced by running the add-on's original ``run.sh`` (the
bashio script that rendered a ``config.ini``) over an options set that touches
every add-on option, and reading the settings back out of the file it wrote.
The add-on backend now derives the same settings from the same options without
that detour, and this pins them: a mapping that quietly changes what an option
does — a renamed key, a wrong unit, an option wired to the wrong field —
changes these values and fails here.

Regenerating the fixture is only correct when an option is *meant* to change
meaning, and that is a user-visible change.

Options newer than the add-on 2.x — the ``dashboard_*`` ones — have no run.sh
behaviour to record. They are carried here anyway, each set to something other
than its default, so the same assertion still fails if one stops reaching its
field. ``GeneralSettings.dashboard`` is pinned too even though no option feeds
it: in the add-on the dashboard is always on, and that is the assertion.
``web_config_enabled`` is pinned as unset for the same kind of reason —
``run.sh`` never wrote that key either, and the config editor here is the
dashboard's Configuration tab rather than something the options switch on.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from astrameter.config.addon import AddonAppConfig

GOLDEN = json.loads(
    (Path(__file__).parent / "data" / "addon_golden_settings.json").read_text(
        encoding="utf-8"
    )
)


class GoldenSupervisor:
    """The Supervisor answers the fixture was recorded against."""

    base_url = "http://supervisor"

    def mqtt_service(self) -> dict[str, Any] | None:
        return GOLDEN["mqtt_service"]

    def addon_slug(self) -> str:
        return GOLDEN["mqtt"]["addon_slug"]


@pytest.fixture
def config() -> AddonAppConfig:
    return AddonAppConfig(GOLDEN["options"], GoldenSupervisor())


#: Options that replace this configuration rather than add to it, so they
#: cannot appear in the same recording: a custom config file takes over from
#: the add-on options entirely, and a broker URI replaces Home Assistant's own
#: broker (both have their own tests).
ALTERNATIVES = {"custom_config", "mqtt_uri"}


def test_the_fixture_covers_every_add_on_option():
    """A fixture that skips options would pin nothing about them."""
    from astrameter.config.addon_schema_test import schema_options

    missing = schema_options() - set(GOLDEN["options"]) - ALTERNATIVES
    assert not missing, f"options missing from the golden fixture: {sorted(missing)}"


def test_general_settings_match_the_add_on_2x_behaviour(config):
    assert asdict(config.general()) == GOLDEN["general"]


def test_ct_settings_match_the_add_on_2x_behaviour(config):
    assert asdict(config.ct("ct002")) == GOLDEN["ct002"]


def test_marstek_settings_match_the_add_on_2x_behaviour(config):
    assert asdict(config.marstek()) == GOLDEN["marstek"]


def test_mqtt_settings_match_the_add_on_2x_behaviour(config):
    insights = config.mqtt_insights()
    assert insights is not None
    assert asdict(insights) == GOLDEN["mqtt"]


def test_power_source_conditioning_matches_the_add_on_2x_behaviour(config):
    """The signal options land on the power source, not in the global defaults.

    Offsets, smoothing, the Hampel filter and the PID are applied where the
    meter is built, so asserting `general()` alone would leave their meaning
    unpinned.
    """
    meter, _, wait_for_next_message = config.powermeters(config.general())[0]
    expected = GOLDEN["power_source_signal"]

    assert wait_for_next_message == expected["wait_for_next_message"]
    stack = {}
    current = meter
    while hasattr(current, "wrapped_powermeter"):
        stack[type(current).__name__] = current
        current = current.wrapped_powermeter

    transform = stack["TransformedPowermeter"]
    assert transform.offsets == expected["offsets"]
    assert transform.multipliers == expected["multipliers"]
    assert (
        stack["ThrottledPowermeter"].throttle_interval == expected["throttle_interval"]
    )
    hampel = stack["HampelPowermeter"]
    assert hampel._window_size == expected["hampel_window"]
    assert hampel._n_sigma == expected["hampel_n_sigma"]
    assert hampel._min_threshold == expected["hampel_min_threshold"]
    smoothed = stack["SmoothedPowermeter"]
    assert smoothed._alpha == expected["smooth_alpha"]
    assert smoothed._max_step == expected["max_smooth_step"]
    assert stack["DeadbandPowermeter"]._deadband == expected["deadband"]
    pid = stack["PidPowermeter"]
    assert pid.kp == expected["pid_kp"]
    assert pid.ki == expected["pid_ki"]
    assert pid.kd == expected["pid_kd"]
    assert pid.output_max == expected["pid_output_max"]
    assert pid.mode == expected["pid_mode"]
