"""ESPHome codegen for the AstraMeter MQTT Insights component.

Publishes per-consumer state and Home Assistant Device Discovery for a
`ct002:` component, and (optionally) answers Marstek App polls on the
local MQTT broker. Mirrors `src/astrameter/mqtt_insights/service.py`
adapted for ESPHome's single-loop architecture — see mqtt_insights.h
for the architectural diff.

Required upstream YAML:

```yaml
mqtt:
  broker: 192.168.1.10   # configured separately, this component just
                         # speaks to the global mqtt client.
ct002:
  id: ct002_main
  ...

astrameter_mqtt_insights:
  ct002_id: ct002_main
```
"""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.const import CONF_ID

# Pulled in transitively (and we re-export the ct002 namespace, see below).
# json is required for build_json / parse_json — explicit so ESPHome bundles
# ArduinoJson into the build even if no other component pulls it in.
DEPENDENCIES = ["ct002", "mqtt", "json"]
CODEOWNERS = ["@tomquist"]

CONF_CT002_ID = "ct002_id"
CONF_BASE_TOPIC = "base_topic"
CONF_HA_DISCOVERY = "ha_discovery"
CONF_HA_DISCOVERY_PREFIX = "ha_discovery_prefix"
CONF_ADDON_SLUG = "addon_slug"
CONF_MARSTEK_MQTT_ENABLED = "marstek_mqtt_enabled"
CONF_MARSTEK_MQTT_INTERVAL = "marstek_mqtt_interval"
CONF_DEVICE_ID = "device_id"

astrameter_mqtt_insights_ns = cg.esphome_ns.namespace("astrameter_mqtt_insights")
MqttInsightsComponent = astrameter_mqtt_insights_ns.class_(
    "MqttInsightsComponent", cg.Component
)

# Forward-declared in C++; ESPHome's codegen needs the class object so
# `use_id(...)` resolves the variable typed correctly.
ct002_ns = cg.esphome_ns.namespace("ct002")
CT002Component = ct002_ns.class_("CT002Component", cg.Component)


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MqttInsightsComponent),
        cv.Required(CONF_CT002_ID): cv.use_id(CT002Component),
        # Per-installation namespace for everything we publish. Multiple
        # AstraMeter installs on one broker should each pick a distinct
        # base_topic (or the discovery dedupe will collide).
        cv.Optional(CONF_BASE_TOPIC, default="astrameter"): cv.string_strict,
        # Used in node_ids / unique_ids and in the device topic.
        # Defaults to the ct002 id slug, but can be overridden to keep a
        # stable identity when the ct002 id is renamed.
        cv.Optional(CONF_DEVICE_ID, default=""): cv.string,
        cv.Optional(CONF_HA_DISCOVERY, default=True): cv.boolean,
        cv.Optional(
            CONF_HA_DISCOVERY_PREFIX, default="homeassistant"
        ): cv.string_strict,
        cv.Optional(CONF_ADDON_SLUG, default=""): cv.string,
        cv.Optional(CONF_MARSTEK_MQTT_ENABLED, default=True): cv.boolean,
        cv.Optional(
            CONF_MARSTEK_MQTT_INTERVAL, default="300s"
        ): cv.positive_time_period_milliseconds,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    ct002_var = await cg.get_variable(config[CONF_CT002_ID])
    cg.add(var.set_ct002(ct002_var))
    # mqtt is wired via the global client at setup() time (see mqtt_insights.cpp);
    # we don't need to pass it explicitly here.

    device_id = config[CONF_DEVICE_ID] or str(config[CONF_CT002_ID])
    cg.add(var.set_device_id(device_id))
    cg.add(var.set_base_topic(config[CONF_BASE_TOPIC]))
    cg.add(var.set_ha_discovery(config[CONF_HA_DISCOVERY]))
    cg.add(var.set_ha_discovery_prefix(config[CONF_HA_DISCOVERY_PREFIX]))
    cg.add(var.set_addon_slug(config[CONF_ADDON_SLUG]))
    cg.add(var.set_marstek_mqtt_enabled(config[CONF_MARSTEK_MQTT_ENABLED]))
    cg.add(
        var.set_marstek_mqtt_interval_ms(
            int(config[CONF_MARSTEK_MQTT_INTERVAL].total_milliseconds)
        )
    )
