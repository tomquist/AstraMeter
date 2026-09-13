# Frequently Asked Questions (FAQ)

## General usage and setup

### The emulator starts and shows "listening" message but nothing else happens. Is this a problem?

A: No, that's normal. The emulator waits for your storage system to ask for
data, and only polls when asked. Until your Marstek device sends a request, you
won't see any further activity.

### My Marstek device can't find the emulated powermeter. What could be wrong?

A: Common causes include:

- **Firmware issues:** See the firmware requirements in the
  [Device and firmware](#device-and-firmware-specific) section below
- **Network setup:** Put both devices on the same subnet (255.255.255.0)
- **Bluetooth interference:** Disconnect any Bluetooth connections while you set
  up
- **Docker configuration:** With Docker, set `network_mode: host` so the
  container can receive UDP broadcasts
- **CT002/CT003 pairing flow:** For managed fake CTs, refresh the CT device list
  (or log out and back in). Then pick `AstraMeter CT002` / `AstraMeter CT003`,
  switch battery mode to automatic, and select that CT. You can select it as
  soon as it appears in the device list. The fake CT shows as offline in the CT
  list (expected).
- **Config source confusion:** If you use `custom_config` in the Home Assistant
  app, it overrides the credentials and options from the app UI.

### The emulator isn't visible in the Shelly app or network scanners. Is this normal?

A: Yes. The emulator implements only the minimal protocol that Marstek storage
systems need. It is not a complete Shelly device emulation.

### How do I autostart the script on boot?

A: Use systemd to create a service:

1. Create a unit file (e.g., `/etc/systemd/system/astrameter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable astrameter && sudo systemctl start astrameter`

### Can I run multiple instances for different storage devices?

A: Yes. Define several sections in `config.ini` (e.g., `[SHELLY_1]`,
`[SHELLY_2]`), then use the `NETMASK` setting to assign each one to specific
client IPs. See [Multiple Powermeters](configuration.md#multiple-powermeters).

## Configuration & integration

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

- `CURRENT_POWER_ENTITY`: For a single bidirectional sensor (positive/negative
  values)
- `POWER_INPUT_ALIAS`/`POWER_OUTPUT_ALIAS`: Entity IDs for separate import/export
  sensors (with `POWER_CALCULATE = True`)

### How should I feed import and export power — one sensor or two? (Home Assistant App)

A: One sensor, if you have a single signed one (positive for import, negative
for export). In the Home Assistant App, put it in `POWER_INPUT_ALIAS` (or
`CURRENT_POWER_ENTITY`) only, and leave `POWER_OUTPUT_ALIAS` empty. Separate
import/export sensors can update at different moments and get read out of sync,
which causes drift and oscillation. A single signed value avoids that.

### Should I use Shelly emulation or CT002/CT003 for multiple batteries?

A: Prefer CT002/CT003 (set `DEVICE_TYPE = ct002` or `ct003`) for multi-battery
setups. With Shelly emulation, each battery reacts on its own, and they tend to
fight each other — one charges while another discharges. The CT emulation
coordinates one shared target across the fleet, so the load is spread more
evenly and stays stable. See [CT002 / CT003 steering](ct002.md).

## Device and firmware specific

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

A: Possibly. The CT002/CT003 emulation is Marstek-specific, but the Shelly
emulation is not: any storage system that reads a Shelly Pro 3EM as its meter
can use AstraMeter. Pick a `shellypro3em` device type, and if your battery
discovers meters by itself rather than taking an address, it should find
AstraMeter with no extra setup — see
[Shelly discovery and the HTTP surface](shelly-tcp.md).

This is not verified against every model, so treat it as "worth trying" rather
than supported. If it works — or doesn't — please open an issue saying which
model and what happened.

## Troubleshooting

### I get permission errors when binding to port 1010/2220.

A: Ports below 1024 need root privileges on Linux. Solutions:

- Use Docker or Home Assistant App (recommended)
- Use `setcap` to grant permissions
- Run as root (not recommended)

Note: the Docker image runs as a non-root user, but it grants its interpreter
`CAP_NET_BIND_SERVICE`, so privileged ports — 1010 for `shellypro3em_old` and
the combined `shellypro3em`, and 80 for the Shelly HTTP surface — bind without
any extra setup under `network_mode: host`. Port 2220 (`shellypro3em_new`) never
needed it.

If you run the image with capabilities dropped (`cap_drop: [ALL]` in Compose,
`drop: ["ALL"]` in Kubernetes, rootless Podman), the container still starts but
cannot bind those ports; you will see one logged error naming the port. Add
`cap_add: [NET_BIND_SERVICE]`, lower the host's privileged-port range
(`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80`, persist via
`/etc/sysctl.d/`), or run as root (`user: "0:0"`).

Publishing these ports via bridge networking does **not** work for the UDP ones,
because the Marstek discovery packets are UDP broadcasts to the subnet address
and aren't forwarded by Docker's port mapping. The same is true of mDNS, so
Shelly discovery needs host networking too — see
[Shelly discovery and the HTTP surface](shelly-tcp.md).

### A battery discovers the meter but never connects.

A: It has most likely found the announcement and then tried to reach the meter
on port 80. Check `TCP_PORT` is `80` (the default) and that nothing else on the
host already holds that port — a reverse proxy add-on, for instance.
[Shelly discovery and the HTTP surface](shelly-tcp.md#if-a-battery-wont-pair)
walks through the rest.

### I get parsing errors on startup or the app crashes.

A: Common causes:

- Incorrect entity IDs or API access
- Memory limitations (especially on RPi 2 or similar devices)
- Check logs for specific error messages

### How can I test without a storage device?

A: Only partly — you can verify the initial configuration. Full testing needs a
Marstek device in "self-adaptation" mode to request data. For local end-to-end
testing without hardware, use the [simulator](simulator.md).

### My output power oscillates or yo-yos between zero and full.

A: This usually happens when your battery asks AstraMeter for a new reading
more often than your meter has a fresh one. The battery keeps reacting
to stale numbers, overshoots, and swings back and forth. The fix is to slow
things down and smooth out the readings. Try these one at a time. After each
change, watch how the battery behaves for a few minutes before moving on:

1. **Prefer a fresh reading.** This setting matters most here. Your battery adds
   each answer to its current output, so the same reading twice makes it apply
   the same correction twice — and that is what an overshoot looks like. Leave
   `WAIT_FOR_NEXT_MESSAGE = true` (the default) so AstraMeter waits briefly, up
   to 2 seconds, for a *new* reading before it answers. If that wait times out
   it still replies from the cache, so this improves the odds rather than
   guaranteeing that every reply is fresh. If your meter is polled rather than
   pushed, set `THROTTLE_INTERVAL = 1` so AstraMeter re-reads it at most once a
   second.

   Reach for `DEDUPE_TIME_WINDOW` only if a battery really floods you with
   repeat polls. It leaves the extra polls **unanswered**, so raising it does
   not slow a battery down smoothly — the answered rate can only drop to half, a
   third, a quarter of the battery's own poll rate. That is why nudging the
   value often changes nothing, and then changes a lot.
2. **Ignore tiny wobbles.** Raise `DEADBAND` to around `10`–`20` (watts). Small
   fluctuations near zero then count as "close enough" and don't trigger a
   correction.
3. **Smooth the changes.** Set `SMOOTH_TARGET_ALPHA` to around `0.2`–`0.4` and
   `MAX_SMOOTH_STEP` to around `40`–`60`, so the reported power moves in gentle
   steps instead of jumping.

If it still swings after that, the most effective option is the
**[PID Controller](configuration.md#pid-controller)** — a smart helper that
gently nudges the reading toward zero and calms down a battery that tends to
over- or under-react. To get started, just set `PID_KP = 0.5` and
`PID_MODE = bias`, and leave the other `PID_*` settings alone. A few more
optional filters (including one that throws out occasional bad spikes) are
described under
[Per-powermeter options](configuration.md#per-powermeter-options) if you want to
fine-tune further.

### My second battery never kicks in, or my batteries won't settle near zero.

A: `MIN_EFFICIENT_POWER` governs this: it decides how many batteries are engaged
for a given demand. It's meant for AC batteries that can hold a precise
setpoint; pure DC battery pools can't be steered to exactly zero the same way.
If a second unit won't engage, lower `MIN_EFFICIENT_POWER`. For DC-only setups,
set it to `0`. See
[Battery efficiency optimization](ct002.md#battery-efficiency-optimization).

### I have batteries of different capacities and the smaller one drains much faster.

A: By default AstraMeter splits the load equally, so a 5 kWh battery gets the
same share as a 15 kWh one and saturates first. Three settings work together to
fix this:

- **`MIN_EFFICIENT_POWER`** is a *per-battery* threshold, not a total. Set it to
  900 W, say, and AstraMeter won't activate a second battery until demand is
  high enough for each battery to handle at least 900 W. For asymmetric packs,
  lower this value. Both batteries then run together across a wider demand
  range, instead of the smaller one absorbing everything alone.
- **Distribution Weight** (the per-battery slider exposed via MQTT Insights)
  splits load in proportion to capacity, once both batteries are active
  simultaneously. For example, a 15 kWh battery paired with a 5 kWh one might
  use weights of `3.0` and `1.0` for a 75:25 split.
- **Efficiency Window Weight** (also a per-battery slider via MQTT Insights,
  shown only when `MIN_EFFICIENT_POWER > 0`) controls what fraction of the
  efficiency-rotation active time each battery holds. When demand is low, only
  one battery runs at a time, so a larger battery should hold a proportionally
  bigger slice of the rotation window — e.g. `75` % and `25` % for the same 3:1
  capacity ratio — and handle more of the cumulative energy over time. It
  applies only while demand is low enough to run **one** battery at a time,
  which is what it describes: how long each battery holds the single rotating
  slot. Once demand runs several at once, every turn is a full interval again,
  so only `0` % still has an effect (it parks a battery whenever the others can
  cover) — use **Distribution Weight** to divide load between batteries that run
  together.

See [CT002 / CT003 steering](ct002.md) for details on all three settings.

### The Marstek app shows the meter offline or doesn't display my real meter values.

A: This is expected for purely local operation. The emulated meter usually fills
in only one phase, and the app won't show your raw readings because each battery
is handed only its share of the target (so the totals steer toward zero). It
does not mean the integration is failing. If you do want live readings in the
Marstek app, configure the `[MARSTEK]` section together with
[hame-relay](https://github.com/tomquist/hame-relay) (≥ 1.3.5). AstraMeter can
then answer the app's polls via MQTT. See
[MQTT Insights](mqtt-insights.md#optional-marstek-mobile-app-live-mqtt).

## Advanced

### How can I distribute load based on each battery's State of Charge (SoC)?

A: AstraMeter exposes a **Distribution Weight** entity for every battery in a
CT002/CT003 fleet (requires [MQTT Insights](mqtt-insights.md) with HA discovery
enabled). Raise the weight on a battery and it receives a larger share of the
charging or discharging target. You can adjust these weights dynamically from a
Home Assistant automation, so emptier batteries are prioritised and fuller ones
are throttled back.

#### Step 1 — Find the Distribution Weight entity for each battery

1. In Home Assistant go to **Settings → Devices & Services → MQTT** and open the
   **Devices** tab.
2. Look for devices named **AstraMeter Consumer …** (one per battery). Open each
   one.
3. Under **Controls** you will find a **Distribution Weight** slider. Note its
   entity ID — it looks like
   `number.astrameter_consumer_<mac>_distribution_weight`, where `<mac>` is the
   battery's MAC address lowercased with all non-alphanumeric characters removed
   (e.g. a battery with MAC `AA:BB:CC:DD:EE:FF` produces
   `number.astrameter_consumer_aabbccddeeff_distribution_weight`).

   You can also find the entity ID on the entity's detail page: click the gear
   icon, and the entity ID is shown at the top of the settings dialog.

#### Step 2 — Find the SoC sensor for each battery

The SoC sensor comes from your battery's native integration (e.g. hm2mqtt,
hame-relay, or any other source). Open the battery device in Home Assistant,
find the **State of Charge** sensor, and note its entity ID
(e.g. `sensor.marstek_b2500_aabbccddeeff_soc`).

#### Step 3 — Create the automation

The formula below maps SoC to weight linearly: an empty battery (0 %) gets
weight 2.0, and a full battery (100 %) gets weight 0.0. Adjust the formula to
taste — for example, clamp the minimum above 0 if you never want a battery fully
excluded.

Go to **Settings → Automations & Scenes → Create Automation → Start with an
empty automation** and paste the following YAML (switch to YAML mode with the
three-dot menu):

```yaml
alias: AstraMeter – SoC-based distribution weights
description: >
  Adjust each battery's distribution weight inversely proportional to its SoC
  so that the emptiest battery is charged first.
triggers:
  - trigger: state
    entity_id:
      - sensor.marstek_b2500_aabbccddeeff_soc   # battery 1 SoC — replace with yours
      - sensor.marstek_b2500_112233445566_soc   # battery 2 SoC — replace with yours
    for:
      seconds: 10
actions:
  - action: number.set_value
    target:
      entity_id: number.astrameter_consumer_aabbccddeeff_distribution_weight
    data:
      value: >
        {{ [0, 2.0 * (1 - states('sensor.marstek_b2500_aabbccddeeff_soc') | float(100) / 100)] | max | round(1) }}
  - action: number.set_value
    target:
      entity_id: number.astrameter_consumer_112233445566_distribution_weight
    data:
      value: >
        {{ [0, 2.0 * (1 - states('sensor.marstek_b2500_112233445566_soc') | float(100) / 100)] | max | round(1) }}
mode: queued
max: 2
```

Replace the four entity IDs with the real ones you found in steps 1 and 2. Add
one `number.set_value` action block per additional battery.

> **Tip:** `mode: queued` with `max: 2` keeps a burst of rapid SoC updates from
> piling up, and the 10-second `for:` delay further debounces short-lived
> spikes. Increase `max` if you have more than two batteries, so that a
> concurrent trigger for every battery can queue safely.

### How do signed (positive/negative) power values work with the emulator?

A: Powermeters typically report import as positive and export as negative (see
[What's the correct power value convention?](#whats-the-correct-power-value-convention)
above). The Shelly and CT002/CT003 emulators forward those signed watts into the
Marstek protocols. What happens on the battery side depends on your firmware and
device type.
