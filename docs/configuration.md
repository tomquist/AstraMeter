# Configuration Reference

> **New to AstraMeter?** The
> [**config generator**](https://astrameter.com/generator.html)
> asks a few questions about your power meter and produces a ready-to-use
> `config.ini` or ESPHome YAML, explaining each option along the way. You can
> save, share, and reload your answers. It's the easiest way to get a working
> configuration. (The generator is part of the
> [AstraMeter website](../web/), hosted at [astrameter.com](https://astrameter.com),
> and you can also run it locally from `web/`.)

Configuration is managed via a `config.ini` file. This page documents the
options that apply across the whole app and to every powermeter. For details
specific to one area, see:

- **[Powermeter sources](powermeters.md)** — the `config.ini` section for each
  supported meter (Shelly, Tasmota, MQTT, Home Assistant, SMA, HomeWizard, …).
- **[CT002 / CT003 steering](ct002.md)** — the CT emulator, active control,
  multi-battery balancing, and efficiency optimization.
- **[MQTT Insights & Home Assistant entities](mqtt-insights.md)** — publishing
  internal state to MQTT, HA Device Discovery, and per-battery controls.
- **[ESPHome powermeter sources](esphome-powermeters.md)** — the equivalent
  grid-power `sensor:` configuration when running on an ESP32.

## Contents

- [General Configuration](#general-configuration)
  - [Per-powermeter options](#per-powermeter-options)
- [Value Transformation](#value-transformation)
- [PID Controller](#pid-controller)
- [Multiple Powermeters](#multiple-powermeters)

## General Configuration

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

### Per-powermeter options

These apply in any powermeter section (e.g. `[TASMOTA]` or `[HOMEASSISTANT]`), or
globally under `[GENERAL]` as a default for every powermeter:

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

## Value Transformation

You can optionally apply a linear transformation to the power values returned by
any powermeter. This is useful for calibrating readings (e.g., correcting a
consistent offset) or scaling values (e.g., adjusting for a CT clamp ratio).

The formula applied to each value is: `value * POWER_MULTIPLIER + POWER_OFFSET`

For example, if your meter reads 1050W and you set `POWER_MULTIPLIER=0.95` and
`POWER_OFFSET=-50`, the result is `1050 * 0.95 + (-50) = 947.5W`.

Both settings are optional and can be added to any powermeter section:

- `POWER_MULTIPLIER` — Scales each power value. Default: 1 (no scaling).
- `POWER_OFFSET` — Added to each power value after the multiplier is applied.
  Default: 0 (no offset).

For three-phase meters, you can specify a single value (applied to all phases) or
comma-separated values (one per phase):

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

**Note:** Transforms are applied when readings are taken from the powermeter,
before values are passed to the emulated device (Shelly, CT002/CT003, etc.).

## PID Controller

You can optionally layer a PID (Proportional-Integral-Derivative) controller on
top of any powermeter. The controller uses the grid power reading as its process
variable and steers the reported value toward zero (net-zero grid exchange). This
creates a second, software-level closed loop that can accelerate convergence or
compensate for slow storage device response.

**How it works:**

- `PID_MODE = bias` (default) — adds the PID output to the raw meter reading. The
  storage device's own closed-loop controller still acts, so the effective gain
  is `(1 − Kp) × Kb` where `Kb` is the device's internal gain. Use
  `0 < Kp < 1`; `Kp = 0.5` is the recommended starting point.
- `PID_MODE = replace` — uses only the PID output as the reported value,
  bypassing the device's own loop entirely.

**Anti-windup** is built in: the integral term is clamped so that the total PID
output never exceeds `±PID_OUTPUT_MAX`, and accumulation pauses while the output
is saturated.

All parameters can be set globally in `[GENERAL]` or per powermeter section
(per-section values override the global ones):

| Parameter | Description | Default |
|-----------|-------------|---------|
| `PID_KP` | Proportional gain. Set > 0 to enable the PID. | `0` (disabled) |
| `PID_KI` | Integral gain. Usually not needed; risks windup. | `0` |
| `PID_KD` | Derivative gain. Noisy on real meters; leave at 0. | `0` |
| `PID_OUTPUT_MAX` | Maximum absolute PID output in watts. | `800` |
| `PID_MODE` | `bias` or `replace`. | `bias` |

For a small import safety buffer that prevents accidental export, combine with a
negative `POWER_OFFSET` (applied before the PID):

```ini
[SHELLY]
TYPE = 1PM
IP = 192.168.1.100
POWER_OFFSET = -20     # 20 W safety buffer toward import
PID_KP = 0.5
PID_OUTPUT_MAX = 800
PID_MODE = bias
```

## Multiple Powermeters

You can configure multiple powermeters by adding additional sections with the
same prefix (e.g. `[SHELLY<unique_suffix>]`). Each powermeter should specify
which client IP addresses are allowed to access it using the NETMASK setting.

When a storage system requests power values, the script will check the client IP
address against the NETMASK settings of each powermeter and use the first that
matches.

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
