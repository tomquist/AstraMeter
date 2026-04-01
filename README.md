# B2500 Meter

This project emulates Smart Meter devices for Marstek storage systems such as the B2500, Marstek Jupiter, and Marstek Venus energy storage systems while allowing integration with almost any smart meter. It does this by emulating one or more of the following devices:
- CT002 / CT003 (Marstek CT protocol; use for **multiple** storage devices)
- Shelly Pro 3EM
  - Uses port 1010 (B2500 firmware up to version 224) and port 2220 (B2500 firmware version 226+)
  - Can be specifically targeted with shellypro3em_old (port 1010) or shellypro3em_new (port 2220)
- Shelly EM gen3
- Shelly Pro EM50

**Note:** Use **CT002** or **CT003** when you steer **multiple** storage devices; use a **Shelly** device type (`shellypro3em`, `shellyemg3`, `shellyproem50`, …) otherwise. See [Configuration](#configuration) and [docs/ct002-ct003-protocol.md](docs/ct002-ct003-protocol.md) for CT002/CT003.

## Getting Started

The B2500 Meter project can be installed and run in several ways depending on your needs and environment:

1. **Home Assistant Add-on** (Recommended for Home Assistant users)
   - Easiest installation method if you're using Home Assistant
   - Provides a user-friendly interface for configuration
   - Integrates seamlessly with your Home Assistant installation

2. **Docker** (Recommended for standalone server deployment)
   - Containerized solution that works on any Docker-compatible system
   - Easy deployment and updates
   - Consistent environment across different platforms

3. **Direct Installation** (For development or custom setups)
   - Manual installation on Windows, macOS, or Linux
   - Requires Python environment setup
   - More flexible for customization and development

### Home Assistant Add-on Installation

1. **Add the Repository to Home Assistant**

   [![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fb2500-meter)

3. **Install the Add-on**
   - Click on "Add-on Store" in the bottom right corner
   - The B2500 Meter add-on should appear in the add-on store
   - Click on it and then click "Install"

4. **Configure the Add-on**
   You can configure the add-on in two ways:

   A) Using the Add-on Configuration Interface:
   - After installation, go to the add-on's Configuration tab
   - For single-phase monitoring:
     - Set the `Power Input Entity ID` and optionally the `Power Output Entity ID` to the entity IDs of your power sensors
   - For three-phase monitoring:
     - Set the `Power Input Entity ID` to a comma-separated list of three entity IDs (one for each phase)
     - If using calculated power, also set the `Power Output Entity ID` to a comma-separated list of three entity IDs
     - Example: `sensor.phase1,sensor.phase2,sensor.phase3`
   - Set `Device Types` (comma-separated list) to the device types you want to emulate:
     - `ct002`: CT002 emulator (Marstek CT002 protocol)
     - `ct003`: CT003 emulator (same protocol as CT002)
     - `shellypro3em`: Shelly Pro 3EM emulator (uses both ports 1010 and 2220 for compatibility with all B2500 firmware versions)
     - `shellypro3em_old`: Shelly Pro 3EM emulator using port 1010 (for B2500 firmware up to v224)
     - `shellypro3em_new`: Shelly Pro 3EM emulator using port 2220 (for B2500 firmware v226+)
     - `shellyemg3`: Shelly EM gen3 emulator
     - `shellyproem50`: Shelly Pro EM50 emulator
     
     **Tip:** Use `ct002`/`ct003` for multiple devices; use a Shelly type (e.g. `shellypro3em` or `_old`/`_new`) otherwise.
   - Click "Save" to apply the configuration

   B) Using a Custom Configuration File for Advanced Configuration:
   - Create a `config.ini` file based on the examples in the [Configuration](#configuration) section
   - Place the file in `/addon_configs/a0ef98c5_b2500_meter/`. You can do that via "File editor" Addon in Home Assistant. Make sure to disable the "Enforce Basepath" setting in the File editor Addon config to access the `/addon_configs` folder.
   - In the add-on configuration, set `Custom Config` to the filename (e.g., "config.ini" without the path)
   - When using a custom configuration file, other configuration options will be ignored

5. **Start the Add-on**
   - Go to the add-on's Info tab
   - Click "Start" to run the add-on

### Docker Installation

#### Prerequisites
- Docker installed on your system
- Docker Compose (optional, but recommended)

#### Installation Steps
1. Create a directory for the project
2. Create your `config.ini` file
3. Use the provided `docker-compose.yaml` to start the container:
   ```bash
   docker-compose up -d
   ```
   You can control the verbosity by setting the `LOG_LEVEL` environment
   variable (for example `-e LOG_LEVEL=debug`). If not set the container
   defaults to `info`.
Note: Host network mode is required because the B2500 device uses UDP broadcasts for device discovery. Without host networking, the container won't be able to receive these broadcasts properly.

### Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with the **`next`** tag on GitHub Container Registry. These track the latest changes before a stable release and **may be less stable** than **`latest`**—use them to try fixes early or to validate the add-on before it lands on **`main`**.

**Home Assistant Add-on**

1. Add the repository pointing at the **`develop`** branch (same flow as [Home Assistant Add-on Installation](#home-assistant-add-on-installation), but use this URL):

   `https://github.com/tomquist/b2500-meter#develop`

   [![Add develop repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fb2500-meter%23develop)

2. Install or update the **B2500 Meter** add-on from the store. Supervisor will pull the **`next`**-tagged image (`ghcr.io/tomquist/b2500-meter-addon:next`).

To return to stable releases, remove this repository and add the normal URL without `#develop` ([step 1 under Home Assistant Add-on Installation](#home-assistant-add-on-installation)), then reinstall or wait for an update to the **`latest`** track.

**Docker**

Use the **`next`** image instead of **`latest`** in `docker-compose.yaml` (or `docker run`):

```yaml
image: ghcr.io/tomquist/b2500-meter:next
```

### Direct Installation

#### Prerequisites

1. **Python Installation:** Use Python **3.10 or newer** (see [CONTRIBUTING.md](CONTRIBUTING.md)). You can download Python from the [official Python website](https://www.python.org/downloads/).
2. **Configuration:** Create a `config.ini` file in the root directory of the project and add the appropriate configuration as described in the [Configuration](#configuration) section.

#### Installation Steps

1. **Open Terminal/Command Prompt**
   - Windows: Press `Win + R`, type `cmd`, press Enter
   - macOS: Press `Cmd + Space`, type `Terminal`, press Enter
   - Linux: Use your preferred terminal emulator

2. **Navigate to Project Directory**
   ```bash
   cd path/to/b2500-meter
   ```

3. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)** (dependency manager).

4. **Install dependencies and run**
   ```bash
   uv sync
   uv run b2500-meter
   ```
   With dev tools (tests, ruff, mypy): `uv sync --extra dev`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

All commands above work across Windows, macOS, and Linux. The only difference is how you open your terminal.

## Additional Notes

When the script is running, switch your B2500 to "Self-Adaptation" mode to enable the powermeter functionality.

For details on the CT002/CT003 UDP protocol used by Marstek storage systems, see [docs/ct002-ct003-protocol.md](docs/ct002-ct003-protocol.md).

## Configuration

Configuration is managed via `config.ini`. Each powermeter type has specific settings.

### General Configuration

```ini
[GENERAL]
# Use ct002/ct003 for multiple storage devices; use shelly* types otherwise.
# Comma-separated list of device types to emulate (ct002, ct003, shellypro3em, shellyemg3, shellyproem50, shellypro3em_old, shellypro3em_new)
DEVICE_TYPE = shellypro3em
# Optional: comma-separated device IDs, same order as DEVICE_TYPE (auto-generated if omitted). Use for stable IDs across reinstalls or to match an existing device.
#DEVICE_IDS = shellypro3em-c59b15461a21
# Skip initial powermeter test on startup
SKIP_POWERMETER_TEST = False
# Global throttling interval in seconds to prevent control instability or oscillation
# Set to 0 to disable throttling (default). Recommended: 1-3 seconds for slow data sources
# Can be overridden per powermeter section
THROTTLE_INTERVAL = 0
```

Per-powermeter options (e.g. in `[TASMOTA]`):
- **THROTTLE_INTERVAL** — Override global throttling for this powermeter

CT002/CT003 active-steering options (all under `[CT002]` or `[CT003]`):
- **ACTIVE_CONTROL** — When true (default), the emulator smooths the grid reading, splits
  the target across batteries, and balances their load.
  When false, the emulator relays raw meter values and batteries decide on their own.

*Smoothing — how fast the emulator tracks grid changes:*
- **SMOOTH_TARGET_ALPHA** (default 0.9) — EMA factor for the grid reading. Higher values
  track load changes faster; lower values filter noise but add lag. The battery's own ramp
  rate already filters noise, so values close to 1.0 work well when the powermeter updates
  at ≥ 1 Hz. Reduce toward 0.3 if your powermeter updates significantly slower than 1 Hz
  (higher lag in readings needs heavier smoothing to avoid oscillation).
- **DEADBAND** (default 20 W) — When the grid total is within ± this value, the smoothed
  target decays toward zero instead of chasing noise. Keeps batteries from hunting around
  the zero-crossing. 10–30 W is a sensible range; set to 0 to disable.
- **MAX_SMOOTH_STEP** (default 0 = unlimited) — Maximum watts the smoothed target may
  change per request cycle. Acts as a slew-rate limit. Rarely needed at high alpha.

*Fair distribution — balancing load across multiple batteries:*
- **FAIR_DISTRIBUTION** (default true) — Adjust each battery's target so they share the
  load evenly. Only matters with two or more batteries.
- **BALANCE_GAIN** (default 0.2) — How aggressively to correct imbalance between batteries.
  0.0 = no correction (equal split only); 0.3–0.5 = faster rebalancing but may overshoot.
- **BALANCE_DEADBAND** (default 15 W) — Ignore imbalance smaller than this.
  Prevents micro-corrections when batteries are already close.
- **MAX_CORRECTION_PER_STEP** (default 80 W) — Cap on the per-cycle balance correction.
  Limits how much a single battery's target can deviate from its fair share in one step.
- **ERROR_BOOST_THRESHOLD** / **ERROR_BOOST_MAX** (defaults 150 W / 0.5) — When the
  imbalance exceeds the threshold, the balance gain is multiplied by up to
  (1 + ERROR_BOOST_MAX). With the defaults, effective gain rises from 0.2 to at most 0.3
  at ≥ 150 W imbalance. Helps large imbalances converge faster.
- **ERROR_REDUCE_THRESHOLD** (default 20 W) — Below this imbalance, the gain is scaled
  down proportionally, producing gentler corrections as batteries approach equilibrium.
- **MAX_TARGET_STEP** (default 0 = unlimited) — Maximum change in a battery's target
  relative to its current output. A hard clamp on per-cycle change.

*Saturation detection — handling full/empty batteries:*
- **SATURATION_DETECTION** (default true) — Track how well each battery follows its
  target. When a battery cannot deliver (full or empty), its share is reduced and
  redistributed to others.
- **SATURATION_ALPHA** (default 0.15) — EMA factor for the saturation score.
  Lower = slower to declare a battery saturated (and slower to recover).
- **MIN_TARGET_FOR_SATURATION** (default 20 W) — Ignore saturation tracking when
  the target is below this value (avoids false positives at low power).

### CT002 / CT003

```ini
[CT002]
# CT type is derived from the emulated device (ct002 -> HME-4, ct003 -> HME-3).
# CT MAC (12 hex digits, from Marstek app).
# If empty, the emulator accepts any request CT MAC and echoes the request’s
# CT MAC in responses. If set, the emulator responds only to that CT MAC.
CT_MAC = 001122334455
# UDP port to bind for CT002/CT003 (default 12345).
UDP_PORT = 12345
# WiFi RSSI reported to the storage system
WIFI_RSSI = -50
# Ignore repeated requests from the same client within this window (seconds)
DEDUPE_TIME_WINDOW = 0
# Forget consumers after this many seconds without updates (multi-consumer support)
CONSUMER_TTL = 120
```

Optional Marstek cloud auto-registration:
- **MARSTEK.ENABLE** — auto-create/check managed fake CT device(s) at startup
- **MARSTEK.MAILBOX / PASSWORD** — credentials used to call Marstek API
- For `ct002` a managed `HME-4` device is ensured, for `ct003` a managed `HME-3` device.
- Device fields created by b2500-meter:
  - `devid == mac` (random lowercase hex)
  - `bluetooth_name = MST-SMR_<last4(mac)>`
  - `name = B2500-Meter CT002` / `B2500-Meter CT003`
- If a matching managed device of expected type already exists, no new device is created.
- Important behavior notes:
  - Managed fake CT devices appear as **offline** in the app CT list (expected behavior).
  - Refresh the CT device list after registration (or log out/in if needed). Then select `B2500-Meter CT002` / `B2500-Meter CT003`, switch battery mode to automatic, and choose that CT. It should be selectable as soon as it appears in the device list.
  - Marstek credentials are only needed for one-time registration. You can remove `MARSTEK.MAILBOX` / `MARSTEK.PASSWORD` immediately after registration succeeds (or if the managed device already exists).
  - If you use Home Assistant add-on `custom_config`, values from that file take precedence over add-on UI fields.

### Value Transformation

You can optionally apply a linear transformation to the power values returned by any powermeter. This is useful for calibrating readings (e.g., correcting a consistent offset) or scaling values (e.g., adjusting for a CT clamp ratio).

The formula applied to each value is: `value * POWER_MULTIPLIER + POWER_OFFSET`

For example, if your meter reads 1050W and you set `POWER_MULTIPLIER=0.95` and `POWER_OFFSET=-50`, the result is `1050 * 0.95 + (-50) = 947.5W`.

Both settings are optional and can be added to any powermeter section:
- `POWER_MULTIPLIER` — Scales each power value. Default: 1 (no scaling).
- `POWER_OFFSET` — Added to each power value after the multiplier is applied. Default: 0 (no offset).

For three-phase meters, you can specify a single value (applied to all phases) or comma-separated values (one per phase):

```ini
# Single value — applies to all phases
[SHELLY_1]
TYPE = 1PM
IP = 192.168.1.100
POWER_OFFSET = -50
POWER_MULTIPLIER = 1.05

# Per-phase values — if the list length does not match the device phase count,
# values are applied cyclically and a runtime warning is emitted
[SHELLY_2]
TYPE = 3EMPro
IP = 192.168.1.101
POWER_OFFSET = -50,-30,-40
POWER_MULTIPLIER = 1.05,1.02,1.03

# Flip the sign of all readings (e.g. when import/export polarity is reversed)
[SHELLY_3]
TYPE = 1PM
IP = 192.168.1.102
POWER_MULTIPLIER = -1

# Null a single phase on a three-phase meter
[SHELLY_4]
TYPE = 3EMPro
IP = 192.168.1.103
POWER_MULTIPLIER = 1,0,1
```

**Note:** Transforms are applied when readings are taken from the powermeter, before values are passed to the emulated device (Shelly, CT002/CT003, etc.).

### Shelly

#### Shelly 1PM
```ini
[SHELLY]
TYPE = 1PM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

#### Shelly Plus 1PM
```ini
[SHELLY]
TYPE = PLUS1PM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

#### Shelly EM
```ini
[SHELLY]
TYPE = EM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

#### Shelly 3EM
```ini
[SHELLY]
TYPE = 3EM
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

#### Shelly 3EM Pro
```ini
[SHELLY]
TYPE = 3EMPro
IP = 192.168.1.100
USER = username
PASS = password
METER_INDEX = meter1
```

### Tasmota

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

### Shrdzm

```ini
[SHRDZM]
IP = 192.168.1.102
USER = shrdzm_user
PASS = shrdzm_pass
```

### Emlog

```ini
[EMLOG]
IP = 192.168.1.103
METER_INDEX = 0
JSON_POWER_CALCULATE = True
```

### IoBroker

```ini
[IOBROKER]
IP = 192.168.1.104
PORT = 8087
CURRENT_POWER_ALIAS = Alias.0.power
POWER_CALCULATE = True
POWER_INPUT_ALIAS = Alias.0.power_in
POWER_OUTPUT_ALIAS = Alias.0.power_out
```

### HomeAssistant
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

### VZLogger

```ini
[VZLOGGER]
IP = 192.168.1.106
PORT = 8080
UUID = your-uuid
```

### ESPHome

```ini
[ESPHOME]
IP = 192.168.1.107
PORT = 6052
DOMAIN = your_domain
ID = your_id
```

### AMIS Reader

```ini
[AMIS_READER]
IP = 192.168.1.108
```

### Modbus TCP

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
```

### MQTT

```ini
[MQTT]
BROKER = broker.example.com
PORT = 1883
TOPIC = home/powermeter
JSON_PATH = $.path.to.value (Optional for JSON payloads)
USERNAME = mqtt_user (Optional)
PASSWORD = mqtt_pass (Optional)
# Per-powermeter throttling override
# THROTTLE_INTERVAL = 2
```

The `JSON_PATH` option is used to extract the power value from a JSON payload. The path must be a [valid JSONPath expression](https://goessner.net/articles/JsonPath/).
If the payload is a simple integer value, you can omit this option.

#### Multi-phase MQTT

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

### JSON HTTP

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

### TQ Energy Manager

```ini
[TQ_EM]
IP = 192.168.1.100
#PASSWORD = pass
#TIMEOUT = 5.0 (Optional)
```

### HomeWizard

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

### SMA Energy Meter

Reads an [SMA Energy Meter](https://www.sma.de/) (EM 1.0/2.0) or Sunny Home Manager via the **Speedwire** multicast protocol (UDP). The listener joins the default multicast group and reports per-phase active power (L1, L2, L3). Use `SERIAL_NUMBER = 0` to auto-detect the first meter seen on the network, or set the device serial to pin a specific unit. Like other UDP-based features, this requires the host to receive multicast traffic (use Docker host networking or equivalent).

```ini
[SMA_ENERGY_METER]
MULTICAST_GROUP = 239.12.255.254
PORT = 9522
SERIAL_NUMBER = 0
# INTERFACE = 192.168.1.10
# THROTTLE_INTERVAL = 0
```

### Modbus

```ini
[MODBUS]
HOST =
PORT =
UNIT_ID =
ADDRESS =
COUNT =
DATA_TYPE = UINT16
BYTE_ORDER = BIG
WORD_ORDER = BIG
REGISTER_TYPE = HOLDING
```

### Script

You can also use a custom script to get the power values. The script should output at most 3 integer values, separated by a line break.
```ini
[SCRIPT]
COMMAND = /path/to/your/script.sh
```

### SML

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

### Multiple Powermeters

You can configure multiple powermeters by adding additional sections with the same prefix (e.g. `[SHELLY<unique_suffix>]`). Each powermeter should specify which client IP addresses are allowed to access it using the NETMASK setting.

When a storage system requests power values, the script will check the client IP address against the NETMASK settings of each powermeter and use the first that matches.

```ini
[SHELLY_1]
TYPE = 1PM
IP = 192.168.1.100
USER = username
PASS = password
NETMASK = 192.168.1.50/32

[SHELLY_2]
TYPE = 3EM
IP = 192.168.1.101
USER = username
PASS = password
# You can specify multiple IPs by separating them with a comma:
NETMASK = 192.168.1.51/32,192.168.1.52/32

[HOMEASSISTANT_1]
IP = 192.168.1.105
PORT = 8123
HTTPS = True
ACCESSTOKEN = YOUR_ACCESS_TOKEN
CURRENT_POWER_ENTITY = sensor.current_power
# No NETMASK specified - will match all clients (0.0.0.0/0)
```

# Frequently Asked Questions (FAQ)

## General Usage and Setup

### The emulator starts and shows "listening" message but nothing else happens. Is this a problem?

A: No, this is expected behavior. The emulator waits for the storage system to request data and only polls when requested. Without an active request from your Marstek device, you won't see further activity.

### My Marstek device can't find the emulated powermeter. What could be wrong?

A: Common causes include:
- **Firmware issues:** See the firmware requirements in the Device section below
- **Network setup:** Ensure both devices are on the same subnet (255.255.255.0)
- **Bluetooth interference:** Disconnect any Bluetooth connections during setup
- **Docker configuration:** When using Docker, set `network_mode: host` to enable UDP broadcast reception
- **CT002/CT003 pairing flow:** For managed fake CTs, refresh the CT device list (or log out/in), then pick `B2500-Meter CT002` / `B2500-Meter CT003`, switch battery mode to automatic, and select that CT. It should be selectable as soon as it appears in the device list. The fake CT appears as offline in the CT list (expected).
- **Config source confusion:** If Home Assistant add-on `custom_config` is used, it overrides add-on UI credentials/options.

### The emulator isn't visible in the Shelly app or network scanners. Is this normal?

A: Yes. The emulator only implements the minimal protocol needed for Marstek storage systems and is not a complete Shelly device emulation.

### How do I autostart the script on boot?

A: Use systemd to create a service:
1. Create a unit file (e.g., `/etc/systemd/system/b2500-meter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable b2500-meter && sudo systemctl start b2500-meter`

### Can I run multiple instances for different storage devices?

A: Yes. Define multiple sections in `config.ini` (e.g., `[SHELLY_1]`, `[SHELLY_2]`) and use the `NETMASK` setting to assign each to specific client IPs.

## Configuration & Integration

### What's the correct power value convention?

A: Power from grid to house (import): **positive**  
Power from house to grid (export): **negative**

### How do I convert kW values to the required W?

A: Create a template sensor in Home Assistant:
```jinja
{{ states('sensor.power_in_kilowatts') | float * 1000 }}
```

### How do I set up three-phase measurement in the Home Assistant Addon?

A: Use comma-separated entity IDs:
```
sensor.phase1,sensor.phase2,sensor.phase3
```

### What's the difference between the power entity settings?

A: 
- `CURRENT_POWER_ENTITY`: For a single bidirectional sensor (positive/negative values)
  - `POWER_INPUT_ALIAS`/`POWER_OUTPUT_ALIAS`: Entity IDs for separate import/export sensors (with `POWER_CALCULATE = True`)

## Device and Firmware Specific

### What firmware do I need for my Marstek device?

A:
- **Venus:** Firmware 120+ for Shelly support, 152+ for improved regulation
- **B2500:** Firmware 108+ (HMJ devices) or 224+ (all others)

### How do I handle the different ports for Shelly Pro 3EM?

A: Use one of these device types:
- `shellypro3em_old`: Port 1010 (B2500 firmware ≤224 or Jupiter & Venus)
- `shellypro3em_new`: Port 2220 (B2500 firmware ≥226)
- `shellypro3em`: Both ports (most compatible)

### Can I use this with non-Marstek storage systems (e.g., Zendure, Hoymiles)?

A: No, this project is Marstek-specific. For other brands, see [uni-meter](https://github.com/sdeigm/uni-meter).

## Troubleshooting

### I get permission errors when binding to port 1010/2220.

A: Ports below 1024 require root privileges on Linux. Solutions:
- Use Docker or Home Assistant Add-on (recommended)
- Use `setcap` to grant permissions
- Run as root (not recommended)

### I get parsing errors on startup or the add-on crashes.

A: Common causes:
- Incorrect entity IDs or API access
- Memory limitations (especially on RPi 2 or similar devices)
- Check logs for specific error messages

### How can I test without a storage device?

A: You can only verify the initial configuration. Full testing requires a Marstek device in "self-adaptation" mode to request data.

## Advanced

### How do signed (positive/negative) power values work with the emulator?

A: Powermeters typically report import as positive and export as negative (see [What's the correct power value convention?](#whats-the-correct-power-value-convention) above). Shelly and CT002/CT003 emulators forward those signed watts into the Marstek protocols; behavior on the battery side depends on your firmware and device type.

## Simulator

The project includes a standalone battery and powermeter simulator (`b2500-sim`) that lets you test the CT002 emulator without real hardware. It simulates N batteries speaking the CT002 UDP protocol and exposes an HTTP endpoint that b2500-meter reads as a powermeter.

### Install

```bash
pip install 'b2500-meter[sim]'
# or with uv:
uv pip install 'b2500-meter[sim]'
```

### Quick Start

**Terminal 1** — Start the simulator (1 battery, single-phase, with TUI):
```bash
b2500-sim run --batteries 1 --phases 1
```

**Terminal 2** — Start b2500-meter with the matching config:
```bash
b2500-sim config > config.ini   # generate a config snippet
b2500-meter -c config.ini
```

The generated `config.ini` looks like:
```ini
[GENERAL]
DEVICE_TYPE = ct002

[CT002]
UDP_PORT = 12345
ACTIVE_CONTROL = True

[JSON_HTTP]
URL = http://localhost:8080/power
JSON_PATHS = $.phase_a
```

For three-phase setups, use `JSON_PATHS = $.phase_a,$.phase_b,$.phase_c`.

### Multi-Battery 3-Phase Setup

```bash
# 3 batteries distributed across 3 phases
b2500-sim run --batteries 3 --phases 3

# Custom base load and initial SOC
b2500-sim run --batteries 2 --phases 3 --base-load 500,300,200 --soc 0.8
```

### JSON Config File

For full control, use a JSON config file:

```bash
b2500-sim run -c sim_config.json
```

Example `sim_config.json`:
```json
{
  "ct": {
    "mac": "112233445566",
    "host": "127.0.0.1",
    "port": 12345
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "powermeter": {
    "base_load": [100, 100, 100],
    "loads": [
      {"name": "LED lights", "power": 30, "phase": "A"},
      {"name": "TV + entertainment", "power": 80, "phase": "B"},
      {"name": "Router + NAS", "power": 40, "phase": "A"},
      {"name": "Microwave", "power": 800, "phase": "A"},
      {"name": "Washing machine", "power": 400, "phase": "B"}
    ],
    "solar_max": 2000,
    "solar_phases": ["A"]
  },
  "batteries": [
    {"mac": "02B250000001", "phase": "A", "capacity_wh": 2560, "initial_soc": 0.5},
    {"mac": "02B250000002", "phase": "B", "capacity_wh": 2560, "initial_soc": 0.8}
  ]
}
```

A more complete example simulating a European 3-phase household with rooftop solar,
multiple appliances, and 4 batteries (two on the heaviest phase):

```json
{
  "ct": {
    "mac": "AABBCCDDEEFF",
    "host": "127.0.0.1",
    "port": 12345
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "powermeter": {
    "base_load": [120, 80, 60],
    "base_noise": 30,
    "loads": [
      {"name": "LED lights",        "power":   30, "phase": "A"},
      {"name": "Router + NAS",      "power":   40, "phase": "A"},
      {"name": "Coffee machine",    "power":  200, "phase": "A"},
      {"name": "TV + entertainment","power":   80, "phase": "B"},
      {"name": "Washing machine",   "power":  400, "phase": "B"},
      {"name": "Laptop charger",    "power":   65, "phase": "B"},
      {"name": "Microwave",         "power":  800, "phase": "A"},
      {"name": "Fridge/freezer",    "power":  120, "phase": "C"},
      {"name": "Vacuum cleaner",    "power":  600, "phase": "C"}
    ],
    "solar_max": 5000,
    "solar_phases": ["A", "B", "C"]
  },
  "batteries": [
    {
      "mac": "02B250000001",
      "phase": "A",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.9,
      "ramp_rate": 150,
      "poll_interval": 1.0
    },
    {
      "mac": "02B250000002",
      "phase": "A",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.7
    },
    {
      "mac": "02B250000003",
      "phase": "B",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 5120,
      "initial_soc": 0.4
    },
    {
      "mac": "02B250000004",
      "phase": "C",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.2
    }
  ],
  "auto_mode": true,
  "auto_interval": [15, 45],
  "log_interval": 10
}
```

This configuration demonstrates:
- **Phase imbalance**: Kitchen loads (coffee machine, microwave) are concentrated on phase A with two batteries to compensate; entertainment/laundry on B; fridge/cleaning on C
- **Two batteries on one phase**: Batteries `0001` and `0002` both serve phase A — CT002's fair distribution algorithm splits the target between them
- **Mixed capacities**: Battery `0003` has a larger 5.12 kWh capacity (simulating a newer model)
- **Varied SOC**: Batteries start at different charge levels (90%, 70%, 40%, 20%) to test saturation timing
- **3-phase solar**: 5 kWp rooftop system balanced across all three phases — even moderate production exceeds the base load, causing grid export (negative readings) and battery charging
- **Custom ramp rate**: Battery `0001` ramps at 150 W/s instead of the default 200 W/s
- **Auto mode**: Randomly toggles loads and solar every 15–45 seconds for hands-free testing

### Interactive Controls

When running with the TUI (`b2500-sim run`, without `--no-tui`), you can interact with the simulation using keyboard shortcuts displayed on screen. The TUI shows live battery state (power, SOC, targets), grid readings per phase, and active loads.

Without the TUI, you can control the simulation via the HTTP API:

```bash
# Toggle a load on/off (1-based index)
b2500-sim load toggle 1

# Set solar production (watts)
b2500-sim solar set 800
b2500-sim solar set off

# Set a battery's SOC (for testing saturation)
b2500-sim battery 02B250000001 soc 0.0

# Show full status
b2500-sim status
```

### Daemon Mode

Run the simulator in the background and attach/detach the TUI:

```bash
# Start headless daemon
b2500-sim start -c sim_config.json

# Attach TUI to running daemon
b2500-sim attach

# Stop daemon
b2500-sim stop
```

### Custom Ports

If you need non-default ports (e.g. to avoid conflicts):

```bash
# Simulator on custom ports
b2500-sim run --batteries 2 --phases 3 --ct-port 54321 --http-port 9090

# Generate matching b2500-meter config
b2500-sim config --ct-port 54321 --http-port 9090 > config.ini
```

### Headless Mode

For CI or scripted testing, run without the TUI:

```bash
b2500-sim run --batteries 2 --phases 3 --no-tui
```

### How It Works

The simulator is fully decoupled from b2500-meter — it communicates purely over the network:

- **Battery simulators** send UDP requests to b2500-meter's CT002 emulator using the same protocol as real Marstek B2500 batteries
- **Powermeter simulator** serves an HTTP JSON endpoint (`GET /power`) that b2500-meter reads via its `[JSON_HTTP]` powermeter config
- Grid power is computed as: `grid = base_load + active_loads + noise - solar - battery_output`
- When solar exceeds consumption, grid goes negative (export) and batteries charge
- Batteries track SOC and saturate at 0%/100%

## License

This project is licensed under the General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
