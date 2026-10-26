# Marstek CT002 / CT003 MQTT & HTTP (cloud / app) protocol

This documents how a CT002 (`HME-4`) / CT003 (`HME-3`) talks to the Marstek
mobile app over MQTT and to the Marstek cloud over HTTP. Use it to **replicate**
that side; the `mqtt_insights:` Marstek responder emulates part of it. The UDP
control protocol between the CT and the batteries is separate — see
[ct002-ct003-protocol.md](ct002-ct003-protocol.md).

Both models speak the same MQTT protocol **and both report to the HTTP cloud**
(`setCtReporting` + `getDateInfoeu.php`) on their newest firmware. The report
**field set differs by model** (see §6): the `HME-4` clamp adds instantaneous
voltage/current and uses 32-bit energy; the `HME-3` smart-meter reader uses
64-bit energy and leaves out voltage/current.

## 1. MQTT connection

| parameter | value |
|-----------|-------|
| transport | TLS, **port 8883** |
| MQTT version | 3.1.1 (protocol level 4) |
| keepalive | 30 s |
| clean session | yes |
| client id | **`mst_<mac>`** where `<mac>` is the 12‑hex lowercase device MAC |
| username / password | **none** — the device authenticates with a **client certificate** |
| broker host | **provisioned at runtime** (not hard‑coded in the image); the cloud side lives under `hamedata.com` (the HTTP host is `eu.hamedata.com` for the EU region) |

