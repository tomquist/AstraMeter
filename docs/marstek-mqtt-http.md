# Marstek CT002 / CT003 MQTT & HTTP (cloud / app) protocol

This documents how a CT002 (`HME-4`) / CT003 (`HME-3`) talks to the Marstek
mobile app over MQTT and to the Marstek cloud over HTTP. It is a reference for
**replicating** that side (the `mqtt_insights:` Marstek responder emulates part
of it). The UDP control protocol between the CT and the batteries is separate —
see [ct002-ct003-protocol.md](ct002-ct003-protocol.md).

Both models speak the same MQTT protocol. **Only the CT003 (`HME-4` = CT002 does
not) reports to the HTTP cloud**, because only the CT003 reads a P1/SML smart
meter with energy data worth reporting.

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

## 6. HTTP cloud reporting (CT003 only)

The CT003 also reports to the Marstek cloud over plain **HTTP GET** (via the
Wi‑Fi module: configure URL → GET → read response). Two endpoints, both under
`eu.hamedata.com` (EU region host):

### 6.1 Status report — `setCtReporting`

```text
GET http://eu.hamedata.com/prod/api/v1/setCtReporting
    ?id=<mac>&eled=<lld>&elet=<lld>
    &ap=<d>&bp=<d>&cp=<d>&dp=<d>
    &rssi=<d>&slv=<d>&udp=<d>&mqtt=<d>
    &timeNo=<d>&date=<YYYY>-<MM>-<DD>
    &cz=<d>&ca=<d>&cb=<d>&cc=<d>&cd=<d>
    &dz=<d>&da=<d>&db=<d>&dc=<d>&dd=<d>
```

| field | meaning |
|-------|---------|
| `id` | device MAC |
| `eled`, `elet` | cumulative energy values (64‑bit) — import/export style totals |
| `ap`, `bp`, `cp`, `dp` | per‑phase power for phases A/B/C and **D** (the combined/合相 bucket) |
| `rssi` | Wi‑Fi RSSI |
| `slv` | connected battery count |
| `udp`, `mqtt` | UDP / MQTT link state flags |
| `timeNo` | monotonic sequence / time number |
| `date` | `Y-M-D` |
| `cz,ca,cb,cc,cd` | charge power: combined‑unassigned (`z`) + phases A/B/C/D |
| `dz,da,db,dc,dd` | discharge power: combined‑unassigned (`z`) + phases A/B/C/D |

The `cz/ca/cb/cc/cd` and `dz/da/db/dc/dd` groups mirror the UDP response's
`x`/`A`/`B`/`C`/`ABC` charge/discharge buckets (`z`↔`x`, `d`↔`ABC`). It is the
cloud's source of the per‑phase power and energy history shown in the app.

**Cadence.** This is a **timer‑driven, repeating** push — each report carries an
incrementing `timeNo` and a `date`, i.e. it is scheduled, not event‑driven. The
exact interval between reports is **not documented here** — the known timers are
per‑operation, not the report period: the HTTP GET steps time out at **5 s**
(config/read) and **65 s** (response wait), and the MQTT reconnect backoffs are
30 s / 50 s. To get the real cadence, **measure it from the device** — the gap
between successive `setCtReporting` GETs to `eu.hamedata.com` in a DNS/HTTP
capture is the ground truth.

> Note: the on‑wire URL has a missing `&` between `slv=%d` and `udp=%d`
> (`…&slv=%dudp=%d…`), so on the wire the slave count and udp flag run together as
> one token. Replicas mimicking it byte‑for‑byte should reproduce that quirk;
> a tolerant server should parse `slv` as everything up to `udp=`.

### 6.2 Config / time fetch — `getDateInfoeu.php`

```text
GET http://eu.hamedata.com/app/neng/getDateInfoeu.php?uid=<mac>&fcv=<fw>&aid=<id>&sv=<d>
```
Used to fetch date / config / OTA info: `uid` = MAC, `fcv` = firmware version,
`aid` = an account/app id, `sv` = a settings/schema version. (`hamedata.com` is
also the OTA download host.)

## 7. CT002 vs CT003 summary

| | CT002 (`HME-4`) | CT003 (`HME-3`) |
|---|---|---|
| MQTT (8883, mutual TLS, both topic prefixes) | yes | yes |
| `cd` command set | yes | yes |
| runtime‑info frame | shorter, ends `…slv_n,cur_d` | longer, `eng_t` + `com_t…udp_v` diagnostics |
| HTTP cloud reporting | **no** | **yes** (`setCtReporting`, `getDateInfoeu`) |

## 8. Relation to AstraMeter

The `mqtt_insights:` Marstek responder
(`src/astrameter/mqtt_insights/marstek_mqtt.py`) emulates the **`cd=1`** runtime
frame and the **`cd=4`** slave list against a local broker so the app shows live
grid power via [hame‑relay](https://github.com/tomquist/hame-relay). It emits a
tolerant superset rather than a byte‑exact copy (different key order, extra
`kwh/...` keys, comma‑joined `cd=4` rows); see that module's note. AstraMeter does
**not** implement the auxiliary `cd` frames (§5.3–§5.6), the mutual‑TLS cloud
connection, or the HTTP cloud reporting in §6 — those are documented here for
completeness and for anyone aiming to fully replicate a real CT.
