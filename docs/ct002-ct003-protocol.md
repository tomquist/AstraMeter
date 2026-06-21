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

> The AstraMeter emulator mirrors this bucketing: `'0'`/unassigned reporters
> aggregate into the `x_*` fields, phase‑`D` reporters into the `ABC_*` fields
> and the `ABC_chrg_nb` count, and `A`/`B`/`C` into their own buckets. It does
> not yet implement a combined (`ABC`) **control** mode, though: a phase‑`D`
> battery is served the relay path (raw grid reading + aggregates) even when
> active control is on, exactly as during inspection.

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
   (AstraMeter mirrors this by default: a consumer that misses ~2 of its own
   poll cycles drops out of the counts/aggregates; set `CONSUMER_TTL` /
   `consumer_ttl` to use a fixed window instead.)
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

> **Model scope.** The float controller documented here is the **HMG‑50**
> one (Venus C; **not** Venus D — see the VNSD‑0 note below). It is **not**
> universal:
> - The **VNSE3‑0** (Venus E) shares the **same step‑3 conditioning gate** — the
>   same >50 W spike filter, <20 W own‑output exemption, signed deadband and
>   small‑import hold — but with a tighter **±10 W** deadband instead of ±20 W,
>   and it uses a **different ramp/step law** (no float gain table, integer
>   setpoint). So the gate logic carries over, but the gain table and ramp
>   arithmetic below are HMG‑50‑specific.
> - The **VNSD‑0** (Venus D) does **not** run this float law at all — **none**
>   of the float gain‑table / `sqrt`‑step constants apply. Its CT‑following
>   controller is **integer** and built as a configurable proportional
>   **integrator**, run per CT response:
>   `setpoint += (ctrl_ratio/100)·error − 5 W`, where `error = g − grid_standard`
>   (the configured grid setpoint offset). The per‑step branches are
>   sign‑conditioned on `error` and the device's own measured grid; the result
>   is clamped to the configured charge / discharge limits (defaults +2200 W /
>   −800 W) and zeroed inside a **±11 W** (single‑phase) / **±15 W** (combined)
>   deadband. `ctrl_ratio` is the loop gain (30–100 %, default **100** ⇒ unity,
>   i.e. one step ≈ `error − 5`). There is **no** −5…+5 gain‑scheduled ramp, no
>   `sqrt` step and no float ±2500 W clamp — so a sustained 500 W import ramps
>   ~495 W per cycle straight to the charge clamp, not the HMG‑50's ~50 W
>   near‑zero step. (The fine power slewing is delegated to a separate inverter
>   sub‑processor reached over the internal bus.)
> - The **B2500 class** (HMJ) is a **DC‑coupled** unit (PV/DC in, DC out to one
>   or two external microinverters), so it steers its **DC output power** rather
>   than an AC inverter setpoint. Its controller is **integer‑only** and
>   **table/hysteresis‑based** — none of the float gain table, the `sqrt` step,
>   or the step‑3 spike filter apply. It is documented separately under
>   **"B2500‑class (HMJ) DC‑output steering"** below. (SOC and temperature are
>   handled by a *separate* BMS — charge‑current derating, cell‑voltage limits —
>   and are **not** part of its steering loop.)

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

