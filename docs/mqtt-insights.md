# MQTT Insights & Home Assistant Entities

**Primary use:** publish CT002/Shelly internal state (grid power, targets,
saturation, topology, switches) to MQTT with **optional Home Assistant MQTT
Device Discovery** so entities show up in HA.

**Home Assistant app:** With the Mosquitto add-on installed, MQTT Insights is
auto-configured; entities appear without manual `[MQTT_INSIGHTS]` wiring.

**Small add-on:** the same broker connection can optionally answer **Marstek
CT002/CT003 MQTT polls** so the Marstek mobile app shows live grid power when you
use [hame-relay](https://github.com/tomquist/hame-relay) on that broker (see
[Optional: Marstek mobile app](#optional-marstek-mobile-app-live-mqtt) below).
You can turn that off with `MARSTEK_MQTT_ENABLED=false` and keep HA publishing
unchanged.

## Manual configuration

When not using the HA app defaults:

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

## Powermeter health (Home Assistant entities)

When HA discovery is on, every configured powermeter section gets its own
**"AstraMeter Powermeter `<Section>`"** device (the section name is Capital-Cased
for the label, and the device is grouped under the **AstraMeter** hub device —
keyed on `ADDON_SLUG` on the add-on, with a stable base-topic fallback so the
grouping also works in standalone/Docker). It carries:

- an **Online** connectivity `binary_sensor` (diagnostic) that flips **off** when
  the source stops delivering fresh, usable readings — a stalled or disconnected
  push stream, or a polling source whose reads start failing — so you can alert on
  a meter that has gone quiet even though AstraMeter keeps running on its last
  cached value;
- **Power**, **Power L1**, **Power L2**, **Power L3** sensors carrying the latest
  per-phase readings and their total (single-phase meters leave L2/L3 empty).

Push sources (HomeWizard, MQTT, SMA, Home Assistant) report their stream state
directly; polling sources reflect the control loop, or are probed about once per
`POWERMETER_HEALTH_INTERVAL` when no battery is reading them. For multi-phase
sources, a phase that simply stops changing (e.g. an idle circuit reporting a
steady value) stays **online** — only an unavailable/missing reading or a
disconnect marks it offline.

## Per-battery controls (Home Assistant entities)

When HA discovery is on, each battery gets a few **config** entities you can set
live from Home Assistant:

- **Manual Target** / **Auto Target** — override a battery's power, or hand it
  back to automatic control.
- **Active** — pause/resume a battery (paused batteries are steered to 0 W).
- **Distribution Weight** — its relative share of the load when the balancer
  splits demand across batteries. `1.0` is neutral; raise it on a larger battery
  (or lower it on a smaller one) to bias the split. For example, a 5.12 kWh and a
  2.08 kWh battery that you'd like to run roughly **60:40** can be set to weights
  `1.5` and `1.0`. The split is ratio-based, so only the proportion between
  batteries matters; `0` parks a battery at 0 W while leaving it in the pool. Tune
  it while watching the batteries — the change takes effect on the next control
  cycle.
- **Efficiency Window Weight** — how much of the **efficiency rotation** each
  battery takes when demand is low and the balancer runs only some batteries to
  keep them efficient. `100 %` is neutral; `0 %` skips a battery (parked while
  limiting, but still used when all batteries are needed); in between gives it less
  active time. Separate from **Distribution Weight** (which biases the split among
  active batteries).
- **Min DC Output** — minimum discharge in watts to keep this battery's inverter
  from switching off at 0 W and falling asleep (see
  [MIN_DC_OUTPUT](ct002.md#dc-battery-keep-alive)). Only shown for DC batteries
  where it has an effect (e.g. the Marstek B2500); overrides the global setting for
  that battery.

The CT device itself also exposes a config switch:

- **Active Control** — on (default) lets the emulator smooth the grid reading and
  compute per-battery targets; turn it **off** to fall back to relay mode (the raw
  per-phase aggregate is forwarded and the batteries decide), the live equivalent
  of **ACTIVE_CONTROL = False**.

Each of these controls publishes its set-command **retained**, so Home Assistant
restores your values across an AstraMeter restart without any extra configuration.

## Optional: Marstek mobile app (live MQTT)

This is **not** required for Home Assistant. It only helps the **Marstek app**
show live CT002/CT003 grid power over the same cloud MQTT path when
**[hame-relay](https://github.com/tomquist/hame-relay)** bridges your broker — use
**hame-relay ≥ 1.3.5** so poll/replies work reliably. UDP between batteries and
AstraMeter is unchanged for control.

**If you want it**

- **`[MARSTEK]`** — Managed fake CT so the **MQTT MAC** matches the cloud device.
- **Same broker as hame-relay** — `[MQTT_INSIGHTS]` must point at the broker relay
  uses toward Marstek's cloud.

**Toggles** (defaults in table above)

- **`MARSTEK_MQTT_ENABLED`** — `false` = HA MQTT Insights only, no Marstek poll
  replies.
- **`MARSTEK_MQTT_INTERVAL`** — Optional periodic aggregate pushes; **`0`** =
  answer polls only.

Replies follow the usual `hame_energy/…` / `marstek_energy/…` App/device topics
for a real CT; AstraMeter matches your CT002/CT003 **type** and **MAC**.

**Published entities** (per CT002 consumer):

- Grid power (L1/L2/L3/total), charge target (L1/L2/L3), reported power,
  saturation
- Diagnostic: phase, device type, battery IP, CT type, CT MAC, last seen
- **Active switch**: pause/resume individual consumers (targets zeroed when
  inactive)

**Published entities** (per CT002 device):

- Smooth target, consumer count, and an **Active Control** switch (on by default;
  turn off for relay mode)

**Published entities** (per Shelly battery):

- Grid power (L1/L2/L3/total), active status, last seen

**Topics**: `{base}/ct002/{id}/consumer/{cid}`, `{base}/ct002/{id}/status`,
`{base}/shelly/{id}/battery/{ip}`, `{base}/shelly/{id}/status`, `{base}/status`
(LWT)
