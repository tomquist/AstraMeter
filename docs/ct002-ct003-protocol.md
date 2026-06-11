# CT002 / CT003 UDP Protocol

This document summarizes the CT002/CT003 protocol based on community reverse‑engineering and the reference scripts in:
- https://github.com/rweijnen/marstek-venus-e-firmware-notes
- https://github.com/d-shmt/hass_marstek-smart-meter

Capture-based findings for issue #111 are documented in:
- [ct002-capture-analysis.md](ct002-capture-analysis.md)

The CT002 and CT003 share the **same protocol**. The only difference is the CT type value:
- **CT002:** `HME-4`
- **CT003:** `HME-3`

CT003 additionally reads a P1/SML smart meter, so it carries cumulative energy
data that CT002 does not (see "CT003 energy fields" below).

## Transport

- **Protocol:** UDP
- **Port:** `12345`
- **Direction:** Storage system (consumer) sends a request to the CT. CT replies with measurements.

## Frame Format

All messages (request and response) are ASCII payloads wrapped with control bytes and a checksum.

```text
SOH (0x01)
STX (0x02)
<LENGTH ASCII digits>
|<field1>|<field2>|...|<fieldN>
ETX (0x03)
<CHECKSUM ASCII HEX>
```

### Length
`LENGTH` is the **total byte length** of the entire packet, including the length digits and checksum bytes.
Because the length field is part of the packet, you must compute it iteratively until the digit count matches
(the CT does the same: it sizes the body, then adds the framing/checksum bytes and one extra byte once the
length crosses from two to three digits).

### Checksum
The checksum is a 2‑character ASCII hex string. It is the XOR of all bytes from the
**start of SOH up to and including ETX**. Parse it case‑insensitively: the CT emits uppercase `A`–`F`
for two‑digit values and may emit a leading space for small values.

Pseudo‑code:

```python
xor = 0
for b in payload_without_checksum:
    xor ^= b
checksum = f"{xor:02x}".encode("ascii")
```

## Request Fields

Request payload fields (consumer → CT):

| # | Field | Type / width | Notes |
|---|-------|--------------|-------|
| — | length | 16‑bit int | leading digits of the framed packet |
| 1 | **meter_dev_type** | string ≤ 10 | requester type (e.g. `HMG-50`); copied into the response |
| 2 | **meter_mac_code** | string ≤ 30 | battery MAC (12 hex chars in practice, from the Marstek app) |
| 3 | **hhm_dev_type** | string ≤ 8 | CT type (`HME-4` or `HME-3`) |
| 4 | **hhm_mac_code** | string ≤ 13 | CT MAC (12 hex chars) |
| 5 | **phase** | single char | `A`/`B`/`C` = physical phase; `D` = **combined** (see "Phase selection"); `0`/empty = **unassigned / inspection** |
| 6 | **phase_power** | 16‑bit signed | watts for the phase in field 5 (range −32768…32767) |
| 7 | **participate** | unsigned byte | **optional**; `0` = do **not** aggregate this reporter, non‑zero = include. **Defaults to `1`** when the field is absent |

Fields 1–6 appear in all observed traffic; field 7 is the newer "UDP protocol v4"
addition and may be omitted by older senders (in which case the CT treats it as `1`).
Whether it is sent is **model‑dependent**: the Venus class (HMG‑50, VNSE3‑0)
builds only fields 1–6, while the B2500 class (HMJ) builds a 7th field (its
request template carries an extra trailing integer). Power is sent as a 16‑bit
value by the Venus class and a 32‑bit value by the B2500 class; both fit the
same ASCII wire field.

### Phase handling and inspection mode

