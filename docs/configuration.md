# Configuration Reference

> **New to AstraMeter?** The
> [**config generator**](https://astrameter.com/generator.html)
> asks a few questions about your power meter and writes a ready-to-use
> `config.ini` or ESPHome YAML, explaining each option as it goes. You can
> save, share, and reload your answers. It's the easiest way to get a working
> configuration. (The generator is part of the
> [AstraMeter website](../web/), hosted at [astrameter.com](https://astrameter.com),
> and you can also run it locally from `web/`.)

You configure AstraMeter with a `config.ini` file. This page covers the options
that apply to the whole app and to every powermeter. For one specific area, see:

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
- [Shelly discovery and HTTP](#shelly-discovery-and-http)

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
# A suppressed request is left UNANSWERED, so a battery's answered rate can only
# fall to a whole fraction of its own poll rate (half, a third, ...) — the value
# is honoured exactly, but its effect changes in steps, not smoothly.
#DEDUPE_TIME_WINDOW = 0
```

### Per-powermeter options

These work in any powermeter section (e.g. `[TASMOTA]` or `[HOMEASSISTANT]`).
Put them under `[GENERAL]` instead to set a default for every powermeter:

- **THROTTLE_INTERVAL** — Override global throttling for this powermeter
- **WAIT_FOR_NEXT_MESSAGE** — Override the global wait-for-fresh-push behaviour
  for this powermeter (set to `false` to opt out of the wait entirely)
- **SMOOTH_TARGET_ALPHA** (default 0 = disabled) — EMA factor (exponential
  moving average: a running average that fades out older readings) for the
  powermeter reading, in (0, 1]. Higher values track load changes faster; lower
  values filter noise but add lag. Values close to 1.0 work well when the
  powermeter updates at ≥ 1 Hz; reduce toward 0.3 if it updates significantly
  slower than 1 Hz.
- **MAX_SMOOTH_STEP** (default 0 = unlimited) — The most watts the smoothed
  reading may change per request cycle while `SMOOTH_TARGET_ALPHA` is active.
  It caps how fast the value can move (a slew-rate limit).
- **DEADBAND** (default 0 = disabled, W) — A dead zone around zero. When the
  absolute reading is below this value, the wrapper emits zeros instead of
  chasing noise. This keeps batteries from hunting around the zero-crossing;
  10–30 W is a sensible range.
- **HAMPEL_WINDOW** (default 0 = disabled) — Rolling window size for the Hampel
  filter, which drops outliers that sit far from the recent median. Typical
  values 5–7. Useful for MQTT/HTTP sources that occasionally emit wild samples.
  It runs after throttling and before EMA smoothing.
- **HAMPEL_N_SIGMA** (default 3.0) — Rejection threshold, in sigmas derived from
  the MAD (median absolute deviation: a spread measure that outliers barely
  affect). Lower values reject more aggressively.
- **HAMPEL_MIN_THRESHOLD** (default 0, W) — Minimum rejection threshold in
  watts. It stops spikes slipping through during long periods of constant
  readings (the MAD=0 degenerate case); 50 W is a reasonable starting value.

## Value Transformation

You can apply a linear transformation to the power values any powermeter
returns. Use it to calibrate readings (e.g., to correct a consistent offset) or
to scale them (e.g., for a CT clamp ratio).

The formula applied to each value is: `value * POWER_MULTIPLIER + POWER_OFFSET`

So if your meter reads 1050W and you set `POWER_MULTIPLIER=0.95` and
`POWER_OFFSET=-50`, the result is `1050 * 0.95 + (-50) = 947.5W`.

Both settings are optional, and you can add them to any powermeter section:

- `POWER_MULTIPLIER` — Scales each power value. Default: 1 (no scaling).
- `POWER_OFFSET` — Added to each power value after the multiplier is applied.
  Default: 0 (no offset).

For three-phase meters, give a single value (applied to all phases) or
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
before the values reach the emulated device (Shelly, CT002/CT003, etc.).

## PID Controller

You can layer a PID (Proportional-Integral-Derivative) controller on top of any
powermeter. The controller takes the grid power reading as its process variable
and steers the reported value toward zero (net-zero grid exchange). That adds a
second, software-level closed loop, which can speed up convergence or make up
for a storage device that responds slowly.

**How it works:**

- `PID_MODE = bias` (default) — adds the PID output to the raw meter reading. The
  storage device's own closed-loop controller still acts, so the effective gain
  is `(1 − Kp) × Kb` where `Kb` is the device's internal gain. Use
  `0 < Kp < 1`; `Kp = 0.5` is the recommended starting point.
- `PID_MODE = replace` — reports only the PID output, bypassing the device's own
  loop entirely.

**Anti-windup** is built in: the integral term is clamped so that the total PID
output never exceeds `±PID_OUTPUT_MAX`, and accumulation pauses while the output
is saturated.

Set all parameters globally in `[GENERAL]`, or per powermeter section — a
section value overrides the global one:

| Parameter | Description | Default |
|-----------|-------------|---------|
| `PID_KP` | Proportional gain. Set > 0 to enable the PID. | `0` (disabled) |
| `PID_KI` | Integral gain. Usually not needed; risks windup. | `0` |
| `PID_KD` | Derivative gain. Noisy on real meters; leave at 0. | `0` |
| `PID_OUTPUT_MAX` | Maximum absolute PID output in watts. | `800` |
| `PID_MODE` | `bias` or `replace`. | `bias` |

For a small import safety buffer that prevents accidental export, combine the
PID with a negative `POWER_OFFSET` (applied before the PID):

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

You can configure several powermeters by adding more sections with the same
prefix (e.g. `[SHELLY<unique_suffix>]`). Use the NETMASK setting in each one to
say which client IP addresses may access it.

When a storage system requests power values, the script checks the client IP
address against each powermeter's NETMASK setting and uses the first that
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

## Shelly discovery and HTTP

With a `shellypro3em` device type, AstraMeter also announces itself on your
network and serves the HTTP surface a Shelly Pro 3EM serves, so batteries that
discover meters by themselves can find it. Both are on by default.

The settings live in `[EMULATOR_SHELLYPRO3EM]` — note the prefix, which keeps
the emulator's own section apart from the `[SHELLY…]` sections that describe a
Shelly power meter you *read from*. See
[Shelly discovery and the HTTP surface](shelly-tcp.md) for the full reference,
including what is announced, how the emulated identity stays stable, and what
each installation method needs for port 80.
