# Powermeter Configuration Reference (ESPHome external component)

When you run AstraMeter as the [ESPHome external
component](../README.md#esphome-external-component-run-on-an-esp32) on an ESP32,
the `ct002:` block does **not** talk to your meter directly. Instead it consumes
**any ESPHome `sensor`** that reports grid power in watts. So "configuring a
powermeter" here means: *give ESPHome a sensor that reads your meter, then point
`ct002:` at it.*

**Find your meter in the list below and copy the matching `sensor:` block.** Each
example defines a sensor with `id: grid_l1` and wires it in like this:

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]

# ... a sensor with id: grid_l1 from one of the sections below ...

ct002:
  id: ct002_main
  power_sensor_l1: grid_l1
  # power_sensor_l2: grid_l2   # three-phase only (define grid_l2 / grid_l3 too)
  # power_sensor_l3: grid_l3
```

Per-phase calibration/throttling (`offset:`, `multiply:`, `throttle:`) goes in
`filters:` **on the sensor**, not in `ct002:` — see the
[main README note](../README.md#esphome-external-component-run-on-an-esp32). For
the full annotated emulator config see
[`esphome.example.yaml`](../esphome.example.yaml). Running the Python add-on
instead? See [powermeters.md](powermeters.md).

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
- [TQ Energy Manager](#tq-energy-manager) — 🟠 Alternate (Modbus/MQTT)
- [HomeWizard](#homewizard) — 🟠 Alternate (local v1 HTTP, or native P1)
- [Enphase Envoy (IQ Gateway)](#enphase-envoy-iq-gateway) — 🔴 Not yet available
- [SMA Energy Meter](#sma-energy-meter) — 🔴 Not yet available

> **Script** (the Python `[SCRIPT]` source) has no ESPHome equivalent by design —
> an ESP32 can't run a host shell command — so it is intentionally omitted here.

## A note on the 🔵 Generic HTTP pattern

Several sources below have no dedicated ESPHome component but expose a JSON HTTP
endpoint. They all follow the same shape: poll the URL on an `interval:`, parse
the body, and publish into a `template` sensor. The
[`http_request`](https://esphome.io/components/http_request/) and
[`json`](https://esphome.io/components/json/) components are built in — no
external component needed.

```yaml
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
          url: http://192.168.1.50/some/endpoint
          capture_response: true
          on_response:
            then:
              - lambda: |-
                  json::parse_json(body, [](JsonObject root) -> bool {
                    id(grid_l1).publish_state(root["power"]);   // adapt the field
                    return true;
                  });
```

The per-source sections below only show the **URL** and the **lambda body** that
differ from this template.

## Shelly

**Tier: 🔵 Generic** (poll over the network) — or **🟢 Native** if the Shelly is
ESP32-based and you flash ESPHome onto it.

Most Shelly devices are reachable over HTTP from the ct002 ESP32. Gen2/Gen3/Pro
expose an RPC API; Gen1 a REST `/status`.

Single-phase (Shelly Plus 1PM / Pro family, RPC `apower`):

```yaml
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
```

Three-phase (Shelly 3EM Pro, RPC `EM.GetStatus`) — define `grid_l1/2/3` and:

```yaml
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
```

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
```

**Native alternative:** the device is ESP-based — flashing ESPHome lets you read
the underlying energy-monitor chip (CSE7766 / HLW8012 / BL0942 / ADE7953)
directly as a native sensor.

## Shrdzm

**Tier: 🔵 Generic.** The SHRDZM module serves `GET /getLastData?user=…&password=…`
returning OBIS keys; grid power is `1.7.0` (import) minus `2.7.0` (export):

```yaml
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
```

## Emlog

**Tier: 🔵 Generic.** EmLog serves
`GET /pages/getinformation.php?heute&meterindex=<n>` with `Leistung170`
(import) and `Leistung270` (export):

```yaml
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
```

## IoBroker

**Tier: 🔵 Generic.** Two options:

1. **HTTP (simpleAPI adapter)** — `GET /getPlainValue/<id>` returns a bare
   number you can publish directly, or `GET /getBulk/<id>` returns a JSON array:

   ```yaml
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
   ```

2. **MQTT** — if you run ioBroker's MQTT adapter, publish the state to a topic
   and read it with the native [`mqtt_subscribe`](#mqtt) sensor below (simpler
   and push-based).

## HomeAssistant

