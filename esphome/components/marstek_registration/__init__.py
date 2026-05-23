"""ESPHome codegen for the AstraMeter Marstek cloud-registration component.

Mirrors `src/astrameter/marstek_api.py`. Runs the same state machine as
Python's `ensure_managed_fake_device` (token → list → maybe-add → confirm)
against the configured Marstek base URL, then assigns the resulting MAC
to the bound `ct002:` so its responses + Marstek MQTT topics line up
with the cloud-side device record. MAC is persisted to ESPPreferences
so the second boot skips the cloud flow entirely.

Required upstream YAML:

```yaml
http_request:
  timeout: 20s

ct002:
  id: ct002_main

marstek_registration:
  ct002_id: ct002_main
  base_url: https://eu.hamedata.com
  mailbox: you@example.com
  password: !secret marstek_password
```
"""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import http_request
from esphome.const import CONF_ID, CONF_PASSWORD, CONF_TIMEZONE

DEPENDENCIES = ["ct002", "http_request"]
# AUTO_LOAD pulls in md5 and json without requiring the user to add
# `md5:` / `json:` blocks to their YAML. They're internal implementation
# details — md5 for hashing the Marstek password, json for parsing
# replies — so users shouldn't need to know about them.
AUTO_LOAD = ["md5", "json"]
CODEOWNERS = ["@tomquist"]

CONF_CT002_ID = "ct002_id"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_BASE_URL = "base_url"
CONF_MAILBOX = "mailbox"
CONF_DEVICE_TYPE = "device_type"
CONF_RETRY_INTERVAL = "retry_interval"
CONF_FORCE_REREGISTER = "force_reregister"

marstek_ns = cg.esphome_ns.namespace("marstek_registration")
MarstekRegistrationComponent = marstek_ns.class_(
    "MarstekRegistrationComponent", cg.Component
)

ct002_ns = cg.esphome_ns.namespace("ct002")
CT002Component = ct002_ns.class_("CT002Component", cg.Component)

DEVICE_TYPES = ("ct002", "ct003")


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(MarstekRegistrationComponent),
        cv.Required(CONF_CT002_ID): cv.use_id(CT002Component),
        cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
            http_request.HttpRequestComponent
        ),
        # Marstek cloud base URL — `https://eu.hamedata.com` for EU
        # accounts, `https://us.hamedata.com` for US. Mirrors Python's
        # MARSTEK_BASE_URL default.
        cv.Required(CONF_BASE_URL): cv.url,
        cv.Required(CONF_MAILBOX): cv.string_strict,
        cv.Required(CONF_PASSWORD): cv.string_strict,
        cv.Optional(CONF_TIMEZONE, default="Europe/Berlin"): cv.string_strict,
        cv.Optional(CONF_DEVICE_TYPE, default="ct002"): cv.one_of(
            *DEVICE_TYPES, lower=True
        ),
        # Backoff between transient-error retries. The Marstek cloud is
        # rate-limited; 60s matches what Python's config_loader does
        # implicitly via process-restart cadence.
        cv.Optional(
            CONF_RETRY_INTERVAL, default="60s"
        ): cv.positive_time_period_milliseconds,
        # Force the HTTP flow even if a MAC is already persisted. Useful
        # for re-registering after the cloud-side record was deleted.
        cv.Optional(CONF_FORCE_REREGISTER, default=False): cv.boolean,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    ct002_var = await cg.get_variable(config[CONF_CT002_ID])
    cg.add(var.set_ct002(ct002_var))

    http_var = await cg.get_variable(config[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http(http_var))

    cg.add(var.set_base_url(config[CONF_BASE_URL]))
    cg.add(var.set_mailbox(config[CONF_MAILBOX]))
    cg.add(var.set_password(config[CONF_PASSWORD]))
    cg.add(var.set_timezone(config[CONF_TIMEZONE]))
    cg.add(var.set_device_type(config[CONF_DEVICE_TYPE]))
    cg.add(
        var.set_retry_interval_ms(int(config[CONF_RETRY_INTERVAL].total_milliseconds))
    )
    cg.add(var.set_force_reregister(config[CONF_FORCE_REREGISTER]))
