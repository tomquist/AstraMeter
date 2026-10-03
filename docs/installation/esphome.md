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
  - source: github://tomquist/astrameter@develop
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

## Add the board to the Marstek app

A battery only follows a CT that is in your Marstek account and selected in
the battery's settings. Pick one of three ways to get the board there:

| Way | Use it when | Marstek login in the YAML |
|---|---|---|
| [Bluetooth](#with-bluetooth) (default) | Your ESP32 has Bluetooth: any variant but the ESP32-S2 and the ESP32-P4 | No |
| [Cloud registration](#with-cloud-registration) | The board has no Bluetooth, or you turned it off | Yes |
| [Replacing a real CT](#replacing-a-real-ct002ct003) | The board takes over from a CT002/CT003 already in your account | No |

### With Bluetooth

Bluetooth is on by default, so the minimal YAML above is all it takes.

1. Flash the board and let it join your Wi-Fi. The log shows
   `Advertising as MST-TPM_xxxx` (`ct_type: HME-4`, the default) or
   `MST-SMR_xxxx` (`ct_type: HME-3`).
2. In the Marstek app, tap **+** and wait for the scan, with the phone near
   the board. Pick the board when it shows up, and give it a name.
3. Go through the Wi-Fi step. The board stays on the Wi-Fi from its YAML
   whatever you pick there, and the step still completes. To let the app's
   choice take over instead, set:

   ```yaml
   ct002:
     bluetooth:
       allow_wifi_change: true
   ```

   The board then tries the network you pick and keeps it once it connects,
   or goes back to its old Wi-Fi after 30 seconds. The new network takes
   precedence over the one in the YAML.

4. The firmware check finds nothing to update; finish the setup.
5. In each battery's settings, switch to automatic mode and select the new CT.

While your phone is near the board, the app shows the CT's live grid power
over Bluetooth. The board doesn't connect to Marstek's cloud by default, so the
CT shows as offline in the device list; that is expected.

Like a real meter, the board accepts any phone in range without pairing, and
that phone can read its live data and restart it. With
`allow_wifi_change: true` it can also move the board to another Wi-Fi network,
which is why that is off by default.
`bluetooth: false` takes the Bluetooth stack out of the firmware entirely. The
[Bluetooth reference](../ct002-ct003-ble.md#astrameters-esphome-implementation)
lists everything the board answers.

### With cloud registration

Without Bluetooth, the board can create the CT in your account itself. It
logs in once, on its first boot, and saves the device it created:

```yaml
http_request:
  timeout: 20s

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
  marstek_registration:
    base_url: https://eu.hamedata.com   # https://us.hamedata.com in the US
    mailbox: you@example.com
    password: !secret marstek_password
    # device_type: ct003                # with ct_type: HME-3
```

1. Flash the board. Once it is online, the log shows the MAC it registered.
2. In the Marstek app, refresh the device list (or log out and back in).
   The new CT is called `AstraMeter CT002` or `AstraMeter CT003` and shows as
   offline; that is expected.
3. In each battery's settings, switch to automatic mode and select it.

Keep the `marstek_registration:` block afterwards: it doesn't contact the
cloud again, but it is what applies the saved MAC on every boot. To drop the
credentials from the YAML, set `ct_mac:` to the MAC from the log first.

With Bluetooth on as well, the board restarts once after its first
registration so its Bluetooth address matches the registered CT; the app then
connects to that CT over Bluetooth too. There is no need to add it again with
**+**.

### Replacing a real CT002/CT003

If the batteries already follow a real meter, the board can take its place:
set `ct_mac` to that meter's MAC (shown in the app's device details) and
`ct_type` to its model. The batteries keep their settings, and with Bluetooth
on, the app connects to the board as that meter. Unplug the real meter first,
so the two don't answer at the same time.

```yaml
ct002:
  power_sensor_l1: grid_l1
  ct_type: HME-4          # HME-3 for a CT003
  ct_mac: 0123456789ab    # the real meter's MAC
```

## Optional sub-blocks

Optional sub-blocks nest under the same `ct002:` key:

- **`dashboard:`** — serves AstraMeter's [live status dashboard](../dashboard.md)
  from the ESP32 itself: the same page the add-on shows, at
  `http://<device>/`. **On by default** — you only need the block to change
  something, and `dashboard: false` leaves it out of the firmware. It is
  read-only unless you add `controls: true`.
- **`bluetooth:`** — speaks a real CT002/CT003's Bluetooth protocol, so the
  Marstek app adds the board like a real meter and shows its live grid power.
  **On by default** on every ESP32 with a Bluetooth radio of its own (all but
  the ESP32-S2 and the ESP32-P4); `bluetooth: false` leaves the BLE stack out
  of the firmware, and `allow_wifi_change: true` inside the block lets the app
  change the board's Wi-Fi. See [With Bluetooth](#with-bluetooth).
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
  picks up the MAC on its own. See
  [With cloud registration](#with-cloud-registration).

## Upgrading from an earlier version

Versions before Bluetooth support keep working after the update without
changes to the YAML:

- **Bluetooth turns on by itself.** The board starts advertising as a CT, and
  everything else (batteries, MQTT, the dashboard) works as before. On a
  classic ESP32 that is short of memory, add `bluetooth: false`.
- **With `marstek_registration:`**, the board takes the registered CT's MAC
  as its Bluetooth address, so the CT already in your account opens in the app
  over Bluetooth. Don't add it again with **+**.
- **With `ct_mac` set**, the board answers the app as that CT. The MAC has to
  be a unicast MAC (lowest bit of the first byte clear), which a real meter's
  MAC and AstraMeter's own `02b250…` MACs are; otherwise the board warns at
  boot that the app can't reconnect.
- **With `power_save_mode: none`** under `wifi:`, remove it or add
  `bluetooth: false`. Wi-Fi and Bluetooth together need Wi-Fi modem sleep,
  which is ESPHome's default on an ESP32.

## Status & requirements

**Status:** experimental — the UDP emulator, balancer, filter pipeline,
MQTT-insights, and Marstek cloud registration all work. Adding the board over
Bluetooth has been tried with the Android app on an ESP32-S3; switching its
Wi-Fi from the app has not been tried yet. Wider field testing welcome.

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
