# Frequently Asked Questions (FAQ)

## General usage and setup

### The log stops after "listening". Is AstraMeter working?

A: That is normal until a battery sends its first request. AstraMeter only
replies to requests, so after `CT002 UDP server listening on port 12345` or
`Shelly emulator listening on UDP port 1010` the log stays quiet until a
battery polls. To check whether a battery is polling:

- Open the dashboard: AstraMeter in the Home Assistant sidebar, or
  `http://<host>:52500/`. "No batteries have reported yet" means no request
  has arrived. Otherwise each battery card shows when the battery last polled
  and how often it polls.
- Look in the log at the default `info` level. The first request from a
  CT002/CT003 battery logs `CT002 consumer <battery MAC> phase detected: A`
  (or B, C, D). The first Shelly request logs
  `Battery detected on Shelly UDP port 1010: <battery IP>`. A battery that
  hasn't detected its phase yet logs nothing at `info`; the dashboard lists it
  with phase *Unassigned*.

If no request arrives, see the next question. A Python install outside Docker
and Home Assistant logs only warnings by default. Start it with
`--loglevel info` to see these lines.

### My Marstek device can't find the emulated powermeter. What could be wrong?

A: In almost every case the battery's request never reaches AstraMeter, or
AstraMeter receives it and ignores it. Check the battery, then the network,
then the log.

**Battery and Marstek app**

- The battery only sends requests after you select a meter for it and set it
  to Automatic (Self-Adaptation) mode.
