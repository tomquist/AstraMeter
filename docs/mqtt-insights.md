# MQTT Insights & Home Assistant Entities

**Primary use:** publish CT002/Shelly internal state (grid power, targets,
saturation, topology, switches) to MQTT. Home Assistant gets **optional MQTT
Device Discovery** so entities show up automatically, but the underlying topics
are **plain JSON on stable paths** — you can consume them from Node-RED,
openHAB, Telegraf/Grafana, `mosquitto_sub`, or any custom script **without Home
Assistant**. The [Topic reference](#topic-reference) below documents every topic
and payload so you can build your own dashboards and automations.

**Home Assistant app:** With the Mosquitto add-on installed, MQTT Insights is
auto-configured; entities appear without manual `[MQTT_INSIGHTS]` wiring.

**Small add-on:** the same broker connection can optionally answer **Marstek
CT002/CT003 MQTT polls** so the Marstek mobile app shows live grid power when you
use [hame-relay](https://github.com/tomquist/hame-relay) on that broker (see
[Optional: Marstek mobile app](#optional-marstek-mobile-app-live-mqtt) below).
You can turn that off with `MARSTEK_MQTT_ENABLED=false` and keep publishing
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
| `HA_DISCOVERY` | `true` | Enable Home Assistant MQTT Device Discovery (the state/command topics below are published regardless) |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | HA discovery topic prefix |
| `MARSTEK_MQTT_ENABLED` | `true` | Optional: answer Marstek app CT002/CT003 polls on this broker (needs `[MARSTEK]`); set `false` for HA-only |
| `MARSTEK_MQTT_INTERVAL` | `300` | Optional: seconds between background aggregate publishes for the app; `0` = polls only |
| `POWERMETER_HEALTH_INTERVAL` | `30` | Seconds between per-powermeter health (Online + power) updates; `0` disables it |

> **HA discovery is independent of the data.** Turning `HA_DISCOVERY` off only
> stops the retained `homeassistant/.../config` discovery messages (and the
> `{base}/bridge` hub summary). All of the state and command topics in the
> [Topic reference](#topic-reference) are still published and accepted, so a
> non-HA setup loses nothing.

## Topic reference

Every topic below is rooted at `BASE_TOPIC` (default `astrameter`), shown here as
`{base}`. State topics are **published `retain`ed** unless noted, so a client
that connects later immediately receives the last known value. JSON payloads are
compact UTF-8 objects.

Path variables:

- `{did}` — the device's configured `DEVICE_ID` (sanitized: any character
  outside `A–Z a–z 0–9 _ -` becomes `_`). Empty when no `DEVICE_ID` is set.
- `{cid}` — a CT002 **consumer**, i.e. one Marstek battery, keyed by its
  lowercased battery MAC (e.g. `0123456789ab`).
- `{ip}` — a Shelly-mode battery, keyed by its IP with dots sanitized to `_`
  (e.g. `192_168_1_50`).
- `{pm}` — a powermeter, keyed by its sanitized config section name.

### Service status (LWT)

| Topic | Retain | Payload |
|---|---|---|
| `{base}/status` | yes | `online` while AstraMeter is connected; `offline` on clean shutdown and as the broker's Last-Will if the process dies. Plain string, not JSON. |

Use this as the availability/heartbeat for everything else.

### Hub summary (HA discovery only)

| Topic | Retain | Payload |
|---|---|---|
| `{base}/bridge` | yes | `{"version": "<app version>", "consumer_count": <int>}` — only published when `HA_DISCOVERY = true`. `consumer_count` is the total of CT002 consumers + Shelly batteries currently known. |

### CT002 — per-battery (consumer) state

`{base}/ct002/{did}/consumer/{cid}` — published on every poll from that battery.
Example payload:

```json
{
  "grid_power":  {"l1": 120.0, "l2": 0.0, "l3": -30.0, "total": 90.0},
  "target":      {"l1": -50.0, "l2": 0.0, "l3": 0.0},
  "phase": "l1",
  "reported_power": 600,
  "device_type": "HMG-50",
  "battery_ip": "192.168.1.50",
  "ct_type": "HME-3",
  "ct_mac": "0123456789ab",
  "saturation": 0.0,
  "last_target": -50.0,
  "active": true,
  "poll_interval": 1.0,
  "last_seen": "2026-06-22T10:15:00+00:00",
  "manual_target": null,
  "auto_target": true,
  "distribution_weight": 1.0,
  "efficiency_window_weight": 1.0,
  "min_dc_output": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `grid_power` | object | Smoothed grid reading sent to this battery, per phase plus `total` (watts; **+ = import**, − = export). |
| `target` | object | Per-phase charge/discharge target the balancer computed for this battery (watts; sign convention as reported to the battery). No `total` key. |
| `phase` | string | Phase this battery is assigned to (`l1`/`l2`/`l3`), or its reported phase. |
| `reported_power` | number | Power the battery reported it is currently producing/consuming (watts). |
| `device_type` | string | Battery model string it announced. |
| `battery_ip` | string | Source IP of the battery's UDP poll. |
| `ct_type` / `ct_mac` | string | Emulated CT type and MAC this consumer polled. |
| `saturation` | number | 0–1 estimate of how saturated (maxed-out) the battery is; 1 = can't absorb/deliver more. |
| `last_target` | number/null | Previous target sent, for rate-of-change context. |
| `active` | bool | `false` when this battery is paused (steered to 0 W). |
| `poll_interval` | number/null | Measured seconds between this battery's polls. |
| `last_seen` | string | ISO-8601 UTC timestamp of this update. |
| `manual_target` | number/null | Active manual override in watts, or `null` when on automatic. |
| `auto_target` | bool | `true` = automatic control; `false` = manual override in effect. |
| `distribution_weight` | number | Relative share of demand when splitting across batteries (ratio-based; `1.0` neutral). |
| `efficiency_window_weight` | number | Internal 0–1 fraction of efficiency-rotation active time (HA surfaces this ×100 as a percent). |
| `min_dc_output` | number/null | Per-battery minimum DC discharge (watts) keep-alive override, or `null`. |

Availability companion:

| Topic | Retain | Payload |
|---|---|---|
| `{base}/ct002/{did}/consumer/{cid}/availability` | yes | `online` while the battery is known; `offline` when it ages out / is removed. |

### CT002 — per-device status

| Topic | Retain | Payload |
|---|---|---|
| `{base}/ct002/{did}/status` | yes | `{"smooth_target": <w>, "active_control": <bool>, "consumer_count": <int>}` |

- `smooth_target` — the device-wide smoothed grid target (watts).
- `active_control` — `true` when the emulator is computing per-battery targets;
  `false` in relay mode (raw aggregate forwarded).
- `consumer_count` — number of batteries currently polling this device.

### Shelly — per-battery state

`{base}/shelly/{did}/battery/{ip}`:

```json
{
  "grid_power": {"l1": 120.0, "l2": 0.0, "l3": -30.0, "total": 90.0},
  "active": true,
  "poll_interval": 1.0,
  "last_seen": "2026-06-22T10:15:00+00:00"
}
```

| Field | Type | Meaning |
|---|---|---|
| `grid_power` | object | Per-phase grid power forwarded to this battery plus `total` (watts; + = import). |
| `active` | bool | `false` when the battery is marked inactive. |
| `poll_interval` | number/null | Measured seconds between polls. |
| `last_seen` | string | ISO-8601 UTC timestamp. |

| Topic | Retain | Payload |
|---|---|---|
| `{base}/shelly/{did}/battery/{ip}/availability` | yes | `online` / `offline` |
| `{base}/shelly/{did}/status` | yes | `{"battery_count": <int>}` — batteries currently polling this Shelly device. |

### Powermeter health

`{base}/powermeter/{pm}` — published every `POWERMETER_HEALTH_INTERVAL` seconds
(0 disables):

```json
{"online": true, "grid_power": {"l1": 120.0, "l2": 0.0, "l3": -30.0, "total": 90.0}}
```

| Field | Type | Meaning |
|---|---|---|
| `online` | bool | `false` when the source stops delivering fresh, usable readings (stalled push stream, or a polling source whose reads fail) — alert on this to catch a meter that has gone quiet while AstraMeter keeps running on its last cached value. |
| `grid_power` | object | Latest per-phase reading and `total` (watts). Single-phase meters leave `l2`/`l3` `null`. |

Push sources (HomeWizard, MQTT, SMA, Home Assistant) report stream state
directly; polling sources reflect the control loop, or are probed about once per
interval when no battery is reading them. A multi-phase source whose value
simply stops changing (an idle circuit reporting a steady number) stays
**online** — only an unavailable/missing reading marks it offline.

### Command topics (set values without Home Assistant)

AstraMeter **subscribes** to the topics below; publish to them from any client
to change settings live. Publishing **retained** is recommended — AstraMeter
re-reads them on restart so your values survive a restart (this is exactly how
the HA entities persist). An empty payload clears a retained command.

Per-consumer (one battery), one scalar value per topic:

| Topic suffix on `{base}/ct002/{did}/consumer/{cid}/…/set` | Payload | Effect |
|---|---|---|
| `active/set` | `true`/`false` (also `on`/`off`, `1`/`0`) | Pause (`false`) or resume a battery; paused is steered to 0 W. |
| `auto_target/set` | `true`/`false` | `true` hands the battery back to automatic control; `false` keeps the manual override. |
| `manual_target/set` | number, −10000…10000 | Force this battery's power (watts). Setting it implies manual mode. |
| `distribution_weight/set` | number, 0.0…10.0 | Relative share of the split (ratio-based; `0` parks at 0 W but keeps it in the pool). |
| `efficiency_window_weight/set` | number, 0…100 (**percent**) | Share of efficiency-rotation active time. `100` neutral, `0` skips while limiting. |
| `min_dc_output/set` | number, 0…1000 | Per-battery minimum DC discharge keep-alive (watts). |

Per-device, JSON body on `{base}/ct002/{did}/set`:

| Payload | Effect |
|---|---|
| `{"active_control": true}` / `{"active_control": false}` | Turn active control on (compute per-battery targets) or off (relay mode — raw aggregate forwarded, the live equivalent of `ACTIVE_CONTROL = False`). |
| `{"force_rotation": true}` | Immediately rotate the efficiency window to the next battery. |

Out-of-range, non-numeric, or non-boolean payloads are ignored with a warning.

> **Sign / unit conventions.** All power values are watts. Grid power follows
> **import-positive** (+ = drawing from the grid, − = exporting). Battery
> `target`/`reported_power` use the value as sent to the battery. Timestamps are
> ISO-8601 in UTC.

#### Quick examples (`mosquitto`)

```bash
# Watch everything AstraMeter publishes
mosquitto_sub -h 192.168.1.100 -v -t 'astrameter/#'

# Pause a battery (retained so it sticks across restarts)
mosquitto_pub -h 192.168.1.100 -r \
  -t 'astrameter/ct002/myct/consumer/0123456789ab/active/set' -m 'false'

# Force a manual 300 W discharge target
mosquitto_pub -h 192.168.1.100 -r \
  -t 'astrameter/ct002/myct/consumer/0123456789ab/manual_target/set' -m '-300'

# Switch the whole CT002 device to relay mode
mosquitto_pub -h 192.168.1.100 \
  -t 'astrameter/ct002/myct/set' -m '{"active_control": false}'
```

## Powermeter health (Home Assistant entities)

When HA discovery is on, every configured powermeter section gets its own
**"AstraMeter Powermeter `<Section>`"** device (the section name is Capital-Cased
for the label, and the device is grouped under the **AstraMeter** hub device —
keyed on `ADDON_SLUG` on the add-on, with a stable base-topic fallback so the
grouping also works in standalone/Docker). It carries:

- an **Online** connectivity `binary_sensor` (diagnostic) backed by the
  `online` field of `{base}/powermeter/{pm}`;
- **Power**, **Power L1**, **Power L2**, **Power L3** sensors backed by that
  topic's `grid_power` (single-phase meters leave L2/L3 empty).

See [Powermeter health](#powermeter-health) above for the raw topic and the
exact online/offline semantics.

## Per-battery controls (Home Assistant entities)

When HA discovery is on, each battery gets a few **config** entities you can set
live from Home Assistant. Each maps to a command topic in
[Command topics](#command-topics-set-values-without-home-assistant), so the same
controls are available to any MQTT client:

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
for a real CT; AstraMeter matches your CT002/CT003 **type** and **MAC**. These
are a separate, Marstek-cloud-specific protocol — unrelated to the
`{base}/…` insight topics documented above.
