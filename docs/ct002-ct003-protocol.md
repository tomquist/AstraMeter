# CT002 / CT003 UDP Protocol

This document summarizes the CT002/CT003 protocol based on community reverse‑engineering and the reference scripts in:
- https://github.com/rweijnen/marstek-venus-e-firmware-notes
- https://github.com/d-shmt/hass_marstek-smart-meter

Capture-based findings for issue #111 are documented in:
- [ct002-capture-analysis.md](ct002-capture-analysis.md)

All CTs share the **same protocol**. What differs is the CT type value a
battery puts in its requests and the CT names in its replies:

| CT | Type |
|----|------|
| CT002, original hardware (TPM-100CTW) | `HME-4` |
| CT002, newer hardware (TPM2-100CT) | `TPM2-0` |
| CT003 | `HME-3` |

A battery picks the type from its meter‑type setting. The Venus E 3.0
firmware (`VNSE3-0` v150) maps setting 3 to `HME-4`, 4 to `HME-3`, 11 to
`TPM2-0`, and 8–10 to `SMR-0`, `SMR-1` and `SMR-2`. It talks UDP to port 12345
for all six. We don't know which products the `SMR-*` types belong to.

CT003 and the newer CT002 (`TPM2-0`) also carry cumulative energy data that
the original CT002 (`HME-4`) does not (see [Cumulative energy
fields](#cumulative-energy-fields-fields-2528)).

The CT‑side behavior described under [Transport](#transport), [What the CT
accepts](#what-the-ct-accepts), [Aggregation, eviction and response
cadence](#aggregation-eviction-and-response-cadence) and [CT MAC
behavior](#ct-mac-behavior) was read from the decompiled CT firmware: CT002
(`HME-4`) v120 and v124, and CT003 (`HME-3`) v116, v117, v118 and v122, from
the firmware archives [rweijnen/marstek-firmware-archive][fw-archive] and
[sphings79/marstek-firmware-archiv][fw-archive2]. The images are ARM Cortex‑M
application images that load at `0x08006800`. Where a behavior differs
between versions, the text names the versions.

No `TPM2-0` firmware is public. What is known about it comes from captures in
[#544](https://github.com/tomquist/astrameter/pull/544):
- it answers a request from a battery MAC it has never seen;
- it reports a battery with phase `0` and `participate = 0` in no bucket;
- it sends the 28‑field reply.
Everything else here is verified for `HME-4` and `HME-3` only.

## Transport

- **Protocol:** UDP
- **Port:** `12345` on the CT.
- **Direction:** Storage system (consumer) sends a request to the CT. CT replies with measurements.
- **Where the reply goes:** to the IP address and **source port the request
  came from**. The CT stores both in the battery's slot and sends the reply
  there. Batteries happen to send from port `22222` (see the
  [capture analysis](ct002-capture-analysis.md)), but a request from any port
  is answered on that port.
- **No traffic of its own:** the CT never broadcasts, and every battery
  protocol datagram it sends is a reply to a request. The only other UDP send
  in the images is CT003's plain‑text `getkwhInfo:…` energy report. We haven't
  found what triggers it, so treat it as unused.

The CT's network stack is not on its main MCU. UDP runs on its Quectel Wi‑Fi
module, which the MCU drives with AT commands: the socket is opened as a
`"UDP SERVICE"` on local port 12345, and each incoming datagram arrives as a
`+QIURC: "recv"` notice carrying the sender's IP and port.

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
The length field counts itself, so compute it iteratively until the digit count matches. The CT does the
same: it sizes the body, then adds the framing/checksum bytes, plus one extra byte once the length crosses
from two to three digits.

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
| 3 | **hhm_dev_type** | string ≤ 8 | CT type (`HME-4`, `HME-3` or `TPM2-0`) |
| 4 | **hhm_mac_code** | string ≤ 13 | CT MAC (12 hex chars) |
| 5 | **phase** | single char | `A`/`B`/`C` = physical phase; `D` = **combined** (see "Phase selection"); `0`/empty = **unassigned / inspection** |
| 6 | **phase_power** | 16‑bit signed | watts for the phase in field 5 (range −32768…32767) |
| 7 | **participate** | unsigned byte | **optional**; `0` = do **not** aggregate this reporter, non‑zero = include. **Defaults to `1`** when the field is absent |

Fields 1–6 appear in all observed traffic. Field 7 is the newer "UDP protocol v4"
addition, and older senders may omit it; the CT then treats it as `1`. Whether a
sender includes it is **model‑dependent**: the Venus class (HMG‑50, VNSE3‑0)
builds only fields 1–6, while the B2500 class (HMJ) builds a 7th field — its
request template carries an extra trailing integer. The Venus class sends power
as a 16‑bit value and the B2500 class as a 32‑bit value; both fit the same
ASCII wire field.

On the CT side, only CT002 v124 and CT003 v122 read field 7. The older images
(CT002 v120, CT003 v116–v118) read fields 1–6 only. They still accept a request
that carries a 7th field and simply ignore it, so for them every battery
participates.

### What the CT accepts

The CT reads a request with
`SOH STX %hd|%10[^|]|%30[^|]|%8[^|]|%13[^|]|%c|%hd|%hhu ETX` after checking
the checksum. That is where the field widths in the table come from. It
answers a request only when all of the following hold:

- **The checksum matches** and fields 1–6 are present.
- **`hhm_mac_code` is the wildcard `000000000000` or the CT's own MAC** (see
  [CT MAC behavior](#ct-mac-behavior)).
- **`hhm_dev_type` matches the CT's own type**, so `HME-4` reaches only an
  original CT002 and `HME-3` only a CT003. A newer CT002 likewise ignored
  requests until they named `TPM2-0` (#544). The comparison runs over the
  length of the request's value, so it is strictly a prefix match. Every real
  battery sends the full type, which is the only form worth relying on.

Nothing else is checked. The CT does **not** validate `meter_dev_type` or
`meter_mac_code`: it stores whatever arrives. It also keeps no list of
batteries paired in the app, so it answers any battery that passes the checks
above, provided a slot is free (see [Aggregation, eviction and response
cadence](#aggregation-eviction-and-response-cadence)). A `phase` other than
`A`–`D` is stored as `0`.

A datagram whose payload starts with `Hello` is not parsed as a request. The
CT counts it and keeps a copy, apparently for diagnostics, and never replies
to it.

> **AstraMeter's emulator is more lenient** on both stacks. It checks only
> `hhm_mac_code` (see [CT MAC behavior](#ct-mac-behavior)), answers any
> `hhm_dev_type`, and has no slot limit. The one request it drops is
> AstraMeter's own: the `[MARSTEK_METER]` source polls a real CT as a battery
> whose MAC starts with `02a57e`, and the emulator never answers that MAC, so
> the source can't end up reading an emulator's output.

### Phase handling and inspection mode

- The CT accepts `A`, `B`, `C` (physical phases) and `D` (combined / 合相 — see
  [Phase selection](#phase-selection-and-the-combined-abc-mode)) as committed phase labels.
- Any other value (observed: `0`, empty) is the **`'0'` unassigned bucket** — the
  **inspection mode** a device uses while it is still determining which phase it is on.
- The emulator:
  - **Responds** to the request (so the device can continue its phase detection),
  - **Does not** add an unassigned/inspection reporter's power to a committed‑phase aggregate,
  - **Leaves it out of the steering pool** under active control: it is answered with the
    raw reading, never a target, so it takes no share and is no reference the steered
    batteries are balanced toward (issue #732),
  - **Adds its reported power back** onto the grid value active control steers on, so
    the steered batteries do not cancel its detection probe (see
    [Battery-side phase detection](#battery-side-phase-detection)),
  - **Resets the meter's conditioning wrappers** (Hampel window, EMA, deadband) on every
    inspection poll and once more when the probe ends — see below, and
  - **Does not answer** it while the meter is unavailable: the zero hold steered
    batteries get would read as a move to its phase detection, so it keeps its last
    real reading instead (see
    [Battery-side phase detection](#battery-side-phase-detection)).
- Inspection is **not** only a startup state: a Venus on firmware 1.50 was observed
  re‑running it roughly every 35 minutes. The routine is a *probe* — the battery takes
  itself off the CT, steps its own output by up to 2000 W in one direction and watches
  which phase of the CT reading follows; see
  [Battery-side phase detection](#battery-side-phase-detection). That grid swing is real,
  and it is the whole point, so a rolling‑median filter must not reject it as a spike.
  Such a filter would answer the probe with a frozen pre‑probe reading, then reject the
  true readings that come back, steering the battery against an inverted grid sign once
  it commits its phase (issue #652).
- The `participate` flag (field 7) is an additional, explicit gate: even a fully phase‑committed reporter
  is **excluded from aggregation** when it sends `0`.

### When a battery sends `participate = 0`

The flag lets a battery opt out of being counted by the CT. The Venus class
(HMG‑50, VNSE3‑0) never sends the field, so the CT always treats it as
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
storage‑then‑CT order. The captures, the CT and the storage side all confirm this; the
storage side reads token 0 as its `meter_dev_type`, which is the CT type. It
matches AstraMeter's `RESPONSE_LABELS`.

The numeric section uses **four phase buckets plus one "unassigned" bucket**:

- `x_*` — the **`'0'`/unassigned** bucket: reporters that have not committed to a phase (inspection).
- `A_*` / `B_*` / `C_*` — the three **physical phases**.
- `ABC_*` — the **combined / 合相** bucket: a reporter balancing against the **sum of all three phases** (see "Phase selection" below).

| # | Field | Type / width | Meaning |
|---|-------|--------------|---------|
| 1 | **meter_dev_type** | string | CT/meter type (`HME-4`, `HME-3` or `TPM2-0`) |
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
| 25 | **low_price_ele_in** | 32‑bit unsigned | (`HME-3`, `TPM2-0`) off‑peak import energy; all import on `TPM2-0` |
| 26 | **normal_price_ele_in** | 32‑bit unsigned | (`HME-3`, `TPM2-0`) peak import energy; always `0` on `TPM2-0` so far |
| 27 | **low_price_ele_out** | 32‑bit unsigned | (`HME-3`, `TPM2-0`) off‑peak export energy; all export on `TPM2-0` |
| 28 | **normal_price_ele_out** | 32‑bit unsigned | (`HME-3`, `TPM2-0`) peak export energy; always `0` on `TPM2-0` so far |

¹ **Field‑width difference between models:** on **CT002 (`HME-4`)** the three
per‑phase powers (fields 5–7) are **16‑bit signed** (±32767 W); the total
(field 8) is 32‑bit. On **CT003 (`HME-3`)** all four are 32‑bit. The widths on
`TPM2-0` are unknown.

² The charge/discharge breakdown (fields 15–24) is **16‑bit signed on `HME-4`
and `HME-3`**, so each value saturates at ±32767 W.

These names match `RESPONSE_LABELS` in `src/astrameter/ct002/protocol.py`. In
ordinary single/three‑phase traffic the `x_*` and `ABC_*` buckets are usually
`0`, because nobody is mid‑inspection or in combined mode. That is why earlier
capture analysis saw the 4th slot as "always 0".

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
it into the `ABC` bucket and reads back `ABC_chrg_power` / `ABC_dchrg_power`. It
therefore balances against the **sum of all three phases** (whole‑home net grid
power) rather than its own phase. This is the mode behind a B2500 reporting
phase `D`: the meter is wired across a 3‑phase supply and the battery
compensates the **total** grid exchange, not a single phase. The `x` bucket is
the transient state while a device is still detecting its phase (`phase_t = 0`).

> The AstraMeter emulator mirrors this bucketing: `'0'`/unassigned reporters
> aggregate into the `x_*` fields, phase‑`D` reporters into the `ABC_*` fields
> and the `ABC_chrg_nb` count, and `A`/`B`/`C` into their own buckets. A phase‑`D`
> battery is a valid, **actively‑steered** operating mode. Under active control it
> receives a per‑consumer target in the summed grid field (`total_power`, field 8) with an
> `ABC_chrg_nb` count of `1`, so it applies the target as‑is instead of dividing
> by `N`. Its instructed net power aggregates into the `ABC_*` cross‑talk
> fields — exactly like an `A`/`B`/`C` battery. The emulator treats only
> `'0'`/unassigned reporters (still detecting their phase) as inspection and serves
> them the raw relay path.

### Cumulative energy fields (fields 25–28)

CT003 (`HME-3`) and the newer CT002 (`TPM2-0`) — but **not** the original CT002
(`HME-4`) — append **four** trailing unsigned‑32‑bit fields, making the reply
**28 fields** instead of 24. Their names are `low_price_ele_in`,
`normal_price_ele_in`, `low_price_ele_out` and `normal_price_ele_out`:
off‑peak/peak import and off‑peak/peak export.

**Scale: 0.1 Wh per count.** Two independent sources agree:
- **CT003 firmware:** the CT003 reads these registers from a connected P1
  smart meter, from the standard DSMR registers `1-0:1.8.1`/`1.8.2` (import)
  and `1-0:2.8.1`/`2.8.2` (export). Its parser reads each `kWh` value as
  integer and fraction and stores `kWh × 10000 + fraction × 10`. That is
  0.1 Wh per count, provided the meter sends the usual three decimals. (We
  traced the parse but not the copy into the reply.)
- **`TPM2-0` captures** (#544): fields 25 and 27 rise by 0.1 Wh per count as
  energy is imported and exported. Fields 26 and 28 have stayed `0`, and the
  counters change only about every three minutes.

The AstraMeter emulator does not source a smart meter and does not emit these
fields.

## Aggregation, eviction and response cadence

The CT keeps a table of battery slots, keyed by battery MAC:

| Model | Slots |
|-------|-------|
| CT002 (`HME-4`) v120 and later | **15** (the v120 release notes: "raised the number of connectable slave devices to 15") |
| CT003 (`HME-3`), every archived image (v116–v122) | **9** |
| CT002 (`TPM2-0`) | unknown |

Each accepted request updates the matching slot or takes a free one. The slot
stores the reporter's IP and source port, type, phase, signed power and
`participate` flag. The request also resets the slot's age and marks the slot
as waiting for a reply. When every slot is taken, a request from a new battery
is dropped without a reply.

The CT runs three jobs over the slot table:

1. **Evicts stale slots.** Every 30 s the CT adds one to the age of every
   occupied slot and clears any slot whose age has reached 2. Each request
   resets the age to 0, so a battery that stops polling is dropped **30–60 s
   after its last request**. (Earlier versions of this document said "within
   ~1–2 cycles"; the firmware counts 30 s steps, not polls.) AstraMeter's
   default is much stricter: a consumer drops out once it misses ~2 of its own
   poll intervals (at least 5 s). Set `CONSUMER_TTL` / `consumer_ttl` to use a
   fixed window instead; 60 s matches the real CT's upper bound.
2. **Builds per‑bucket aggregates** over the live slots, but only for a slot that
   is occupied **and** has `participate != 0`:
   - bucket by phase (`A`/`B`/`C`, the combined `ABC` bucket for phase `D`, or
     the `x` bucket for the `'0'`/unassigned phase), and
   - **sign‑split** the power: negative → that bucket's `*_chrg_power`,
     positive → its `*_dchrg_power`; bump the bucket's `*_chrg_nb` count. The
     `x` bucket has no count, so an unassigned reporter adds only its power. A
     reporter with phase `0` and power `0` therefore leaves every count and
     sum unchanged, on every firmware version.
3. **Sends one reply per request.** The CT walks the slots in turn and answers
   the next one that is waiting. Each requester gets its own unicast reply, to
   the IP and port stored in its slot. A slot that sent no new request since
   its last reply gets nothing, and a second request before the reply does not
   queue a second one. After each reply the CT waits for the Wi‑Fi module to
   confirm the send (giving up after 1 s). CT002 v124 and CT003 v122 then
   **pause 500 ms** before serving the next slot; the older images move on
   straight away. A current CT therefore sends at most about **two replies per
   second, shared by all batteries**. With N batteries polling faster than
   that, each gets a reply roughly every N × 0.5 s.

The reply's measurement fields (`A/B/C_phase_power`, `total_power`) are the
same for every requester. Only the identity fields and the aggregates depend
on the slot table.

If no request passes the checks in [What the CT accepts](#what-the-ct-accepts)
for **30 minutes**, the CT logs `UDP:RESET` and reboots. This is present in
every archived image.

All timings above come from the firmware's 2 kHz timer interrupt (prescaler
47, period 499), which drives a 1 ms and a 1 s counter. They assume the 48 MHz
clock that prescaler implies.

**Effect of `participate = 0` on steering.** The CT still **serves a normal
response** to a non‑participating battery every cycle: step 2 leaves it out of the
aggregation, but step 3 still answers its requests. It keeps receiving
the grid reading and the aggregates, so it can run its own program (e.g. a manual
schedule) while reading the meter. But its power and its slot stay out of the
per‑phase `*_chrg_power` / `*_dchrg_power` buckets **and** the `*_chrg_nb` count, so:

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
type (`ct_t`) is a Shelly rather than a CT002/CT003. The CT response is
therefore one of several interchangeable grid‑power sources feeding the same
controller.

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

   The `x`/unassigned bucket is parsed alongside, but a device that is still
   detecting its phase does not steer at all (see
   [Battery-side phase detection](#battery-side-phase-detection)). This is the
   consumer side of the
   [phase selection](#phase-selection-and-the-combined-abc-mode): phase `D` is
   exactly "read the combined bucket".
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

> **Model scope.** The float controller documented here is the **HMG‑50** one
> (Venus C), and it is HMG‑50‑**only**. It is **not** universal — and an
> HMG‑50 talking to AstraMeter does **not** run it either. That firmware
> carries two laws and selects between them on a model code it parses from the
> CT meter's greeting, taking the float ramp only for code `1` (the "no model
> suffix" fallback). AstraMeter announces `HME-4`, so a real HMG‑50 driven by
> it runs the **same integer integrator as the Venus A/D/E**, differing only in
> its rest deadband (±20 W) and its single‑unit park.
> - The **VNSA‑0**, **VNSD‑0** and **VNSE3‑0** (Venus A / D / E) all run **one
>   shared law** instead — an integer proportional **integrator** behind an
>   input‑conditioning gate — and **none** of them contains any of the eleven
>   float gain‑table constants (all eleven are present in all ten archived
>   HMG‑50 images). Verified against the archived Control images in
>   [rweijnen/marstek-firmware-archive][fw-archive] and
>   [sphings79/marstek-firmware-archiv][fw-archive2]: the VNSD‑0 and VNSE3‑0
>   integrator, gate and share split are the same code instruction for
>   instruction, the VNSA‑0 integrator and gate match them, and the law is
>   unchanged across VNSE3‑0 v144/v148/v1476/v150 and VNSD‑0
>   v147/v149/v1492/v150. (These are app images flashed above a bootloader, so
>   they load at `0x08004800`, not `0x08000000` — code offsets are listed in
>   `venus_integer_steering.py` as file offsets to keep that ambiguity out.)
>
>   Per CT response: `setpoint += (ctrl_ratio/100)·g − 5 W`, where
>   `g = bucket − grid_standard` divided by the bucket's `*_chrg_nb` count. The
>   per‑step branches are sign‑conditioned on `g` and on the unit's **own
>   measured output**; the result is clamped to the configured charge /
>   discharge limits (defaults +2200 W / −800 W) and parked inside **±11 W**
>   (alone on the bucket) / **±15 W** (sharing it). `ctrl_ratio` is the loop
>   gain (30–100 %, default **100** ⇒ unity, i.e. one step ≈ `g − 5`). There is
>   **no** −5…+5 gain‑scheduled ramp, no `sqrt` step and no float ±2500 W clamp
>   — so a sustained 500 W import ramps ~495 W per cycle straight to the clamp,
>   not the HMG‑50's ~50 W near‑zero step. (The fine power slewing is delegated
>   to a separate inverter sub‑processor reached over the internal bus.)
>
>   The gate is this step‑3 gate with a tighter **±10 W** deadband, and its
>   spike filter is a **one‑shot**: the sample after a skipped one is forced
>   through, bypassing the deadband and small‑import hold. A shared bucket
>   (`*_chrg_nb > 1`) skips the spike filter entirely.
>
>   The integrator adds to its **own stored setpoint**, never to its measured
>   output, so a command too small for the inverter to execute still accumulates
>   until it is large enough — every other write to that RAM word is a reset to
>   zero. (A B2500 is the opposite; see the HMJ section.) Modelled in
>   `src/astrameter/simulator/venus_integer_steering.py`, which cites the
>   firmware addresses for each part.
>
>   [fw-archive]: https://github.com/rweijnen/marstek-firmware-archive
>   [fw-archive2]: https://github.com/sphings79/marstek-firmware-archiv
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

**Cadence.** A timer gates each regulation step (~**3000 ticks**). The steps
below run once per gate, which is why the response visibly ramps rather than
snapping.

> **AstraMeter note (issue #459).** The share-split is why AstraMeter's
> *active control* reports `*_chrg_nb = 1` and must keep doing so. It sends
> each battery an **individual** target in the phase-power field. A real
> count `N` would make every battery divide that already-individual target by
> `N` and under-respond by that factor. Only *relay mode*
> (`ACTIVE_CONTROL = False`) forwards the per-phase **aggregate**, so only it
> reports the real count and lets the batteries do the `g / nb` split themselves.

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
- Two subtleties the step‑3 gate's one‑line summaries hide. First, the deadband's
  `out < 1` is a **signed** comparison: a charging battery reads `out < 0` and is
  also held. Second, the spike filter has **no** one‑shot — `prev_g`/`prev_out`
  advance every cycle. So a sustained drift whose own output never moves is
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

## Battery-side phase detection

A battery works out which phase it is wired to by itself. The CT plays no part
beyond answering: CT003 (`HME-3` v116–v122) parses the request with
`%hd|%10[^|]|%30[^|]|%8[^|]|%13[^|]|%c|%hd|%hhu` and stores the phase character
it is sent; its slave list reports it back as `slv_p=%c`. No CT image contains
a battery type name, so a CT cannot treat one model differently by name.

> Read out of the archived Control images: HMG-50 (Venus E 2.0) v156 and
> VNSE3-0 (Venus E 3.0) v151 disassembled, the B2500 `HMJ-2` v110 routine read
> separately (below). VNSD-0 (Venus D) v151 was compared with VNSE3-0 v151
> (below); VNSA-0 was not. The simulator reimplements the Venus routines in
> `src/astrameter/simulator/phase_detection.py`, which lists the code sites.

### The phase byte

The battery stores its phase as a byte: 1–3 = A–C, 4 = D (combined), 0 =
unset, 5 = detection failed. The request builder sends `A`/`B`/`C`/`D` for 1–4
and `0` for everything else. **While the byte is 0 or 5 the regular steering
does not run at all**: the meter-task loop returns before the integrator, so
the battery only does what the detector commands. Phase 5 is never saved.

### When detection runs

- At boot, when no phase is stored.
- When the meter type or the CT MAC is changed, over BLE or MQTT.
- On an MQTT `seq_check` command.

`rechrg_seq=N` (MQTT, also over BLE) sets the phase directly and ends any
detection. The triggers were read on the HMG-50 only, and none of them runs on
a timer there; what re-ran detection about every 35 minutes on a Venus with
firmware 1.50 (issue #652) is not known.

### One attempt (HMG-50 and Venus)

Each pass of the meter-task loop runs the detector. The loop wakes on every
accepted CT reply and otherwise after 100 ms (HMG-50) / 30 ms (VNSE3-0), and
re-evaluates the **last accepted reading** each time. All timers count whole
seconds.

| step | HMG-50 v156 | VNSE3-0 v151 |
|---|---|---|
| Gate, checked first on every pass | above 50 % SoC one BMS flag must be set, at or below another; otherwise nothing happens, no timer runs | same |
| 1. Zero | output to 0 W (0.01 W) | output to 0 W |
| 2. Baseline | on the next pass: the last reading's A, B, C and total | after 3 s |
| 3. Probe | +2000 W (discharge) above 50 % SoC, −2000 W at or below | ± the configured power `P`, same rule; meter type 10 always charges |
| Threshold | min(upper power window, 2000) / 2; a negative window reads as 0 | min(`P`, 2000) / 2 |
| A phase *moved* when | threshold ≤ \|reading − baseline\| ≤ 2500 W | threshold ≤ \|reading − baseline\| |
| Decision | move counter == 2 on a pass with a move | move counter ≥ 2 on a pass with a move |
| One phase moved | that phase | that phase |
| Several moved | phase 5, detection **ends, no retry** | the highest-numbered phase that moved |
| No phase moved, total did | phase D, only for CT003 (and meter codes 5–9) and only while all three phase readings are exactly 0 | phase D, every meter type but 0, 1, 13 |
| Timeout from probe start | 20 s; 40 s for CT003 | 20 s; 40 s for CT003 and a few others; 60 s for one |
| On timeout | phase 5, output 0, retry after 60 s | same |
| After 3 attempts | detection stops for good, phase stays 5 | same |

The move counter counts phases, not readings, and every pass re-reads the same
reading. So one reading that shows exactly one phase moved decides on the next
pass, 100 ms later at most. A reading where two phases moved adds 2 at once:
the HMG-50 then ends with phase 5 and the Venus picks the higher phase. On the
HMG-50, a counter that passes 2 without hitting it exactly cannot decide any
more, and the attempt can only time out.

The two gate flags are BMS words the firmware tests: one above 50 % SoC, the
other at or below. Pairing them with the probe direction suggests "discharge
allowed" and "charge allowed", but that meaning is inferred. Likewise `P` is a
configured power word of unconfirmed meaning (the simulator uses the discharge
limit).

A battery that fails detection therefore reports phase `0`, holds about 0 W
and never steers, until it is rebooted (a boot with no stored phase starts
detection again) or one of the triggers above fires.

### What a battery reports while detecting

Detection leaves the work mode alone, so the request's power field keeps doing
what it always does:

- **HMG-50 / VNSE3-0**: in work mode Auto (0) or AI (5) — or with one internal
  flag set — the power field carries a status value; in Manual (1) or Passive
  (3) it is 0. On the HMG-50 that value is the one a debug print labels
  "发出功率" (sent / output power), next to "设置功率" (set power) and
  "实际功率" (actual power); on the VNSE3-0 it is the status word the steering
  also reads as the unit's own output.
- **B2500 (HMJ-2 v110)**: always the sum of its two DC output channels — the
  fields its runtime report calls `g1` and `g2`, which hm2mqtt shows as the
  output powers — with no mode condition.

A real device confirms it: in the issue #652 log a Venus E 3.0 on firmware 1.50
polled phase `0` reporting 0 W for about five seconds, then 232, 1603, 1941 and
1993 W, held 1993–1994 W, and came back on phase A. A battery whose firmware
reports something else (the Venus E Mini sends 1 W throughout) gives an
emulator nothing to go on.

### What a reply must satisfy (HMG-50 parser)

A reading only reaches the detector — or the steering — when the reply is
accepted:

- its CT type (field 1) equals the type the battery requested; any other is
  rejected;
- once a CT MAC is bound, field 2 must equal it (see [CT MAC behavior](#ct-mac-behavior));
- `info_idx` (field 14) must change: a non-zero value equal to the previous
  reply's is accepted but brings **no new reading**.

Each rejected reply or missed poll counts as a failure. After about 24 in a row
(six per reconnect cycle, four cycles) the meter is marked offline. Offline,
the HMG-50 holds 0 W; the VNSE3-0 holds 0 W, zeroes its integrator and skips
detection.

### What this means for an emulator

Everything a battery sees during detection is the phase fields of the replies it
accepts. In particular:

- the battery's own probe has to show up on its phase within the timeout, by at
  least the threshold (half the capped power window). Batteries that steer on
  the same grid see the probe as a load change and cancel it unless told
  otherwise; the slower the probe ramps, the less of it ever shows;
- no other phase may move by as much meanwhile. A real CT reports only the
  meter, so another battery on another phase moves that phase only when it
  reacts to its own phase;
- a substitute value counts like a measurement: an all-zero reply reads as a
  move on every phase whose baseline was at least the threshold away from zero.

### Venus D (VNSD-0 v151)

The detection routine (`0x08032114`), the CT reply parser and the MQTT
`seq_check`/`rechrg_seq` handler are the VNSE3-0 v151 code, instruction for
instruction; the only differences are some fields of one structure that sit a
byte lower. What differs is the power setter both detection and steering go
through (`0x0800a968`; VNSE3-0 `0x0800a48c`), which on the Venus D applies two
PV rules first:

- **Charging gives way to PV** (`0x08008bfc`). Once a PV input delivers more
  than 10 W, AC charging is cut so that PV plus AC charging stays within the
  charge limit (the BMS limit, at most 2500 W); if PV alone reaches it, the
  battery does not charge from AC at all.
- **Surplus feed-in** (`0x08009ab0`, logged as "余电上网中"). With the battery
  full (set at 100 %, held down to 95 %), a PV input above 14 V and a setting
  on (`0x2001769c+0x16`, not identified), the output never drops below the PV
  power plus 10 W.

Detection sets its zero and its probe through this setter, but its threshold
stays half the configured power `P`. So on a Venus D a charge probe (50 % SoC
or less) fails once PV leaves less than `P` / 2 of AC charging, and a probe
during surplus feed-in fails once the PV power plus 10 W is more than `P` / 2.
Nothing in a CT reply changes either rule.

### B2500 (HMJ-2 v110)

The B2500 runs its own routine inside its operating state machine
(`0x08017c44`, state byte at `0x2000815a`); the phase is stored as an ASCII
character (`0x2000eb22`). `seq_check` writes `0` and enters state 5;
`rechrg_seq=N` writes `'@' + N`; a configuration reset writes `0`.

- **State 5**: outputs off. If a phase is stored or CT mode is off, back to
  normal (state 4); otherwise wait 20 s (meter type `HME-1`), 11 s
  (Ecotracker) or 1 s, then state 7.
- **State 7**: snapshot the per-phase readings (and the total, for meter types
  Shelly Pro 3EM, `HME-3`, Ecotracker), switch the DC outputs on at half the
  configured output power each (the second only when both are in use), then
  state 8.
- **State 8**: while neither channel is producing (below 500 on its current
  field) the baseline keeps being re-taken. After 2 s (inverters with id
  5000–5499) or 5 s, compare: the threshold is 60 % of the output the producing
  channels are set to (a flat 50 W for `HME-1`). For CT002 each of A, B, C is
  compared; for CT003, Shelly Pro 3EM and Ecotracker only the total, which can
  only yield phase D. Exactly one move: that phase is saved, state 4. Two or
  more: outputs off, state 11. None before a configured timeout: outputs off,
  state 9.
- States 9 and 11 hold; no retry was found.

Meter types (`0x2000eb28`): 0 `HME-1`, 1 Shelly Pro 3EM, 3 `HME-4`, 4 `HME-3`,
5 Shelly EM Gen3, 6 Shelly Pro EM50, 7 Ecotracker. The configured output power
(capped at 800 W) and the timeout (capped at 360, unit not confirmed) are
settings whose defaults were not
found, and neither were the meaning of the "producing" field and a check that
sends states 7 and 8 to state 11, so the B2500 routine is not in the simulator
yet.

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

Newer devices use the **`marstek_energy/…`** prefix; earlier devices use the
older **`hame_energy/…`** prefix (after the `hamedata.com` cloud). AstraMeter
subscribes/answers on **both** prefixes.

A poll arrives as a CSV `k=v` body. `cd=1` requests aggregate runtime info;
`cd=4` (+`p1=<id>`) requests the slave list. (The App control channel carries a
wider command set too — e.g. reset, debug‑stream toggles, error clears — but
those are outside the scope of the meter emulator.) The reply omits any `cd=`
echo. The aggregate (`cd=1`) field set **differs between models**:

```text
CT002 (HME-4; the TPM2-0 layout has not been captured):
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
> extra keys, so this works in practice. The layouts above are the reference for
> what a *real* CT sends, not a spec the responder must match.

## CT MAC behavior

The CT responds when the request `hhm_mac_code` is the wildcard `000000000000`
**or** matches the CT's own MAC, compared byte for byte. An empty
`hhm_mac_code` does not parse, so the CT drops it. Either way the response
carries the CT's **own** type and MAC, never the request's.

A battery sends the wildcard while it has no CT bound and has not learned one
yet. It accepts any response then, and puts that response's CT MAC into its
next requests. Once a CT MAC is bound (set from the app over BLE or the
cloud), it drops any response whose CT MAC is not byte‑for‑byte the bound one,
so case and separators matter.

> Verified in the CT002 (`HME-4` v120/v124) and CT003 (`HME-3`
> v116/v117/v118/v122) images, and on the battery side in Venus E 3.0
> (`VNSE3-0` v151) and Venus E 2.0 (`HMG-50` v156), from the firmware
> archives cited above. The Venus E Mini (`VNSEM-0`), which needs the CT's own
> MAC in the wildcard reply to pair, has no public image.

In AstraMeter: if `CT_MAC` is configured, the emulator responds to the wildcard
and to requests naming `CT_MAC` (case‑insensitively), always with `CT_MAC` in
the response, and ignores every other CT MAC. `CT_MAC` is normalized to
lowercase hex without separators, the form a battery stores. If `CT_MAC` is
empty, it accepts requests for any CT MAC and echoes the request CT MAC back
in responses.
