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