> **AstraMeter note (issue #459).** The share-split is why AstraMeter's
> *active control* reports `*_chrg_nb = 1` and must keep doing so: it sends
> each battery an **individual** target in the phase-power field, so a real
> count `N` would make every battery divide its already-individual target by
> `N` and under-respond by that factor. Only *relay mode*
> (`ACTIVE_CONTROL = False`), which forwards the per-phase **aggregate**,
> reports the real count so the batteries do this `g / nb` split themselves.

```text
# 1. share split across batteries on the same phase/bucket
g = g / nb                         # nb >= 1

# 2. validity / direction debounce
#    - require the meter to be active, else reset the setpoint
#    - a 10-tick debounce rejects a charge<->discharge sign flip until it persists

# 3. input-conditioning gate (spike filter, deadband, small-import hold)
#    prev_g / prev_out are updated FIRST, on every cycle (held samples too).
prev_g = g; prev_out = out_as_int
if abs(g) > 20 and abs(g - prev_g_old) > 50 and abs(out_as_int - prev_out_old) < 20:
    return                         # >50 W spike the own output can't explain: skip.
                                   # No one-shot — a sustained drift whose own
                                   # output never moves keeps being skipped.
if abs(g) < 20 and out < 1:        # +/-20 W deadband (SIGNED out: a charging
    return                         # battery reads out < 0 and is held too)
if 0 <= g < 10:                    # small residual import: hold even while
    return                         # producing, don't chase the last few watts

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
- Two subtleties the step‑3 gate's one‑line summaries hide: the deadband's
  `out < 1` is a **signed** comparison (a charging battery reads `out < 0` and is
  also held), and the spike filter has **no** one‑shot — `prev_g`/`prev_out`
  advance every cycle, so a sustained drift whose own output never moves is
  skipped every cycle, while a one‑off blip is gone from the baseline by the next
  sample.

> AstraMeter does not implement this storage‑side controller — when AstraMeter is
> the active‑control authority it computes per‑battery targets itself (see below).
> This section documents what a *real* Marstek battery does with the response so a
> faithful battery simulator can be built.

> Sign convention: in the buckets, **negative** power is charging (grid surplus
> to absorb) and **positive** is discharging (load to cover); the controller
> drives the selected phase's net toward zero.

### B2500‑class (HMJ) DC‑output steering (simulator‑grade)

The B2500 is **DC‑coupled**: PV/DC in, DC out to one or two external
microinverters. It self‑consumes by reading the meter and steering its **DC
output power** per channel — there is no AC inverter loop and **none** of the
Venus float ramp law above applies. The controller is **integer‑only** and built
from a meter‑derived setpoint feeding a per‑channel hysteresis regulator. It is
enough to reproduce the device's output behavior.

> **Keep this separate from the BMS.** SOC (interpolated from cell voltage) and
> temperature drive a *charge‑current derating* curve and cell‑voltage limits in
> a **different** subsystem. Those never enter the steering loop below — do not
> fold an SOC/temperature term into the output setpoint.

**Per‑channel persistent state** (the B2500 has **two** independent DC outputs,
each with its own copy; steps 2–4 run per channel):

| name | meaning |
|------|---------|
| `setpoint` | desired output power for this channel, W (from the meter) |
| `target` | ramped intermediate that chases `setpoint` (AC‑active path only) |
| `cmd` | internal output command being slewed; calibrated, then applied |
| `power` | measured output power = `V × I / k` (per‑channel volts × amps) |
| `state` | per‑channel state machine (0–4: off / starting / regulating / …) |

**Cadence.** One regulation pass per poll cycle, like the Venus.

```text
# 1. setpoint from the residual grid (per cycle) -- INCREMENTAL
#    `grid` is the residual grid power read back (positive = import). The setpoint
#    is the current output plus 90% of the residual, so the loop *integrates* the
#    grid toward zero (fixed point output = load). A proportional `0.9 * grid`
#    would droop to ~47% of load and never null the grid; the device reaches full
#    self-consumption because it drives off an accumulated meter value. The 90%
#    per-step gain is the firmware's; the +output makes it integral.
setpoint = power + grid * 9 / 10           # 90% of the residual, added to output
setpoint = clamp(setpoint, 0, max_power)   # never negative (no AC charge), capped
#    (per channel: half this, since the two outputs split the demand)

# 2. measured output power for the channel
power = volts * amps / k                   # k a fixed unit divisor

# 3. feedback conditioning (deadband smoothing, only once stable ~500 cycles)
if abs(power - setpoint) < 10:   power = setpoint            # snap within +/-10 W
elif abs(power - setpoint) <= 20: power += sign(setpoint-power) * 10   # else step 10 W

# 4. per-channel hysteresis regulator (every cycle)
#    `cmd` is an INTERNAL command unit, not watts; it steps by +/-100.
if   power > setpoint + 10:  cmd -= 100     # +/-10 W deadband (on measured power)
elif power < setpoint - 10:  cmd += 100     # +/-100 internal-cmd step per cycle
# else: hold
applied = (cmd - 5) * 10 / 59 + cal[mode]   # command -> output (watts), per-mode cal
drive_converter(applied)                    # hand to the DC power stage
```

Notes for an implementer:
- **`cmd` is not watts; the effective output slew is ~17 W/cycle.** The `× 10 /
  59` in the calibration means a ±100 `cmd` step moves the *output* by only
  `100 × 10 / 59 ≈ 17 W`. So in a watt‑domain model, either keep `cmd` internal
  with `output = (cmd − 5) × 10 / 59`, or model the output directly as a bounded
  integrator that **slews ~17 W/cycle** toward `setpoint` with a **±10 W
  deadband**. (The `k` in step 2 is the device's V·I → W conversion; a
  watt‑domain `power` takes it as identity.)
- **The setpoint is incremental, not absolute.** `output + 0.9 × grid` integrates
  the residual to zero. Modelling it as an absolute `0.9 × grid` is a subtle bug:
  with closed‑loop feedback (`grid = load − output`) it parks at ~47% of load and
  the grid never nulls.
- The **±10 W deadband** plus the slew is the integer analog of the Venus
  deadband+ramp; there is **no** acceleration counter, no `sqrt`, and no spike
  filter. The response is a plain bounded integrator toward `setpoint`.
- **Clamp the command (anti‑windup).** On the device the `power` feedback is the
  *measured* output, which saturates at the inverter limit — so once the output
  is capped, `power ≈ setpoint` and the command stops growing. A watt‑domain
  model whose feedback is a separately‑clamped output must add the clamp
  explicitly (`cmd` bounded so its output stays within `[0, max_power]`), or the
  integrator winds up while the output is pinned and then recovers at only
  ~17 W/cycle.
- **At full SoC the output can't drop below the PV throughput.** When the pack is
  full, incoming PV passes straight through to the output (it has nowhere else to
  go), so the effective setpoint is floored at the PV power; the grid steering
  can't curtail below it (it exports the surplus, which a co‑resident AC battery
  absorbs). Don't let the steering fight that floor.
- **Two control variants exist.** The above is the normal path. When the AC line
  is in a specific window the device runs an **AC‑active** path that additionally
  (a) averages the channel current over 5 samples with the min and max dropped,
  and (b) ramps a `target` toward `setpoint` in **±40 W** steps (gated by a
  ~100‑cycle timer and `power ≥ 20 W`) before regulating to it. Model the normal
  path first; the AC‑active path only makes the approach slower/smoother.
- The power envelope is the **B2500's** (≈800 W class), **not** the Venus's
  ±2500 W. `max_power` in step 1 is that envelope.
- Everything is integer (16‑bit watt‑scale values); there is no float state.

**Reference trajectories.** Check an implementation against these. Both start
from `cmd = 60` (output ≈ 9 W) and let the output feed back each cycle
(`power := previous output`); `mode = 0`, `cal = 0`.

*Regulator (GOLDEN)* — fixed `setpoint = 300 W`, watching `(cmd, output)` per
cycle as it slews up at ~17 W/cycle and parks once `|output − setpoint| ≤ 10`:

```text
cmd:    160  260  360  460  560  660  760  860  960 1060 1160 1260 1360 1460 ...
output:  26   43   60   77   94  111  127  144  161  178  195  212  229  246 ... -> 297 (hold)
```

*Closed loop* — the full `step` with the incremental setpoint against a fixed
**300 W load** (`grid = load − output` each cycle). The output integrates up and
parks once it offsets the load (grid within the deadband), confirming the loop
nulls the grid rather than drooping:

```text
output: 26 43 60 77 94 111 127 144 161 178 195 212 229 246 263 280 297 -> 297 (hold, grid≈3)
```

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
