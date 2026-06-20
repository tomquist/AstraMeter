# Marstek CT002 / CT003 MQTT & HTTP (cloud / app) protocol

This documents how a CT002 (`HME-4`) / CT003 (`HME-3`) talks to the Marstek
mobile app over MQTT and to the Marstek cloud over HTTP. It is a reference for
**replicating** that side (the `mqtt_insights:` Marstek responder emulates part
of it). The UDP control protocol between the CT and the batteries is separate —
see [ct002-ct003-protocol.md](ct002-ct003-protocol.md).

Both models speak the same MQTT protocol **and both report to the HTTP cloud**
(`setCtReporting` + `getDateInfoeu.php`). The report **field set differs by
model** (see §6): the `HME-4` clamp adds instantaneous voltage/current and uses
32-bit energy; the `HME-3` smart-meter reader uses 64-bit energy and omits
voltage/current.

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

TLS is **mutual**: the device is provisioned with a **CA certificate, a client
certificate, and a client private key** (all carried on the device) and presents
the client cert to the broker. Certificate verification of the server is
configured permissively (the connect proceeds on the client cert). To replicate
the device you need its provisioned client cert/key for that broker; a stand‑in
broker that doesn't require the client cert can be used for local testing (this
is what the AstraMeter responder + [hame‑relay](https://github.com/tomquist/hame-relay)
rely on).

Connection bring‑up sequence (the device drives a Quectel Wi‑Fi module over `AT`):
load the three certs, configure TLS (`verify` permissive, all ciphersuites,
TLS 1.2), then `QMTOPEN` (host, 8883) → `QMTCONN` (`mst_<mac>`) → `QMTSUB` the
App topic. On Wi‑Fi loss it `QMTCLOSE`/`QMTDISC` and retries with backoff.

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
on `,`, then splits each token on `=` expecting exactly one `=`. Lists of repeated
records use `;` between records.

**App → device** commands are a single `cd=<NN>` token (zero‑padded, e.g.
`cd=01`), optionally followed by parameters (`,p1=<n>` etc.). The device parses
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

`cd` `1`–`7` are the core read polls; `1` (runtime info) and `4` (slave list)
are the two the app uses most and the two the AstraMeter responder implements. The other low codes return the auxiliary frames
in §5 (network, identity, smart‑meter, etc.); their exact `cd` numbers beyond the
table above were not all pinned down.

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
(`A`/`B`/`C`/`D`/`0`), `slv_id` = battery MAC. A second compact slave/device form
also exists: `type=%s,sid=%s,ip=%s,phpos=%c;`.

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
plain **HTTP GET** — no TLS, no token/signature; the device is identified only by
the cleartext `id`/`aid` query params. The Wi‑Fi module does it in three AT steps
(`AT+QHTTPCFG="url",…` → `AT+QHTTPGET=60` → `AT+QHTTPREAD=60`). Two endpoints,
both under `eu.hamedata.com` (the EU‑region host; other regions presumably swap
the host).

### 6.1 Status report — `setCtReporting`

The query string is **model‑dependent**. The exact templates:

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
| `id` | device id (MAC) |
| `eled`, `elet` | cumulative energy registers — import/export style totals (`HME-4`: 32‑bit; `HME-3`: 64‑bit) |
| `ap`, `bp`, `cp`, `dp` | per‑phase power for phases A/B/C and **D** (the combined/合相 bucket) |
| `rssi` | Wi‑Fi RSSI |
| `slv` | connected battery count |
| `udp`, `mqtt` | UDP / MQTT link state flags |
| `timeNo` | monotonic sequence / time number |
| `date` | `Y-M-D` |
| `va,vb,vc` | *(HME-4 only)* per‑phase voltage |
| `ia,ib,ic` | *(HME-4 only)* per‑phase current (2 decimals, A) |
| `cz,ca,cb,cc,cd` | charge power: combined‑unassigned (`z`) + phases A/B/C/D |
| `dz,da,db,dc,dd` | discharge power: combined‑unassigned (`z`) + phases A/B/C/D |

The `cz/ca/cb/cc/cd` and `dz/da/db/dc/dd` groups mirror the UDP response's
`x`/`A`/`B`/`C`/`ABC` charge/discharge buckets (`z`↔`x`, `d`↔`ABC`). This is the
cloud's source of the per‑phase power and energy history shown in the app. The
`HME-4` additionally feeds the cloud its clamp‑measured voltage/current; the
`HME-3` (which reads a smart meter, not a clamp) sends only the energy registers.

> **`HME-3` quirk:** the on‑wire URL has a **missing `&`** between
> `slv=%d` and `udp=%d` (`…&slv=%dudp=%d…`), so the slave count and udp flag run
> together as one token. The `HME-4` template has the `&`. Replicas mimicking
> the `HME-3` byte‑for‑byte should reproduce the quirk; a tolerant server should
> parse `slv` as everything up to `udp=`.

