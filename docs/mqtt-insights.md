# MQTT Insights & Home Assistant Entities

**Primary use:** publish CT002/Shelly internal state (grid power, targets,
saturation, topology, switches) to MQTT. Home Assistant can pick this up through
**optional MQTT Device Discovery**, so entities appear on their own. The topics
underneath are **plain JSON on stable paths** — Home Assistant is just one
optional consumer. You can read and command the same topics from Node-RED,
openHAB, Telegraf/Grafana, `mosquitto_sub`, or any custom MQTT client. The
[Topic reference](#topic-reference) below documents every topic and payload, so
you can build your own dashboards and automations.

**Home Assistant app:** With the Mosquitto add-on installed, MQTT Insights
configures itself; entities appear without manual `[MQTT_INSIGHTS]` wiring.

**Small add-on:** the same broker connection can also answer **Marstek
CT002/CT003 MQTT polls**, so the Marstek mobile app shows live grid power when
you run [hame-relay](https://github.com/tomquist/hame-relay) on that broker (see
[Optional: Marstek mobile app](#optional-marstek-mobile-app-live-mqtt) below).
Set `MARSTEK_MQTT_ENABLED=false` to turn that off; publishing stays the same.

## Manual configuration

When you don't use the HA app defaults:

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
| `URI` | — | MQTT URI (`mqtt[s]://user:pass@host:port`); when set, it overrides `BROKER`/`PORT`/`USERNAME`/`PASSWORD`/`TLS` |
| `BROKER` | `localhost` | MQTT broker hostname or IP |
| `PORT` | `1883` | MQTT broker port |
| `USERNAME` / `PASSWORD` | — | Credentials (optional) |
| `TLS` | `false` | Turn on TLS encryption |
| `BASE_TOPIC` | `astrameter` | Root topic for everything published |
| `HA_DISCOVERY` | `true` | Turn on Home Assistant MQTT Device Discovery (the state and command topics below go out either way) |
| `HA_DISCOVERY_PREFIX` | `homeassistant` | HA discovery topic prefix |
| `MARSTEK_MQTT_ENABLED` | `true` | Optional: answer Marstek app CT002/CT003 polls on this broker (needs `[MARSTEK]`); set `false` for HA-only |
| `MARSTEK_MQTT_INTERVAL` | `300` | Optional: seconds between background aggregate publishes for the app; `0` = polls only |
| `POWERMETER_HEALTH_INTERVAL` | `30` | Seconds between per-powermeter health updates (Online + power); `0` turns it off |
| `STATE_THROTTLE_INTERVAL` | `0` | Smallest gap in seconds between two state publishes for the same battery; `0` publishes on every poll |

> **HA discovery is independent of the data.** Turning `HA_DISCOVERY` off only
> stops the retained `homeassistant/.../config` discovery messages (and the
> `{base}/bridge` hub summary). Every state and command topic in the
> [Topic reference](#topic-reference) is still published and accepted, so a
> non-HA setup loses nothing.

## Topic reference

Every topic below sits under `BASE_TOPIC` (default `astrameter`), written here as
`{base}`. State topics are **published `retain`ed** unless noted, so a client
that connects later gets the last known value at once. JSON payloads are compact
UTF-8 objects.

Path variables:

- `{did}` — the device's configured `DEVICE_ID`. It is sanitized: any character
  outside `A–Z a–z 0–9 _ -` becomes `_`. Empty when no `DEVICE_ID` is set.
- `{cid}` — a CT002 **consumer**, meaning one Marstek battery, keyed by its
  lowercased battery MAC (e.g. `0123456789ab`).
- `{ip}` — a Shelly-mode battery, keyed by its IP with dots turned into `_`
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
| `{base}/bridge` | yes | `{"version": "<app version>", "consumer_count": <int>}` — published only when `HA_DISCOVERY = true`. `consumer_count` is the total of CT002 consumers plus Shelly batteries currently known. |

### CT002 — per-battery (consumer) state

`{base}/ct002/{did}/consumer/{cid}` — published on every poll from that battery,
unless `STATE_THROTTLE_INTERVAL` is set (see
[Quietening the state topics](#quietening-the-state-topics)).
Example payload:

```json
{
  "grid_power":  {"l1": 120.0, "l2": 0.0, "l3": -30.0, "total": 90.0},
  "target":      {"l1": -50.0, "l2": 0.0, "l3": 0.0},
  "phase": "A",
  "reported_power": 600,
  "device_type": "HMG-50",
  "battery_ip": "192.168.1.50",
  "ct_type": "HME-3",
  "ct_mac": "0123456789ab",
  "saturation": 0.0,
  "last_target": -50.0,
  "active": true,
  "poll_interval": 1.0,
  "answer_interval": 1.0,
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
| `target` | object | Per-phase charge/discharge target the balancer worked out for this battery (watts; sign convention as reported to the battery). No `total` key. |
| `phase` | string | Phase the battery says it is clamped to: `A`, `B` or `C`, or `D` for combined / whole-home mode. |
| `reported_power` | number | Power the battery reports it is producing or consuming right now (watts). |
| `device_type` | string | Battery model string it announced. |
| `battery_ip` | string | Source IP of the battery's UDP poll. |
| `ct_type` / `ct_mac` | string | Emulated CT type and MAC this consumer polled. |
| `saturation` | number | 0–1 estimate of how saturated (maxed-out) the battery is; 1 = it can't absorb or deliver more. |
| `last_target` | number/null | Previous target sent, for rate-of-change context. |
| `active` | bool | `false` when this battery is paused (steered to 0 W). |
| `poll_interval` | number/null | Measured seconds between this battery's polls, counting every poll it sends. |
| `answer_interval` | number/null | Measured seconds between the replies it actually receives. Matches `poll_interval` unless `DEDUPE_TIME_WINDOW` is suppressing replies. |
| `last_seen` | string | ISO-8601 UTC timestamp of this update. No Home Assistant entity — see [Is a battery still reporting?](#is-a-battery-still-reporting-home-assistant). |
| `manual_target` | number/null | Active manual override in watts, or `null` when on automatic. |
| `auto_target` | bool | `true` = automatic control; `false` = manual override in effect. |
| `distribution_weight` | number | Relative share of demand when the balancer splits it across batteries (ratio-based; `1.0` neutral). |
| `efficiency_window_weight` | number | Internal 0–1 fraction of efficiency-rotation active time (HA shows this ×100 as a percent). |
| `min_dc_output` | number/null | Per-battery minimum DC discharge (watts) keep-alive override, or `null`. |

Availability companion:

| Topic | Retain | Payload |
|---|---|---|
| `{base}/ct002/{did}/consumer/{cid}/availability` | yes | `online` while the battery is known; `offline` when it ages out or is removed. |

### CT002 — per-device status

| Topic | Retain | Payload |
|---|---|---|
| `{base}/ct002/{did}/status` | yes | see below |

```json
{
  "smooth_target": 90.0,
  "active_control": true,
  "consumer_count": 2,
  "control_quality": "off_target",
  "control_quality_score": 41.5,
  "control_quality_error_w": 214.0,
  "control_quality_in_band_pct": 11.0,
  "control_quality_crossings_per_min": 3.4,
  "control_quality_band_w": 25.0
}
```

- `smooth_target` — the device-wide smoothed grid target (watts).
- `active_control` — `true` when the emulator computes per-battery targets;
  `false` in relay mode, where the raw aggregate is forwarded.
- `consumer_count` — number of batteries polling this device right now.
- `control_quality` — whether the grid is being held at zero: `stable`,
  `off_target`, `limited`, `warmup` or `idle`. See
  [Control quality](ct002.md#control-quality) for what each verdict means and
  what to change.
- `control_quality_score` — the same judgement as a 0–100 % number, for trending
  and alerting. It is `null` (HA: `unknown`) while the loop is idle or warming
  up, so a "score below X" automation never fires on a reading that does not
  exist. `control_quality` itself always carries a verdict — `idle` or `warmup`
  in those states, never `unknown`.
- `control_quality_error_w` — mean absolute grid error over the recent window
  (watts). The number behind the verdict.
- `control_quality_in_band_pct` — share of that window the grid spent inside
  the settling band, 0–100 %.
- `control_quality_crossings_per_min` — how often the error crossed zero while
  it was large. Read it together with the verdict: a high rate beside
  `off_target` points at a loop overshooting past zero, a near-zero one at a
  loop that never reaches it. A noisy meter also raises it — see
  [Control quality](ct002.md#control-quality).
- `control_quality_band_w` — the settling band the rest is judged against
  (`BALANCE_DEADBAND`, floored at 25 W). It is configuration, so never `null`.

The three evidence fields are `null` (HA: `unknown`) until at least one reading
has been folded in, for the same reason as the score.

Home Assistant gets all five as diagnostic entities on the CT device:
**Control Quality** (an enum sensor), **Control Quality Score**, **Control
Quality Mean Error**, **Control Quality Time In Band** and **Control Quality
Zero Crossings**.

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
| `last_seen` | string | ISO-8601 UTC timestamp. No Home Assistant entity — see [Is a battery still reporting?](#is-a-battery-still-reporting-home-assistant). |

| Topic | Retain | Payload |
|---|---|---|
| `{base}/shelly/{did}/battery/{ip}/availability` | yes | `online` / `offline` |
| `{base}/shelly/{did}/status` | yes | `{"battery_count": <int>}` — batteries currently polling this Shelly device. |

### Powermeter health

`{base}/powermeter/{pm}` — published every `POWERMETER_HEALTH_INTERVAL` seconds
(0 turns it off):

```json
{"online": true, "grid_power": {"l1": 120.0, "l2": 0.0, "l3": -30.0, "total": 90.0}}
```

| Field | Type | Meaning |
|---|---|---|
| `online` | bool | `false` when the source stops delivering fresh, usable readings — a stalled push stream, or a polling source whose reads fail. Alert on this to catch a meter that has gone quiet while AstraMeter keeps running on its last cached value. |
| `grid_power` | object | Latest per-phase reading and `total` (watts). Single-phase meters leave `l2`/`l3` `null`. |

Push sources (HomeWizard, MQTT, SMA, Home Assistant) report stream state
directly. Polling sources reflect the control loop, or get probed about once per
interval when no battery is reading them. A multi-phase source whose value
simply stops changing — an idle circuit reporting a steady number — stays
**online**. Only a missing or unavailable reading marks it offline.

### Command topics (set values from any MQTT client)

AstraMeter **subscribes** to the topics below. Publish to them from any client
to change settings live. Publish **retained** if you can: AstraMeter re-reads
those topics on restart, so your values survive one (this is exactly how the HA
entities persist). An empty payload clears a retained command.

Per-consumer (one battery), one scalar value per topic:

| Topic suffix on `{base}/ct002/{did}/consumer/{cid}/…/set` | Payload | Effect |
|---|---|---|
| `active/set` | `true`/`false` (also `on`/`off`, `1`/`0`) | Pause (`false`) or resume a battery; a paused battery is steered to 0 W. |
| `auto_target/set` | `true`/`false` | `true` hands the battery back to automatic control; `false` keeps the manual override. |
| `manual_target/set` | number, −10000…10000 | Force this battery's power (watts). Setting it switches to manual mode. |
| `distribution_weight/set` | number, 0.0…10.0 | Relative share of the split (ratio-based; `0` parks the battery at 0 W but keeps it in the pool). |
| `efficiency_window_weight/set` | number, 0…100 (**percent**) | Share of efficiency-rotation active time. `100` neutral, `0` skips while limiting. |
| `min_dc_output/set` | number, 0…1000 | Per-battery minimum DC discharge keep-alive (watts). |

Per-device, JSON body on `{base}/ct002/{did}/set`:

| Payload | Effect |
|---|---|
| `{"active_control": true}` / `{"active_control": false}` | Turn active control on (compute per-battery targets) or off (relay mode — the raw aggregate is forwarded, the live equivalent of `ACTIVE_CONTROL = False`). |
| `{"force_rotation": true}` | Rotate the efficiency window to the next battery right away. |

Out-of-range, non-numeric, or non-boolean payloads are ignored with a warning.

> **Sign / unit conventions.** All power values are watts. Grid power is
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
**"AstraMeter Powermeter `<Section>`"** device. The section name is Capital-Cased
for the label, and the device is grouped under the **AstraMeter** hub device —
keyed on `ADDON_SLUG` on the add-on, with a stable base-topic fallback so the
grouping also works in standalone/Docker. It carries:

- an **Online** connectivity `binary_sensor` (diagnostic), backed by the
  `online` field of `{base}/powermeter/{pm}`;
- **Power**, **Power L1**, **Power L2**, **Power L3** sensors backed by that
  topic's `grid_power` (single-phase meters leave L2/L3 empty).

See [Powermeter health](#powermeter-health) above for the raw topic and the
exact online/offline rules.

## Per-battery controls (Home Assistant entities)

When HA discovery is on, each battery gets a few **config** entities you can set
live from Home Assistant. Each one maps to a command topic in
[Command topics](#command-topics-set-values-from-any-mqtt-client), so any MQTT
client has the same controls:

- **Manual Target** / **Auto Target** — override a battery's power, or hand it
  back to automatic control.
- **Active** — pause or resume a battery (paused batteries are steered to 0 W).
- **Distribution Weight** — its relative share of the load when the balancer
  splits demand across batteries. `1.0` is neutral. Raise it on a larger battery,
  or lower it on a smaller one, to bias the split. For example, a 5.12 kWh and a
  2.08 kWh battery that you'd like to run roughly **60:40** can use weights
  `1.5` and `1.0`. The split is ratio-based, so only the proportion between
  batteries matters; `0` parks a battery at 0 W while leaving it in the pool. Tune
  it while watching the batteries — the change takes effect on the next control
  cycle.
- **Efficiency Window Weight** — how much of the **efficiency rotation** each
  battery takes when demand is low and the balancer runs only some batteries to
  keep them efficient. `100 %` is neutral. `0 %` skips a battery: it is parked
  while limiting, but still used when all batteries are needed. Values in between
  give it proportionally less, since a battery holds each turn for that fraction
  of [EFFICIENCY_ROTATION_INTERVAL](ct002.md#battery-efficiency-optimization). Two
  batteries you want to take turns in a 1:2 ratio can be set to `50 %` and
  `100 %`: with the default 15-minute interval that is 7.5 minutes against 15.
  This applies while demand runs one battery at a time. Once several run at once,
  every turn is a full interval again (only `0 %` still parks a battery), and
  **Distribution Weight** is what splits the load among them.
- **Min DC Output** — minimum discharge in watts, to stop this battery's inverter
  switching off at 0 W and falling asleep (see
  [MIN_DC_OUTPUT](ct002.md#dc-battery-keep-alive)). Only shown for DC batteries
  where it has an effect (e.g. the Marstek B2500); it overrides the global setting
  for that battery.

The CT device itself also exposes a config switch:

- **Active Control** — on (the default) lets the emulator smooth the grid reading
  and compute per-battery targets. Turn it **off** to fall back to relay mode: the
  raw per-phase aggregate is forwarded and the batteries decide. That is the live
  equivalent of **ACTIVE_CONTROL = False**.

Each of these controls publishes its set-command **retained**, so Home Assistant
restores your values across an AstraMeter restart with no extra configuration.

## Quietening the state topics

A battery polls about once a second and every poll carries a fresh grid
reading, so by default its state topic is published at that rate — and with
several batteries the meter's `status` topic goes out once per battery on top.
Every subscriber then pays to receive and parse all of it, which on a small
machine can cost more than the broker itself ([#663](https://github.com/tomquist/AstraMeter/issues/663)).

`STATE_THROTTLE_INTERVAL` puts a floor on the gap between two publishes of the
same topic:

```ini
[MQTT_INSIGHTS]
BROKER = 192.168.1.100
STATE_THROTTLE_INTERVAL = 5
```

The value that goes out is whichever reading is current when the gap has
passed — readings in between are superseded, not queued — so nothing is
delayed, it is simply sampled less often. With three batteries polling once a
second, `5` takes the state topics from about 6 messages per second to well
under one.

What it does **not** touch: availability, battery removals, discovery, commands,
the powermeter health sensor and the Marstek app broadcast. A battery going
silent still turns Unavailable immediately.

The cost is resolution. Home Assistant's history for grid power and targets
becomes as coarse as the interval you set, so pick the largest value your
graphs and automations can live with. `0` (the default) keeps the old
behaviour. The **Poll Interval** sensor is unaffected either way — it reports
the battery's real cadence, not how often we publish it.

On the ESPHome side the same setting is `state_throttle_interval` under
`mqtt_insights:`, as a time period:

```yaml
ct002:
  mqtt_insights:
    state_throttle_interval: 5s
```

## Is a battery still reporting? (Home Assistant)

There is deliberately **no "Last Seen" entity**. Its value changed on every poll,
and Home Assistant records every change of a timestamp sensor in the logbook, so
a battery polling once a second buried the logbook under its own heartbeat. Home
Assistant already tracks the same thing for you.

**Unavailable means "not reporting".** When a battery stops polling, AstraMeter
drops it, publishes `offline` on its availability topic, and **every entity of
that battery turns Unavailable**. It drops the battery after it misses ~2 of its
own poll cycles, or after `CONSUMER_TTL` seconds if you set a fixed window
([CT002 basic configuration](ct002.md#basic-configuration); 120 s for a Shelly
battery). That is the signal to alert on, and it needs no template:

```yaml
alias: AstraMeter – battery stopped reporting
triggers:
  - trigger: state
    entity_id: sensor.astrameter_consumer_hmj_2_aabbccddeeff_poll_interval  # replace with yours
    to: unavailable
    for:
      minutes: 5
actions:
  - action: notify.persistent_notification
    data:
      message: The battery has not reported to AstraMeter for five minutes.
```

**The timestamp itself is on every entity.** Home Assistant stamps each state
write with `last_reported`, which advances even when the value is unchanged —
exactly what Last Seen used to publish. Any entity of the battery will do:

```jinja
{{ states.sensor.astrameter_consumer_hmj_2_aabbccddeeff_poll_interval.last_reported }}
```

Seconds since the battery was last heard from:

```jinja
{{ (now() - states.sensor.astrameter_consumer_hmj_2_aabbccddeeff_poll_interval.last_reported).total_seconds() | round }}
```

Two things to know when you use these:

- `last_changed` / `last_updated` — the **Last changed** line in an entity's
  more-info dialog — only move when the value itself changes, so a battery
  reporting the same wattage twice does not bump them. `last_reported` is the one
  that follows every poll.
- A template re-renders when an entity's state **changes**, not when it merely
  re-reports the same value. Including `now()`, as above, also refreshes the
  template at the start of every minute, and that is what keeps an age
  calculation live.
- If you have set [`STATE_THROTTLE_INTERVAL`](#quietening-the-state-topics),
  `last_reported` follows every **publish** rather than every poll, so it can
  sit up to that interval behind a battery that is answering perfectly. Keep
  any "not reporting" threshold comfortably above the interval, or trigger on
  the entity going `unavailable` as above — availability is never throttled.

**For the polling rate rather than recency**, use the **Poll Interval** and
**Answer Interval** sensors. They carry a unit, so Home Assistant treats them as
continuous and keeps them out of the logbook however fast they change.

If you still want a Last Seen entity of your own, a
[template sensor](https://www.home-assistant.io/integrations/template/) over
`last_reported` gives you one. Exclude it under `recorder:` → `exclude:`, or it
floods your logbook exactly as the built-in one did.

## Optional: Marstek mobile app (live MQTT)

You do **not** need this for Home Assistant. It only helps the **Marstek app**
show live CT002/CT003 grid power over the same cloud MQTT path, when
**[hame-relay](https://github.com/tomquist/hame-relay)** bridges your broker. Use
**hame-relay ≥ 1.3.5** so polls and replies work reliably. UDP between the
batteries and AstraMeter is unchanged for control.

**If you want it**

- **`[MARSTEK]`** — Managed fake CT, so the **MQTT MAC** matches the cloud device.
- **Same broker as hame-relay** — `[MQTT_INSIGHTS]` must point at the broker relay
  uses toward Marstek's cloud.

**Toggles** (defaults in table above)

- **`MARSTEK_MQTT_ENABLED`** — `false` = HA MQTT Insights only, no Marstek poll
  replies.
- **`MARSTEK_MQTT_INTERVAL`** — Optional periodic aggregate pushes; **`0`** =
  answer polls only.

Replies follow the usual `hame_energy/…` / `marstek_energy/…` App/device topics
for a real CT; AstraMeter matches your CT002/CT003 **type** and **MAC**. These
use a separate protocol specific to the Marstek cloud, unrelated to the
`{base}/…` insight topics above.
