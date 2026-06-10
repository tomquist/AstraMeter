# Marstek battery-side firmware behavior (CT002 control path)

This note documents how the **battery** (the consumer/storage side) processes a
CT002 response, based on static reverse‑engineering of real Marstek control
firmware. It complements the wire‑level description in
[ct002-ct003-protocol.md](ct002-ct003-protocol.md) (which describes what the
**emulator** sends) by describing what the firmware on the other end actually
*does* with those fields.

It exists so the simulator (`src/astrameter/simulator/battery.py`) and our
mental model of "how do the batteries react" can be checked against ground
truth rather than inference.

## Source images

Analyzed from the community firmware archive
(`github.com/rweijnen/marstek-firmware-archive`):

- **`HMG-50` Control app** (e.g. build 155) — fully analyzable: it carries the
  `ct002_get_info.*` debug strings, which pin every struct field offset. This is
  the firmware that matters most in practice: in real captures a "Venus E" unit
  reports `meter_dev_type=HMG-50`, so the HMG‑50 control app *is* the Venus E
  control firmware for our purposes.
- **`VNSE3-0` Control app** — release build with the CT002 debug strings
  compiled out, so field offsets can't be recovered as cleanly. It carries the
  same protocol and the same `dchrg=`/`rechrg` configuration, so the HMG‑50
  findings are taken as representative.

Tooling: radare2 (`-a arm -b 16 -e asm.cpu=cortex`, loaded raw at
`0x08000000`). Confidence levels below are explicit because parts of the
control law live behind a FreeRTOS queue and were not traced end‑to‑end.

## Response parsing → struct (CONFIRMED)

The firmware parses the CT002 response into a single global struct
(`ct002_get_info`, at RAM `0x2000ece8` in HMG‑50 build 155). The field layout
recovered from the struct‑dump function matches our `RESPONSE_LABELS` exactly:

| Field            | Offset | | Field             | Offset |
|------------------|--------|-|-------------------|--------|
| `A_phase_power`  | +0x2c  | | `A_chrg_power`    | +0x54  |
| `B_phase_power`  | +0x30  | | `B_chrg_power`    | +0x58  |
| `C_phase_power`  | +0x34  | | `C_chrg_power`    | +0x5c  |
| `total_power`    | +0x38  | | `A_dchrg_power`   | +0x68  |
| `A_chrg_nb`      | +0x3c  | | `B_dchrg_power`   | +0x6c  |
| `B_chrg_nb`      | +0x40  | | `C_dchrg_power`   | +0x70  |
| `C_chrg_nb`      | +0x44  | | `ABC_dchrg_power` | +0x74  |

So the battery reads the per‑phase grid powers, the total, **and** the
`*_chrg_power` / `*_dchrg_power` cross‑talk block. None of these are ignored.

## Cross‑talk aggregation → control input (CONFIRMED)

A dedicated function builds this battery's control input from the parsed
struct. The key behavior is how it reduces the per‑phase `*_dchrg_power` /
`*_chrg_power` block to a single discharge/charge signal, and it depends on a
mode byte (`rechrg_mode`, at `0x20002fb3 + 0x71`):

- **Per‑phase mode** (`rechrg_mode != 1`): the battery uses **only its own
  phase**. A unit on phase C reads `C_dchrg_power` / `C_chrg_power`; A→`A_*`,
  B→`B_*`.
- **Aggregate mode** (`rechrg_mode == 1`): the battery uses the **sum across
  all phases** — `A_dchrg + B_dchrg + C_dchrg + ABC_dchrg` (and likewise for
  chrg). This is the natural mode for a single whole‑house meter feeding
  several batteries on different phases.

The crucial point for fidelity: **in both modes the battery's own phase is
included** in the discharge signal it consumes. There is no "react to other
phases but ignore my own" path. In aggregate mode a discharge reported on *any*
phase (including the battery's own) lands in the signal; in per‑phase mode only
the own‑phase value is used (which still counts the own phase).

A separate `choose_meter` byte (`0x20002fb3 + 0x72`) selects the meter
*protocol* (Shelly EM / EM1 / CT002 "HME‑"), not the phase aggregation — don't
confuse the two.

## Control law (PARTIALLY CONFIRMED)

The control‑input struct (grid total + per‑phase powers + the aggregated
dchrg/chrg signal) is handed to a FreeRTOS queue (`xQueueSend`) and consumed by
a separate control task. That task applies the integral‑style setpoint update
and the discharge reaction.

- **Integral setpoint** (`new_target ≈ current_output + grid_reading`): strongly
  implied by the architecture and consistent with all captures, but the exact
  consumer arithmetic (whether it integrates `total_power` or the sum of the
  three phase fields — numerically identical for any emulator response, since we
  set `total = A+B+C`) was **not** traced through the queue.
- **Discharge reaction** ("a charging battery backs off when the consumed
  discharge signal is positive"): the discharge signal is provably *extracted
  and queued* (above); that it then forces a charging unit toward idle is
  inferred from observed behavior (issue #376 / #447) and matches the data
  flow, but the exact comparison and threshold live in the unread consumer
  task. The end‑of‑log evidence in #447 (a `+2 W` cross‑phase discharge
  preceding a cut‑out) suggests the effective threshold is near zero.

## Implications for the simulator

`src/astrameter/simulator/battery.py` models the discharge reaction with the
opt‑in `idle_on_cross_phase_discharge` flag, plus a `discharge_idle_mode`
selector that mirrors the firmware `rechrg_mode` switch:

- `discharge_idle_mode="aggregate"` (default, `rechrg_mode == 1`): the reaction
  keys off the **sum** of all phases' `*_dchrg_power`, **including the
  battery's own phase**. This is the realistic configuration for a single
  household meter and the one that reproduces #447.
- `discharge_idle_mode="per_phase"` (`rechrg_mode != 1`): the reaction keys off
  **only the battery's own phase** `*_dchrg_power`.

Both modes include the battery's own phase in the signal. The earlier model
that excluded the own phase (reacting to "other phases only") corresponded to
no firmware mode and has been removed.