- The CT accepts `A`, `B`, `C` (physical phases) and `D` (combined / 合相 — see
  [Phase selection](#phase-selection-and-the-combined-abc-mode)) as committed phase labels.
- Any other value (observed: `0`, empty) is the **`'0'` unassigned bucket** — the
  **inspection mode** a device uses while it is still determining which phase it is on.
- The emulator:
  - **Responds** to the request (so the device can continue its phase detection), and
  - **Does not** add an unassigned/inspection reporter's power to a committed‑phase aggregate.
- The `participate` flag (field 7) is an additional, explicit gate: even a fully phase‑committed reporter
  is **excluded from aggregation** when it sends `0`.

### When a battery sends `participate = 0`

The flag lets a battery opt out of being counted by the CT. The Venus class
(HMG‑50, VNSE3‑0) never sends the field, so it is always treated as
participating. The B2500 class (HMJ) computes it from its operating state and
sends `1` **only while it is actively following the CT** — that is, when **all**
of the following hold:

- the CT/meter‑control feature is **enabled**,
- the work mode is the **automatic CT‑following** mode (not one of the
  manual / scheduled modes),
- no internal **inhibit** flag is set, and
- at least one of its two inverter/battery channels is in the **running** state
  (the inverter is actually on, not idle/booting).

Otherwise it sends `participate = 0`. In practice a B2500 opts out when it is in
a **manual or scheduled work mode**, when CT control is **disabled**, when an
inhibit is active, or when its **inverter is not running** (off/idle/starting).
It also reports power `0` when the feature is disabled or in one of the manual
modes (in the other manual mode it still reports its real power but with
`participate = 0`).

> The exact work‑mode numbering and the precise enable/inhibit conditions are
> inferred from the device's behavior; the rule "participates only while actively
> CT‑following with the inverter running" is the reliable summary.

### Example Request (human readable)

```text
<SOH><STX>53|HMG-50|AABBCCDDEEFF|HME-4|112233445566|B|-217<ETX>xx
```

## Response Fields

Response payload fields (CT → consumer). **Identity order differs from the
request:** the response leads with the **CT/meter** type/MAC, then the **storage**
type/MAC (`…|HME-3|<ct_mac>|HMG-50|<battery_mac>|…`) — the reverse of the request's
storage‑then‑CT order. This is confirmed by the captures, the CT, and the storage
side (which reads token 0 as its `meter_dev_type` = the CT type). It matches
AstraMeter's `RESPONSE_LABELS`.

The numeric section uses **four phase buckets plus one "unassigned" bucket**:

- `x_*` — the **`'0'`/unassigned** bucket: reporters that have not committed to a phase (inspection).
- `A_*` / `B_*` / `C_*` — the three **physical phases**.
- `ABC_*` — the **combined / 合相** bucket: a reporter balancing against the **sum of all three phases** (see "Phase selection" below).

| # | Field | Type / width | Meaning |
|---|-------|--------------|---------|
| 1 | **meter_dev_type** | string | CT/meter type (`HME-4` / `HME-3`) |
| 2 | **meter_mac_code** | string | CT MAC |
| 3 | **hhm_dev_type** | string | storage type (echoes request, e.g. `HMG-50`) |
| 4 | **hhm_mac_code** | string | storage MAC (echoes request battery MAC) |
| 5 | **A_phase_power** | int ¹ | watts, phase A |
| 6 | **B_phase_power** | int ¹ | watts, phase B |
| 7 | **C_phase_power** | int ¹ | watts, phase C |
| 8 | **total_power** | 32‑bit int | watts, total |
| 9 | **A_chrg_nb** | unsigned byte | count of reporters on phase A |
| 10 | **B_chrg_nb** | unsigned byte | count of reporters on phase B |
| 11 | **C_chrg_nb** | unsigned byte | count of reporters on phase C |
| 12 | **ABC_chrg_nb** | unsigned byte | count of reporters in the combined (`ABC`) bucket |
| 13 | **wifi_rssi** | **signed** byte | RSSI (negative dBm) |
| 14 | **info_idx** | unsigned byte | response index, `0..255`, wraps; the consumer uses it to drop duplicate responses |
| 15 | **x_chrg_power** | 16‑bit signed ² | unassigned‑bucket charge sum (negative powers) |
| 16 | **A_chrg_power** | 16‑bit signed ² | phase A charge sum (negative powers) |
| 17 | **B_chrg_power** | 16‑bit signed ² | phase B charge sum |
| 18 | **C_chrg_power** | 16‑bit signed ² | phase C charge sum |
| 19 | **ABC_chrg_power** | 16‑bit signed ² | combined‑bucket charge sum |
| 20 | **x_dchrg_power** | 16‑bit signed ² | unassigned‑bucket discharge sum (positive powers) |
| 21 | **A_dchrg_power** | 16‑bit signed ² | phase A discharge sum (positive powers) |
| 22 | **B_dchrg_power** | 16‑bit signed ² | phase B discharge sum |
| 23 | **C_dchrg_power** | 16‑bit signed ² | phase C discharge sum |
| 24 | **ABC_dchrg_power** | 16‑bit signed ² | combined‑bucket discharge sum |
| 25 | **low_price_ele_in** | 32‑bit unsigned | (CT003 only) off‑peak import energy |
| 26 | **normal_price_ele_in** | 32‑bit unsigned | (CT003 only) peak import energy |
| 27 | **low_price_ele_out** | 32‑bit unsigned | (CT003 only) off‑peak export energy |
| 28 | **normal_price_ele_out** | 32‑bit unsigned | (CT003 only) peak export energy |

¹ **Field‑width difference between models:** on **CT002 (`HME-4`)** the three
per‑phase powers (fields 5–7) are **16‑bit signed** (±32767 W); the total
(field 8) is 32‑bit. On **CT003 (`HME-3`)** all four are 32‑bit.

² The charge/discharge breakdown (fields 15–24) is **16‑bit signed on both
models**, so each value saturates at ±32767 W.

These names match `RESPONSE_LABELS` in `src/astrameter/ct002/protocol.py`. In
ordinary single/three‑phase traffic the `x_*` and `ABC_*` buckets are usually
`0` (nobody is mid‑inspection or in combined mode), which is why earlier capture
analysis saw the 4th slot as "always 0".

### Phase selection and the combined (`ABC`) mode

The storage device picks the request **phase** character from a `phase_t`
configuration value:

| `phase_t` | request phase char | response bucket used |
|-----------|--------------------|----------------------|
| 0 | `'0'` | `x_*` (unassigned / still detecting) |
| 1 | `A` | `A_*` |
| 2 | `B` | `B_*` |
| 3 | `C` | `C_*` |
| 4 | `D` | `ABC_*` (combined) |

So **phase `D` on the wire is not a fourth physical phase** — it selects the
**combined / 合相** aggregation. A consumer in `phase_t = 4` tells the CT to lump
it into the `ABC` bucket and reads back `ABC_chrg_power` / `ABC_dchrg_power`,
i.e. it balances against the **sum of all three phases** (whole‑home net grid
power) rather than its own phase. This is the mode behind a B2500 reporting
phase `D`: the meter is wired across a 3‑phase supply and the battery is set to
compensate the **total** grid exchange, not a single phase. The `x` bucket is
the transient state while a device is still detecting its phase (`phase_t = 0`).

> The AstraMeter emulator models phases `A`/`B`/`C` and folds `D` and any
> non‑`A/B/C` value into the unassigned/inspection path; it does not yet
> implement the combined (`ABC`) control mode.

### CT003 energy fields (fields 25–28)

CT003 (`HME-3`) — but **not** CT002 (`HME-4`) — appends **four** trailing
unsigned‑32‑bit fields, making the CT003 response **28 fields** vs. CT002's 24.
They are the cumulative import/export energy registers CT003 reads from a
connected P1/SML smart meter, split by tariff, named
`low_price_ele_in`, `normal_price_ele_in`, `low_price_ele_out`,
`normal_price_ele_out` — i.e. off‑peak/peak import and off‑peak/peak export,
corresponding to the standard DSMR/SML registers `1-0:1.8.x` (import) and
`1-0:2.8.x` (export). The exact scaling (the OBIS values are carried as `kWh`
with three decimals) is the remaining unknown. The AstraMeter emulator (a
clamp‑style CT002) does not source a smart meter and does not emit these fields.

## Aggregation, eviction and response cadence

The CT keeps a table of up to **9 consumer slots**, keyed by battery MAC. Each
incoming request updates the matching slot (or allocates a free one), storing
the reporter's IP, type, phase, signed power, and `participate` flag.

Per response cycle the CT:

1. **Evicts stale slots** — a slot not refreshed within ~1–2 cycles is cleared.
2. **Builds per‑bucket aggregates** over the live slots, but only for a slot that
   is occupied **and** has `participate != 0`:
   - bucket by phase (`A`/`B`/`C`, the combined `ABC` bucket for phase `D`, or
     the `x` bucket for the `'0'`/unassigned phase), and
   - **sign‑split** the power: negative → that bucket's `*_chrg_power`,
     positive → its `*_dchrg_power`; bump the bucket's `*_chrg_nb` count.
3. **Replies to one slave per cycle**, round‑robin across the active slots
   (each requester receives its own unicast response).

**Effect of `participate = 0` on steering.** A non‑participating battery is still
**served a normal response** every cycle — it is excluded from the aggregation in
step 2, **not** from the round‑robin in step 3 — so it keeps receiving the grid
reading and the aggregates, and can run its own program (e.g. a manual schedule)
while reading the meter. But because its power and its slot are left out of the
per‑phase `*_chrg_power` / `*_dchrg_power` buckets **and** the `*_chrg_nb` count:

- **for that battery:** it is effectively invisible to the shared pool — its
  activity is not reflected back to anyone;
- **for the other batteries on its phase:** the count they divide the per‑phase
  grid by is **smaller** (so each participating battery takes a **larger share**,
  not counting on the opted‑out one to help), and the cross‑talk
  charge/discharge aggregate **omits** the opted‑out battery's contribution.

This is the relay‑mode coordination: opting out removes a battery from the
shared load split without stopping it from being polled. (AstraMeter mirrors the
aggregation/count exclusion, and additionally drops a non‑participating consumer
from its active‑control distribution pool — the real CT is relay‑only and has no
active‑control authority.)

The storage side validates each response (checksum, length, and `info_idx`
de‑duplication) before applying it. This aggregate + sign‑split model matches
the observed captures.

## How the storage side consumes the response

The battery — not the CT — drives the exchange: it is the **UDP master** and
polls the CT on a timer (open socket → send request → parse reply). The same
poll loop also speaks the Shelly EM / Pro JSON API when the configured meter
type (`ct_t`) is a Shelly rather than a CT002/CT003, so the CT response is one
of several interchangeable grid‑power sources feeding the same controller.

Once a response passes validation, the storage turns it into a charge/discharge
command in three steps:

1. **Parse** the 28 fields into a record. Two kinds of numbers are kept: the
   measured per‑phase grid power (`A/B/C_phase_power`, `total_power`) and the
   sign‑split activity buckets (`x` / `A` / `B` / `C` / `ABC`, each with a
   `*_chrg_power` and `*_dchrg_power`).
2. **Select the bucket for this device** using the `phase_t` setting:
   - `phase_t` 1/2/3 → the `A`/`B`/`C` bucket,
   - `phase_t` 4 (and the default) → the combined **`ABC`** bucket,
   - a separate "sum all phases" config flag overrides this to add
     `A + B + C + ABC` (whole‑home total).

   The `x`/unassigned bucket is always carried alongside so the device still
   reacts while it is still detecting its phase. This is the consumer side of
   the [phase selection](#phase-selection-and-the-combined-abc-mode): phase `D`
   is exactly "read the combined bucket".
3. **Regulate** with a closed loop (detailed below). The storage keeps an
   inverter power **setpoint** and, each step, nudges it toward driving the
   selected grid value to zero (self‑consumption) — it does **not** copy the
   reading straight to the inverter.

So a CT reading is an **input to an integral‑style self‑consumption controller**,
not a direct power command. The per‑phase `*_chrg_power` / `*_dchrg_power`
buckets additionally let several batteries on the same phase divide the load
rather than all chasing the same target.

### Steering / ramp logic (simulator‑grade)

This is the exact control law the storage runs on the selected grid value. It is
enough to reproduce the device's behavior bit‑for‑bit. Powers are in **watts**;
the constants below are the literal values used.

> **Model scope.** The float controller documented here is the **Venus class**
> (HMG‑50, VNSE3‑0). It is **not** universal: the **B2500 class** (HMJ) runs an
> **integer‑only** controller (its firmware has no floating‑point unit and no
> float gain table), so while the higher‑level behavior is the same — poll the
> meter, select the phase/combined bucket via `phase_t`, divide by the per‑phase
> device count, and slew an inverter setpoint — its step sizing, deadband and
> clamps use different (integer) values and are **not** the table below. The
> B2500 also has a smaller power envelope than the Venus's ±2500 W. Treat the
> numbers below as Venus‑class; model a B2500 with the same structure but its
> own limits.

**Per‑device persistent state**

| name | meaning |
|------|---------|
| `setpoint` | commanded inverter power (float W); sign is the charge/discharge direction (follows the bucket convention — + discharge / − charge; the inverter‑side polarity was not independently confirmed) |
| `ramp` | direction/acceleration counter, integer, clamped to **−5…+5** |
| `last` | the previous step's `g` (grid value) |
| `ref` | a reference reading captured the last time the error grew |
| `out_min` | running minimum of `g` since the last reset |
| `prev_g`, `prev_out` | previous grid value / own output, for the spike filter |

**Inputs each step**

- `g` — the selected bucket value (this phase, or the combined `ABC` bucket for
  phase `D`; or `A+B+C+ABC` when the "sum all phases" flag is set).
- `nb` — the `*_chrg_nb` count for that bucket (number of batteries sharing it).
- `out` — the battery's own measured output power.
- `hi`, `lo` — the current upper/lower **power limits** (runtime values, e.g.
  derived from the configured max charge/discharge power and SOC headroom).

**Cadence.** A regulation step is gated by a timer (~**3000 ticks**); the steps
below run once per gate, which is why the response visibly ramps rather than
snapping.

```text
# 1. share split across batteries on the same phase/bucket
g = g / nb                         # nb >= 1

# 2. validity / direction debounce
#    - require the meter to be active, else reset the setpoint
#    - a 10-tick debounce rejects a charge<->discharge sign flip until it persists

# 3. deadband + spike filter
if abs(g) < 20 and out < 1:        # +/-20 W deadband
    return                         # hold setpoint, do nothing
if abs(g - prev_g) > 50 and abs(out_as_int - prev_out) < 20:
    skip_one_step()                # transient load step: act on the next sample
prev_g = g; prev_out = out_as_int

# 4. keep the setpoint inside the dynamic power window
if setpoint > hi:                  # above the upper power limit
    setpoint = hi - 100; ramp = -1; goto CLAMP
if setpoint < lo:                  # below the lower power limit
    setpoint = lo + 100; ramp =  0; goto CLAMP

# 5. ramp / direction accumulator
out_min = min(out_min, g)
if g > last:                       # error grew since last step
    ramp = -1 if ramp > 0 else 0   # brake / flip toward decreasing
    ref = out_min; out_min = g
else:                              # error steady or shrinking
    if ramp > 0: ramp = min(ramp + 1, +5)   # accelerate same direction
    else:        ramp = max(ramp - 1, -5)

# 6. step size
step = sqrt(abs(g*g - ref*ref)) + 10
step = min(step, GAIN[ramp])       # gain table below
step = min(step, abs(g))           # never overshoot the remaining error
if ramp > 0: setpoint += step
else:        setpoint -= step

# 7. (only in work mode 5) remap setpoint through a per-step calibration table

CLAMP:
setpoint = clamp(setpoint, -2500, +2500)   # hard limit, ±2500 W (Venus E)
apply_to_inverter(setpoint)
last = g
```

**`GAIN[ramp]` — maximum step per cadence, in watts**

| `ramp` | −5 | −4 | −3 | −2 | −1 | 0 | +1 | +2 | +3 | +4 | +5 |
|--------|----|----|----|----|----|---|----|----|----|----|----|
| W | 410.35 | 350.41 | 180.30 | 60.02 | 50.12 | 50.23 | 50.10 | 50.21 | 100.01 | 200.02 | 400.40 |

Notes for an implementer:
- The cap is ~50 W while `ramp` is near 0 and grows toward ~400 W at `ramp = ±5`,
  so the longer the error persists in one direction the larger each correction —
  a coarse acceleration curve, not a linear gain. The curve is slightly
  asymmetric (charge vs discharge).
- `ramp` only reaches ±5 after several consecutive steps without the error
  growing; any step where `g` increases versus `last` brakes it back toward 0.
- `step` is additionally bounded by `sqrt(|g² − ref²|)+10` and by `|g|`, so near
  convergence the per‑step change collapses to a few watts.
- The `±2500 W` clamp is the absolute hardware limit; the softer `hi`/`lo` window
  (step 4) is what enforces the configured max charge / max discharge power (the
  same limits the BLE/MQTT "set max charge/discharge power" commands write — e.g.
  the discharge cap that rejects values over 800 W).
- The final `apply_to_inverter` hands the setpoint to the power‑stage controller
  across the inverter‑MCU boundary.

> AstraMeter does not implement this storage‑side controller — when AstraMeter is
> the active‑control authority it computes per‑battery targets itself (see below).
> This section documents what a *real* Marstek battery does with the response so a
> faithful battery simulator can be built.

> Sign convention: in the buckets, **negative** power is charging (grid surplus
> to absorb) and **positive** is discharging (load to cover); the controller
> drives the selected phase's net toward zero.

## Active vs. relay control (AstraMeter)

The sections above describe the CT/emulator wire behavior. AstraMeter adds two
operating modes on top:

### Relay mode (ACTIVE_CONTROL = False)

The emulator behaves like a passive relay:
- Forwards per-phase aggregates from the latest known reports, grouped by phase.
- Uses the sign split above (negative → `*_chrg_power`, positive → `*_dchrg_power`).

Each battery then decides its own charge/discharge from the relayed aggregates.

### Active control mode (ACTIVE_CONTROL = True, default)

The emulator becomes the control authority:
- Reads grid/meter power from a configured powermeter (Tasmota, Shelly, etc.).
- Smooths the raw reading (EMA) and splits the target across consumers.
- Sends per-consumer targets in the response fields (`A/B/C_phase_power`, `*_chrg_power`, `*_dchrg_power`).

Additional options refine the control:
- **Fair distribution** — Adjusts each consumer’s target to balance actual load (under‑performers get
  higher targets, over‑performers get lower).
- **Saturation detection** — Reduces share for batteries that cannot follow target (e.g. full or empty).
- **Error boost** — Increases correction gain when offset is large for faster convergence.
- **Error reduce** — Decreases correction gain when offset is small to avoid oscillation.

In this mode, the emulator computes what each battery should do; the batteries follow the targets.

## MQTT runtime‑info frame

> The full MQTT protocol (connection/TLS, topics, the complete `cd` command set,
> every payload) and the CT003 HTTP cloud reporting are documented separately in
> [marstek-mqtt-http.md](marstek-mqtt-http.md). This section is just the
> `cd=1`/`cd=4` frames the AstraMeter responder emulates.

The same CTs also answer the Marstek app over MQTT (this is what the
`mqtt_insights:` Marstek subsection emulates). The CT subscribes to the App
control topic and publishes to the device control topic:

```text
subscribe: marstek_energy/<ct_type>/App/<mac>/ctrl
publish:   marstek_energy/<ct_type>/device/<mac>/ctrl
```

Newer devices use the **`marstek_energy/…`** prefix; the older
**`hame_energy/…`** prefix (after the `hamedata.com` cloud) is used by earlier
devices. AstraMeter subscribes/answers on **both** prefixes.

A poll arrives as a CSV `k=v` body. `cd=1` requests aggregate runtime info;
`cd=4` (+`p1=<id>`) requests the slave list. (The App control channel carries a
wider command set too — e.g. reset, debug‑stream toggles, error clears — but
those are outside the scope of the meter emulator.) The reply omits any `cd=`
echo. The aggregate (`cd=1`) field set **differs between models**:

```text
CT002 (HME-4):
  pwr_a,pwr_b,pwr_c,pwr_t,ble_s,wif_r,fc4_v,ver_v,wif_s,slv_n,cur_d

CT003 (HME-3):
  pwr_a,pwr_b,pwr_c,pwr_t,ble_s,wif_r,fc4_v,ver_v,eng_t,wif_s,slv_n,
  com_t,com_b,ptl_t,smt_n,har_f,sof_f,irs_f,pwr_f,frm_c,upd_t,udp_v
```

- Both start with `pwr_a/pwr_b/pwr_c/pwr_t` (per‑phase + total power) then
  `ble_s` (BLE state), `wif_r` (Wi‑Fi RSSI), `fc4_v` (Wi‑Fi module firmware
  string), `ver_v` (device version).
- CT002 then carries `wif_s` (Wi‑Fi state), `slv_n` (connected slave count) and
  `cur_d`, and stops.
- CT003 inserts `eng_t` (cumulative energy, 64‑bit) after `ver_v`, and after
  `wif_s,slv_n` adds a diagnostics block: `com_t/com_b` (comms), `ptl_t`
  (protocol type), `smt_n` (smart‑meter count), `har_f/sof_f/irs_f/pwr_f`
  (fault flags), `frm_c` (frame counter), `upd_t` (update time), `udp_v`
  (UDP protocol version).

The slave list (`cd=4`) reply uses repeated `;`‑terminated tokens, **capped at
5 entries** and only for occupied slots:

```text
slv_ip=<ip>,slv_t=<type>,slv_p=<phase>,slv_id=<mac>;
```

> **AstraMeter responder vs. a real CT:** the AstraMeter MQTT responder
> (`src/astrameter/mqtt_insights/marstek_mqtt.py`) does **not** reproduce these
> layouts byte‑for‑byte — it emits a tolerant `k=v` superset in a different key
> order, adds `kwh/n_kwh/used_kwh/fed_kwh` placeholders, and formats `cd=4` rows
> as comma‑joined `slv_t,slv_id,slv_ip,slv_p` (no `;` terminator). The app's
> parser (`replaceAll(' ','')` → split) is order‑independent and tolerant of
> extra keys, so this works in practice; the layouts above are the reference for
> what a *real* CT sends, not a spec the responder must match.

## CT MAC behavior

The CT responds when the request `hhm_mac_code` is the wildcard `000000000000`
**or** matches the CT's own MAC. In AstraMeter: if `CT_MAC` is configured, the
emulator only responds when the request `hhm_mac_code` matches `CT_MAC`; if
`CT_MAC` is empty, it accepts requests for any CT MAC and echoes the request CT
MAC back in responses.