TLS is **mutual**: the device carries a **CA certificate, a client certificate,
and a client private key**, and presents the client cert to the broker. Server
certificate verification is permissive — the connect proceeds on the client cert.
To replicate the device you need its provisioned client cert and key for that
broker. For local testing, use a stand‑in broker that doesn't require the client
cert; that is what the AstraMeter responder +
[hame‑relay](https://github.com/tomquist/hame-relay) rely on.

Connection bring‑up sequence (the device drives a Quectel Wi‑Fi module over `AT`):
load the three certs, configure TLS (`verify` permissive, all ciphersuites,
TLS 1.2), then `QMTOPEN` (host, 8883) → `QMTCONN` (`mst_<mac>`) → `QMTSUB` the
App topic. On Wi‑Fi loss it runs `QMTCLOSE`/`QMTDISC` and retries with backoff.

## 2. Topics

The device **subscribes** to the App control topic and **publishes** to the
device control topic:

```text
subscribe (app → device):  <prefix>/<ct_type>/App/<mac>/ctrl
publish   (device → app):  <prefix>/<ct_type>/device/<mac>/ctrl
```

- `<prefix>` is **`marstek_energy`** on current firmware; older firmware used
  **`hame_energy`** (after the `hamedata.com` cloud). Replicas should accept both.
- `<ct_type>` is the model string `HME-4` (CT002) or `HME-3` (CT003).
- `<mac>` is the 12‑hex device MAC.

## 3. Message framing

Payloads are UTF‑8 CSV `key=value` text. The app's parser strips spaces, splits
on `,`, then splits each token on `=`, expecting exactly one `=`. Lists of
repeated records use `;` between records.

**App → device** commands are a single `cd=<NN>` token (zero‑padded, e.g.
`cd=01`), optionally followed by parameters (`,p1=<n>` etc.). The device reads
the `cd` number, latches it as the pending response type plus any parameter, and
the publish task then emits the matching frame on the device topic. **Replies do
not echo a `cd=` key** — the app already knows what it asked for.

## 4. Command reference (`cd` codes)

Confirmed handlers (app sends `cd=<NN>` on the App topic; device answers on the
device topic):

| `cd` | direction / action | device reply payload |
|------|--------------------|----------------------|
| `1` | poll runtime info | `pwr_a=…` aggregate frame (see §5.1) |
| `4` | poll slave list (page 1, slaves 0–4) | repeated `slv_ip=…;` (see §5.2) |
| `7` | (takes a parameter) | — |
| `8` | **reset_ct** — device replies `reset_ct` then reboots | `reset_ct` |
| `9` | **start_debug** (parameter) — enable raw‑UDP debug stream | `start_debug`, then `sCtn=…` frames (§5.4) |
| `10` | **clear_Err** (parameter) — clear / read error counters | `clear_Err` or `Err=…` (§5.5) |
| `11` | slave list (page 2, slaves 5–8) | repeated `slv_ip=…;` |
| `29` | poll detail record `N` (param `1..30`) | `PP=…` power‑detail frame (§5.6) |
| `30` | poll detail record `N` (param `1..30`) | detail frame |
| `33` | (command) | — |
| `41` | slave list (page, slaves 0–4) | repeated `slv_ip=…;` |
| `42` | slave list (page, slaves 5–8) | repeated `slv_ip=…;` |

`cd` `1`–`7` are the core read polls. `1` (runtime info) and `4` (slave list)
are the two the app uses most, and the two the AstraMeter responder implements.
The other low codes return the auxiliary frames in §5 (network, identity,
smart‑meter, etc.); their exact `cd` numbers beyond the table above were not all
pinned down.

## 5. Device → app payloads

All numeric fields are decimal ASCII. Strings are bare (no quoting).

### 5.1 Runtime info (`cd=1`)

**CT002 (`HME-4`):**
```text
pwr_a=%d,pwr_b=%d,pwr_c=%d,pwr_t=%d,ble_s=%d,wif_r=%d,fc4_v=%s,ver_v=%d,wif_s=%d,slv_n=%d,cur_d=%d
```
**CT003 (`HME-3`):**
```text
pwr_a=%d,pwr_b=%d,pwr_c=%d,pwr_t=%d,ble_s=%d,wif_r=%d,fc4_v=%s,ver_v=%d,eng_t=%lld,wif_s=%d,slv_n=%d,
com_t=%d,com_b=%d,ptl_t=%d,smt_n=%d,har_f=%d,sof_f=%d,irs_f=%d,pwr_f=%d,frm_c=%d,upd_t=%d,udp_v=%d
```

| field | meaning |
|-------|---------|
| `pwr_a/b/c` | per‑phase grid power (W) |
| `pwr_t` | total grid power (W) |
| `ble_s` | BLE state |
| `wif_r` | Wi‑Fi RSSI (dBm) |
| `fc4_v` | Wi‑Fi module (FC41D) firmware string |
| `ver_v` | device firmware version |
| `wif_s` | Wi‑Fi state |
| `slv_n` | number of connected batteries (slaves) |
| `cur_d` | *(CT002 only)* current‑direction/day field |
| `eng_t` | *(CT003 only)* cumulative energy, 64‑bit |
| `com_t,com_b` | *(CT003)* comms counters |
| `ptl_t` | *(CT003)* smart‑meter protocol type |
| `smt_n` | *(CT003)* smart‑meter count |
| `har_f,sof_f,irs_f,pwr_f` | *(CT003)* hardware / software / interrupt / power fault flags |
| `frm_c` | *(CT003)* frame counter |
| `upd_t` | *(CT003)* update time |
| `udp_v` | *(CT003)* UDP protocol version (`4`) |

### 5.2 Slave list (`cd=4` / `11` / `41` / `42`)

Repeated, `;`‑terminated, **max 5 records per message** (hence the page codes):
```text
slv_ip=%s,slv_t=%s,slv_p=%c,slv_id=%s;
```
`slv_ip` = battery IP, `slv_t` = battery type, `slv_p` = its phase char
(`A`/`B`/`C`/`D`/`0`), `slv_id` = battery MAC. A second, more compact
slave/device form also exists: `type=%s,sid=%s,ip=%s,phpos=%c;`.

### 5.3 Network / identity

```text
wif_n=%s,ip_ad=%s,udp_f=%d,gate=%s,mask=%s,dns=%s
type=%s,id=%s,mac=%s,dev_ver=%d,fc_ver=%s
```
Wi‑Fi SSID, IP, UDP flag, gateway, netmask, DNS; and device type / id / MAC /
device version / Wi‑Fi‑module version.

### 5.4 Debug stream (`cd=9`)

```text
sCtn=%d,rCtn=%d,udpData=%s
```
Send count, receive count, and a raw UDP frame (the on‑wire CT packet) — a live
tap of the UDP control traffic, used for diagnostics.

### 5.5 Error counters (`cd=10`)

```text
Err=%d,%d,%d,%d,%d,%d,%d,%d,Urt_e=%d,Urt_s=%d
```
Eight error counters plus UART error / success counters.

### 5.6 Power / meter detail (`cd=29` / `30`)

```text
PP=%d,NP=%d,aPP=%d,bPP=%d,cPP=%d,aNP=%d,bNP=%d,cNP=%d
sm_t=%d,sm_b=%d,sm_p=%d,sm_n=%d,rec_l=%d,sm_dctn=%d,sm_p1iscon=%d,sm_p=%d,sm_wl=%d,e1=%d,rst=%d,isr=%d,urt_e=%d,urt_s=%d,isThr=%d
```
`PP`/`NP` = total positive (import) / negative (export) power; `aPP/bPP/cPP` and
`aNP/bNP/cNP` = the per‑phase positive/negative split. The `sm_*` frame is CT003
smart‑meter diagnostics (type, baud, protocol, count, record length, direction,
phase‑1 connected, wiring, etc.).

## 6. HTTP cloud reporting (both models)

**Both** the CT002 (`HME-4`) and CT003 (`HME-3`) report to the Marstek cloud over
plain **HTTP GET** — no TLS, no token or signature. The device is identified only
by the cleartext `id`/`uid` query params. The Wi‑Fi module does it in three AT
steps (`AT+QHTTPCFG="url",…` → `AT+QHTTPGET=60` → `AT+QHTTPREAD=60`). There are
two endpoints, both under `eu.hamedata.com` (the EU‑region host; other regions
presumably swap the host).

Only the newest public images do it: `HME-4` **v124** and `HME-3` **v122**.
`HME-4` v120 and `HME-3` v116–v118 carry neither URL. Everything below was read
from those two images (same archives and load address as in
[ct002-ct003-protocol.md](ct002-ct003-protocol.md)); both run the same code.

### 6.1 The reporting cycle

The main loop calls one state machine whenever Wi‑Fi is up. Each step is one
blocking HTTP GET, and the state survives a Wi‑Fi drop:

1. **Handshake** — `getDateInfoeu.php` (§6.3). Until the module reports HTTP
   200 the step repeats on the next pass. On success the CT parses the server
   time out of the body, rolls its energy registers forward (§6.4) and moves on.
2. **Report** — `setCtReporting` (§6.2), repeated until it gets HTTP 200. A
   retry reuses the server time from step 1; it does not handshake again.
3. **Wait** — 300 ticks of its seconds counter (a 2 kHz timer divided down),
   so **300 s**, then back to step 1.

So a real CT sends one handshake and one report about every five minutes, plus
whatever the requests themselves take. The CT reads the response body of
`setCtReporting` but does not use it.

### 6.2 Status report — `setCtReporting`

The query string **depends on the model**. The exact templates:

**CT002 (`HME-4`)** — 32‑bit energy, **plus** instantaneous voltage/current:
```text
GET http://eu.hamedata.com/prod/api/v1/setCtReporting
    ?id=%s&eled=%d&elet=%d&ap=%d&bp=%d&cp=%d&dp=%d&rssi=%d&slv=%d&udp=%d&mqtt=%d
    &timeNo=%d&date=%d-%02d-%02d
    &va=%d&vb=%d&vc=%d&ia=%.2f&ib=%.2f&ic=%.2f
    &cz=%d&ca=%d&cb=%d&cc=%d&cd=%d&dz=%d&da=%d&db=%d&dc=%d&dd=%d
```

**CT003 (`HME-3`)** — 64‑bit energy, **no** voltage/current:
```text
GET http://eu.hamedata.com/prod/api/v1/setCtReporting
    ?id=%s&eled=%lld&elet=%lld&ap=%d&bp=%d&cp=%d&dp=%d&rssi=%d&slv=%dudp=%d&mqtt=%d
    &timeNo=%d&date=%d-%02d-%02d
    &cz=%d&ca=%d&cb=%d&cc=%d&cd=%d&dz=%d&da=%d&db=%d&dc=%d&dd=%d
```

| field | meaning |
|-------|---------|
| `id` | device id (MAC), the same `id` as in the handshake and the MQTT topics |
| `eled` | energy since the start of the server's **day**, in **0.1 Wh** (§6.4) |
| `elet` | energy since the start of the server's **hour**, in **0.1 Wh** (§6.4) |
| `ap`, `bp`, `cp` | phase A/B/C power (W) — the runtime frame's `pwr_a/b/c` |
| `dp` | total power (W) — the runtime frame's `pwr_t` |
| `rssi` | Wi‑Fi RSSI (`wif_r`) |
| `slv` | connected battery count (`slv_n`) |
| `udp` | `1` once the module has opened the UDP service |
| `mqtt` | `1` while the cloud MQTT connection is up |
| `timeNo` | the **10‑minute slot** of the server's day: `(hour × 60 + minute) / 10`, `0`–`143` |
| `date` | the server's date, `Y-MM-DD` |
| `va,vb,vc` | *(HME-4 only)* per‑phase voltage (V, truncated) |
| `ia,ib,ic` | *(HME-4 only)* per‑phase current (A, 2 decimals) |
| `cz,ca,cb,cc,cd` | the UDP response's `x`/`A`/`B`/`C`/`ABC` **`_dchrg_power`** (fields 20–24) |
| `dz,da,db,dc,dd` | the UDP response's `x`/`A`/`B`/`C`/`ABC` **`_chrg_power`** (fields 15–19) |

`timeNo` and `date` come from the **handshake's server time**, not from a local
clock. Both images compute `timeNo` with the same helper.

The bucket fields are read from the very struct the UDP response is built from,
so by position `c*` is what goes out over UDP as `*_dchrg_power` and `d*` what
goes out as `*_chrg_power` — the letters do not mean charge/discharge in the UDP
naming.

**Power sign.** The `HME-4` converts its float powers with an unsigned 16‑bit
cast (`__aeabi_f2uiz`, which ignores the sign, then `uxth`), so `ap`–`dp` go out
as **magnitudes**: an export of 150 W is `ap=150`. The UDP response and the
runtime frame keep the sign. The `HME-3` takes its powers as signed integers
from the meter (import − export when the meter reports both), so they stay
signed.

**What the cloud does with a report.** Measured against the live cloud on a
registered `HME-4` record:

- The server answers every report with HTTP 200 and the JSON body
  `{"code":1,"msg":"success"}` — including an out‑of‑range `timeNo`, an
  epoch‑seconds `timeNo` and an id no account has. A request with no parameters
  gets `{}`. So the answer says nothing about whether the report was used.
- Within seconds, the device list (`ems/api/v1/getDeviceList`, the Marstek app's
  device overview) marks the device online (`status` 1) and sets its `grid` and
  `load` to **`ap + bp + cp + dp`**, sign kept. The bucket fields, `timeNo` and
  `date` do not show there, and `report_time` does not change. After about an
  hour without reports the device goes offline again.

Because `dp` is the total, a report shaped like a real CT's shows **twice** the
grid power in the device list, and an `HME-4` export shows as an import.

> **`HME-3` quirk:** the on‑wire URL is **missing an `&`** between
> `slv=%d` and `udp=%d` (`…&slv=%dudp=%d…`), so the slave count and udp flag run
> together as one token. The `HME-4` template has the `&`. Replicas mimicking
> the `HME-3` byte‑for‑byte should reproduce the quirk; a tolerant server should
> parse `slv` as everything up to `udp=`.

### 6.3 Handshake — `getDateInfoeu.php`

```text
GET http://eu.hamedata.com/app/neng/getDateInfoeu.php?uid=%s&fcv=%s&aid=%s&sv=%d
```
Both images fill it from the same variables as the MQTT runtime frame:

| param | value | runtime frame |
|-------|-------|---------------|
| `uid` | device id (MAC) | topic id |
| `fcv` | Wi‑Fi module firmware string | `fc4_v` |
| `aid` | model, `HME-4` / `HME-3` | — |
| `sv` | the CT firmware version, a constant: `124` (`HME-4`), `122` (`HME-3`) | `ver_v` |

The response is HTTP 200, `text/html`, and its body is the server's wall‑clock
time, which the CT scans with `_%hu_%hhu_%hhu_%hhu_%hhu_%hhu_` (year, month,
day, hour, minute, second). A live answer looks like
`_2026_10_26_09_15_48_04_0_0_0`: that request was sent at 08:15:47 UTC, so the
time is UTC+1, which was Europe/Berlin then and also the time zone of that
device record. The four trailing fields stayed the same across calls; what they
mean is not known, and the CT ignores them. (`hamedata.com` is also the OTA
download host.)

The handshake does **not** set the device record's `version`. A registered
`HME-4` record whose `version` was `148` kept it after handshakes sending
`sv=124`. That `148` was what AstraMeter's MQTT responder then sent as
`ver_v`; it now sends the firmware version, as a real CT does. Earlier notes said the call also writes `aid` into the record's
`type`, so that a non‑model `aid` turns the Marstek app's device card into a
generic one. That part has not been re‑checked, so AstraMeter still sends the
CT model as `aid`, and as `sv` the version a reporting CT of that model runs.

### 6.4 Energy registers — `eled` / `elet`

At every successful handshake the CT reads one cumulative energy value `E` in
0.1 Wh and updates two registers:

```text
eled += E − E_prev;  elet += E − E_prev;  E_prev = E
if server hour ≠ hour of the previous handshake: elet = 0
if server day  ≠ day  of the previous handshake: eled = 0
```

So `eled` is the energy since the first handshake of the server's day and
`elet` since the first handshake of its hour; the gain of the interval that
crosses the boundary is dropped. The source of `E` differs:

- `HME-4` sums its metering chip's three per‑phase import counters minus the
  three export counters — **net** energy. It keeps whole kWh and hundredths and
  computes `kWh × 10000 + hundredths × 100`.
- `HME-3` reads the smart meter's SML entry for **OBIS 1‑0:1.8.0** (import)
  whose unit is Wh, applies the scaler and multiplies by 10.

### 6.5 What's needed to replicate, and the open unknowns

Because it's a plaintext GET with no signing, reproducing the requests is
mechanical. What blocks a cloud the real backend will *accept* is identity, not
crypto:

- **The report `id` (MAC)** must be a device the cloud already knows. The binding
  to an account comes from having registered/paired that device; `setCtReporting`
  itself carries no account id.
- Still unknown: whether the handshake's time follows the server or the device
  record's time zone (both were Europe/Berlin when measured), what the trailing
  handshake fields mean, and what besides the device list reads a report.

## 7. CT002 vs CT003 summary

| | CT002 (`HME-4`) | CT003 (`HME-3`) |
|---|---|---|
| MQTT (8883, mutual TLS, both topic prefixes) | yes | yes |
| `cd` command set | yes | yes |
| runtime‑info frame | shorter, ends `…slv_n,cur_d` | longer, `eng_t` + `com_t…udp_v` diagnostics |
| HTTP cloud reporting (v124 / v122) | **yes** — `setCtReporting` **with** `va/vb/vc`+`ia/ib/ic`, 32‑bit energy, power magnitudes | **yes** — `setCtReporting` 64‑bit energy, no V/I, signed power, missing‑`&` quirk |

## 8. Relation to AstraMeter

The `mqtt_insights:` Marstek responder
(`src/astrameter/mqtt_insights/marstek_mqtt.py`) emulates the **`cd=1`** runtime
frame and the **`cd=4`** slave list against a local broker, so the app shows live
grid power via [hame‑relay](https://github.com/tomquist/hame-relay). It emits a
tolerant superset rather than a byte‑exact copy (different key order, extra
`kwh/...` keys, comma‑joined `cd=4` rows); see that module's note. AstraMeter does
**not** implement the auxiliary `cd` frames (§5.3–§5.6) or the mutual‑TLS cloud
MQTT connection. Those are documented here for completeness, and for anyone aiming
to fully replicate a real CT.

**HTTP cloud reporting is implemented as an opt‑in feature** (§6) on **both**
stacks. In Python, set `CLOUD_REPORTING = true` in the `[CT002]`/`[CT003]`
section; on ESPHome, add a `cloud_reporting:` sub‑block under `ct002:` (it needs
an `http_request:` block). Either way AstraMeter runs the cycle of §6.1 —
handshake, report, a 300 s wait (the interval option) — retrying a failed step
after 60 s, and picks the `HME-4`/`HME-3` field layout from the emulated
`ct_type`. It departs from a real CT in two places so the app's device list
shows the true grid power (§6.2): `dp` always goes out as `0`, and the phase
powers keep their sign on `HME-4` too. `timeNo` and `date` come from the handshake's server time
(the local clock only when the body carries none). It fills the fields
AstraMeter knows (per‑phase power, the bucket fields, RSSI, slave count, link
flags) and zero‑fills V/I on `HME-4`, except that the ESPHome build sends the
optional `voltage_sensor_l1`–`l3` readings as `va`/`vb`/`vc`. Having no energy
counter, it integrates the grid power it samples every 10 s into one — net for `HME-4`, import only for
`HME-3` — and keeps `eled`/`elet` from it as in §6.4. The reported `id` is the
CT's MAC. When a Marstek account is configured (the `[MARSTEK]` section, or the
ESPHome `marstek_registration:` block), it uses the MAC of the device AstraMeter
registers there — the id the cloud already knows; otherwise it uses the
configured `CT_MAC` / `ct_mac`. An ESP32 the app added over Bluetooth, with no
`ct_mac`, reports as that CT. The model and firmware version the handshake
sends (§6.3) are derived automatically, so there is no account‑id knob to set.
On an ESP32 the requests run on a background task, so a slow cloud never holds
up the replies to the batteries. The web config generator produces all three forms
(config.ini, the add‑on options, the ESPHome sub‑block). See `config.ini.example`.
