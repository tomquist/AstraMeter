# ESPHome External Component (run on an ESP32)

AstraMeter also ships as an **ESPHome external component**. It runs the
CT002/CT003 emulator, the balancer, and the cross-phase filter pipeline straight
on an ESP32 — no Python add-on, no Home Assistant. Use it if you'd rather flash
a dedicated board than run a server, and if ESPHome can already reach your
grid-power source (Modbus, M-Bus, Tasmota, MQTT, Shelly, Envoy, etc.).

> **Tip:** The
> [config generator](https://astrameter.com/generator.html) can
> produce a ready-to-flash ESPHome YAML — pick the "ESPHome (run on an ESP32)"
> target.

## Minimal YAML

Point `power_sensor_l1` at any ESPHome sensor that reports grid power:

```yaml
external_components:
  - source: github://tomquist/astrameter@2.3.1
    components: [ct002]

sensor:
  - platform: homeassistant       # or modbus_controller / mqtt / template / …
    id: grid_l1
    entity_id: sensor.grid_power

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

**Units:** the emulator works in watts internally. A sensor that declares
`unit_of_measurement: kW` (or `MW`/`mW`) is converted to W for you. A sensor
that declares a non-power unit (`°C`, `%`, `kWh`, …) is rejected at config
validation with an explicit error. A sensor with **no** declared unit is treated
as already reporting W. If such a sensor really feeds kW, typical household
values round to 0 W on the wire. The firmware warns you when readings look like
kW, but the fix is to declare the real unit, or to scale the value to W.

Everything else is optional. See **[`esphome.example.yaml`](../../esphome.example.yaml)**
for the complete, annotated config, with every knob shown at its default: it
covers three-phase sensors, the cross-phase filter pipeline (Hampel / smoothing
/ deadband / PID), balancer and saturation tuning, and the two optional
sub-blocks below. For the grid-power `sensor:` configuration per meter type (and
which meters the ESP doesn't support yet), see
**[esphome-powermeters.md](../esphome-powermeters.md)**.
AstraMeter ships one more external component for that: `tibber_pulse` reads a
Tibber Pulse through its bridge over your LAN (see
[Tibber Pulse](../esphome-powermeters.md#tibber-pulse)).

## Optional sub-blocks

Optional sub-blocks nest under the same `ct002:` key:

- **`dashboard:`** — serves AstraMeter's [live status dashboard](../dashboard.md)
  from the ESP32 itself: the same page the add-on shows, at
  `http://<device>/`. **On by default** — you only need the block to change
  something, and `dashboard: false` leaves it out of the firmware. It is
  read-only unless you add `controls: true`.
- **`mqtt_insights:`** — publishes Home Assistant Device Discovery (one device
  per battery, plus a parent CT002 device with manual-target / active /
  auto-target / distribution-weight controls and a force-rotation button). It
  also answers Marstek-app polls on your MQTT broker, so the emulator shows up in
  the app without hame-relay. Requires an `mqtt:` block. See
  [MQTT Insights](../mqtt-insights.md).
- **`marstek_registration:`** — registers a managed CT002/CT003 with your Marstek
  cloud account on first boot (the same flow as the Python `[MARSTEK]` section).
  It saves the assigned MAC and feeds it back into `ct002.ct_mac`. Requires an
  `http_request:` block. Add `mqtt_insights:` too and the App-topic subscription
  picks up the MAC on its own — no reboot needed.

## Status & requirements

**Status:** experimental — the UDP emulator, balancer, filter pipeline,
MQTT-insights, and Marstek cloud registration all work. Wider field testing
welcome.

**Requirements:** an ESP32 with ≥4 MB flash (the default for `esp32dev`,
`esp32-s3-devkitc-1`, etc.). ESP8266 is not supported in v1: its RAM and flash
budgets are too tight once HTTPS+TLS, MQTT, and the balancer are linked
together. Pick a board with `flash_size: 4MB` or larger. ESP-IDF builds may need
a custom partition table when you also add HTTPS+MQTT. There is no top-level
`flash_size:` YAML key — set it through your `board:` choice and, for ESP-IDF,
`esp32: framework: type: esp-idf` with suitable `sdkconfig_options:` or a
partition CSV.

## One important divergence from the Python emulator

Per-phase transforms and throttling are *not* part of `ct002:`. ESPHome's
standard `sensor: filters:` (`offset:`, `multiply:`, `throttle:`) handle them on
the upstream sensor instead. This matches the canonical order in Python
(`Transform → Throttle → Hampel → Smoothed → Deadband → PID`). Put per-phase
filters on the sensor itself, not after `ct002:` — they need to apply to the raw
input, not to the balancer's output.
