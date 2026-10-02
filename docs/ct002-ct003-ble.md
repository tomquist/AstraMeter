# CT002 / CT003 Bluetooth (BLE) Protocol

This page covers the Bluetooth side of the Marstek CT002 (`HME-4`) and CT003
(`HME-3`): how the Marstek app finds and onboards a meter, and the command set
it speaks to the meter over BLE. AstraMeter's ESPHome build implements it, so
the app can add the board through its regular "Add device" flow instead of the
cloud auto-registration in [ct002.md](ct002.md#marstek-cloud-auto-registration);
see [AstraMeter's ESPHome implementation](#astrameters-esphome-implementation)
for what it answers. The Python/Docker build has no Bluetooth.

The meter's measurement protocol towards the batteries (UDP) is in
[ct002-ct003-protocol.md](ct002-ct003-protocol.md). The cloud MQTT/HTTP side is
in [marstek-mqtt-http.md](marstek-mqtt-http.md).

**Sources.** This is community reverse-engineering for interoperability, checked
from both ends of the link:

- the Marstek Android app, version **1.6.76**, which shows what the phone sends
  and how it reads the answers;
- the meters' own MCU firmware, **HME-4 v120/v124** and **HME-3 v122**, from
  [rweijnen/marstek-firmware-archive](https://github.com/rweijnen/marstek-firmware-archive),
  which shows what the device accepts and how it builds its replies.

Nothing here has been tested against a live app session with an emulated meter
yet. Where only one source confirms a detail, the text says which. "Unconfirmed"
means neither source pins it down.

## At a glance

| | CT002 | CT003 |
|---|---|---|
| Device type string | `HME-4` | `HME-3` |
| Advertised name | `MST-TPM_<suffix>` | `MST-SMR_<suffix>` |
| GATT service | `FF00` | `FF00` |
| Phone → meter | write to `FF01` | write to `FF01` |
| Meter → phone | notify on `FF02` | notify on `FF02` |
| Frame | `73 LEN 23 CMD … XOR` | same |
| Status reply (`0x03`) | 24-byte frame | 42-byte frame |
| Firmware version in replies | 124 (v124) | 122 (v122) |

## Transport

The meter's MCU drives a Quectel FC41D Wi-Fi/BLE module, which acts as a BLE
GATT server.

- **Advertising.** The meter advertises connectably with a fixed interval of
  about 94 ms (150 × 0.625 ms). Only the name matters to the app; there is no
  service-UUID or manufacturer-data filter (see [Scan](#1-scan)).
- **Name.** The name is `<prefix>_<suffix>`, with prefix `MST-TPM` for CT002 and
  `MST-SMR` for CT003. The firmware takes the suffix from the last four
  characters of its 12-character device ID. The app's own sample data pairs
  `MST-TPM_fbd9` with MAC `00:9C:17:C1:FB:D9`, so in practice the suffix is the
  last four hex digits of the MAC, in lower case (medium confidence).
- **GATT.** The meter has one primary service, `FF00`, with three
  characteristics:
  - `FF01` receives every command from the phone. The app writes with response
    when the characteristic offers the `write` property, and without response
    otherwise.
  - `FF02` carries every reply as a notification. The app enables
    notifications on it shortly after connecting.
  - `FF06` is declared, but the command protocol never uses it. The app only
    touches it in its OTA routines for other device families.

  The app picks the service and characteristics by checking whether the
  lower-cased UUID contains `ff00` / `ff01` / `ff02`, so both 16-bit and
  128-bit (`0000ff01-0000-1000-8000-00805f9b34fb`) forms work.
- **MTU.** On Android the app requests an MTU of 500. The meter never splits a
  reply across notifications, so an emulator should accept the MTU request.
  The fixed-size replies are at most 42 bytes (the CT003 status frame); only
  the linked-battery list grows, with the number of batteries.

## Frame format

Requests and replies use the same frame:

```text
+------+-----+------+-----+-----------------+-----+
| 0x73 | LEN | 0x23 | CMD | payload (LEN-5) | XOR |
+------+-----+------+-----+-----------------+-----+
```

- `LEN` is the length of the **whole frame**, including the `0x73` and the
  checksum byte. A frame without payload has `LEN = 5`.
- `XOR` is the XOR of every preceding byte.
- Example: the status request is `73 06 23 03 01 54`
  (`0x73 ^ 0x06 ^ 0x23 ^ 0x03 ^ 0x01 = 0x54`).

The two ends check frames differently:

- **The meter is strict.** It drops a frame silently when byte 0 is not `0x73`,
  `LEN` exceeds 128, byte 2 is not `0x23`, or the XOR does not match. It also
  gives up on a partial frame after about one second without new bytes.
- **The app is lenient.** It checks only the leading `0x73` and a minimum
  length, then dispatches on byte 3. An emulator should still send correct
  frames.

The meter replies with the request's command byte. Commands that reboot the
meter (`0x06`, `0x09`) send no reply.

Because the meter drops frames over 128 bytes, a `0x05` Wi-Fi payload (SSID,
separator and password) must fit in 123 bytes. The meter keeps up to 32 bytes of
SSID and 64 bytes of password.

## The app's "Add device" flow

This is the regular onboarding flow, from the **+** button on the device list.
The step numbers are the order in which the app runs them.

### 1. Scan

The app scans for 10 seconds with no filter. It keeps a device whose name
contains, case-insensitively, any entry of a prefix list:

- The default list is `HM_`, `KS_`, `ZX`, `MST`, `MARS`, `EZA`, `JPLS`.
- The app tries to download a newer list from the Marstek cloud (the
  `marstek_ble_prefix_name` entry of its config endpoint) and uses the default
  only if that fails.
- Names containing `MST-CHAR` or `MST_CHAR` are dropped.
- A device whose MAC is already on the account is marked as added.

Both meter names contain `MST`, so they match. Nothing else in the
advertisement is checked.

### 2. Connect

The app connects, discovers `FF00`, enables notifications on `FF02` and, on
Android, requests MTU 500.

### 3. Identify (`0x04`)

The app sends `0x04` with payload `01` and retries up to 20 times until it gets
a usable answer. The meter replies with ASCII text (format in
[`0x04`](#0x04--identity)):

```text
type=HME-4,id=<id>,mac=<mac>,dev_ver=124,fc_ver=<module version>
```

The app accepts the reply only if all of the following hold:

- the whole frame is longer than 20 bytes;
- the text has at least three comma-separated fields;
- `type` is not empty;
- `id` is longer than 5 characters.

If any check fails, the app reports "Failed to retrieve the device ID". The
values are used as follows:

- `type` picks the setup path and, later, the parser for `0x03` replies (see
  [`0x03`](#0x03--status)). It must be exactly `HME-4` or `HME-3`.
- `id` and `mac` become the cloud record's `devid` and `mac`. The app strips
  colons from the MAC and lower-cases it.
- `dev_ver` is the firmware version the app shows and the one it checks for
  updates.

### 4. Status request (`0x03`), not awaited

Right after identifying the meter, the app sends `0x03`. It does not wait for
the answer: it pauses a fixed 500 ms and moves on. A status reply that arrives
is still parsed, and the Wi-Fi flag in it matters later (see
[step 6](#6-wi-fi-0x08-0x05-0x08)).

### 5. Setup steps

The app has dedicated setup screens for batteries and some other devices.
`HME-4` and `HME-3` match none of them, so they fall through to the generic
setup sequence. Its steps are, in order:

1. **Name the device and register it.** The user enters a name. The app then
   adds the device to the account with an HTTP **GET** to
   `/app/Solar/v2_add_device.php`. The query carries `name`, `mailbox`,
   `devid`, `mac`, `type`, `token`, `access=1`, `version`, `bluetooth_name`,
   `sn`, `position` and `timeZone`, plus empty battery-only fields. This is the
   same call AstraMeter's auto-registration makes; the server accepts made-up
   IDs for these types.
   - The serial-number check (`/ems/api/v1/checkDeviceSn`) runs only if the
     user typed a serial number.
2. **Wi-Fi.** See [step 6](#6-wi-fi-0x08-0x05-0x08).
3. **Firmware check.** The app asks the cloud for updates for the device's
   `type` and `dev_ver`.
   - If the reported version is current, the step just says so.
   - If an update exists, the screen offers "Update Now". A "Skip" button is
     shown only under some conditions.
   - So an emulator should report a `dev_ver` at least as high as the newest
     published firmware: 124 for HME-4 and 122 for HME-3 at the time of
     writing.

   Under some conditions the app shows an installation guide before these
   steps.

### 6. Wi-Fi (`0x08`, `0x05`, `0x08`)

1. The app first reads the meter's current SSID with `0x08`. If the meter
   already reports Wi-Fi as connected and the phone knows the password for that
   SSID, the app shows "already connected" and skips the rest.
   - "Connected" here is the Wi-Fi byte of the last `0x03` reply being `1`.
2. Otherwise the user picks a network and enters a password. Only 2.4 GHz is
   supported. The app sends `0x05` with the UTF-8 payload
   `<ssid><.,.><password>`. It never waits for the meter's acknowledgement.
3. About a second later, the app polls `0x08` up to ten times. It counts a
   match when the **first byte plus the last byte** of the returned SSID equals
   the same sum over the SSID it sent. It does not compare the strings. On a
   mismatch it re-sends `0x05`.
4. On a match the app reports "Wi-Fi setup successful" and continues.

After that, the app connects to Marstek's cloud MQTT broker and publishes `cd=01`
to `hame_energy/<type>/App/<id>/ctrl`. The result is only logged, so a meter
that never comes online in the cloud does not fail the setup.

### Adding a CT from battery settings

The battery's CT settings have their own "Add CT" flow, which also uses BLE:

1. **Scan and pick.** The app scans and the user picks a meter.
2. **Identify.** The app sends `0x04`, exactly as in step 3, and branches on the
   lower-cased BLE name containing `smr`, `tpm`, `sensor`, `mst-p1`, `mst-ir`
   or `mst-tic`.
3. **Register.** The user names the meter, and the app registers it with the
   same `v2_add_device.php` call.
4. **Poll status.** The app polls `0x03` until a reply carries the Wi-Fi status
   byte.
   - If the byte is `1`, the meter is done.
   - Otherwise the app **silently** sends `0x05` with the Wi-Fi credentials it
     stored when the battery was set up. No Wi-Fi screen is shown.
   - If no status arrives at all, the app just finishes.

### What an emulator needs for onboarding

For the add flow to succeed, a BLE-provisioned AstraMeter would have to:

- advertise as `MST-TPM_<last 4 of MAC>` for CT002 or `MST-SMR_…` for CT003;
- expose `FF00`/`FF01`/`FF02`, and declare `FF06` for fidelity;
- answer `0x04` with
  `type=HME-4,id=<12 hex>,mac=<12 hex>,dev_ver=<≥ newest>,fc_ver=<string>`;
  - use the meter's CT MAC (`CT_MAC` / `ct_mac`) for both `id` and `mac`, so the
    cloud record, the UDP replies and the Marstek MQTT topics all agree;
    AstraMeter's auto-registration already uses `devid == mac`;
- answer `0x03` with a well-formed status frame whose Wi-Fi byte is `1`;
- answer `0x08` with the SSID it is actually connected to;
- accept `0x05`, either ignoring the credentials or applying them, and echo the
  new SSID on the next `0x08`.

The app's own Wi-Fi details for the meter are only used to provision it, so an
emulator that keeps its own network still passes. AstraMeter's ESPHome build
applies them; see [Wi-Fi from the app](#wi-fi-from-the-app).

## Command reference

Command bytes as handled by the meter firmware. The **Sent by** column says
which app screens send the command, where known.

| CMD | CT002 | CT003 | Request payload | Reply | Sent by |
|---|---|---|---|---|---|
| `0x03` | ✓ | ✓ | `01` | binary status, see [`0x03`](#0x03--status) | onboarding, CT screens |
| `0x04` | ✓ | ✓ | `01` | ASCII identity, see [`0x04`](#0x04--identity) | onboarding |
| `0x05` | ✓ | ✓ | `ssid<.,.>password` (UTF-8) | 1-byte acknowledgement | Wi-Fi setup |
| `0x06` | ✓ | ✓ | `01` | none (clears config, reboots) | probably "factory reset", see below |
| `0x08` | ✓ | ✓ | `01` | SSID as ASCII | Wi-Fi setup |
| `0x09` | ✓ | ✓ | `01` | none (reboots) | probably "hardware reset", see below |
| `0x10` | ✓ | – | `01` | – | not sent by the CT screens |
| `0x11` | – | ✓ | 4 bytes | 1-byte result | CT003 meter configuration |
| `0x12` | ✓ | ✓ | `00` | ASCII list of linked batteries, see [`0x12`](#0x12--linked-batteries) | CT screens |
| `0x13` | ✓ | – | – | – | probably voltage diagnostics (unconfirmed) |
| `0x14` | ✓ | – | `01` | frequency check | CT002 diagnostics |
| `0x15` | ✓ | – | `01` | 3 × int16 + 1 byte | CT002 diagnostics (voltage phase angle) |
| `0x16` | ✓ | – | `01` | 3 × int16 | CT002 diagnostics (active power) |
| `0x17` | ✓ | – | direction bits | 1-byte status | CT002 current-direction reverse |
| `0x18` | – | ✓ | – | – | unknown |
| `0x27`–`0x29` | ✓ | ✓ | – | – | unknown |
| `0x50`, `0x51` | ✓ | ✓ | sub-command byte + data | – | probably firmware-update / module control |

Any other command byte, for example `0x07`, `0x0a`–`0x0f` or (on CT002)
`0x11`, is ignored without a reply.

### `0x03` — status

The app polls this for the meter's live view and uses its Wi-Fi byte during
onboarding.

- Which parser the app runs depends on the `type` the meter reported in `0x04`:
  - the "SMR" layout for `HME-3`, `HME-5` and `SMR-` types;
  - the "TPM" layout for `HME-2`, `HME-4`, `TPM-CN` and any remaining `HME-`
    type except `HME-1`, which has its own parser.
- Multi-byte values are little-endian. The meter stores its readings as floats
  and truncates them to integers when it fills this frame, so all values are
  whole numbers with no scaling.

**CT002 (`HME-4`).** Frame `73 18 23 03 <19 bytes> XOR`, 24 bytes in total.
Offsets are frame offsets.

| Bytes | Type | Content | Confirmed by |
|---|---|---|---|
| 4 | u8 | firmware version (`0x7c` = 124) | both |
| 5–6, 7–8, 9–10 | u16 ×3 | three per-phase readings, probably voltages (V); the app ignores them | firmware only |
| 11–12 | s16 | phase A power (W) | both |
| 13–14 | s16 | phase B power (W) | both |
| 15–16 | s16 | phase C power (W) | both |
| 17–18 | s16 | total power (W) | both |
| 19 | u8 | Wi-Fi state: `1` = connected, else `0` | both |
| 20 | u8 | a second connection state, meaning unconfirmed | both |
| 21 | s8 | Wi-Fi RSSI (dBm); `0` when not connected | both (meaning from the app) |
| 22 | u8 | bits 0–2: phase A/B/C current direction reversed | app |
| 23 | u8 | XOR | |

- The app needs at least 23 bytes and fails partway through a shorter frame.
- Power values are signed; which direction is positive is unconfirmed.

**CT003 (`HME-3`).** Frame `73 2a 23 03 <37 bytes> XOR`, 42 bytes in total. The
firmware fills it in one of two ways depending on how the meter is configured;
the offsets below are the ones the app reads.

| Bytes | Type | Content |
|---|---|---|
| 4 | u8 | firmware version (`0x7a` = 122) |
| 5–8 | s32 | total power (W) |
| 9–16 | 64-bit | cumulative energy; the app treats it as signed only for some meter variants |
| 17 | u8 | Wi-Fi state: `1` = connected |
| 18 | u8 | a second connection state, as on CT002 |
| 19 | s8 | Wi-Fi RSSI (dBm) |
| 23 | u8 | number of meters |
| 24 | u8 | error code |
| 28–31, 32–35, 36–39 | s32 ×3 | phase A/B/C power (W) |
| 40 | u8 | flag bits |
| 41 | u8 | XOR. For the `SMR-1`/`SMR-2` variants the app also reads this offset as data, so those may send a longer frame (unconfirmed) |

- The app reads anything after byte 19 only if the frame is long enough, so a
  20-byte frame already gives it power, energy and the Wi-Fi state.
- The firmware fills the 32-bit power fields from differences of its import and
  export accumulators, so they are signed net values. Which electrical quantity
  sits in each energy field is unconfirmed.

### `0x04` — identity

The reply is the ASCII string

```text
type=<type>,id=<id>,mac=<mac>,dev_ver=<int>,fc_ver=<module firmware>
```

- `type` is `HME-4` or `HME-3`.
- `dev_ver` is the same number as byte 4 of the status frame.
- `fc_ver` is the FC41D module's own version string.

The app splits the text on `,` and finds each key by substring search. Order
doesn't matter, and the app also knows a number of battery-only keys that a
meter never sends.

### `0x05` / `0x08` — Wi-Fi

`0x05` carries the SSID and password separated by the five-character literal
`<.,.>`. The meter stores both and connects; without the separator, the whole
payload is taken as the SSID of an open network. The meter acknowledges with a
one-byte payload, which the app ignores.

`0x08` returns the stored SSID as plain ASCII in the payload.

### `0x06` / `0x09` — reset and reboot

`0x06` clears the meter's stored configuration and reboots it. `0x09` only
reboots it. Neither sends a reply.

The app has separate "factory reset" and "hardware reset" commands for CT002,
but their bytes could not be read from the app. `0x06` and `0x09` are
the two firmware commands that fit.

### `0x11` — CT003 meter configuration

The request carries four single-byte parameters. The meter answers with a
one-byte result in byte 4, which the app reports as success or failure. The
MQTT equivalent is `cd=5,p1=…,p2=…,p3=…,p4=…`. The meaning of the four values
is unconfirmed.

### `0x12` — linked batteries

The app sends `73 06 23 12 00 44`. Over MQTT the same query is `cd=4,p1=<n>`.
The reply is ASCII: one `;`-terminated entry per battery that is polling the
meter, with comma-separated `key=value` fields:

```text
type=<battery type>,sid=<battery id>,ip=<battery IP>,phpos=<phase letter>;
```

### `0x14`–`0x17` — CT002 diagnostics

These back the CT002 diagnostics screens.

- `0x14` reads the grid frequency and checks it against 45.00–55.00 Hz
  (apparently held in 0.01 Hz).
- `0x15` returns three signed 16-bit per-phase values plus one byte. The app
  labels this check "voltage phase angle".
- `0x16` returns three signed 16-bit per-phase active powers.
- `0x17` sets which phases have their current direction reversed. The payload is
  one byte with bit 0 = A, bit 1 = B and bit 2 = C. The meter answers with a
  one-byte status. The MQTT equivalent is `cd=5,dir=<bits>`.

The exact field layouts of the `0x14` and `0x15` replies are unconfirmed.

### Not covered

- **`0x10`, `0x13`, `0x18`, `0x27`–`0x29`, `0x50`, `0x51`.** The meter handles
  these, but their payloads were not decoded. `0x50` and `0x51` take a
  sub-command byte and look like firmware-update and module-control functions.
- **Other frame families.** The app also has frames that start with `0xAA`,
  `0x21` or `0xA8`. These are bootloader and OTA transfers for other Marstek
  devices; the meters don't use them.
- **Firmware updates.** The meter downloads a new image over Wi-Fi/HTTP. The
  app can start that from BLE or MQTT, but the command bytes for the trigger
  were not decoded.

## AstraMeter's ESPHome implementation

The `ct002:` component speaks this protocol on every ESP32 with a Bluetooth
radio of its own (all but the ESP32-S2 and the ESP32-P4). It is **on by default**; `bluetooth: false` under
`ct002:` leaves the BLE stack out of the firmware.

The board always advertises while Bluetooth is on. The app reconnects to a CT
it already knows the same way it finds a new one: it scans, keeps the devices
whose name matches, and then looks for the stored MAC among them. A board that
stopped advertising, or advertised without its name, would be out of the app's
reach too.

### Adding the board with the Marstek app

1. Flash the board and let it join your Wi-Fi.
2. In the Marstek app, tap **+** and wait for the scan. The board shows up as
   `MST-TPM_xxxx` (`ct_type: HME-4`) or `MST-SMR_xxxx` (`ct_type: HME-3`).
3. Pick it, give it a name, and go through the Wi-Fi step. If the board is
   already on the network you pick, nothing changes. Otherwise it switches to
   that network, as a real meter does, unless `apply_wifi: false` is set; see
   [Wi-Fi from the app](#wi-fi-from-the-app).
4. The firmware check reports the latest version; finish the setup.
5. In the battery's settings, switch to automatic mode and select the new CT,
   as with a real meter.

The app then shows the CT's live grid power over Bluetooth while the phone is
connected to it. The device stays "offline" in the app's cloud view, because
the board doesn't connect to Marstek's cloud.

### Identity

A real meter's ID, its MAC and its Bluetooth address are one and the same. The
app stores the MAC it was given at onboarding and later reconnects only to a
device at that Bluetooth address, dropping any other. The board keeps the
three equal:

- With `ct_mac` set, the radio takes `ct_mac` as its Bluetooth address
  before Bluetooth starts. It has to be a unicast MAC (lowest bit of the first
  byte clear), which AstraMeter's own `02b250…` MACs are; otherwise the board
  warns at boot that the app won't be able to reconnect.
- Without `ct_mac`, the ID is the board's own Bluetooth address (its Wi-Fi
  MAC + 2 on most ESP32s). The UDP side then answers polls for any CT MAC, so
  the battery works with that ID without further changes. To pin it, set
  `ct_mac` to the "Device ID" the board logs.

The advertised name ends in the last four characters of the ID. It is carried
in the advertisement itself, not only in the scan response, and it replaces
any `esp32_ble: name:` in the YAML. With ESPHome's `name_add_mac_suffix` on,
ESPHome appends its own suffix to the name; the app still finds the board,
because it only looks for `MST`.

If `marstek_registration:` applies a MAC after boot, the replies use it
straight away and the board re-advertises under the matching name, but the
radio's address can't change while Bluetooth runs. The board then logs a
warning; set `ct_mac` to that MAC in the YAML so the address follows on the
next boot. That component has already put the device in your account, so
there is no need to add it again from the app.

### Wi-Fi from the app

The board tries the network the app sends the way ESPHome's Improv
provisioning does:

- Connected within 30 seconds: the board saves the network, and it takes
  precedence over the networks in the YAML from then on. Flashing a changed
  configuration drops it again, which is how ESPHome treats any network saved
  at runtime.
- Not connected within 30 seconds: nothing is saved, and the board restarts on
  the Wi-Fi it had before.
- Already on that network: the board keeps its credentials, so a mistyped
  password in the app can't take it offline.

A board without Wi-Fi (Ethernet, or Wi-Fi disabled) logs the request and
ignores it. To keep the board on the networks in its YAML whatever the app
sends, set:

```yaml
ct002:
  bluetooth:
    apply_wifi: false
```

The app's Wi-Fi step still completes, because the board echoes the network
name back as the app expects.

### What the board answers

| Command | Answer |
|---|---|
| `0x03` status | Live per-phase and total grid power: the raw readings of the configured sensors, as in the Marstek MQTT reply and cloud reporting. The connection byte is set while the board is online over Wi-Fi or Ethernet; RSSI is the Wi-Fi signal (`0` on Ethernet). The CT002 voltage fields and the CT003 energy counter are `0`, as AstraMeter has neither. |
| `0x04` identity | `type=<ct_type>,id=<id>,mac=<id>,dev_ver=124` (CT002) or `122` (CT003), `fc_ver=202409090159` |
| `0x05` set Wi-Fi | Acknowledged; the network name is remembered for `0x08`, and the board tries the network unless `apply_wifi: false` (see [Wi-Fi from the app](#wi-fi-from-the-app)) |
| `0x08` read SSID | The network the app sent during the current connection, otherwise the one the board is connected to |
| `0x06`, `0x09` | Restart the board. Its configuration is untouched |
| `0x12` linked batteries | The batteries currently polling the board, as many whole entries as fit one notification at the negotiated MTU |
| `0x16` active power (CT002) | The three phase powers |

Everything else, such as calibration, current-direction reversal, CT003 meter
configuration and firmware updates, gets no reply. The app reports those
buttons as failed. They have nothing to act on in an emulator, whose polarity
and meter come from its YAML.

Frames split across several writes are reassembled, and malformed ones are
dropped silently, as on the meter. The app's MTU request (up to 517) is
accepted as is.

### Notes

- **Other BLE features.** The service sits on ESPHome's `esp32_ble_server`, so
  other components that use it, such as `esp32_improv`, share the GATT server
  and the advertised name.
- **Open access.** Like a real meter, the board accepts any phone in range
  without pairing: anyone nearby can read its live data, restart it with
  `0x06`/`0x09` and move it to another Wi-Fi network. `apply_wifi: false`
  takes the last one away; `bluetooth: false` removes the stack altogether.
- **Wi-Fi power saving.** ESP-IDF needs Wi-Fi modem sleep while Bluetooth runs.
  ESPHome's ESP32 default (`power_save_mode: light`) is fine; a config that
  sets `power_save_mode: none` should turn Bluetooth off.
- **Resources.** On an `esp32dev` build the BLE stack adds about 350 KB of flash
  and 20 KB of static RAM, plus the heap Bluedroid allocates at runtime. The
  test builds that combine it with the dashboard, MQTT and HTTPS still use at
  most 1.42 MB of the 1.83 MB app partition. A classic ESP32 that also runs
  the dashboard, MQTT and HTTPS registration is the tightest case for heap.
  The board logs its free internal heap when Bluetooth comes up; if that runs
  low, `bluetooth: false` takes all of it back.
- **Cloud registration name.** AstraMeter's cloud auto-registration sets
  `bluetooth_name = MST-SMR_<suffix>` for both models. A real CT002 advertises as
  `MST-TPM_…`, and the app's name table maps `MST-TPM_` to "CT002". The field is
  only cosmetic in the app today, but a CT002 record should use `MST-TPM_`.
- **Field testing.** The reply layouts follow the meter firmware and the app's
  parsers, and are covered by host tests. Adding the board, reconnecting after
  a restart and the live view have been tried with the Android app on an
  ESP32-S3; switching Wi-Fi from the app has not yet.
