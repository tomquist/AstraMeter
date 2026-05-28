# Powermeter Configuration Reference (ESPHome external component)

When you run AstraMeter as the [ESPHome external
component](../README.md#esphome-external-component-run-on-an-esp32) on an ESP32,
the `ct002:` block does **not** talk to your meter directly. Instead it consumes
**any ESPHome `sensor`** that reports grid power in watts. So "configuring a
powermeter" here means: *give ESPHome a sensor that reads your meter, then point
`ct002:` at it.*

## How a reading reaches the emulator

There is no "powermeter" object in the ESPHome component — the integration is a
plain **id reference**. Every example below publishes a watts value into a sensor
whose `id` is `grid_l1` (and `grid_l2` / `grid_l3` for the other phases). The
`ct002:` block names those ids in its `power_sensor_l*` keys. That id match is
the entire wiring; whenever the sensor publishes a new value, the emulator picks
it up on its next Marstek CT002 poll — you never call `ct002:` directly.

**Each section below is a complete, copy-pasteable config** for one meter — from
`external_components:` through `ct002:`. To keep them focused, every example
**omits the `wifi:`, `api:`, `ota:`, and board (`esp32:`) blocks** — add those
for your hardware (see [`esphome.example.yaml`](../esphome.example.yaml) for a
full board config). What's shown is complete for the meter → emulator wiring.

Per-phase calibration/throttling (`offset:`, `multiply:`, `throttle:`) goes in
`filters:` **on the sensor**, not in `ct002:` — see the
[main README note](../README.md#esphome-external-component-run-on-an-esp32).
Running the Python add-on instead? See [powermeters.md](powermeters.md).

> The polling/lambda examples are **illustrative**. ESPHome's `http_request`,
> `json`, and lambda APIs differ slightly between releases — check the linked
> component docs for the exact syntax on your version.

## Support legend

| Tier | Meaning |
|------|---------|
| 🟢 **Native** | A built-in ESPHome component reads this exact source. |
| 🔵 **Generic** | No device-specific component, but ESPHome's built-in `http_request`+`json` or `mqtt_subscribe` reads it with a small lambda. |
| 🟠 **Alternate** | The exact API the Python class uses has no ESPHome port, but the *same device* also speaks a protocol ESPHome reads natively (Modbus/MQTT/P1). |
| 🔴 **Not yet available** | No practical way to read this on an ESP32 today. Documented so we know what to build. |

## Contents

- [Shelly](#shelly) — 🔵 Generic (or 🟢 native if you flash the Shelly)
- [Tasmota](#tasmota) — 🔵 Generic (or 🟢 native if you flash the device)
- [Shrdzm](#shrdzm) — 🔵 Generic
- [Emlog](#emlog) — 🔵 Generic
- [IoBroker](#iobroker) — 🔵 Generic
- [HomeAssistant](#homeassistant) — 🟢 Native
- [VZLogger](#vzlogger) — 🔵 Generic (or 🟢 native by reading the meter directly)
- [ESPHome](#esphome) — 🟢 Native (it's already ESPHome)
- [AMIS Reader](#amis-reader) — 🔵 Generic
- [Modbus](#modbus) — 🟢 Native (RS485 serial; see TCP caveat)
- [MQTT](#mqtt) — 🟢 Native
- [JSON HTTP](#json-http) — 🟢 Native (generic `http_request`)
- [SML](#sml) — 🟢 Native
- [TQ Energy Manager](#tq-energy-manager) — 🟠 Alternate (Modbus/MQTT)
- [HomeWizard](#homewizard) — 🟠 Alternate (local v1 HTTP, or native P1)
- [Enphase Envoy (IQ Gateway)](#enphase-envoy-iq-gateway) — 🔴 Not yet available
- [SMA Energy Meter](#sma-energy-meter) — 🔴 Not yet available

> **Script** (the Python `[SCRIPT]` source) has no ESPHome equivalent by design —
> an ESP32 can't run a host shell command — so it is intentionally omitted here.
>
> **The 🔵 generic HTTP sections** all share the same shape: a `template` sensor
> named `grid_l1` holds the value, an `interval:` polls the URL, and a lambda
> parses the JSON body with the built-in
> [`json::parse_json`](https://esphome.io/components/json/) helper and publishes
> into `grid_l1`. Only the URL and the lambda field differ between them. The
> [`http_request`](https://esphome.io/components/http_request/) and
> [`json`](https://esphome.io/components/json/) components are built in — no
> extra external component needed.

## Shelly

**Tier: 🔵 Generic** (poll over the network) — or **🟢 Native** if the Shelly is
ESP32-based and you flash ESPHome onto it.

Most Shelly devices are reachable over HTTP from the ct002 ESP32. Gen2/Gen3/Pro
expose an RPC API; Gen1 a REST `/status`. Single-phase (Shelly Plus 1PM / Pro
family, RPC `apower`):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.100/rpc/Switch.GetStatus?id=0
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["apower"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

Three-phase (Shelly 3EM Pro, RPC `EM.GetStatus`) — three template sensors, one
poll, all three phases on `ct002:`:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power
  - platform: template
    id: grid_l2
    unit_of_measurement: W
    device_class: power
  - platform: template
    id: grid_l3
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.100/rpc/EM.GetStatus?id=0
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["a_act_power"]);
                    id(grid_l2).publish_state(root["b_act_power"]);
                    id(grid_l3).publish_state(root["c_act_power"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
  power_sensor_l2: grid_l2
  power_sensor_l3: grid_l3
```

(This splits `EM.GetStatus` into per-phase readings; the Python `[SHELLY]`
`3EMPro` source instead reads the aggregate `total_act_power` from the same
response. Both are valid — use whichever your setup needs.)

Gen1 (Shelly 1PM/EM/3EM) expose `http://<ip>/status` with a `meters[]` /
`emeters[]` array — point the lambda at `root["emeters"][0]["power"]` etc.

**Native alternative:** Shelly hardware is ESP-based, so you can flash ESPHome
directly onto it and read its onboard energy chip (BL0942 / ADE7953 / ADE7880)
as a native sensor. If that Shelly is ESP32-based (e.g. Shelly Pro 3EM) it can
even run the `ct002:` component itself. See
[devices.esphome.io](https://devices.esphome.io/) for per-model configs.

## Tasmota

**Tier: 🔵 Generic** — or **🟢 Native** if you flash ESPHome onto the device.

Tasmota answers `GET /cm?cmnd=status%2010` with sensor JSON nested under
`StatusSNS`. Adapt the prefix/label to your meter (here `SML`/`Power`):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.101/cm?cmnd=status%2010
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["StatusSNS"]["SML"]["Power"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

**Native alternative:** the device is ESP-based — flashing ESPHome lets you read
the underlying energy-monitor chip (CSE7766 / HLW8012 / BL0942 / ADE7953)
directly as a native sensor.

## Shrdzm

**Tier: 🔵 Generic.** The SHRDZM module serves `GET /getLastData?user=…&password=…`
returning OBIS keys; grid power is `1.7.0` (import) minus `2.7.0` (export):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.102/getLastData?user=USER&password=PASS
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    float in = root["1.7.0"];
                    float out = root["2.7.0"];
                    id(grid_l1).publish_state(in - out);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

## Emlog

**Tier: 🔵 Generic.** EmLog serves
`GET /pages/getinformation.php?heute&meterindex=<n>` with `Leistung170`
(import) and `Leistung270` (export):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.103/pages/getinformation.php?heute&meterindex=0
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    float in = root["Leistung170"];
                    float out = root["Leistung270"];
                    id(grid_l1).publish_state(in - out);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

## IoBroker

**Tier: 🔵 Generic.** With ioBroker's simpleAPI adapter, `GET /getBulk/<id>`
returns a JSON array (`GET /getPlainValue/<id>` returns a bare number):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.104:8087/getBulk/Alias.0.power
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonArray arr) -> bool {
                    id(grid_l1).publish_state(arr[0]["val"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

**Alternative:** if you run ioBroker's MQTT adapter, publish the state to a topic
and read it with the native [`mqtt_subscribe`](#mqtt) sensor instead (simpler and
push-based).

## HomeAssistant

**Tier: 🟢 Native.** Use the built-in
[`homeassistant`](https://esphome.io/components/sensor/homeassistant/) sensor
platform — the ESP subscribes to a HA entity over the native API (so the `api:`
block, normally part of the omitted boilerplate, is shown here because it's
required for this source):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

api:        # native API link to Home Assistant is required for this source

sensor:
  - platform: homeassistant
    id: grid_l1
    entity_id: sensor.grid_power

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

For three-phase, add `grid_l2` / `grid_l3` sensors pointing at the per-phase
entities and set `power_sensor_l2` / `power_sensor_l3`. This is the same data
path the Python `[HOMEASSISTANT]` source uses, but pushed to the ESP instead of
polled from a server.

## VZLogger

**Tier: 🔵 Generic** — or **🟢 Native** by reading the meter directly.

vzlogger's HTTP interface serves `GET /<uuid>` with the latest tuple at
`data[0].tuples[0][1]`:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.106:8080/your-uuid
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["data"][0]["tuples"][0][1]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

**Native alternative:** vzlogger itself just reads a physical meter (usually
SML or DLMS/D0 over an IR head). You can skip vzlogger entirely and read that
meter directly on the ESP with the native [`sml`](#sml) component (or
[`dsmr`](https://esphome.io/components/sensor/dsmr/) for P1/D0), removing the
middleware.

## ESPHome

**Tier: 🟢 Native.** The Python `[ESPHOME]` source polls another ESPHome
device's web-server REST API. On the ESP32 there's no bridge to build — if your
grid-power source is already an ESPHome device, either define that meter's sensor
in the **same** YAML as `ct002:` (any native chip / Modbus / pulse-counter sensor
with `id: grid_l1`), or import another ESPHome node's entity via Home Assistant.
The latter, complete:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

api:        # required to import the other node's entity from Home Assistant

sensor:
  - platform: homeassistant     # the other ESPHome node's entity, via HA
    id: grid_l1
    entity_id: sensor.other_esphome_grid_power

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

You can also subscribe over [MQTT](#mqtt) if both nodes share a broker.

## AMIS Reader

**Tier: 🔵 Generic.** The AMIS reader serves `GET /rest` with a `saldo` field
(signed grid power):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.108/rest
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["saldo"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

## Modbus

**Tier: 🟢 Native** — with one important caveat (see below). Use the built-in
[`modbus_controller`](https://esphome.io/components/sensor/modbus_controller/)
sensor over an RS485 transceiver wired to the ESP:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

uart:
  id: mod_uart
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 9600
  stop_bits: 1

modbus:
  id: modbus1
  uart_id: mod_uart

modbus_controller:
  - id: meter
    address: 1            # Modbus unit / slave id
    modbus_id: modbus1
    update_interval: 1s

sensor:
  - platform: modbus_controller
    modbus_controller_id: meter
    id: grid_l1
    register_type: holding   # or read (input)
    address: 0
    value_type: U_WORD       # S_DWORD / U_DWORD / FP32 / … to match your meter
    unit_of_measurement: W

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

> **Modbus-TCP caveat.** ESPHome's `modbus_controller` is a **serial (RS485)**
> master — it does not open a raw Modbus-TCP socket the way the Python
> `[MODBUS]` source does with `TRANSPORT = TCP`. To read a network Modbus-TCP
> meter from the ESP you need either a wired RS485 connection to the meter, or a
> Modbus-TCP↔RTU gateway. Map `DATA_TYPE`/`BYTE_ORDER`/`WORD_ORDER` from your
> Python config onto `value_type` and the register's byte/word order.

## MQTT

**Tier: 🟢 Native.** For a plain numeric payload, use
[`mqtt_subscribe`](https://esphome.io/components/sensor/mqtt_subscribe/):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

mqtt:
  broker: 192.168.1.10
  port: 1883

sensor:
  - platform: mqtt_subscribe
    id: grid_l1
    topic: home/powermeter
    unit_of_measurement: W

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

For a **JSON** payload (the Python `JSON_PATH` case), `mqtt_subscribe` only
handles bare floats, so extract the field with `on_json_message` into a template
sensor:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

mqtt:
  broker: 192.168.1.10
  port: 1883
  on_json_message:
    topic: home/powermeter
    then:
      - sensor.template.publish:
          id: grid_l1
          state: !lambda 'return x["path"]["to"]["value"];'

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

For three-phase, subscribe to three topics (or read three fields) into
`grid_l1/2/3` and set `power_sensor_l2` / `power_sensor_l3`.

## JSON HTTP

**Tier: 🟢 Native** (generic `http_request`). Point the URL at your endpoint and
set the lambda to your JSON field. Headers and basic auth are supported on the
`http_request.get` action:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://example.com/api
          headers:
            Authorization: Bearer token
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["power"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

## SML

**Tier: 🟢 Native.** Smart meters that emit SML over an IR head map directly to
ESPHome's built-in [`sml`](https://esphome.io/components/sml/) component (the
ESP-side equivalent of the Python `[SML]` source). Wire a photo-transistor to a
UART RX pin, then select the OBIS register:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

uart:
  id: uart_bus
  rx_pin: GPIO16
  baud_rate: 9600
  data_bits: 8
  parity: NONE
  stop_bits: 1

sml:
  id: mysml
  uart_id: uart_bus

sensor:
  - platform: sml
    id: grid_l1
    sml_id: mysml
    obis_code: "1-0:16.7.0"     # aggregate active power (Python OBIS_POWER_CURRENT)
    unit_of_measurement: W
    # per-phase instead: 1-0:36.7.0 (L1), 1-0:56.7.0 (L2), 1-0:76.7.0 (L3)
    #   → add grid_l2 / grid_l3 sensors and set power_sensor_l2 / l3 below

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

The default `obis_code` here matches the Python source's default
`OBIS_POWER_CURRENT` (`0100100700ff` → `1-0:16.7.0`); the per-phase codes match
its `OBIS_POWER_L1/L2/L3` defaults.

## TQ Energy Manager

**Tier: 🟠 Alternate.** The Python `[TQ_EM]` source talks to the device's
proprietary session/login JSON API (`/start.php` + `/mum-webservice/data.php`),
which has no ESPHome port. However, the TQ Energy Manager (EM420 and similar)
also exposes **Modbus RTU/TCP and MQTT** — read it through one of those instead.

Via Modbus (RS485 to the EM; use the active-power register from the TQ Modbus
register map — `address` / `value_type` below are placeholders):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

uart:
  id: mod_uart
  tx_pin: GPIO17
  rx_pin: GPIO16
  baud_rate: 9600
  stop_bits: 1

modbus:
  id: modbus1
  uart_id: mod_uart

modbus_controller:
  - id: tq_em
    address: 1
    modbus_id: modbus1
    update_interval: 1s

sensor:
  - platform: modbus_controller
    modbus_controller_id: tq_em
    id: grid_l1
    register_type: holding
    address: 0               # ← set to the TQ active-power register
    value_type: S_DWORD      # ← match the register's type
    unit_of_measurement: W

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

Alternatively enable the EM's MQTT export and use the [`mqtt_subscribe`](#mqtt)
config above with the EM's power topic.

## HomeWizard

**Tier: 🟠 Alternate.** The Python `[HOMEWIZARD]` source uses the v2 WebSocket
API (TLS + token), which has no ESPHome component. Easiest ESP path: enable
*Local API* in the HomeWizard app and poll the **v1 HTTP API** at
`GET /api/v1/data`; grid power is `active_power_w` (and `active_power_l1_w` …
`_l3_w` for three-phase):

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

http_request:
  useragent: esphome/astrameter
  timeout: 5s

sensor:
  - platform: template
    id: grid_l1
    unit_of_measurement: W
    device_class: power

interval:
  - interval: 1s
    then:
      - http_request.get:
          url: http://192.168.1.110/api/v1/data
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["active_power_w"]);
                    return true;
                  });

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
```

**Native alternative:** the HomeWizard dongle just reads your smart meter's P1
telegram. With your own P1-reader hardware you can skip the dongle and use
ESPHome's native [`dsmr`](https://esphome.io/components/sensor/dsmr/) component
(or [`sml`](#sml) for SML meters) on the ESP.

## Enphase Envoy (IQ Gateway)

**Tier: 🔴 Not yet available.** There is currently **no ESPHome component** for
the Enphase Envoy / IQ Gateway, so there is no config to copy yet. The Python
`[ENVOY]` source reads the local `/production.json?details=1` endpoint, which
requires:

- **HTTPS to a self-signed certificate** on the gateway, and
- a **JWT bearer token** — either a long-lived static token or one fetched and
  refreshed via the Enphase Enlighten **cloud** (login → entrez token endpoint),
  including transparent re-auth on HTTP 401.

ESPHome's stock `http_request` can't comfortably do the cloud token exchange and
refresh loop, so this needs a **purpose-built external component**.

*To implement:* an external component that (1) holds a static JWT or performs the
Enlighten login + entrez token fetch over TLS, (2) GETs `production.json` from
the local gateway (TLS, self-signed), (3) parses the `consumption[] →
net-consumption` entry (per-phase `lines[].wNow`, else aggregate `wNow`), and
(4) re-auths on 401. References: the AstraMeter Python implementation in
`src/astrameter/powermeter/envoy.py`, plus existing ESP32 work in
[collin80/Envoy](https://github.com/collin80/Envoy) and
[Matthew1471/Enphase-API](https://github.com/Matthew1471/Enphase-API).

## SMA Energy Meter

**Tier: 🔴 Not yet available.** There is currently **no ESPHome component** for
the SMA **Speedwire** protocol, so there is no config to copy yet. The Python
`[SMA_ENERGY_METER]` source joins the `239.12.255.254:9522` UDP multicast group
and decodes SMA's binary OBIS channel stream (validating the `SMA\0` magic,
protocol id `0x6069`, SUSY/serial, then reading the active-power channels and
dividing the raw value by 10).

*To implement:* an external component that joins the multicast group on the ESP's
network interface and parses the Speedwire datagram (per-phase
`L1/L2/L3 = power_plus − power_minus`, else total). References: the AstraMeter
Python implementation in `src/astrameter/powermeter/sma_energy_meter.py`, and the
protocol as implemented by [sma2mqtt](https://github.com/vindolin/sma2mqtt) and
[SMA-Speedwire](https://github.com/J0B10/SMA-Speedwire).
