# Powermeter Configuration Reference (Python add-on / Docker / direct install)

This is the per-source reference for the Python AstraMeter (Home Assistant
add-on, Docker, or direct install). **Find your meter in the list below and copy
the matching `config.ini` section.**

These sections only cover the *source* of the grid-power reading. Options that
apply to **any** powermeter — throttling, wait-for-fresh-push, EMA smoothing,
deadband, Hampel outlier rejection — plus [Value
Transformation](../README.md#value-transformation), the [PID
Controller](../README.md#pid-controller), and running [Multiple
Powermeters](../README.md#multiple-powermeters) are documented in the main
[README](../README.md#configuration).

> **Running on an ESP32 instead of the Python add-on?** See
> [esphome-powermeters.md](esphome-powermeters.md) for the equivalent
> grid-power sensor configuration for the ESPHome external component.

## Contents

- [Shelly](#shelly)
- [Tasmota](#tasmota)
- [Shrdzm](#shrdzm)
- [Emlog](#emlog)
- [IoBroker](#iobroker)
- [HomeAssistant](#homeassistant)
- [VZLogger](#vzlogger)
- [ESPHome](#esphome)
- [AMIS Reader](#amis-reader)
- [Modbus (TCP/UDP)](#modbus-tcpudp)
- [MQTT](#mqtt)
- [JSON HTTP](#json-http)
- [TQ Energy Manager](#tq-energy-manager)
- [HomeWizard](#homewizard)
- [Enphase Envoy (IQ Gateway)](#enphase-envoy-iq-gateway)
- [SMA Energy Meter](#sma-energy-meter)
- [FRITZ!Smart Energy 250](#fritzsmart-energy-250)
- [Fronius Smart Meter](#fronius-smart-meter)
- [Tibber Pulse](#tibber-pulse)
- [Script](#script)
- [SML](#sml)

## Shelly

### Shelly 1PM
```ini
[SHELLY]
TYPE = 1PM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

### Shelly Plus 1PM
```ini
[SHELLY]
TYPE = PLUS1PM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

### Shelly EM
```ini
[SHELLY]
TYPE = EM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

### Shelly 3EM
```ini
[SHELLY]
TYPE = 3EM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

### Shelly 3EM Pro
```ini
[SHELLY]
TYPE = 3EMPro
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

## Tasmota

```ini
[TASMOTA]
IP = 192.168.1.101
USER = tasmota_user
PASS = tasmota_pass
JSON_STATUS = StatusSNS
JSON_PAYLOAD_MQTT_PREFIX = SML
JSON_POWER_MQTT_LABEL = Power
JSON_POWER_INPUT_MQTT_LABEL = Power1
JSON_POWER_OUTPUT_MQTT_LABEL = Power2
JSON_POWER_CALCULATE = True
```

For 3-phase meters, use comma-separated labels:

```ini
[TASMOTA]
IP = 192.168.1.101
JSON_STATUS = StatusSNS
JSON_PAYLOAD_MQTT_PREFIX = eBZ
JSON_POWER_MQTT_LABEL = Power_L1,Power_L2,Power_L3
```

For 3-phase with `JSON_POWER_CALCULATE`, provide matching comma-separated
input and output labels (counts must be equal):

```ini
[TASMOTA]
IP = 192.168.1.101
JSON_STATUS = StatusSNS
JSON_PAYLOAD_MQTT_PREFIX = SML
JSON_POWER_INPUT_MQTT_LABEL = Power_In_L1,Power_In_L2,Power_In_L3
JSON_POWER_OUTPUT_MQTT_LABEL = Power_Out_L1,Power_Out_L2,Power_Out_L3
JSON_POWER_CALCULATE = True
```

## Shrdzm

```ini
[SHRDZM]
IP = 192.168.1.102
USER = shrdzm_user
PASS = shrdzm_pass
```

## Emlog

```ini
[EMLOG]
IP = 192.168.1.103
METER_INDEX = 0
JSON_POWER_CALCULATE = True
```

## IoBroker

```ini
[IOBROKER]
IP = 192.168.1.104
PORT = 8087
CURRENT_POWER_ALIAS = Alias.0.power
POWER_CALCULATE = True
POWER_INPUT_ALIAS = Alias.0.power_in
POWER_OUTPUT_ALIAS = Alias.0.power_out
```

## HomeAssistant
```ini
[HOMEASSISTANT]
IP = 192.168.1.105
PORT = 8123
# Use HTTPS - if empty False is Fallback
HTTPS = ""|True|False
ACCESSTOKEN = YOUR_ACCESS_TOKEN
# The entity or entities (comma-separated for 3-phase) that provide current power
CURRENT_POWER_ENTITY = ""|sensor.current_power|sensor.phase1,sensor.phase2,sensor.phase3
# If False or Empty the power is not calculated - if empty False is Fallback
POWER_CALCULATE = ""|True|False 
# The entity ID or IDs (comma-separated for 3-phase) that provide power input
POWER_INPUT_ALIAS = ""|sensor.power_input|sensor.power_in_1,sensor.power_in_2,sensor.power_in_3
# The entity ID or IDs (comma-separated for 3-phase) that provide power output
POWER_OUTPUT_ALIAS = ""|sensor.power_output|sensor.power_out_1,sensor.power_out_2,sensor.power_out_3
# Is a Path Prefix needed?
API_PATH_PREFIX = ""|/core
# Per-powermeter throttling override (recommended: 2-3 seconds for HomeAssistant)
THROTTLE_INTERVAL = 2
```

Example: Variant 1 with a single combined input & output sensor
```ini
[HOMEASSISTANT]
IP = 192.168.1.105
PORT = 8123
HTTPS = True
ACCESSTOKEN = YOUR_ACCESS_TOKEN
CURRENT_POWER_ENTITY = sensor.current_power 
```

Example: Variant 2 with separate input & output sensors
```ini
[HOMEASSISTANT]
IP = 192.168.1.105
PORT = 8123
HTTPS = True
ACCESSTOKEN = YOUR_ACCESS_TOKEN
POWER_CALCULATE = True
POWER_INPUT_ALIAS = sensor.power_input
POWER_OUTPUT_ALIAS = sensor.power_output
```

Example: Variant 3 with three-phase power monitoring
```ini
[HOMEASSISTANT]
IP = 192.168.1.105
PORT = 8123
HTTPS = True
ACCESSTOKEN = YOUR_ACCESS_TOKEN
CURRENT_POWER_ENTITY = sensor.phase1,sensor.phase2,sensor.phase3
```

Example: Variant 4 with three-phase power calculation
```ini
[HOMEASSISTANT]
IP = 192.168.1.105
PORT = 8123
HTTPS = True
ACCESSTOKEN = YOUR_ACCESS_TOKEN
POWER_CALCULATE = True
POWER_INPUT_ALIAS = sensor.power_in_1,sensor.power_in_2,sensor.power_in_3
POWER_OUTPUT_ALIAS = sensor.power_out_1,sensor.power_out_2,sensor.power_out_3
# Per-powermeter throttling override (recommended: 2-3 seconds for HomeAssistant)
# THROTTLE_INTERVAL = 2
```

## VZLogger

```ini
[VZLOGGER]
IP = 192.168.1.106
PORT = 8080
UUID = your-uuid
```

For 3-phase meters, provide comma-separated UUIDs (one per phase); phases are
fetched in parallel:

```ini
[VZLOGGER]
IP = 192.168.1.106
PORT = 8080
UUID = uuid-l1, uuid-l2, uuid-l3
```

## ESPHome

```ini
[ESPHOME]
IP = 192.168.1.107
PORT = 6052
DOMAIN = your_domain
ID = your_id
```

## AMIS Reader

```ini
[AMIS_READER]
IP = 192.168.1.108
```

## Modbus (TCP/UDP)

```ini
[MODBUS]
HOST = 192.168.1.100
PORT = 502
UNIT_ID = 1
ADDRESS = 0
COUNT = 1
DATA_TYPE = UINT16
BYTE_ORDER = BIG
WORD_ORDER = BIG
REGISTER_TYPE = HOLDING  # or INPUT
TRANSPORT = TCP  # or UDP
```

`TRANSPORT` selects the Modbus transport: `TCP` (default) or `UDP`.

## MQTT

```ini
[MQTT]
BROKER = broker.example.com
PORT = 1883
TOPIC = home/powermeter
JSON_PATH = $.path.to.value (Optional for JSON payloads)
USERNAME = mqtt_user (Optional)
PASSWORD = mqtt_pass (Optional)
# Optional: connect over TLS (mqtts://) — default false
# TLS = false
# Per-powermeter throttling override
# THROTTLE_INTERVAL = 2
```

Instead of `BROKER`/`PORT`/`USERNAME`/`PASSWORD`/`TLS`, you can provide a single `URI` of the form `mqtt[s]://[user[:pass]@]host[:port]` (use `mqtts://` for TLS; credentials and port are optional). When `URI` is set, the individual `BROKER`/`PORT`/`USERNAME`/`PASSWORD`/`TLS` fields are ignored.

```ini
[MQTT]
URI = mqtts://user:pass@broker.example.com:8883
TOPIC = home/powermeter
```

The `JSON_PATH` option is used to extract the power value from a JSON payload. The path must be a [valid JSONPath expression](https://goessner.net/articles/JsonPath/).
If the payload is a simple integer value, you can omit this option.

Both `JSON_PATH` and `JSON_PATHS` are parsed with the [`jsonpath-ng` extended syntax](https://github.com/h2non/jsonpath-ng#extensions), so you can chain extensions like `` `split(...)` `` or `` `sub(/regex/, replacement)` `` to massage a payload value before it's converted to a float — for instance `` `$.state.`split( , 0, -1)` `` or `` `$.state.`sub(/[^0-9.\-]+$/, )` `` to strip a unit suffix like `"331.74 W"`. See the [JSON HTTP](#json-http) section below for more examples.

### Multi-phase MQTT

For three-phase setups, there are two options:

**Option 1: Multiple topics** — one topic per phase, each publishing a plain numeric value (or JSON with the same path):

```ini
[MQTT]
BROKER = broker.example.com
TOPICS = home/power/l1, home/power/l2, home/power/l3
```

**Option 2: Single topic with multiple JSON paths** — one topic publishing a JSON message containing all phases:

```ini
[MQTT]
BROKER = broker.example.com
TOPIC = home/powermeter
JSON_PATHS = $.phases[0].power, $.phases[1].power, $.phases[2].power
```

`TOPICS` takes precedence over `TOPIC`, and `JSON_PATHS` takes precedence over `JSON_PATH`. You can combine `TOPICS` with `JSON_PATH` (same path applied to each topic) or with `JSON_PATHS` (one path per topic, counts must match).

## JSON HTTP

```ini
[JSON_HTTP]
URL = http://example.com/api
# Comma separated JSON paths - single path for 1-phase or three for 3-phase
JSON_PATHS = $.power
USERNAME = user (Optional)
PASSWORD = pass (Optional)
# Additional headers separated by ';' using 'Key: Value'
HEADERS = Authorization: Bearer token
```

`JSON_PATHS` is parsed with the [`jsonpath-ng` extended syntax](https://github.com/h2non/jsonpath-ng#extensions), so you can chain extensions like `` `split(...)` `` or `` `sub(/regex/, replacement)` `` to massage the value before it's converted to a float. For example, an openHAB `Number:Power` item returns `"331.74 W"` — strip the unit with either of:

```ini
JSON_PATHS = $.state.`split( , 0, -1)`
JSON_PATHS = $.state.`sub(/[^0-9.\-]+$/, )`
```

## TQ Energy Manager

```ini
[TQ_EM]
IP = 192.168.1.100
#PASSWORD = pass
#TIMEOUT = 5.0 (Optional)
```

## HomeWizard

Reads a [HomeWizard](https://www.homewizard.com/) P1 dongle (or compatible device) over the local **WebSocket** API (`wss://`). Obtain a token once via `POST /api/user` while confirming on the device; see the [HomeWizard API docs](https://api-documentation.homewizard.com/docs/v2/).

```ini
[HOMEWIZARD]
IP = 192.168.1.110
TOKEN = YOUR_32_CHAR_HEX_TOKEN
SERIAL = your_device_serial
# Optional: disable TLS certificate verification on a trusted LAN if verification fails (default True)
# VERIFY_SSL = True
# THROTTLE_INTERVAL = 0
```

## Enphase Envoy (IQ Gateway)

Reads grid power from an [Enphase IQ Gateway / Envoy](https://enphase.com/installers/microinverters/iq-gateway) over the local HTTPS API (`/production.json?details=1`). The reading comes from the `net-consumption` measurement (positive = grid import, negative = export). Per-phase readings are reported automatically when the gateway exposes them; otherwise the aggregate single-phase value is used. Requires consumption CTs installed on the Envoy.

```ini
[ENVOY]
HOST = 192.168.1.120
# Option A: pre-obtained long-lived JWT (recommended)
TOKEN = eyJ...
# Option B: let AstraMeter fetch and refresh tokens via the Enphase Enlighten cloud
# USERNAME = you@example.com
# PASSWORD = your-enphase-password
# SERIAL = 123456789012
# Envoy ships a self-signed certificate; verification is disabled by default.
# VERIFY_SSL = False
```

**Token acquisition.** Generate a long-lived (~1 year) static token at <https://entrez.enphaseenergy.com/>. Alternatively, configure `USERNAME`/`PASSWORD`/`SERIAL` and AstraMeter will fetch a token on first use and refresh it automatically when the Envoy returns 401.

**TLS.** `VERIFY_SSL` defaults to `False` because Enphase does not publish a CA bundle for the IQ Gateway's self-signed certificate. This option **only affects the local Envoy connection** — Enphase Enlighten cloud requests (login and token endpoints) always verify TLS using the system trust store, regardless of this setting.

**MFA.** The auto-fetch flow does not support Enlighten accounts with multi-factor authentication enabled. Those users must supply a static `TOKEN`.

**CT direction.** If your readings have the wrong sign (export shows as import or vice versa), one or more CTs are mounted backwards. Flip them in software with the global `POWER_MULTIPLIER = -1` (or per-phase, e.g. `POWER_MULTIPLIER = 1, -1, 1`).

## SMA Energy Meter

Reads an [SMA Energy Meter](https://www.sma.de/) (EM 1.0/2.0) or Sunny Home Manager via the **Speedwire** multicast protocol (UDP). The listener joins the default multicast group and reports per-phase active power (L1, L2, L3). Use `SERIAL_NUMBER = 0` to auto-detect the first meter seen on the network, or set the device serial to pin a specific unit. Like other UDP-based features, this requires the host to receive multicast traffic (use Docker host networking or equivalent).

```ini
[SMA_ENERGY_METER]
MULTICAST_GROUP = 239.12.255.254
PORT = 9522
SERIAL_NUMBER = 0
# INTERFACE = 192.168.1.10
# THROTTLE_INTERVAL = 0
```

## FRITZ!Smart Energy 250

Reads grid power from an [AVM FRITZ!Smart Energy 250](https://fritz.com/en/products/fritz-smart-energy-250-20003088) smart-meter read head. The read head pairs with a FRITZ!Box over DECT and clips onto a digital electricity meter; AstraMeter polls the FRITZ!Box's [AHA-HTTP-Interface](https://fritz.com/fileadmin/user_upload/Global/Service/Schnittstellen/AHA-HTTP-Interface.pdf) (`getdevicelistinfos`) for the current reading.

> **⚠️ Power the read head over USB.** On battery the read head only refreshes its reading roughly **every 2 minutes** — far too slow for AstraMeter's ~1 s control loop, so battery balancing **will not work** (the batteries would be steered from a reading that's minutes stale). Connect the read head to USB power, which raises the update rate to ~10 s and makes it usable. Even at 10 s it's on the slow side; a meter that updates every second (e.g. SML/P1, Shelly, SMA) gives noticeably tighter control.

```ini
[FRITZ]
HOST = fritz.box
USER = smarthome
PASSWORD = your-fritzbox-password
AIN = 12345 0123456
# Reach the box over HTTPS (default False = http); FRITZ!Box certs are self-signed
# HTTPS = False
# VERIFY_SSL = True
# TIMEOUT = 10.0
# Power the read head via USB: on battery it only updates ~every 2 min, too slow
# for control. USB raises that to ~10 s, which this throttle matches.
THROTTLE_INTERVAL = 10
```

**Authentication.** Create a FRITZ!Box user with the **Smart Home** permission under *Home Network → FRITZ!Box Users* and put its name/password in `USER`/`PASSWORD`. AstraMeter logs in through `login_sid.lua` (supporting both the PBKDF2 and legacy MD5 challenge) and reuses the session, re-authenticating automatically if the box expires it.

**AIN.** Find the AIN under *Home Network → SmartHome* (open the device's detail/edit view). The read head exposes two sub-units under that base AIN: `-1` (*Strombezug* / grid import) and `-2` (*Einspeisung* / feed-in). Both report the **signed** instantaneous power (positive = import, negative = feed-in), so AstraMeter reads the `-1` branch as net grid power and appends `-1` automatically when no suffix is given. Spaces in the AIN are optional.

**Update rate.** USB power is effectively required (see the warning above): the ~2 min battery cadence is too slow for battery control, while USB raises it to ~10 s. `THROTTLE_INTERVAL = 10` then matches that USB cadence so AstraMeter doesn't hammer the box between fresh readings.

## Fronius Smart Meter

Reads a [Fronius Smart Meter](https://www.fronius.com/) through a Fronius
inverter's local [Solar API](https://www.fronius.com/en/solar-energy/installers-partners/technical-data/all-products/system-monitoring/open-interfaces/fronius-solar-api-json-). AstraMeter polls `GetMeterRealtimeData.cgi` and reads the signed
`PowerReal_P_Sum` (positive = grid import, negative = feed-in) — no token or
login is required on the local network.

```ini
[FRONIUS]
IP = 192.168.1.130
# Solar API meter device id; 0 is the first/only meter (default)
# DEVICE_ID = 0
```

**Sign.** `PowerReal_P_Sum` is reported signed, with positive = consumption from
the grid. If your readings come out reversed, flip them with the global
`POWER_MULTIPLIER = -1`.

**Per-phase.** By default the single signed sum is used. Set `PER_PHASE = True`
to report the three per-phase real powers (`PowerReal_P_Phase_1..3`) as L1/L2/L3
instead:

```ini
[FRONIUS]
IP = 192.168.1.130
PER_PHASE = True
```

> ⚠️ Only enable `PER_PHASE` if your meter reports **signed** per-phase power.
> Several meter firmwares report `PowerReal_P_Phase_*` *unsigned* (always
> positive), which would make exported power read as imported on each phase. If
> in doubt, leave it off and use the always-signed sum.

## Tibber Pulse

Reads a [Tibber Pulse](https://tibber.com/) locally through the **Pulse Bridge**,
with no dependency on the Tibber cloud. AstraMeter polls the bridge's
`/data.json` endpoint (HTTP Basic auth) and decodes the meter's SML telegram on
the fly, so this works with the SML smart meters the Pulse IR head is attached
to.

```ini
[TIBBER_PULSE]
IP = 192.168.1.140
# Username is always "admin"; the password is the nine-character code printed on
# the bridge (with the dash), e.g. AD56-54BA
PASSWORD = AD56-54BA
# Optional: node id (see http://<bridge>/nodes/); defaults to 1
# NODE_ID = 1
# Optional: override the Basic-auth user (defaults to "admin")
# USER = admin
## Optional OBIS overrides (12 hex digits; omit to use eHZ-style defaults)
# OBIS_POWER_CURRENT = 0100100700ff
# OBIS_POWER_L1 = 0100240700ff
# OBIS_POWER_L2 = 0100380700ff
# OBIS_POWER_L3 = 01004c0700ff
```

**Enable the local API first.** In the bridge's web UI open the *params* page, set
`webserver-force-enable` to `true`, save, and **Store params to flash**. Without
this the `/data.json` endpoint is not served.

**Multi-phase.** Like the [SML](#sml) source, if the meter reports per-phase
active power for L1–L3 those three values are used; otherwise the aggregate
register is used as a single reading. Override the OBIS codes only if your meter
uses different registers.

**Update rate.** SML meters refresh roughly every few seconds, so a
`THROTTLE_INTERVAL` of `2`–`3` avoids hammering the bridge between fresh
telegrams.

**Sign.** Power is signed (positive = import, negative = feed-in). If your
readings are reversed, flip them with the global `POWER_MULTIPLIER = -1`.

## Script

You can also use a custom script to get the power values. The script should output at most 3 integer values, separated by a line break.
```ini
[SCRIPT]
COMMAND = /path/to/your/script.sh
```

## SML

```ini
[SML]
SERIAL = /dev/ttyUSB0
# Optional: override default OBIS hex registers (12 hex digits each; defaults match common German eHZ meters)
#OBIS_POWER_CURRENT = 0100100700ff
#OBIS_POWER_L1 = 0100240700ff
#OBIS_POWER_L2 = 0100380700ff
#OBIS_POWER_L3 = 01004c0700ff
```

Read from a powermeter that is connected via USB and that transmits SML (Smart Message Language) data via an IR head. **`SERIAL` is required**: local device path to the serial interface (e.g. `/dev/ttyUSB0` on Linux).

**Multi-phase:** If the meter exposes per-phase instantaneous active power for L1–L3 (`Summenwirkleistung` / default OBIS above), those three values are used automatically. Otherwise the aggregate instantaneous power register (`aktuelle Wirkleistung` / `OBIS_POWER_CURRENT`) is used as a single reading. When both are present in the same SML list, per-phase values take precedence.

**OBIS overrides:** Only needed if your meter uses different register addresses; values must be exactly 12 hexadecimal characters (lowercase or uppercase).