**Tier: 🟢 Native.** Use the built-in
[`homeassistant`](https://esphome.io/components/sensor/homeassistant/) sensor
platform — the ESP subscribes to a HA entity over the native API:

```yaml
api:        # native API link to Home Assistant is required

sensor:
  - platform: homeassistant
    id: grid_l1
    entity_id: sensor.grid_power
```

For three-phase, add `grid_l2` / `grid_l3` pointing at the per-phase entities.
This is the same data path the Python `[HOMEASSISTANT]` source uses, but pushed
to the ESP instead of polled from a server.

## VZLogger

**Tier: 🔵 Generic** — or **🟢 Native** by reading the meter directly.

vzlogger's HTTP interface serves `GET /<uuid>` with the latest tuple at
`data[0].tuples[0][1]`:

```yaml
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
```

**Native alternative:** vzlogger itself just reads a physical meter (usually
SML or DLMS/D0 over an IR head). You can skip vzlogger entirely and read that
meter directly on the ESP with the native [`sml`](#sml) component (or
[`dsmr`](https://esphome.io/components/sensor/dsmr/) for P1/D0), removing the
middleware.

## ESPHome

**Tier: 🟢 Native.** The Python `[ESPHOME]` source polls another ESPHome
device's web-server REST API. On the ESP32 there's no bridge to build — if your
grid-power source is already an ESPHome device, either:

- define that meter's sensor in the **same** YAML as `ct002:` (e.g. a native
  chip / Modbus / pulse-counter sensor with `id: grid_l1`), or
- import it from another ESPHome node via Home Assistant using the
  [`homeassistant`](#homeassistant) platform, or over [MQTT](#mqtt).

```yaml
sensor:
  - platform: homeassistant     # the other ESPHome node's entity, via HA
    id: grid_l1
    entity_id: sensor.other_esphome_grid_power
```

## AMIS Reader

**Tier: 🔵 Generic.** The AMIS reader serves `GET /rest` with a `saldo` field
(signed grid power):

```yaml
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
```

## Modbus

**Tier: 🟢 Native** — with one important caveat (see below). Use the built-in
[`modbus_controller`](https://esphome.io/components/sensor/modbus_controller/)
sensor:

```yaml
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
mqtt:
  broker: 192.168.1.10
  port: 1883

sensor:
  - platform: mqtt_subscribe
    id: grid_l1
    topic: home/powermeter
    unit_of_measurement: W
```

For a **JSON** payload (the Python `JSON_PATH` case), `mqtt_subscribe` only
handles bare floats, so extract the field with `on_json_message` into a template
sensor:

```yaml
mqtt:
  broker: 192.168.1.10
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
```

For three-phase, subscribe to three topics (or read three fields) into
`grid_l1/2/3`.

## JSON HTTP

**Tier: 🟢 Native** (generic). This is exactly the
[generic HTTP pattern](#a-note-on-the--generic-http-pattern) above — point the
URL at your endpoint and set the lambda to your JSON field. Headers and basic
auth are supported on the `http_request.get` action:

```yaml
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
```

## TQ Energy Manager

**Tier: 🟠 Alternate.** The Python `[TQ_EM]` source talks to the device's
proprietary session/login JSON API (`/start.php` + `/mum-webservice/data.php`),
which has no ESPHome port. However, the TQ Energy Manager (EM420 and similar)
also exposes **Modbus TCP/RTU and MQTT** interfaces — read it through one of
those instead:

- **Modbus** — wire the EM's RS485 port to the ESP and use the
  [`modbus_controller`](#modbus) sensor with the active-power register from the
  TQ Modbus register map.
- **MQTT** — enable the EM's MQTT export and use the [`mqtt_subscribe`](#mqtt)
  sensor.

## HomeWizard

**Tier: 🟠 Alternate.** The Python `[HOMEWIZARD]` source uses the v2 WebSocket
API (TLS + token), which has no ESPHome component. Two ESP-friendly paths:

1. **Local v1 HTTP API** (🔵 generic) — enable *Local API* in the HomeWizard
   app, then poll `GET /api/v1/data`; grid power is `active_power_w` (and
   `active_power_l1_w` … `_l3_w` for three-phase):

   ```yaml
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
   ```

2. **Read the P1 port directly** (🟢 native) — the HomeWizard dongle just reads
   your smart meter's P1 telegram. With your own P1-reader hardware you can skip
   the dongle and use ESPHome's native
   [`dsmr`](https://esphome.io/components/sensor/dsmr/) component (or
   [`sml`](#sml) for SML meters) on the ESP.

## Enphase Envoy (IQ Gateway)

**Tier: 🔴 Not yet available.** There is currently **no ESPHome component** for
the Enphase Envoy / IQ Gateway. The Python `[ENVOY]` source reads the local
`/production.json?details=1` endpoint, which requires:

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
the SMA **Speedwire** protocol. The Python `[SMA_ENERGY_METER]` source joins the
`239.12.255.254:9522` UDP multicast group and decodes SMA's binary OBIS channel
stream (validating the `SMA\0` magic, protocol id `0x6069`, SUSY/serial, then
reading the active-power channels and dividing the raw value by 10).

*To implement:* an external component that joins the multicast group on the ESP's
network interface and parses the Speedwire datagram (per-phase
`L1/L2/L3 = power_plus − power_minus`, else total). References: the AstraMeter
Python implementation in `src/astrameter/powermeter/sma_energy_meter.py`, and the
protocol as implemented by [sma2mqtt](https://github.com/vindolin/sma2mqtt) and
[SMA-Speedwire](https://github.com/J0B10/SMA-Speedwire).
