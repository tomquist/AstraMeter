# ESPHome External Component (run on an ESP32)

AstraMeter also ships as an **ESPHome external component** that runs the
CT002/CT003 emulator, balancer, and cross-phase filter pipeline directly on an
ESP32 — no Python add-on, no Home Assistant required. Useful if you'd rather
flash a dedicated board than run a server, and if your grid-power source is
already addressable by ESPHome (Modbus, M-Bus, Tasmota, MQTT, Shelly, Envoy,
etc.).

> **Tip:** The
> [config generator](https://astrameter.com/generator.html) can
> produce a ready-to-flash ESPHome YAML — pick the "ESPHome (run on an ESP32)"
> target.

## Minimal YAML

Point `power_sensor_l1` at any ESPHome sensor that reports grid power in watts:

```yaml
external_components:
  - source: github://tomquist/astrameter@2.2.0
    components: [ct002]

sensor:
  - platform: homeassistant       # or modbus_controller / mqtt / template / …
    id: grid_l1
    entity_id: sensor.grid_power

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

Everything else is optional. See **[`esphome.example.yaml`](../../esphome.example.yaml)**
for the complete, annotated config — three-phase sensors, the cross-phase filter
pipeline (Hampel / smoothing / deadband / PID), balancer and saturation tuning,
and the two optional sub-blocks below — with every knob shown at its default. For
the grid-power `sensor:` configuration per meter type (and which meters aren't
supported on the ESP yet), see **[esphome-powermeters.md](../esphome-powermeters.md)**.

## Optional sub-blocks

Two optional sub-blocks nest under the same `ct002:` key:

- **`mqtt_insights:`** — publishes Home Assistant Device Discovery (one device
  per battery + a parent CT002 device with manual-target / active / auto-target /
  distribution-weight controls and a force-rotation button) and answers
  Marstek-app polls on your MQTT broker, so the emulator shows up in the app
  without hame-relay. Requires an `mqtt:` block. See
  [MQTT Insights](../mqtt-insights.md).
- **`marstek_registration:`** — registers a managed CT002/CT003 with your Marstek
  cloud account on first boot (same flow as the Python `[MARSTEK]` section),
  persists the assigned MAC, and feeds it back into `ct002.ct_mac`. Requires an
  `http_request:` block. When combined with `mqtt_insights:`, the App-topic
  subscription picks up the MAC automatically — no reboot needed.

## Status & requirements

**Status:** experimental — UDP emulator, balancer, filter pipeline,
MQTT-insights, and Marstek cloud registration are all functional. Wider field
testing welcome.

**Requirements:** ESP32 with ≥4 MB flash (default for `esp32dev`,
`esp32-s3-devkitc-1`, etc.). ESP8266 is not supported in v1 — RAM and flash
budgets are too tight once HTTPS+TLS, MQTT, and the balancer are linked together.
Pick a board with `flash_size: 4MB` or larger; for ESP-IDF builds you may need a
custom partition table when you also add HTTPS+MQTT — there is no top-level
`flash_size:` YAML key, set it via your `board:` choice and (for ESP-IDF)
`esp32: framework: type: esp-idf` with appropriate `sdkconfig_options:` or a
partition CSV.

## One important divergence from the Python emulator

Per-phase transforms and throttling are *not* part of `ct002:` — they're
delegated to ESPHome's standard `sensor: filters:` (`offset:`, `multiply:`,
`throttle:`) on the upstream sensor. This matches the canonical order in Python
(`Transform → Throttle → Hampel → Smoothed → Deadband → PID`). Put per-phase
filters on the sensor itself, not after `ct002:` — they need to apply to the raw
input, not the balancer's output.