- For CT002/CT003, the Marstek app only lists CTs that are registered to your
  account. If you don't own a real CT, AstraMeter can register one (see
  [Do I need a Marstek account?](#do-i-need-a-marstek-account-and-how-does-astrameter-ct002-get-into-the-app)).
  Refresh the CT list or log out and back in, select `AstraMeter CT002` or
  `AstraMeter CT003`, and set the battery to automatic. The CT list shows the
  AstraMeter CT as offline even when everything works.
- The battery firmware must support the meter type you selected (see
  [What firmware do I need?](#what-firmware-do-i-need-for-my-marstek-device)).
- If the battery was paired with a different meter before, or was just
  updated, remove the meter in the app and select it again. A few users needed
  a factory reset.
- If the battery is connected by cable and Wi-Fi at the same time, unplug the
  cable. Several users found that it only works over one connection.
- Close any Bluetooth connection to the battery, from the Marstek app or an
  ESP32 tool, during setup.

**Network**

- The battery sends its request as a UDP broadcast to its subnet, for example
  to `192.168.1.255`. The host running AstraMeter must be in the same subnet
  and use the same netmask. A host with a `/32` mask, or with `/24` while the
  battery has `/22`, discards the broadcast. Run `ip -4 addr` on the host and
  compare the address after `brd` with the battery's subnet.
- Broadcasts don't cross VLANs or guest networks. Many access points, mesh
  systems and repeaters drop them when client isolation, broadcast or
  multicast filtering, or IGMP snooping is enabled. A firewall on the host
  (ufw, iptables) must allow incoming UDP on the ports below.
- In Docker, use `network_mode: host`. Port mappings (`-p`), bridge networks
  and macvlan don't forward broadcasts.
- AstraMeter listens on UDP port 12345 for CT002/CT003, on 1010 and 2220 for
  Shelly Pro 3EM, on 2222 for Shelly EM gen3 and on 2223 for Shelly Pro EM50.
- Run only one meter emulator on the network. The battery uses whichever
  answers first, so a second AstraMeter (for example the Home Assistant App
  and a Docker container), uni-meter, an ESP-based emulator, or a real meter
  that is still connected will interfere.

**Log**

1. Enable a [debug log](#how-do-i-get-a-debug-log). Each request then appears
   as `CT002 request from ('<battery IP>', …)`, or `Parsed request from
   <battery IP>` for Shelly. The next line is either the reply
   (`CT002 response to …` or `Sending response: …`) or the reason AstraMeter
   ignored the request:
   - `… due to CT MAC mismatch`: leave `CT_MAC` empty so AstraMeter answers
     requests for any CT.
   - `No powermeter found for client <IP>`: the `NETMASK` of every powermeter
     section excludes this battery.
2. If the debug log shows no requests, run
   `sudo tcpdump -ni any udp port 12345` (or `1010`, `2220`) on the host. If
   tcpdump shows nothing either, the battery isn't sending or the network
   drops the packets. If tcpdump shows packets that the log doesn't, check
   that their destination address matches the host's broadcast address.
3. For Shelly emulation, send a test request from another computer:
   `echo -n '{"id":1,"method":"EM.GetStatus","params":{"id":0}}' | nc -u -w2 <AstraMeter IP> 1010`.
   If you get a reply, AstraMeter works and the problem is between the battery
   and the host.

### The emulator isn't visible in the Shelly app or network scanners. Is this normal?

A: Yes. The emulator implements only the minimal protocol that Marstek storage
systems need. It is not a complete Shelly device emulation.

### How do I install AstraMeter, update it, or try a pre-release?

A: **Home Assistant App:** AstraMeter isn't in the default app store. Add the
repository `https://github.com/tomquist/astrameter#main` first, or use the
button in [Home Assistant App installation](installation/home-assistant.md).
Enter the URL exactly like this. A link to a GitHub page, such as
`…/tree/develop`, can't be installed. New releases then appear as normal
updates.

**Docker:** create `config.ini` before you run `docker compose up` for the
first time. If the file doesn't exist, Docker creates an empty directory with
that name instead. See [Docker installation](installation/docker.md).

**Pre-release builds** (`next`) are built from the `develop` branch. Use them
to test a fix before it is released, not for everyday use.

- Home Assistant App: add `https://github.com/tomquist/astrameter#develop` and
  install AstraMeter from that repository. Pre-release builds have no version
  number, so Home Assistant never offers an update. To get a newer build,
  write down your settings, uninstall the app and install it again. The
  pre-release is a separate app with its own settings and its own
  `/addon_configs` folder, so copy your custom config file over. Don't run it
  next to the stable app; both use the same ports.
- Docker: use the image `ghcr.io/tomquist/astrameter:next` and update with
  `docker compose pull && docker compose up -d`.
- ESPHome: use `source: github://tomquist/astrameter@develop`.

To go back to the stable release, uninstall the pre-release, remove the
`#develop` repository and install AstraMeter from `#main` again.

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

### Which power value does AstraMeter need?

A: The net power at your grid connection in watts, which is what your
electricity meter measures. Import from the grid is positive, export to the
grid is negative.

This value must change when the battery changes its output. The battery
adjusts its output until the value reaches zero. If the value doesn't react,
the battery keeps increasing its output up to the maximum, or never starts.
This happens when you use:

- house consumption or solar production instead of grid power, or a
  calculated value that leaves out the battery or inverter
- the battery's own output power as the export (output) sensor
- energy counters in kWh, such as Tasmota's `E_in` and `E_out`, instead of
  power in W. `JSON_POWER_CALCULATE` subtracts two power values and doesn't
  work with energy counters.
- a fixed test value

If import and export have the wrong sign, see
[How do I fix an inverted sign?](#how-do-i-fix-an-inverted-sign-or-keep-a-small-grid-import)

### My sensor reports kW. Do I need to convert it to W?

A: Not for Home Assistant sensors, `[ESPHOMENATIVE]`, or sensors on the
ESPHome firmware. Since 2.3.0 AstraMeter reads the unit these sensors declare
and converts kW to W itself. A ×1000 template or `POWER_MULTIPLIER = 1000`
scales the value a second time, so remove any you added before 2.3.0. Also
check that each template sensor declares the unit its value is in. A template
that outputs watts but declares `kW` is read as kilowatts, and the battery
swings between zero and full power.

Other sources need a conversion:

- MQTT, JSON HTTP, Modbus and Script sources carry no unit. Set
  `POWER_MULTIPLIER = 1000`.
- Tasmota, ioBroker and `[ESPHOME]` (the ESPHome web server source) only read
  whole numbers, so 0.42 kW becomes 0 before the multiplier is applied. Set
  the device to report W, or switch from `[ESPHOME]` to `[ESPHOMENATIVE]`.

### How do I fix an inverted sign, or keep a small grid import?

A: Use `POWER_MULTIPLIER` and `POWER_OFFSET` (**Power Multiplier** and
**Power Offset** in the Home Assistant App). Set them in your powermeter
section; they have no effect in `[GENERAL]`. AstraMeter calculates
`value × POWER_MULTIPLIER + POWER_OFFSET`, see
[Value Transformation](configuration.md#value-transformation).

- If export shows as positive, for example because a current clamp is mounted
  the wrong way round, set `POWER_MULTIPLIER = -1`. To invert single phases,
  give one value per phase, for example `1,-1,1`.
- To keep a small import instead of zero, use a negative offset. With
  `POWER_OFFSET = -20`, AstraMeter reports 20 W less than measured, the battery
  steers that value to zero, and your actual grid power settles at about 20 W
  import. A positive offset leads to export. A single offset value is added
  to every phase, so on a three-phase meter `-20` shifts the total by 60 W.
  Use `-20,0,0` to shift it by 20 W.
- With CT002/CT003, AstraMeter also covers the last few watts of import
  itself (`IMPORT_TRIM_W` in `[CT002]` or `[CT003]`, default 15 W). Set it to
  `0` if you want to keep an import margin or if you see small exports.
- On the ESPHome firmware, add a `multiply: -1` or `offset: -20` filter to the
  grid sensor, and set `import_trim_w: 0` under `ct002:`.

No setting prevents export while the battery is full or already charging at
its maximum power. The surplus has nowhere else to go.

### How do I set up three-phase measurement in the Home Assistant App?

A: Use comma-separated entity IDs:

```
sensor.phase1,sensor.phase2,sensor.phase3
```

### Should I use one signed sensor or separate import and export sensors?

A: Use one signed sensor if you have one. Separate import and export sensors
update at different times, so AstraMeter can combine a new import value with
an old export value. The result jumps, and the battery drifts or oscillates.

- Home Assistant App: enter the signed sensor in **Power Input Entity ID** and
  leave **Power Output Entity ID** empty. Fill in both only if you have
  nothing but separate sensors. AstraMeter then uses input minus output.
- `config.ini` with `[HOMEASSISTANT]`: use `CURRENT_POWER_ENTITY` for a signed
  sensor. For separate sensors, set `POWER_CALCULATE = True` together with
  `POWER_INPUT_ALIAS` and `POWER_OUTPUT_ALIAS`. `[IOBROKER]` uses the same
  keys. For `[TASMOTA]`, set `JSON_POWER_CALCULATE = True` together with
  `JSON_POWER_INPUT_MQTT_LABEL` and `JSON_POWER_OUTPUT_MQTT_LABEL`.

For three phases, list one sensor per phase, in the same order for input and
output.

### Should I use Shelly emulation or CT002/CT003?

A: CT002/CT003 (set `DEVICE_TYPE = ct002` or `ct003`). Each battery reports its
output back to the CT emulator, so AstraMeter steers it to its own target and
spreads the load evenly across batteries. With Shelly emulation each battery
reacts to the grid reading on its own, and several of them tend to fight each
other — one charges while another discharges. See
[CT002 / CT003 steering](ct002.md).

### What does custom_config replace in the Home Assistant App, and where does the file go?

A: A custom config file replaces every setting on the Configuration tab: power
sensors, device types, tuning, the Marstek account and the MQTT broker. Only
**Log Level** is still taken from the app, and the dashboard stays enabled.
Your file therefore needs:

- an `[MQTT_INSIGHTS]` section if you want the Home Assistant entities, for
  example `BROKER = core-mosquitto` with your Mosquitto username and password.
  Without a custom config the app sets this up automatically; with one it
  doesn't.
- a `[MARSTEK]` section with `ENABLE = True` if you use the Marstek account.
- for Home Assistant sensors, these connection settings. They use the app's
  own access to Home Assistant, so you don't need a token:

  ```ini
  [HOMEASSISTANT]
  IP = supervisor
  PORT = 80
  API_PATH_PREFIX = /core
  CURRENT_POWER_ENTITY = sensor.your_grid_power
  ```

Save the file in the app's folder `/addon_configs/<id>_b2500_meter/` and enter
only the file name, for example `config.ini`, in **Custom Config**. The `<id>`
depends on the repository URL you added. Switching to or from the `#develop`
repository creates a new folder, and you have to move the file. If the log
shows `Custom config file '…' not found`, the app ignores the setting and uses
the Configuration tab instead.

To create the file from your current settings, click **Migrate to a config
file** at the bottom of the Configuration tab on the dashboard. It writes the
running configuration to `astrameter.ini` and switches the app to that file.
See [Migrating between them](dashboard.md#migrating-between-them).

## Device and firmware specific

### What firmware do I need for my Marstek device?

A: Keep the battery firmware up to date. These minimum versions are known:

- **B2500:** 108 or later on HMJ devices, 224 or later on all others, for
  Shelly emulation. Up to v224 the B2500 uses port 1010, from v226 port 2220.
  `shellypro3em` listens on both.
- **Venus E (HMG-50, the generation before Venus E 3.0):** 120 or later for
  Shelly emulation, 152 or later for better regulation. Venus E 3.0 and other
  models have their own version numbers, so these minimums don't apply to
  them.
- **CT002/CT003:** if the Marstek app doesn't offer CT002 or CT003 as a meter
  for your battery, its firmware is too old.

Marstek often provides updates only on request. Ask through the feedback
function in the app or through Marstek support, and explain why you need the
update.

If the battery stops working with AstraMeter right after an update, check on
the [dashboard](dashboard.md) whether it still polls. For several users,
removing the meter in the app and selecting it again fixed it.

### How do I handle the different ports for Shelly Pro 3EM?

A: Use one of these device types:

- `shellypro3em_old`: Port 1010 (B2500 firmware ≤224 or Jupiter & Venus)
- `shellypro3em_new`: Port 2220 (B2500 firmware ≥226)
- `shellypro3em`: Both ports (most compatible)

### Do I need a Marstek account, and how does "AstraMeter CT002" get into the app?

A: You need the account only if you don't own a real Marstek CT. The Marstek
app only lets you select a CT that is registered to your account, so
AstraMeter can register a virtual CT there:

- Home Assistant App: enable **Auto-register Managed CT Device (Marstek)** and
  enter your Marstek email address and password.
- `config.ini`: add a `[MARSTEK]` section with `ENABLE = True`, `MAILBOX` and
  `PASSWORD`.
- ESPHome: add the `marstek_registration:` block (see
  [ESPHome](installation/esphome.md)).

Use the account your battery belongs to. At startup the log shows
`Managed fake CT registration completed`, or a message that the device already
exists. Then select the CT as described in
[My Marstek device can't find the emulated powermeter](#my-marstek-device-cant-find-the-emulated-powermeter-what-could-be-wrong).
The virtual CT doesn't appear in a Bluetooth search, and the CT list always
shows it as offline. After registration you only need the credentials for live
values in the Marstek app through hame-relay (see
[The Marstek app shows the meter offline](#the-marstek-app-shows-the-meter-offline-or-shows-values-that-dont-match-my-meter)).

Common problems:

- `Token fetch failed (code=4): password incorrect` (`密码错误`): the email
  address or password is wrong.
- `Add device failed … (code=8): 无访问权限` ("no access permission"): the
  login worked, but Marstek refused to add the device. In the reported cases a
  later attempt succeeded. Wait a few hours instead of restarting repeatedly.
- ESPHome registers only once and then logs `Marstek MAC loaded from prefs`.
  If the CT never appeared in the app, set `force_reregister: true`, flash the
  device, and remove the option again once the CT is listed.
- If the app reports the virtual CT as deleted but still lists it, log out,
  log back in and delete it again. Renaming it before deleting has also
  worked.

### Can I use AstraMeter with a real CT002 or CT003 I already own?

A: Yes, in two ways:

- Let AstraMeter replace it. Pair the battery with your real CT in the
  Marstek app, then unplug the real CT. Set AstraMeter to the same type
  (`ct002` or `ct003`) and leave `CT_MAC` empty. AstraMeter then answers in
  place of the real CT. You don't need a Marstek account for this.
- Use the real CT as AstraMeter's meter (2.4.0 or later, not on ESPHome). Read
  it with a [`[MARSTEK_METER]`](powermeters.md#marstek-ct002--ct003) section
  and pair every battery with AstraMeter's CT, usually the virtual CT from the
  previous question, instead of the real one.

Don't leave a battery paired with a real CT that is still running while
AstraMeter also answers. Both would steer the battery.

### Can I use this with non-Marstek storage systems (e.g., Zendure, Hoymiles)?

A: No, this project is Marstek-specific. For other brands, see
[uni-meter](https://github.com/sdeigm/uni-meter).

## Troubleshooting

### I get permission errors when binding to port 1010/2220.

A: Ports below 1024 need root privileges on Linux. Solutions:

- Use Docker or Home Assistant App (recommended)
- Use `setcap` to grant permissions
- Run as root (not recommended)

Note: the Docker image runs as a non-root user. So binding port 1010 (used by
`shellypro3em_old` and the combined `shellypro3em`, which starts both listeners)
still fails with `PermissionError: [Errno 13]` under `network_mode: host`. Port
2220 (`shellypro3em_new`) is unaffected. Either lower the host's
privileged-port range (`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=1010`,
persist via `/etc/sysctl.d/`) or run the container as root (`user: "0:0"` in
compose). Publishing the port via bridge networking does **not** work: the
Marstek discovery packets are UDP broadcasts to the subnet address, and Docker's
port mapping doesn't forward them.

### AstraMeter ignores part of my config.ini or won't start with it.

A: These are the most common causes:

- **Section names:** a powermeter section must start with the source name in
  capitals. `[SHELLY]`, `[MQTT]`, `[MQTT_GRID]` and `[HOMEASSISTANT_2]` work.
  `[POWERMETER_MQTT]` and `[shelly]` are ignored without an error. The section
  name sets the source type; there is no `TYPE = mqtt` key. See
  [Powermeter sources](powermeters.md).
- **Comments:** put comments on their own line, starting with `#` or `;`. A
  comment after a value becomes part of the value: `DEADBAND = 15 # watts` is
  read as `15 # watts`, and startup fails. Quotes also become part of the
  value, so don't use them.
- **Duplicate sections:** each section name can appear only once. A second
  `[SHELLY]` stops startup with `section 'SHELLY' already exists`. Number them
  instead: `[SHELLY_1]`, `[SHELLY_2]`.
- **Settings in the wrong section:** steering settings such as
  `ACTIVE_CONTROL`, `MIN_EFFICIENT_POWER`, `MIN_DC_OUTPUT` and
  `FAIR_DISTRIBUTION` are only read from `[CT002]`, or from `[CT003]` if it
  exists, never from `[GENERAL]`. Signal filters such as `THROTTLE_INTERVAL`,
  `DEADBAND`, `SMOOTH_TARGET_ALPHA` and `PID_*` go in `[GENERAL]` or in a
  powermeter section. `LOG_LEVEL` isn't a config file setting at all (see
  [How do I get a debug log?](#how-do-i-get-a-debug-log)).
- **DEVICE_TYPE and powermeter sections:** `DEVICE_TYPE` sets the device
  AstraMeter emulates. The powermeter section describes your real meter.
  `DEVICE_TYPE = ct002` with a `[SHELLY]` section means AstraMeter acts as a
  CT002 and reads your Shelly. The `IP` in a powermeter section is your
  meter's address. AstraMeter itself answers on the address of the machine it
  runs on.
- **Several powermeter sections:** without `NETMASK`, the first powermeter
  section in the file answers every battery and the others are never used.
  AstraMeter still tests every section at startup, so a broken leftover
  section stops startup. See
  [Multiple Powermeters](configuration.md#multiple-powermeters).
- **`No powermeter found for client <ip>`:** no section covers that battery.
  Either no section was recognised (wrong name, commented out, or missing from
  your custom config file), or the `NETMASK` excludes the battery's IP.
- **Home Assistant sensors:** use the entity ID from Developer Tools → States,
  for example `sensor.grid_power`, not the unique ID. Replace the app's
  placeholder `sensor.current_power_in`.
- **Memory:** on small boards such as a Raspberry Pi 2, AstraMeter can run out
  of memory. The log then ends with `Killed`.

### How can I test without a storage device?

A: Only partly — you can verify the initial configuration. Full testing needs a
Marstek device in "self-adaptation" mode to request data. For local end-to-end
testing without hardware, use the [simulator](simulator.md).

### My battery polls AstraMeter but doesn't behave as expected. Where is the problem?

A: The [dashboard](dashboard.md) shows whether the problem is the meter,
AstraMeter or the battery. Check it in this order:

1. On the **Power source** tab, a meter marked `Read failed`, `Stale` or
   `Offline` means AstraMeter can't read your meter. Fix that first. While the
   meter is unavailable, CT002/CT003 tells the batteries to hold their output,
   and Shelly emulation stops answering.
2. On the **Batteries** tab, check each battery's status:
   - `Disabled` or `Manual`: the battery is switched off or has a manual
     target. Switch it back to automatic.
   - `Parked`: `MIN_EFFICIENT_POWER` keeps the battery idle on purpose (see
     [My second battery never starts](#my-second-battery-never-starts-or-only-some-of-my-batteries-run)).
   - `Saturated`: the battery is at its limit.
   - Phase `Unassigned`: the battery is still detecting its phase (see
     [phase 0](#my-battery-shows-phase-0-unassigned-and-isnt-steered)).
   - `Not reporting`, or **Last seen** keeps increasing: the battery has
     stopped polling. AstraMeter can't make it poll again. Restart the battery
     and check its network connection.
3. Compare **Target** with the battery's reported power. If the target asks
   for more power, or stays the same, while the reported power stays below it
   or swings, the battery, inverter or wiring is the limit. To confirm, set
   the battery to full discharge manually in the Marstek app and check whether
   it reaches full power.
4. On the **Diagnostics** tab, control quality `stable` means the grid is held
   at zero. `limited` means every battery is full, empty or at its power limit,
   so no setting in AstraMeter will change the result.

If the dashboard looks fine, check the battery:

- It must be in Automatic (Self-Adaptation) mode. On a B2500, enable
  simultaneous charging and discharging.
- Its state of charge must be within its limits. DoD is how far the battery
  may discharge, so a DoD of 10 % stops it at 90 %.
- Restart it completely by holding the power button for about 10 seconds.
  After a firmware update, select the meter again in the app.

With Shelly emulation, the dashboard only shows whether each battery polls.
The `Sending response:` lines in the debug log show the values sent to each
battery.

### How do I get a debug log?

A: Set the log level to `debug`, restart AstraMeter, and record several
minutes that include the problem. A few seconds of log are rarely enough.

- **Home Assistant App:** set **Log Level** to `debug` on the Configuration
  tab. This also works with a custom config file. **Settings → System → Logs**
  downloads at most 10,000 lines, which is only a few minutes at debug level.
  For a longer log, run
  `ha host logs --identifier addon_<slug> --lines 200000 > /share/astrameter.log`
  in the Terminal & SSH app. `<slug>` is part of the URL of the app page, for
  example `2ec037da_b2500_meter`. Add `--follow` to keep recording.
- **Docker:** set the environment variable `LOG_LEVEL=debug` (the default is
  `info`) and read the log with `docker logs -f <container>`. `LOG_LEVEL` in
  `config.ini` has no effect.
- **Python:** run `astrameter --loglevel debug` or set `LOG_LEVEL=debug`. The
  default is `warning`.
- **ESPHome:** the log is under Logs in the ESPHome dashboard (`logger:`
  component). The firmware logs its steering decisions but not every request.
  Use its [dashboard](dashboard.md#esphome-on-an-esp32) to see the batteries.

What the debug log contains:

- `CT002 request from …` and `CT002 response to …` once per poll (Shelly:
  `Parsed request from …` and `Sending response: …`).
- `CT002 steer <battery>: …` with the reasons for each battery's command.
- `Health check request received from …` from the Home Assistant or Docker
  health check. You can ignore these lines.

AstraMeter masks passwords, tokens and the Marstek email address in the log.
Read through the log anyway before you post it.

### How often must my power meter send a new reading?

A: About once per second. The battery requests a reading about once per
second and adjusts its output by the value it receives. If the meter updates
only every 5 to 10 seconds, the battery reacts late, overshoots, and may fail
to detect its phase. A faster meter improves regulation more than any
AstraMeter setting.

To find out how often your meter updates, open the sensor's history in Home
Assistant and look at the time between values. With CT002/CT003, the
**Batteries** tab on the dashboard shows **Answered every** next to
**Polls every** when AstraMeter had to wait for a new reading. In that case
your meter updates less often than the battery asks.

To get faster readings, try these in order:

1. Increase the meter's update rate. Tasmota-based IR readers, including
   bitShake, send to MQTT and Home Assistant only every 10 seconds by default.
   Set the last field of each meter line in the Tasmota script to `16`, for
   example `…,Power_curr,0` to `…,Power_curr,16`, to send every reading.
   Home Assistant integrations that poll a device, some every 30 seconds by
   default, usually have an update interval you can lower. DSMR 4 P1 meters
   send every 10 seconds; DSMR 5 meters send every second.
2. Read the meter directly instead of through Home Assistant. AstraMeter
   supports Shelly, Tasmota, HomeWizard, ESPHome, SMA, SML, Modbus and other
   sources, see [Powermeter configuration](powermeters.md). In the Home
   Assistant App this needs a custom config file (see
   [What does custom_config replace?](#what-does-custom_config-replace-in-the-home-assistant-app-and-where-does-the-file-go)).
3. If the meter can't update faster, set `THROTTLE_INTERVAL` to its update
   interval in seconds, so the battery gets one answer per new reading. If the
   interval is longer than 2 seconds, also set `WAIT_FOR_NEXT_MESSAGE = false`.
   The battery then reacts more slowly but stops overshooting. For more tuning
   options, see
   [My output power oscillates](#my-output-power-oscillates-or-yo-yos-between-zero-and-full).

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
   to 2 seconds, for a *new* reading before it answers. If your meter updates
   less often than every 2 seconds, set it to `false` instead (see
   [How often must my power meter send a new reading?](#how-often-must-my-power-meter-send-a-new-reading)).
   If that wait times out
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

### My batteries react slowly or ramp up in small steps.

A: In CT002/CT003 mode, check these one at a time:

1. **Meter speed.** A battery can only correct its output when a new reading
   arrives (see
   [How often must my power meter send a new reading?](#how-often-must-my-power-meter-send-a-new-reading)).
   Leave `DEDUPE_TIME_WINDOW` at `0`. It leaves some requests unanswered, so
   the batteries get fewer corrections.
2. **Copied settings.** Remove tuning settings you took from other
   configurations and go back to the defaults. Don't set `GRID_PREDICT_TRUST`
   or `OSC_DAMP_MAX` to `0`; they compensate for meter delay. With a slow
   meter, `SMOOTH_TARGET_ALPHA` and `DEADBAND` add more delay.
3. **Ramp limits.** `PACE_BASE_STEP` and `PACE_MAX_STEP` in `[CT002]` or
   `[CT003]` (defaults 30 W and 100 W) limit how much each battery's command
   can change per poll. At 100 W, a 1 kW load change takes at least ten polls.
   If the grid power stays away from zero for a while after a load change,
   raise `PACE_MAX_STEP` gradually, for example to 250 and then 500.
   `PACE_BASE_STEP = 0` turns the limit off, which tells you whether it is the
   cause. Lower the values again if the batteries start to overshoot.
4. **`MIN_EFFICIENT_POWER`.** When it is set, a load increase first goes to the
   batteries that are already running. Idle batteries start only after the
   higher demand has lasted for a while. Lower the value if that takes too
   long (see
   [My second battery never starts](#my-second-battery-never-starts-or-only-some-of-my-batteries-run)).

[Control quality](ct002.md#control-quality) explains how to tell slow
regulation from oscillation.

### My second battery never starts, or only some of my batteries run.

A: `MIN_EFFICIENT_POWER` controls this (CT002/CT003, default `0` = off). It is
a minimum power per battery. When splitting the demand across all batteries
would give each one less than this value, AstraMeter runs fewer batteries,
keeps the others idle, and changes which ones run every 15 minutes. To avoid
switching batteries on and off repeatedly, another battery only starts once
demand is about 20 % above the threshold. With two batteries, the second one
starts at about 2.4 × the value. All N batteries run from about 1.2 × N × the
value.

Choose a value within these limits:

- With two or more B2500, use at least `80`, or at least `40` if you selected
  a Marstek inverter for every B2500 in the Marstek app (see
  [My B2500 stays at 0 W](#my-b2500-stays-at-0-w-or-doesnt-cover-a-small-base-load)).
  With `0`, a small load is split into shares below the B2500's minimum, none
  of the batteries start, and you import the whole load.
- Use at most one battery's maximum power divided by 2.4, for example 330 for
  an 800 W battery. This applies to charging too. With a higher value, the
  running battery reaches its limit before the next one starts, and the rest
  is imported or exported.

With only Venus batteries, `0` works. Set a value only if you want small loads
handled by fewer batteries. See
[Battery efficiency optimization](ct002.md#battery-efficiency-optimization).

### My B2500 stays at 0 W or doesn't cover a small base load.

A: This is a limit of the B2500. Each of its two outputs has a minimum power,
and the battery won't deliver less. The minimum depends on the micro-inverter
you selected for the battery in the Marstek app:

- With a non-Marstek inverter selected, each output needs at least 40 W, so a
  B2500 with both outputs connected needs 80 W.
- With a Marstek inverter selected, for example MST-MI0800W, each output needs
  at least 20 W, so the battery needs 40 W. The B2500 only checks which model
  is selected, so this also works when a different inverter is connected.
  Users report that some inverters, such as Hoymiles HMS, don't run reliably
  at 20 W per input.

[B2500 minimum output](b2500-minimum-output.md) lists the minimum for each
inverter model you can select in the app.

Below its minimum the battery stays at a few watts, and the house draws the
power from the grid. These steps help:

- If your inverter runs at 20 W per input, select a Marstek inverter in the
  app.
- With several B2500, set `MIN_EFFICIENT_POWER` to at least the battery's
  minimum, `80` or `40`. A small load then goes to one battery instead of being
  split into shares too small for any battery to start (see
  [My second battery never starts](#my-second-battery-never-starts-or-only-some-of-my-batteries-run)).
- Use AstraMeter 2.3.0 or later. Older versions could leave a B2500 at 0 W
  permanently after sending it a command that was too small.

Grouping B2500 batteries in the Marstek app (parallel mode) doesn't lower the
minimum. It is meant for setups where each B2500 feeds a shared inverter
through only one output. It switches the second output off and halves the
battery's maximum power, for example to 400 W. Don't group batteries that have
both outputs connected.

### My B2500's inverter switches off during solar surplus. What does MIN_DC_OUTPUT do?

A: `MIN_DC_OUTPUT` (in `[CT002]` or `[CT003]`, default `0` = off) makes every
B2500 discharge at least this much power, so its inverter never drops to 0 W
and switches off. Venus and Jupiter batteries have their own inverter and
ignore this setting. The power you set is exported during surplus.

- Set it to at least the battery's minimum power: 80 W, or 40 W if a Marstek
  inverter is selected in the Marstek app (see
  [My B2500 stays at 0 W](#my-b2500-stays-at-0-w-or-doesnt-cover-a-small-base-load)).
  When a battery's share is lower, AstraMeter sends exactly this value. A value
  below the battery's minimum keeps it near 0 W.
- To set a different value for one battery, use its **Min DC Output** entity
  in Home Assistant (see
  [per-battery controls](mqtt-insights.md#per-battery-controls-home-assistant-entities)).
  This value replaces the global one, and `0` turns the minimum off for that
  battery. The entity shows `0` until you set it, even while the global value
  applies.
- The minimum doesn't apply to paused batteries or to batteries with a manual
  target.
- Leave it at `0` unless your inverter switches off.

### I have batteries of different capacities and the smaller one drains much faster.

A: By default AstraMeter splits the load equally, so a 5 kWh battery gets the
same share as a 15 kWh one and saturates first. Three settings work together to
fix this:

- **`MIN_EFFICIENT_POWER`** is a *per-battery* threshold, not a total. Set it to
  900 W, say, and AstraMeter won't activate a second battery until demand
  reaches about 2,160 W: enough for each battery to handle 900 W, plus a 20 %
  margin that stops batteries from switching on and off. For asymmetric packs,
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

### My battery shows phase 0 (Unassigned) and isn't steered.

A: The battery hasn't detected which phase it is connected to yet. It does
this itself: it changes its output by up to about 2000 W and checks which
phase of the meter reading changes. Until it succeeds, AstraMeter sends it the
raw meter reading and leaves it out when splitting the load. Detection runs
when the battery has no stored phase, for example after a reset or a firmware
update, and when you change the CT in the Marstek app. A short jump to full
power while the phase is `0` is this test, not AstraMeter.

If the phase stays at `0`:

- Make sure the meter shows the test. The battery must see its own change in
  the meter reading within a few seconds. A meter that updates only every 5 to
  10 seconds, a sensor stuck at a constant value, strong smoothing, or a large
  `THROTTLE_INTERVAL` or `DEDUPE_TIME_WINDOW` can hide it.
- Update to AstraMeter 2.4.0 or later. It fixed detection failing while
  AstraMeter steers other batteries.
- Repeat the detection while the load is steady, without large appliances
  switching or clouds passing. In the Marstek app, tap the phase mode button
  (3-phase / 1-phase) and then **Auto diagnostics**, or select the CT again.
  Restarting the battery also starts detection. A Venus stops trying after
  three failed attempts.

### All my batteries show phase A, or the wrong phase. Is that a problem?

A: In most setups it isn't. With a single total-power sensor, AstraMeter
reports the whole reading on phase A, so batteries can only detect phase A.
Some batteries show `D` (all phases) instead, which is also fine. With active
control (the default), AstraMeter steers the sum of all phases to zero and
calculates a separate target for each battery, so the detected phase doesn't
matter. It only matters with `ACTIVE_CONTROL = false`, where each battery
reacts to the reading of its own phase.

For correct phase detection, use three per-phase sensors (see
[three-phase measurement](#how-do-i-set-up-three-phase-measurement-in-the-home-assistant-app))
and run **Auto diagnostics** in the Marstek app. AstraMeter can't assign a
phase to a battery.

### The Marstek app shows the meter offline or shows values that don't match my meter.

A: With CT002/CT003 this is expected:

- The CT list in the app always shows the AstraMeter CT as offline.
- With active control (the default), AstraMeter sends each battery the
  correction it should make, often only a few tens of watts. The app shows this
  correction, not your meter reading, so two batteries show different values
  and neither matches your meter. Check the result on your electricity meter or
  on the AstraMeter [dashboard](dashboard.md).
- A battery that is still detecting its phase receives the raw meter reading,
  so only that battery shows real values. In relay mode
  (`ACTIVE_CONTROL = False`) all batteries receive the raw reading.

To see live grid power in the Marstek app, register the CT with the
`[MARSTEK]` section and connect `[MQTT_INSIGHTS]` to the broker that
[hame-relay](https://github.com/tomquist/hame-relay) (1.3.5 or later) uses.
AstraMeter then answers the app's requests. See
[MQTT Insights](mqtt-insights.md#optional-marstek-mobile-app-live-mqtt).

## Advanced

### Can AstraMeter show my battery's SoC or change its work mode or charge limits?

A: No. A battery only reports its power and phase to the CT, and the CT only
sends back a power value. The protocol carries no state of charge, temperature
or settings. This means:

- AstraMeter can't show the SoC. An SoC field linked to the AstraMeter CT
  stays at 0 %.
- AstraMeter can't change the work mode. The battery must be in Automatic
  (Self-Adaptation) mode to follow AstraMeter. In manual or scheduled mode it
  ignores the CT.
- AstraMeter can't set charge or discharge limits or a backup SoC. Set these
  in the Marstek app or in the battery's own integration. When a battery can't
  deliver its share because it is full, empty or at a limit, AstraMeter gives
  that share to the other batteries.

With [MQTT Insights](mqtt-insights.md), Home Assistant receives each battery's
reported power, phase and target. For SoC and battery settings, use an
integration for the battery itself, such as
[hm2mqtt](https://github.com/tomquist/hm2mqtt) with
[hame-relay](https://github.com/tomquist/hame-relay), the Marstek local API or
Modbus. You can use the SoC from there to set the
[Distribution Weight](#how-can-i-distribute-load-based-on-each-batterys-state-of-charge-soc).

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

### Can I pause a battery or run it only at certain times?

A: Yes, with CT002/CT003. Each battery has an **Active** switch in Home
Assistant (through
[MQTT Insights](mqtt-insights.md#per-battery-controls-home-assistant-entities))
and on the dashboard. When you switch it off, AstraMeter steers that battery
to 0 W and the other batteries cover the load. For a schedule, switch it from
a Home Assistant automation with a time trigger. Without Home Assistant,
publish `false` or `true` to `{base}/ct002/{did}/consumer/{cid}/active/set`
(see [Command topics](mqtt-insights.md#command-topics-set-values-from-any-mqtt-client)).

To run a battery at a fixed power, switch **Auto Target** off and set
**Manual Target** in watts. Positive values discharge, negative values charge.
Manual Target has no effect while Auto Target is on. Shelly emulation has no
per-battery controls.

With MQTT Insights enabled, these settings are stored as retained messages on
your MQTT broker. AstraMeter applies them again after a restart and when a
battery reconnects. Without MQTT, changes made on the dashboard are lost when
AstraMeter restarts. If you send commands from your own MQTT client, set the
retain flag.
