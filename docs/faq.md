# Frequently Asked Questions (FAQ)

## General usage and setup

### The emulator starts and shows "listening" message but nothing else happens. Is this a problem?

A: No, this is expected behavior. The emulator waits for the storage system to
request data and only polls when requested. Without an active request from your
Marstek device, you won't see further activity.

### My Marstek device can't find the emulated powermeter. What could be wrong?

A: Common causes include:

- **Firmware issues:** See the firmware requirements in the
  [Device and firmware](#device-and-firmware-specific) section below
- **Network setup:** Ensure both devices are on the same subnet (255.255.255.0)
- **Bluetooth interference:** Disconnect any Bluetooth connections during setup
- **Docker configuration:** When using Docker, set `network_mode: host` to enable
  UDP broadcast reception
- **CT002/CT003 pairing flow:** For managed fake CTs, refresh the CT device list
  (or log out/in), then pick `AstraMeter CT002` / `AstraMeter CT003`, switch
  battery mode to automatic, and select that CT. It should be selectable as soon
  as it appears in the device list. The fake CT appears as offline in the CT list
  (expected).
- **Config source confusion:** If Home Assistant app `custom_config` is used, it
  overrides app UI credentials/options.

### The emulator isn't visible in the Shelly app or network scanners. Is this normal?

A: Yes. The emulator only implements the minimal protocol needed for Marstek
storage systems and is not a complete Shelly device emulation.

### How do I autostart the script on boot?

A: Use systemd to create a service:

1. Create a unit file (e.g., `/etc/systemd/system/astrameter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable astrameter && sudo systemctl start astrameter`

### Can I run multiple instances for different storage devices?

A: Yes. Define multiple sections in `config.ini` (e.g., `[SHELLY_1]`,
`[SHELLY_2]`) and use the `NETMASK` setting to assign each to specific client IPs.
See [Multiple Powermeters](configuration.md#multiple-powermeters).

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

A: In the Home Assistant App, if you have a single signed sensor (positive for
import, negative for export), put it in `POWER_INPUT_ALIAS` (or
`CURRENT_POWER_ENTITY`) only and leave `POWER_OUTPUT_ALIAS` empty. Separate
import/export sensors can update at different moments and get read out of sync,
causing drift and oscillation; a single signed value avoids that.

### Should I use Shelly emulation or CT002/CT003 for multiple batteries?

A: Prefer CT002/CT003 (set `DEVICE_TYPE = ct002` or `ct003`) for multi-battery
setups. With Shelly emulation each battery reacts independently and they tend to
fight each other (one charging while another discharges). The CT emulation
coordinates a shared target across the fleet, giving more even and stable
distribution. See [CT002 / CT003 steering](ct002.md).

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

A: No, this project is Marstek-specific. For other brands, see
[uni-meter](https://github.com/sdeigm/uni-meter).

## Troubleshooting

### I get permission errors when binding to port 1010/2220.

A: Ports below 1024 require root privileges on Linux. Solutions:

- Use Docker or Home Assistant App (recommended)
- Use `setcap` to grant permissions
- Run as root (not recommended)

Note: the Docker image runs as a non-root user, so binding port 1010 (used by
`shellypro3em_old` and the combined `shellypro3em`, which starts both listeners)
still fails with `PermissionError: [Errno 13]` under `network_mode: host`. Port
2220 (`shellypro3em_new`) is unaffected. Either lower the host's privileged-port
range (`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1010`, persist via
`/etc/sysctl.d/`) or run the container as root (`user: "0:0"` in compose).
Publishing the port via bridge networking does **not** work, because the Marstek
discovery packets are UDP broadcasts to the subnet address and aren't forwarded by
Docker's port mapping.

### I get parsing errors on startup or the app crashes.

A: Common causes:

- Incorrect entity IDs or API access
- Memory limitations (especially on RPi 2 or similar devices)
- Check logs for specific error messages

### How can I test without a storage device?

A: You can only verify the initial configuration. Full testing requires a Marstek
device in "self-adaptation" mode to request data. For local end-to-end testing
without hardware, use the [simulator](simulator.md).

### My output power oscillates or yo-yos between zero and full.

A: This usually happens when your battery asks AstraMeter for a new power reading
more often than your meter actually has a fresh one. The battery keeps reacting to
stale numbers, overshoots, and ends up swinging back and forth. The fix is to slow
things down and smooth out the readings. Try these one at a time, and watch how the
battery behaves for a few minutes after each change before moving on:

1. **Don't re-read the meter too often.** Set `THROTTLE_INTERVAL = 1` so
   AstraMeter waits at least one second between readings, and
   `DEDUPE_TIME_WINDOW = 0.9` so it ignores duplicate readings that arrive in that
   window.
2. **Ignore tiny wobbles.** Raise `DEADBAND` to around `10`–`20` (watts) so small
   fluctuations near zero are treated as "close enough" and don't trigger a
   correction.
3. **Smooth the changes.** Set `SMOOTH_TARGET_ALPHA` to around `0.2`–`0.4` and
   `MAX_SMOOTH_STEP` to around `40`–`60` so the reported power moves in gentle
   steps instead of jumping.

If it's still swinging after that, the most effective option is to turn on the
**[PID Controller](configuration.md#pid-controller)** — a smart helper that gently
nudges the reading toward zero and calms down a battery that tends to over- or
under-react. To get started, just set `PID_KP = 0.5` and `PID_MODE = bias`, and
leave the other `PID_*` settings alone. There are a few more optional filters
(including one that throws out occasional bad spikes) described under
[Per-powermeter options](configuration.md#per-powermeter-options) if you want to
fine-tune further.

### My second battery never kicks in, or my batteries won't settle near zero.

A: This is governed by `MIN_EFFICIENT_POWER`, which decides how many batteries are
engaged for a given demand. It's intended for AC batteries that can hold a precise
setpoint; pure DC battery pools can't be steered to exactly zero the same way. If a
second unit won't engage, lower `MIN_EFFICIENT_POWER`; for DC-only setups, set it
to `0`. See [Battery efficiency optimization](ct002.md#battery-efficiency-optimization).

### The Marstek app shows the meter offline or doesn't display my real meter values.

A: This is expected for purely local operation — the emulated meter typically
populates only one phase, and the app won't show your raw readings because each
battery is only handed its share of the target (so the totals steer toward zero).
It does not mean the integration is failing. If you do want live readings in the
Marstek app, configure the `[MARSTEK]` section together with
[hame-relay](https://github.com/tomquist/hame-relay) (≥ 1.3.5) so AstraMeter can
answer the app's polls via MQTT. See
[MQTT Insights](mqtt-insights.md#optional-marstek-mobile-app-live-mqtt).

## Advanced

### How can I distribute load based on each battery's State of Charge (SoC)?

A: AstraMeter exposes a **Distribution Weight** entity for every battery in a
CT002/CT003 fleet (requires [MQTT Insights](mqtt-insights.md) with HA discovery
enabled). Raising the weight on a battery makes it receive a larger share of the
charging or discharging target; you can adjust these weights dynamically from a
Home Assistant automation so that emptier batteries are prioritised and fuller
ones are throttled back.

#### Step 1 — Find the Distribution Weight entity for each battery

1. In Home Assistant go to **Settings → Devices & Services → MQTT** and open the
   **Devices** tab.
2. Look for devices named **AstraMeter Consumer …** (one per battery). Open each
   one.
3. Under **Controls** you will find a **Distribution Weight** slider. Note its
   entity ID — it looks like
   `number.astrameter_consumer_<mac>_distribution_weight`, where `<mac>` is the
   battery's MAC address with all non-alphanumeric characters replaced by `_`
   (e.g. a battery with MAC `AA:BB:CC:DD:EE:FF` produces
   `number.astrameter_consumer_aabbccddeeff_distribution_weight`).

   You can also find the entity ID by opening the entity's detail page and
   clicking the gear icon → the entity ID is shown at the top of the settings
   dialog.

#### Step 2 — Find the SoC sensor for each battery

The SoC sensor comes from your battery's native integration (e.g. hm2mqtt,
hame-relay, or any other source). Open the battery device in Home Assistant,
find the **State of Charge** sensor, and note its entity ID
(e.g. `sensor.marstek_b2500_aabbccddeeff_soc`).

#### Step 3 — Create the automation

The formula below maps SoC to weight so that an empty battery (0 %) gets weight
2.0 and a full battery (100 %) gets weight 0.0, linearly. Adjust the formula to
taste — for example clamp the minimum above 0 if you never want a battery fully
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
        {{ [0, 2.0 * (1 - states('sensor.marstek_b2500_aabbccddeeff_soc') | float(50) / 100)] | max | round(1) }}
  - action: number.set_value
    target:
      entity_id: number.astrameter_consumer_112233445566_distribution_weight
    data:
      value: >
        {{ [0, 2.0 * (1 - states('sensor.marstek_b2500_112233445566_soc') | float(50) / 100)] | max | round(1) }}
mode: queued
max: 2
```

Replace the four entity IDs with the real ones you found in steps 1 and 2. Add
one `number.set_value` action block per additional battery.

> **Tip:** `mode: queued` with `max: 2` ensures that a burst of rapid SoC
> updates doesn't pile up; the 10-second `for:` delay further debounces
> short-lived spikes. Increase `max` if you have more than two batteries so
> that a concurrent trigger for every battery can queue safely.

### How do signed (positive/negative) power values work with the emulator?

A: Powermeters typically report import as positive and export as negative (see
[What's the correct power value convention?](#whats-the-correct-power-value-convention)
above). Shelly and CT002/CT003 emulators forward those signed watts into the
Marstek protocols; behavior on the battery side depends on your firmware and
device type.