**Cadence.** This is a **timer‑driven, repeating** push — each report carries an
incrementing `timeNo` and a `date`, i.e. it is scheduled, not event‑driven. The
exact interval between reports is **not documented** here. To get the real
cadence, **measure it from the device** — the gap between successive
`setCtReporting` GETs to `eu.hamedata.com` in a DNS/HTTP capture is the ground
truth.

### 6.2 Config / time fetch — `getDateInfoeu.php`

```text
GET http://eu.hamedata.com/app/neng/getDateInfoeu.php?uid=%s&fcv=%s&aid=%s&sv=%d
```
A one‑shot handshake the device runs before reporting, to fetch date / config /
OTA info: `uid` = device id (MAC), `fcv` = firmware version, `aid` = an
account/app id, `sv` = a settings/schema version. (`hamedata.com` is also the OTA
download host.) **The server's response format is not documented here**; the
device reads the body but what it does with it (server time, an enable flag, a
reporting interval, account binding) is unconfirmed — another reason a live
capture is needed to fully replicate the round trip.

### 6.3 What's needed to replicate, and the open unknowns

Because it's plaintext GET with no signing, reproducing the requests is
mechanical. The blockers for a cloud the real backend will *accept* are identity
and semantics, not crypto:

- **`aid` (account id)** is **not something the device generates** — it comes
  from the app pairing flow. The cloud only associates reports for an `id`/`aid`
  pair it already knows, so pushing into a real account needs a paired device's
  identifiers.
- **Field units/scaling/sign**, the **report cadence**, and the **handshake
  response contract** are not documented here. A single DNS‑redirect +
  HTTP‑proxy capture of a real CT yields all of them at once.

## 7. CT002 vs CT003 summary

| | CT002 (`HME-4`) | CT003 (`HME-3`) |
|---|---|---|
| MQTT (8883, mutual TLS, both topic prefixes) | yes | yes |
| `cd` command set | yes | yes |
| runtime‑info frame | shorter, ends `…slv_n,cur_d` | longer, `eng_t` + `com_t…udp_v` diagnostics |
| HTTP cloud reporting | **yes** — `setCtReporting` **with** `va/vb/vc`+`ia/ib/ic`, 32‑bit energy | **yes** — `setCtReporting` 64‑bit energy, no V/I, missing‑`&` quirk |

## 8. Relation to AstraMeter

The `mqtt_insights:` Marstek responder
(`src/astrameter/mqtt_insights/marstek_mqtt.py`) emulates the **`cd=1`** runtime
frame and the **`cd=4`** slave list against a local broker so the app shows live
grid power via [hame‑relay](https://github.com/tomquist/hame-relay). It emits a
tolerant superset rather than a byte‑exact copy (different key order, extra
`kwh/...` keys, comma‑joined `cd=4` rows); see that module's note. AstraMeter does
**not** implement the auxiliary `cd` frames (§5.3–§5.6) or the mutual‑TLS cloud
MQTT connection — those are documented here for completeness and for anyone aiming
to fully replicate a real CT.

**HTTP cloud reporting is implemented as an opt‑in feature** (§6) on **both**
stacks. In Python set `CLOUD_REPORTING = true` in the `[CT002]`/`[CT003]` section;
on ESPHome add a `cloud_reporting:` sub‑block under `ct002:` (it needs an
`http_request:` block). Either way AstraMeter runs the same
handshake‑then‑periodic‑`setCtReporting` flow a real CT does, choosing the
`HME-4`/`HME-3` field layout from the emulated `ct_type`. It fills the fields
AstraMeter knows (per‑phase power, the charge/discharge buckets, RSSI, slave
count, link flags) and zero‑fills what it doesn't measure (cumulative energy, and
V/I on `HME-4`). Because the cloud accepts a report only for an `id`/`aid` it
already knows, tune the interval to the cadence you measure. Both identifiers are
derived from your Marstek account when configured: the reported `id` is the CT's
MAC — when a Marstek account is configured (the `[MARSTEK]` section, or the
ESPHome `marstek_registration:` block), the MAC of the device AstraMeter
registers there is used (the id the cloud already knows), otherwise the
configured `CT_MAC` / `ct_mac`. On the Python side the `aid` likewise defaults to
your Marstek account id (the login `uid`) when not set; on ESPHome set
`account_id` in the `cloud_reporting:` block. (Note: `aid` is only sent in the
handshake — `setCtReporting` carries no `aid` — so reports bind by the registered
`id`.) The web config generator produces all three forms (config.ini, the add‑on
options, the ESPHome sub‑block). See `config.ini.example`.
