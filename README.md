# AstraMeter

> **Formerly known as b2500-meter.** The project was renamed to reflect support
> for the full range of Marstek storage systems (B2500, Jupiter, Venus, …),
> not just the B2500.

This project emulates Smart Meter devices for Marstek storage systems such as the B2500, Jupiter, and Venus while allowing integration with almost any smart meter. It does this by emulating one or more of the following devices:
- CT002 / CT003 (Marstek CT protocol; use for **multiple** storage devices)
- Shelly Pro 3EM
  - Uses port 1010 (B2500 firmware up to version 224) and port 2220 (B2500 firmware version 226+)
  - Can be specifically targeted with shellypro3em_old (port 1010) or shellypro3em_new (port 2220)
- Shelly EM gen3
- Shelly Pro EM50

**Note:** Use **CT002** or **CT003** when you steer **multiple** storage devices; use a **Shelly** device type (`shellypro3em`, `shellyemg3`, `shellyproem50`, …) otherwise. See [Configuration](#configuration) and [docs/ct002-ct003-protocol.md](docs/ct002-ct003-protocol.md) for CT002/CT003.

## Getting Started

The AstraMeter project can be installed and run in several ways depending on your needs and environment:

1. **Home Assistant App** (Recommended for Home Assistant users)
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

### Home Assistant App Installation

1. **Add the Repository to Home Assistant**

   [![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23main)

3. **Install the App**
   - Click on "App Store" in the bottom right corner
   - The AstraMeter app should appear in the app store
   - Click on it and then click "Install"

4. **Configure the App**
   You can configure the app in two ways:

   A) Using the App Configuration Interface:
   - After installation, go to the app's Configuration tab
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
   - Optional signal-conditioning filters are also available as Configuration fields (all optional, off by default): power offset/multiplier, smoothing (EMA), deadband, the Hampel outlier filter (see [General Configuration](#general-configuration)), and the [PID Controller](#pid-controller). Leave them empty to keep them disabled.
   - Click "Save" to apply the configuration

   Prefer a guided setup? The [config generator](https://tomquist.github.io/astrameter/generator.html) can produce a ready-to-paste Home Assistant add-on options block (including the filters above) — pick the "Home Assistant add-on" target.

   B) Using a Custom Configuration File for Advanced Configuration:
   - Create a `config.ini` file based on the examples in the [Configuration](#configuration) section
   - Place the file in `/addon_configs/a0ef98c5_b2500_meter/` (path uses the legacy slug `b2500_meter` for in-place upgrade compatibility). You can do that via "File editor" app in Home Assistant. Make sure to disable the "Enforce Basepath" setting in the File editor app config to access the `/addon_configs` folder.
   - In the app configuration, set `Custom Config` to the filename (e.g., "config.ini" without the path)
   - When using a custom configuration file, other configuration options will be ignored

5. **Start the App**
   - Go to the app's Info tab
   - Click "Start" to run the app

### Docker Installation

#### Prerequisites
- Docker installed on your system
- Docker Compose (optional, but recommended)

#### Installation Steps
1. Create a directory for the project
2. Create your `config.ini` file **before** starting the container. The compose
   file bind-mounts `config.ini` as a single file, and Docker will create an
   empty **directory** named `config.ini` if the file doesn't exist yet. (If you
   prefer a directory mount, mount a folder to `/app/config` and point the
   container at it with `command: ["astrameter", "-c", "config/config.ini"]`.)
3. Use the provided `docker-compose.yaml` to start the container:
   ```bash
   docker-compose up -d
   ```
   You can control the verbosity by setting the `LOG_LEVEL` environment
   variable (for example `-e LOG_LEVEL=debug`). If not set the container
   defaults to `info`.
Note: Host network mode is required because Marstek devices use UDP broadcasts for device discovery. Without host networking, the container won't be able to receive these broadcasts properly.

### Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with the **`next`** tag on GitHub Container Registry. These track the latest changes before a stable release and **may be less stable** than **`latest`**—use them to try fixes early or to validate the app before it lands on **`main`**.

**Home Assistant App**

1. Add the repository pointing at the **`develop`** branch (same flow as [Home Assistant App Installation](#home-assistant-app-installation), but use this URL):

   `https://github.com/tomquist/astrameter#develop`

   [![Add develop repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23develop)

2. Install or update the **AstraMeter** app from the store. Supervisor will pull the **`next`**-tagged image (`ghcr.io/tomquist/astrameter-addon:next`).

To return to stable releases, remove this repository and add the normal URL without `#develop` ([step 1 under Home Assistant App Installation](#home-assistant-app-installation)), then reinstall or wait for an update to the **`latest`** track.

**Docker**

Use the **`next`** image instead of **`latest`** in `docker-compose.yaml` (or `docker run`):

```yaml
image: ghcr.io/tomquist/astrameter:next
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
   cd path/to/astrameter
   ```

3. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)** (dependency manager).

4. **Install dependencies and run**
   ```bash
   uv sync
   uv run astrameter
   ```
   With dev tools (tests, ruff, mypy): `uv sync --extra dev`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow.

All commands above work across Windows, macOS, and Linux. The only difference is how you open your terminal.

### ESPHome External Component (run on an ESP32)

AstraMeter also ships as an **ESPHome external component** that runs the CT002/CT003 emulator, balancer, and cross-phase filter pipeline directly on an ESP32 — no Python add-on, no Home Assistant required. Useful if you'd rather flash a dedicated board than run a server, and if your grid-power source is already addressable by ESPHome (Modbus, M-Bus, Tasmota, MQTT, Shelly, Envoy, etc.).

Minimal YAML — point `power_sensor_l1` at any ESPHome sensor that reports grid power in watts:

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

Everything else is optional. See **[`esphome.example.yaml`](esphome.example.yaml)** for the complete, annotated config — three-phase sensors, the cross-phase filter pipeline (Hampel / smoothing / deadband / PID), balancer and saturation tuning, and the two optional sub-blocks below — with every knob shown at its default. For the grid-power `sensor:` configuration per meter type (and which meters aren't supported on the ESP yet), see **[docs/esphome-powermeters.md](docs/esphome-powermeters.md)**.

Two optional sub-blocks nest under the same `ct002:` key:

- **`mqtt_insights:`** — publishes Home Assistant Device Discovery (one device per battery + a parent CT002 device with manual-target / active / auto-target / distribution-weight controls and a force-rotation button) and answers Marstek-app polls on your MQTT broker, so the emulator shows up in the app without hame-relay. Requires an `mqtt:` block.
- **`marstek_registration:`** — registers a managed CT002/CT003 with your Marstek cloud account on first boot (same flow as the Python `[MARSTEK]` section), persists the assigned MAC, and feeds it back into `ct002.ct_mac`. Requires an `http_request:` block. When combined with `mqtt_insights:`, the App-topic subscription picks up the MAC automatically — no reboot needed.

**Status:** experimental — UDP emulator, balancer, filter pipeline, MQTT-insights, and Marstek cloud registration are all functional. Wider field testing welcome.

**Requirements:** ESP32 with ≥4 MB flash (default for `esp32dev`, `esp32-s3-devkitc-1`, etc.). ESP8266 is not supported in v1 — RAM and flash budgets are too tight once HTTPS+TLS, MQTT, and the balancer are linked together. Pick a board with `flash_size: 4MB` or larger; for ESP-IDF builds you may need a custom partition table when you also add HTTPS+MQTT — there is no top-level `flash_size:` YAML key, set it via your `board:` choice and (for ESP-IDF) `esp32: framework: type: esp-idf` with appropriate `sdkconfig_options:` or a partition CSV.

**One important divergence from the Python emulator:** per-phase transforms and throttling are *not* part of `ct002:` — they're delegated to ESPHome's standard `sensor: filters:` (`offset:`, `multiply:`, `throttle:`) on the upstream sensor. This matches the canonical order in Python (`Transform → Throttle → Hampel → Smoothed → Deadband → PID`). Put per-phase filters on the sensor itself, not after `ct002:` — they need to apply to the raw input, not the balancer's output.

## Additional Notes

When the script is running, switch your Marstek battery to "Self-Adaptation" mode to enable the powermeter functionality.

For details on the CT002/CT003 UDP protocol used by Marstek storage systems, see [docs/ct002-ct003-protocol.md](docs/ct002-ct003-protocol.md).

## Configuration

> **New to AstraMeter?** The [**AstraMeter website**](web/) introduces the
> project and includes a step-by-step **config generator** that asks a few
> questions about your power meter and produces a ready-to-use `config.ini` or
> ESPHome YAML, explaining each option along the way. You can save, share, and
> reload your answers. (Once Pages is enabled it's hosted at the repository's
> GitHub Pages URL; you can also run it locally from `web/`.)

Configuration is managed via `config.ini`. Each powermeter type has specific settings — see the per-source reference in **[docs/powermeters.md](docs/powermeters.md)** (and **[docs/esphome-powermeters.md](docs/esphome-powermeters.md)** for the ESPHome external component).

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
# Briefly wait (up to 2s) for a fresh push from event-driven powermeters
# (MQTT, Home Assistant, HomeWizard, SMA, ...) before responding to the
# battery. Set to false to skip the wait and always serve the last-known
# value — recommended when the underlying meter updates slower than 2s
# (e.g. P1 smart meter behind Home Assistant) so that the inevitable timeout
# doesn't add latency to every CT002 response. Default: true.
# Can be overridden per powermeter section.
#WAIT_FOR_NEXT_MESSAGE = true
# Ignore repeated requests from the same emulator client within this window
# (seconds). Applies to CT002/CT003 (keyed by consumer id) and Shelly (keyed
# by battery IP). Can be overridden in the [CT002]/[CT003] section. 0 disables.
#DEDUPE_TIME_WINDOW = 0
```

#### Per-powermeter options

These apply in any powermeter section (e.g. `[TASMOTA]` or `[HOMEASSISTANT]`), or globally under `[GENERAL]` as a default for every powermeter:
- **THROTTLE_INTERVAL** — Override global throttling for this powermeter
- **WAIT_FOR_NEXT_MESSAGE** — Override the global wait-for-fresh-push behaviour
  for this powermeter (set to `false` to opt out of the wait entirely)
- **SMOOTH_TARGET_ALPHA** (default 0 = disabled) — EMA factor for the powermeter
  reading in (0, 1]. Higher values track load changes faster; lower values filter
  noise but add lag. Values close to 1.0 work well when the powermeter updates at
  ≥ 1 Hz; reduce toward 0.3 if it updates significantly slower than 1 Hz.
- **MAX_SMOOTH_STEP** (default 0 = unlimited) — Maximum watts the smoothed reading
  may change per request cycle when `SMOOTH_TARGET_ALPHA` is active. Acts as a
  slew-rate limit.
- **DEADBAND** (default 0 = disabled, W) — When the absolute reading is below this
  value, the wrapper emits zeros instead of chasing noise. Keeps batteries from
  hunting around the zero-crossing; 10–30 W is a sensible range.
- **HAMPEL_WINDOW** (default 0 = disabled) — Rolling window size for
  median-based outlier rejection. Typical values 5–7. Useful for MQTT/HTTP
  sources that occasionally emit wild samples; applied after throttling and
  before EMA smoothing.
- **HAMPEL_N_SIGMA** (default 3.0) — Rejection threshold in MAD-derived sigmas.
  Lower values reject more aggressively.
- **HAMPEL_MIN_THRESHOLD** (default 0, W) — Minimum rejection threshold in
  watts. Prevents spikes from passing through during long periods of constant
  readings (the MAD=0 degenerate case); 50 W is a reasonable starting value.

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
# Ignore repeated requests from the same consumer within this window (seconds).
# Also supported by the Shelly emulator (keyed by battery IP); set it under
# [GENERAL] to apply regardless of the emulated device type.
DEDUPE_TIME_WINDOW = 0
# Forget consumers after this many seconds without updates (multi-consumer support).
# Unset (default): adaptive — a battery is dropped after missing ~2 of its own
# poll cycles (min 5s), like the real CT. Set a number for a fixed window.
CONSUMER_TTL = 120
```

#### Active steering, balancing & efficiency

All keys in this subsection go under the `[CT002]` or `[CT003]` section (they are **not** read from `[GENERAL]` or from powermeter sections):
- **ACTIVE_CONTROL** — When true (default), the emulator smooths the grid reading, splits
  the target across batteries, and balances their load.
  When false, the emulator relays raw meter values and batteries decide on their own.

*Fair distribution — balancing load across multiple batteries:*
- **FAIR_DISTRIBUTION** (default true) — Adjust each battery's target so they share the
  load evenly. Only matters with two or more batteries. To split *unevenly* (e.g. give a
  larger battery a bigger share), set a per-battery **Distribution Weight** from Home
  Assistant — see [Per-battery controls](#per-battery-controls-home-assistant-entities).
- **BALANCE_GAIN** (default 0.2) — How aggressively to correct imbalance between batteries.
  0.0 = no correction (equal split only); 0.3–0.5 = faster rebalancing but may overshoot.
- **BALANCE_DEADBAND** (default 25 W) — Ignore imbalance smaller than this.
  Prevents micro-corrections when batteries are already close; kept above the battery
  firmware's own ±20 W input deadband so corrections it would ignore are never sent.
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
- **PACE_BASE_STEP** / **PACE_MAX_STEP** (defaults 30 W / 100 W) — Ramp pacing for the
  auto control loop: each battery's per-poll command delta is capped, starting at the
  battery firmware ramp's first step and growing toward the max only while the
  battery demonstrably follows the command. Keeps the firmware's accelerating internal
  ramp from overshooting on meter latency. `PACE_BASE_STEP = 0` disables pacing.

*DC battery keep-alive — applies to each DC-only battery on its own (also with a
single battery, independent of balancing):*
- **MIN_DC_OUTPUT** (default 0 = disabled) — Minimum discharge in watts to keep a DC
  battery's inverter from switching off at 0 W and falling asleep under high PV
  surplus (a known behaviour of the Marstek B2500). Applied individually to each
  DC-only battery that has no inverter of its own; AC batteries (Venus) and Jupiter
  are unaffected. Can also be set per battery from Home Assistant (see **Min DC
  Output** below). A value of at least 20 W is recommended.

*Battery efficiency optimization — concentrating power on fewer batteries,
probing handoffs, and swapping away from ones that cannot follow:*

Batteries have a minimum operating power below which their DC-DC converter
efficiency drops sharply. When multiple batteries split a small load, each
one may operate in this inefficient range, wasting energy as heat. The
efficiency optimization detects this situation and concentrates the load on
fewer batteries so each one stays above its efficient minimum, idling the
rest. Batteries rotate periodically so wear is shared evenly.

> **Not recommended for DC batteries.** Efficiency rotation relies on being
> able to steer a deprioritized battery's output down to 0 W. DC-coupled
> batteries such as the Marstek B2500 cannot be commanded to 0 W via the
> CT002 protocol — they keep running at their minimum output power (e.g.
> ~80 W) — so idling them does not work and the feature provides no benefit.
> It is intended for AC batteries (e.g. the Marstek Venus) that can be
> steered all the way to 0 W. For DC batteries, leave the efficiency settings
> below disabled.

When a timed rotation or forced swap promotes a new battery, the handoff now
uses a **probe phase** instead of dropping the previous active battery to zero
immediately. During probe, the promoted battery gets the real CT002
delta-control signal while the previous active battery (or batteries) stays
online as backup and covers the signed residual shortfall based on the
promoted battery's latest reported power. Once the promoted battery shows
meaningful real output, the probe commits and the backup fades out. If it
never ramps, the probe times out and the balancer restores the previous active
battery. After a successful probe, saturation detection stays active so
mid-interval failures still trigger a swap.

- **MIN_EFFICIENT_POWER** (default 0 = disabled) — When the per-battery share of
  total demand falls below this threshold (watts), excess batteries are
  deprioritized so the remaining ones operate above their efficient minimum.
  Example: 2 batteries, 200 W demand, threshold 150 → one battery gets 200 W,
  the other idles. Hysteresis (×1.2) prevents oscillation at the boundary.
- **EFFICIENCY_ROTATION_INTERVAL** (default 900 s, minimum 10) — Seconds between
  rotating which battery has priority. Ensures fair wear across batteries.
- **EFFICIENCY_FADE_ALPHA** (default 0.15) — EMA factor controlling how quickly
  batteries transition during efficiency switchovers. It mainly controls how
  quickly the old battery fades out **after a successful probe** (and also
  smooths ordinary efficiency transitions). Lower values produce smoother,
  slower transitions; higher values are faster. Set to 1.0 for instant
  switching.
- **EFFICIENCY_SATURATION_THRESHOLD** (default 0.4) — When an active battery's
  saturation score exceeds this value (i.e. it can't follow its target because
  it is full, empty, or externally limited), it is immediately swapped out for a
  healthy deprioritized battery instead of waiting for the next timed rotation.
  During a probe, the probe timeout is the main "never ramps" control; this
  threshold still matters after the probe succeeds and for already-active
  batteries. Set to 0 to disable. The saturation EMA is time-weighted, so
  batteries with slower powermeters (>10 s update interval) accumulate saturation
  faster per sample — if you see unnecessary swaps with a slow powermeter,
  raise this value (e.g. to 0.8).
- **SATURATION_DETECTION** (default true) — Track how well each battery follows
  its target. When a battery cannot deliver (full or empty), its share is
  reduced and redistributed to others.
- **SATURATION_ALPHA** (default 0.15) — EMA factor for the saturation score.
  Lower = slower to declare a battery saturated (and slower to recover).
- **MIN_TARGET_FOR_SATURATION** (default 20 W) — Ignore saturation tracking when
  the target is below this value (avoids false positives at low power). Probe
  success uses the same threshold.
- **SATURATION_GRACE_SECONDS** (default 90 s) — The maximum **probe window** when
  a deprioritized battery is promoted by timed rotation or forced swap. During
  this window the previous active battery stays available as backup and covers
  the residual shortfall while the promoted battery ramps. If the promoted
  battery reaches meaningful output earlier, the probe commits early.
- **SATURATION_STALL_TIMEOUT_SECONDS** (default 60 s) — Stall escape for
  non-probe grace cases, such as batteries rejoining auto control after being
  paused or switched out of manual mode. Probe handoffs themselves now use the
  full probe window above as the primary timer.
- **SATURATION_DECAY_FACTOR** (default 0.995) — How quickly a swapped-out
  battery's saturation score decays while it has no target. Applied each cycle.
  Lower values allow faster recovery; 1.0 means the battery never becomes
  eligible again.

Optional Marstek cloud auto-registration:
- **MARSTEK.ENABLE** — auto-create/check managed fake CT device(s) at startup
- **MARSTEK.MAILBOX / PASSWORD** — credentials used to call Marstek API
- For `ct002` a managed `HME-4` device is ensured, for `ct003` a managed `HME-3` device.
- Device fields created by astrameter:
  - `devid == mac` (random lowercase hex)
  - `bluetooth_name = MST-SMR_<last4(mac)>`
  - `name = AstraMeter CT002` / `AstraMeter CT003`
- If a matching managed device of expected type already exists, no new device is created.
- Important behavior notes:
  - Managed fake CT devices appear as **offline** in the app CT list (expected behavior).
  - Refresh the CT device list after registration (or log out/in if needed). Then select `AstraMeter CT002` / `AstraMeter CT003`, switch battery mode to automatic, and choose that CT. It should be selectable as soon as it appears in the device list.
  - Marstek credentials are only needed for one-time registration. You can remove `MARSTEK.MAILBOX` / `MARSTEK.PASSWORD` immediately after registration succeeds (or if the managed device already exists).
  - If you use Home Assistant app `custom_config`, values from that file take precedence over app UI fields.
  - **Marstek app (optional):** live CT grid power over MQTT uses the same `[MQTT_INSIGHTS]` broker as [hame-relay](https://github.com/tomquist/hame-relay) **≥ 1.3.5**; see [MQTT Insights](#mqtt-insights) (optional Marstek subsection). HA entities do not depend on this.

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

### PID Controller

You can optionally layer a PID (Proportional-Integral-Derivative) controller on top of any powermeter. The controller uses the grid power reading as its process variable and steers the reported value toward zero (net-zero grid exchange). This creates a second, software-level closed loop that can accelerate convergence or compensate for slow storage device response.

**How it works:**

- `PID_MODE = bias` (default) — adds the PID output to the raw meter reading. The storage device's own closed-loop controller still acts, so the effective gain is `(1 − Kp) × Kb` where `Kb` is the device's internal gain. Use `0 < Kp < 1`; `Kp = 0.5` is the recommended starting point.
- `PID_MODE = replace` — uses only the PID output as the reported value, bypassing the device's own loop entirely.

**Anti-windup** is built in: the integral term is clamped so that the total PID output never exceeds `±PID_OUTPUT_MAX`, and accumulation pauses while the output is saturated.

All parameters can be set globally in `[GENERAL]` or per powermeter section (per-section values override the global ones):

| Parameter | Description | Default |
|-----------|-------------|---------|
| `PID_KP` | Proportional gain. Set > 0 to enable the PID. | `0` (disabled) |
| `PID_KI` | Integral gain. Usually not needed; risks windup. | `0` |
| `PID_KD` | Derivative gain. Noisy on real meters; leave at 0. | `0` |
| `PID_OUTPUT_MAX` | Maximum absolute PID output in watts. | `800` |
| `PID_MODE` | `bias` or `replace`. | `bias` |

For a small import safety buffer that prevents accidental export, combine with a negative `POWER_OFFSET` (applied before the PID):

```ini
[SHELLY]
TYPE = 1PM
IP = 192.168.1.100
POWER_OFFSET = -20     # 20 W safety buffer toward import
PID_KP = 0.5
PID_OUTPUT_MAX = 800
PID_MODE = bias
```

### Powermeter sources

The per-source configuration for every supported meter lives in dedicated
reference docs — find your meter and copy the matching section:

- **[docs/powermeters.md](docs/powermeters.md)** — `config.ini` sections for the
  Python add-on / Docker / direct install (Shelly, Tasmota, Shrdzm, Emlog,
  IoBroker, HomeAssistant, VZLogger, ESPHome, AMIS Reader, Modbus, MQTT, JSON
  HTTP, TQ Energy Manager, HomeWizard, Enphase Envoy, SMA Energy Meter,
  FRITZ!Smart Energy 250, Script, SML).
- **[docs/esphome-powermeters.md](docs/esphome-powermeters.md)** — the equivalent
  grid-power `sensor:` configuration when running the
  [ESPHome external component](#esphome-external-component-run-on-an-esp32) on an
  ESP32, including which meters aren't supported on the ESP yet.

The value transformation, PID controller, and per-powermeter options
(throttling, smoothing, deadband, Hampel) documented above apply to every source.

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

### MQTT Insights

**Primary use:** publish CT002/Shelly internal state (grid power, targets, saturation, topology, switches) to MQTT with **optional Home Assistant MQTT Device Discovery** so entities show up in HA.

**Home Assistant app:** With the Mosquitto add-on installed, MQTT Insights is auto-configured; entities appear without manual `[MQTT_INSIGHTS]` wiring.

**Small add-on:** the same broker connection can optionally answer **Marstek CT002/CT003 MQTT polls** so the Marstek mobile app shows live grid power when you use [hame-relay](https://github.com/tomquist/hame-relay) on that broker (see below). You can turn that off with `MARSTEK_MQTT_ENABLED=false` and keep HA publishing unchanged.

**Manual configuration** (when not using the HA app defaults):

```ini
[MQTT_INSIGHTS]
BROKER = 192.168.1.100
PORT = 1883
USERNAME = mqtt_user
PASSWORD = mqtt_pass
TLS = false
BASE_TOPIC = astrameter
HA_DISCOVERY = true
HA_DISCOVERY_PREFIX = homeassistant
```

| Option | Default | Description |
|---|---|---|
| `URI` | — | MQTT URI (`mqtt[s]://user:pass@host:port`); when set, overrides `BROKER`/`PORT`/`USERNAME`/`PASSWORD`/`TLS` |
| `BROKER` | `localhost` | MQTT broker hostname/IP |
| `PORT` | `1883` | MQTT broker port |
| `USERNAME` / `PASSWORD` | — | Credentials (optional) |
| `TLS` | `false` | Enable TLS encryption |
| `BASE_TOPIC` | `astrameter` | Root topic for all published messages |
| `HA_DISCOVERY` | `true` | Enable Home Assistant MQTT Device Discovery |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | HA discovery topic prefix |
| `MARSTEK_MQTT_ENABLED` | `true` | Optional: answer Marstek app CT002/CT003 polls on this broker (needs `[MARSTEK]`); set `false` for HA-only |
| `MARSTEK_MQTT_INTERVAL` | `300` | Optional: seconds between background aggregate publishes for the app; `0` = polls only |
| `POWERMETER_HEALTH_INTERVAL` | `30` | Seconds between per-powermeter **Online** diagnostic sensor updates; `0` disables it |

#### Powermeter health (Home Assistant entities)

When HA discovery is on, every configured powermeter section gets its own
**"AstraMeter Powermeter `<Section>`"** device (the section name is Capital-Cased
for the label, and the device is grouped under the **AstraMeter** hub device —
keyed on `ADDON_SLUG` on the add-on, with a stable base-topic fallback so the
grouping also works in standalone/Docker). It carries:

- an **Online** connectivity `binary_sensor` (diagnostic) that flips **off** when
  the source stops delivering fresh, usable readings — a stalled or disconnected
  push stream, or a polling source whose reads start failing — so you can alert
  on a meter that has gone quiet even though AstraMeter keeps running on its last
  cached value;
- **Power**, **Power L1**, **Power L2**, **Power L3** sensors carrying the latest
  per-phase readings and their total (single-phase meters leave L2/L3 empty).

Push sources (HomeWizard, MQTT, SMA, Home Assistant) report their stream state
directly; polling sources reflect the control loop, or are probed about once per
`POWERMETER_HEALTH_INTERVAL` when no battery is reading them. For multi-phase
sources, a phase that simply stops changing (e.g. an idle circuit reporting a
steady value) stays **online** — only an unavailable/missing reading or a
disconnect marks it offline.

#### Per-battery controls (Home Assistant entities)

When HA discovery is on, each battery gets a few **config** entities you can set
live from Home Assistant:

- **Manual Target** / **Auto Target** — override a battery's power, or hand it
  back to automatic control.
- **Active** — pause/resume a battery (paused batteries are steered to 0 W).
- **Distribution Weight** — its relative share of the load when the balancer
  splits demand across batteries. `1.0` is neutral; raise it on a larger
  battery (or lower it on a smaller one) to bias the split. For example, a
  5.12 kWh and a 2.08 kWh battery that you'd like to run roughly **60:40** can
  be set to weights `1.5` and `1.0`. The split is ratio-based, so only the
  proportion between batteries matters; `0` parks a battery at 0 W while
  leaving it in the pool. Tune it while watching the batteries — the change
  takes effect on the next control cycle.
- **Min DC Output** — minimum discharge in watts to keep this battery's inverter
  from switching off at 0 W and falling asleep (see **MIN_DC_OUTPUT** above). Only
  shown for DC batteries where it has an effect (e.g. the Marstek B2500); overrides
  the global setting for that battery.

The CT device itself also exposes a config switch:

- **Active Control** — on (default) lets the emulator smooth the grid reading and
  compute per-battery targets; turn it **off** to fall back to relay mode (the raw
  per-phase aggregate is forwarded and the batteries decide), the live equivalent of
  **ACTIVE_CONTROL = False**.

Each of these controls publishes its set-command **retained**, so Home
Assistant restores your values across an AstraMeter restart without any extra
configuration.

#### Optional: Marstek mobile app (live MQTT)

This is **not** required for Home Assistant. It only helps the **Marstek app** show live CT002/CT003 grid power over the same cloud MQTT path when **[hame-relay](https://github.com/tomquist/hame-relay)** bridges your broker—use **hame-relay ≥ 1.3.5** so poll/replies work reliably. UDP between batteries and AstraMeter is unchanged for control.

**If you want it**

- **`[MARSTEK]`** — Managed fake CT so the **MQTT MAC** matches the cloud device.
- **Same broker as hame-relay** — `[MQTT_INSIGHTS]` must point at the broker relay uses toward Marstek's cloud.

**Toggles** (defaults in table)

- **`MARSTEK_MQTT_ENABLED`** — `false` = HA MQTT Insights only, no Marstek poll replies.
- **`MARSTEK_MQTT_INTERVAL`** — Optional periodic aggregate pushes; **`0`** = answer polls only.

Replies follow the usual `hame_energy/…` / `marstek_energy/…` App/device topics for a real CT; AstraMeter matches your CT002/CT003 **type** and **MAC**.

**Published entities** (per CT002 consumer):
- Grid power (L1/L2/L3/total), charge target (L1/L2/L3), reported power, saturation
- Diagnostic: phase, device type, battery IP, CT type, CT MAC, last seen
- **Active switch**: pause/resume individual consumers (targets zeroed when inactive)

**Published entities** (per CT002 device):
- Smooth target, consumer count, and an **Active Control** switch (on by default;
  turn off for relay mode)

**Published entities** (per Shelly battery):
- Grid power (L1/L2/L3/total), active status, last seen

**Topics**: `{base}/ct002/{id}/consumer/{cid}`, `{base}/ct002/{id}/status`, `{base}/shelly/{id}/battery/{ip}`, `{base}/shelly/{id}/status`, `{base}/status` (LWT)

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
- **CT002/CT003 pairing flow:** For managed fake CTs, refresh the CT device list (or log out/in), then pick `AstraMeter CT002` / `AstraMeter CT003`, switch battery mode to automatic, and select that CT. It should be selectable as soon as it appears in the device list. The fake CT appears as offline in the CT list (expected).
- **Config source confusion:** If Home Assistant app `custom_config` is used, it overrides app UI credentials/options.

### The emulator isn't visible in the Shelly app or network scanners. Is this normal?

A: Yes. The emulator only implements the minimal protocol needed for Marstek storage systems and is not a complete Shelly device emulation.

### How do I autostart the script on boot?

A: Use systemd to create a service:
1. Create a unit file (e.g., `/etc/systemd/system/astrameter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable astrameter && sudo systemctl start astrameter`

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

### How do I set up three-phase measurement in the Home Assistant App?

A: Use comma-separated entity IDs:
```
sensor.phase1,sensor.phase2,sensor.phase3
```

### What's the difference between the power entity settings?

A: 
- `CURRENT_POWER_ENTITY`: For a single bidirectional sensor (positive/negative values)
  - `POWER_INPUT_ALIAS`/`POWER_OUTPUT_ALIAS`: Entity IDs for separate import/export sensors (with `POWER_CALCULATE = True`)

### How should I feed import and export power — one sensor or two? (Home Assistant App)

A: In the Home Assistant App, if you have a single signed sensor (positive for import, negative for export), put it in `POWER_INPUT_ALIAS` (or `CURRENT_POWER_ENTITY`) only and leave `POWER_OUTPUT_ALIAS` empty. Separate import/export sensors can update at different moments and get read out of sync, causing drift and oscillation; a single signed value avoids that.

### Should I use Shelly emulation or CT002/CT003 for multiple batteries?

A: Prefer CT002/CT003 (set `DEVICE_TYPE = ct002` or `ct003`) for multi-battery setups. With Shelly emulation each battery reacts independently and they tend to fight each other (one charging while another discharges). The CT emulation coordinates a shared target across the fleet, giving more even and stable distribution.

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
- Use Docker or Home Assistant App (recommended)
- Use `setcap` to grant permissions
- Run as root (not recommended)

Note: the Docker image runs as a non-root user, so binding port 1010 (used by `shellypro3em_old` and the combined `shellypro3em`, which starts both listeners) still fails with `PermissionError: [Errno 13]` under `network_mode: host`. Port 2220 (`shellypro3em_new`) is unaffected. Either lower the host's privileged-port range (`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1010`, persist via `/etc/sysctl.d/`) or run the container as root (`user: "0:0"` in compose). Publishing the port via bridge networking does **not** work, because the Marstek discovery packets are UDP broadcasts to the subnet address and aren't forwarded by Docker's port mapping.

### I get parsing errors on startup or the app crashes.

A: Common causes:
- Incorrect entity IDs or API access
- Memory limitations (especially on RPi 2 or similar devices)
- Check logs for specific error messages

### How can I test without a storage device?

A: You can only verify the initial configuration. Full testing requires a Marstek device in "self-adaptation" mode to request data.

### My output power oscillates or yo-yos between zero and full.

A: This usually happens when your battery asks AstraMeter for a new power reading more often than your meter actually has a fresh one. The battery keeps reacting to stale numbers, overshoots, and ends up swinging back and forth. The fix is to slow things down and smooth out the readings. Try these one at a time, and watch how the battery behaves for a few minutes after each change before moving on:

1. **Don't re-read the meter too often.** Set `THROTTLE_INTERVAL = 1` so AstraMeter waits at least one second between readings, and `DEDUPE_TIME_WINDOW = 0.9` so it ignores duplicate readings that arrive in that window.
2. **Ignore tiny wobbles.** Raise `DEADBAND` to around `10`–`20` (watts) so small fluctuations near zero are treated as "close enough" and don't trigger a correction.
3. **Smooth the changes.** Set `SMOOTH_TARGET_ALPHA` to around `0.2`–`0.4` and `MAX_SMOOTH_STEP` to around `40`–`60` so the reported power moves in gentle steps instead of jumping.

If it's still swinging after that, the most effective option is to turn on the **[PID Controller](#pid-controller)** — a smart helper that gently nudges the reading toward zero and calms down a battery that tends to over- or under-react. To get started, just set `PID_KP = 0.5` and `PID_MODE = bias`, and leave the other `PID_*` settings alone. There are a few more optional filters (including one that throws out occasional bad spikes) described under [General Configuration](#general-configuration) if you want to fine-tune further.

### My second battery never kicks in, or my batteries won't settle near zero.

A: This is governed by `MIN_EFFICIENT_POWER`, which decides how many batteries are engaged for a given demand. It's intended for AC batteries that can hold a precise setpoint; pure DC battery pools can't be steered to exactly zero the same way. If a second unit won't engage, lower `MIN_EFFICIENT_POWER`; for DC-only setups, set it to `0`.

### The Marstek app shows the meter offline or doesn't display my real meter values.

A: This is expected for purely local operation — the emulated meter typically populates only one phase, and the app won't show your raw readings because each battery is only handed its share of the target (so the totals steer toward zero). It does not mean the integration is failing. If you do want live readings in the Marstek app, configure the `[MARSTEK]` section together with [hame-relay](https://github.com/tomquist/hame-relay) (≥ 1.3.5) so AstraMeter can answer the app's polls via MQTT.

## Advanced

### How do signed (positive/negative) power values work with the emulator?

A: Powermeters typically report import as positive and export as negative (see [What's the correct power value convention?](#whats-the-correct-power-value-convention) above). Shelly and CT002/CT003 emulators forward those signed watts into the Marstek protocols; behavior on the battery side depends on your firmware and device type.

## Simulator

The project includes a standalone battery and powermeter simulator (`astra-sim`) that lets you test the CT002 emulator without real hardware. It simulates N batteries speaking the CT002 UDP protocol and exposes an HTTP endpoint that astrameter reads as a powermeter.

### Install

```bash
pip install 'astrameter[sim]'
# or with uv:
uv pip install 'astrameter[sim]'
```

### Quick Start

**Terminal 1** — Start the simulator (1 battery, single-phase, with TUI):
```bash
astra-sim run --batteries 1 --phases 1
```

**Terminal 2** — Start astrameter with the matching config:
```bash
astra-sim config > config.ini   # generate a config snippet
astrameter -c config.ini
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
astra-sim run --batteries 3 --phases 3

# Custom base load and initial SOC
astra-sim run --batteries 2 --phases 3 --base-load 500,300,200 --soc 0.8
```

### JSON Config File

For full control, use a JSON config file:

```bash
astra-sim run -c sim_config.json
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
  "power_update_delay_ticks": 0,
  "batteries": [
    {"mac": "02B250000001", "phase": "A", "capacity_wh": 2560, "initial_soc": 0.5},
    {"mac": "02B250000002", "phase": "B", "capacity_wh": 2560, "initial_soc": 0.8}
  ]
}
```

Optional top-level `power_update_delay_ticks` (or per-battery `power_update_delay_ticks`) delays how many simulator ticks pass before the battery applies each new CT-derived power setpoint (`reported_power + grid_reading` from the response; `0` = immediate). The same delay can be set from the CLI with `astra-sim run --power-update-delay N` (also supported on `astra-sim start`). With a non-zero delay, `GET /status` and the TUI expose **`target`** as the latest CT-requested watts and **`applied_target`** as the setpoint the battery is ramping toward after the delay. When delay is `0`, both match.

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

When running with the TUI (`astra-sim run`, without `--no-tui`), you can interact with the simulation using keyboard shortcuts displayed on screen. The TUI shows live battery state (power, SOC, targets), grid readings per phase, and active loads. If `power_update_delay_ticks` is non-zero, the battery table adds **Req** (CT request) and **Appl** (delayed setpoint) columns so you can see the latency effect; otherwise a single **Target** column shows the setpoint.

Without the TUI, you can control the simulation via the HTTP API:

```bash
# Toggle a load on/off (1-based index)
astra-sim load toggle 1

# Set solar production (watts)
astra-sim solar set 800
astra-sim solar set off

# Set a battery's SOC (for testing saturation)
astra-sim battery 02B250000001 soc 0.0

# Show full status
astra-sim status
```

### Daemon Mode

Run the simulator in the background and attach/detach the TUI:

```bash
# Start headless daemon
astra-sim start -c sim_config.json

# Attach TUI to running daemon
astra-sim attach

# Stop daemon
astra-sim stop
```

### Custom Ports

If you need non-default ports (e.g. to avoid conflicts):

```bash
# Simulator on custom ports
astra-sim run --batteries 2 --phases 3 --ct-port 54321 --http-port 9090

# Generate matching astrameter config
astra-sim config --ct-port 54321 --http-port 9090 > config.ini
```

### Headless Mode

For CI or scripted testing, run without the TUI:

```bash
astra-sim run --batteries 2 --phases 3 --no-tui
```

### How It Works

The simulator is fully decoupled from astrameter — it communicates purely over the network:

- **Battery simulators** send UDP requests to astrameter's CT002 emulator using the same protocol as real Marstek batteries
- **Powermeter simulator** serves an HTTP JSON endpoint (`GET /power`) that astrameter reads via its `[JSON_HTTP]` powermeter config
- Grid power is computed as: `grid = base_load + active_loads + noise - solar - battery_output`
- When solar exceeds consumption, grid goes negative (export) and batteries charge
- Batteries track SOC and saturate at 0%/100%

## License

This project is licensed under the General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
