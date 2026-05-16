# CT002/CT003 Capture Analysis (Issue #111)

This note documents findings from two packet captures shared for issue #111:

- `charge 1 batteryes.pcap` (single-active-battery scenario)
- `charge 3 batteryes.pcap` (multi-active-battery scenario)

The goal is to describe observed wire behavior as clearly as possible.

## 1) Network roles and transport

Observed UDP traffic:

- **Storage endpoint:** `<storage_host>:12345`
- **Battery/consumer endpoints:**
  - `<battery_A_host>:22222` (battery A)
  - `<battery_B_host>:22222` (battery B)
  - `<battery_C_host>:22222` (battery C)

Request direction:
- battery -> storage (`22222 -> 12345`)

Response direction:
- storage -> battery (`12345 -> 22222`)

## 2) Request payload structure (confirmed)

Observed request payload pattern:

`LEN|HMG-50|<battery_mac>|HME-3|<ct_mac>|<phase>|<power>`

Example from capture (anonymized):

`53|HMG-50|<battery_mac>|HME-3|<ct_mac>|B|-217`

Field mapping (from observed packets):

1. `LEN`
2. storage type (`HMG-50`)
3. battery/storage MAC (`battery_mac`)
4. CT type (`HME-3` in these captures)
5. CT MAC (`<ct_mac>`)
6. **phase** (`A`/`B` seen)
7. **phase power** (signed integer)

### Important implication

The last two request fields are **phase + signed power**, not `charge_power/discharge_power`.

## 3) Response payload structure (partially confirmed)

Observed response payload pattern:

`LEN|HME-3|<ct_mac>|HMG-50|<battery_mac>|v1|v2|v3|v4|...`

Example (anonymized):

`135|HME-3|<ct_mac>|HMG-50|<battery_mac>|121|202|-294|29|1|2|0|0|-36|0|0|-353|-270|...`

Confirmed:

- Identity fields are consistent and mirrored:
  - CT type/mac first
  - storage type/mac second
- Numeric section starts immediately after those identity fields.

Likely (high confidence):

- `v1..v3` are per-phase-like values
- `v4` behaves like a total/aggregate value (often close to sum with small +/- drift)

Partially confirmed (targeted experiments still useful for edge-cases):

- later fields after `v4` are largely consistent with status/counter/energy-style telemetry.
- remaining uncertainty is mainly field-by-field naming confidence under all operating states.

## 4) Scenario comparison: single-active vs multi-active

### `charge 1 batteryes.pcap`

Requests: 401, Responses: 290

Observed request senders:
- battery A, phase `A`, power range `-275..-208` (active)
- battery B, phase `B`, power range `0..0` (inactive)
- battery C, phase `B`, power range `0..0` (inactive)

Interpretation:
- Three batteries still communicate, but only one contributes non-zero power.

### `charge 3 batteryes.pcap`

Requests: 604, Responses: 428

Observed request senders:
- battery A, phase `A`, power range `-368..-141`
- battery B, phase `B`, power range `-217..-70`
- battery C, phase `B`, power range `-121..0`

Interpretation:
- All three batteries actively report non-zero power.

## 5) Answers to key behavior questions

### Q1: Does each storage get its own response, or are responses basically the same?

**Each requester gets its own UDP response** (unicast back to the source endpoint).

Payload similarity depends on scenario:

- In `charge 1 batteryes.pcap`, the forwarded A/B/C section (`A_chrg_power|B_chrg_power|C_chrg_power`) is identical across recipients in **94.7%** of multi-recipient cycles.
- In `charge 3 batteryes.pcap`, that same section is identical across recipients in **56.7%** of cycles.

Interpretation:
- Responses are not globally broadcast-identical; they are generated per requester.
- But many fields can still be very similar when system state is similar.

### Q2: What happens with request `phase_power` values?

Observed request tail:
- `...|<phase>|<phase_power>`

Observed response effect:
- `phase_power` values clearly reappear in response forwarding fields (A/B/C charge-power section), e.g. tuples like `(-353, -270, 0)` in the multi-active trace.
- In single-active trace, forwarding is typically `(-X, 0, 0)`, matching one active contributor.
- When comparing `resp_fwd_ABC` against per-phase sums of latest known request powers:
  - single-active capture: exact match in ~96.8% (A) / 100% (B,C)
  - multi-active capture: exact match in ~87.1% (A) / ~81.6% (B) / 100% (C)
  - non-exact cases are consistent with async timing/skew between requests and responses.

Interpretation:
- Request phase-power is not ignored; it is fed into forwarded per-phase values in responses.
- Best current model: forwarded values are per-phase sums across all known storages (with minor timing skew possible).
- Sign split observed in newer traces:
  - negative sums appear in `A/B/C_chrg_power` (fields 16-18)
  - positive sums appear in `A/B/C_dchrg_power` (fields 21-23)

## 6) Cycle-by-cycle snapshots (anonymized)

Legend:
- `resp_p1234` = first four numeric response values after identity fields
- `resp_fwd_ABC` = forwarded A/B/C charge-power-like tuple

### `charge 1 batteryes.pcap` (single-active)

- Cycle 1
  - battery C: req `?/?`, resp_p1234 `[77,63,-147,-6]`, resp_fwd_ABC `[-239,0,0]`
  - battery B: req `B/0`, resp_p1234 `[78,62,-147,-7]`, resp_fwd_ABC `[-239,0,0]`
  - battery A: req `A/-239`, resp_p1234 `[78,62,-147,-7]`, resp_fwd_ABC `[-239,0,0]`
- Cycle 2
  - battery B: req `B/0`, resp_p1234 `[78,66,-146,-1]`, resp_fwd_ABC `[-239,0,0]`
  - battery A: req `A/-239`, resp_p1234 `[78,66,-146,-1]`, resp_fwd_ABC `[-239,0,0]`

### `charge 3 batteryes.pcap` (multi-active)

- Cycle 1
  - battery A: req `A/-353`, resp_p1234 `[121,205,-295,32]`, resp_fwd_ABC `[-353,-275,0]`
  - battery C: req `B/-53`, resp_p1234 `[121,209,-294,36]`, resp_fwd_ABC `[-353,-270,0]`
  - battery B: req `B/-193`, resp_p1234 `[121,202,-294,29]`, resp_fwd_ABC `[-353,-270,0]`
- Cycle 4
  - battery A: req `A/-327`, resp_p1234 `[121,171,-294,-1]`, resp_fwd_ABC `[-327,-218,0]`
  - battery C: req `B/-53`, resp_p1234 `[105,150,-293,-38]`, resp_fwd_ABC `[-327,-214,0]`
  - battery B: req `B/-161`, resp_p1234 `[105,144,-294,-44]`, resp_fwd_ABC `[-324,-214,0]`

## 7) Additional observations

- Newer traces with explicit discharge scenarios confirm the same aggregate logic as charge traces,
  but mapped to the `*_dchrg_power` block instead of `*_chrg_power`.
- Field hypothesis update:
  - `F25` and likely `F26` are plausible CT003 import/export counter-like values (scaling still unknown).
  - `F27` and `F28` may also be meter-state/counter related, but semantics are still not fully confirmed.

- Two devices (battery B and battery C) both report phase `B` in these captures.
  - So phase label is not guaranteed to be globally unique per device.
- Response destination identity aligns with requesting battery identity (no obvious cross-target mismatches).

## 8) Practical modeling guidance

For emulator/protocol implementation:

- Parse request tail strictly as:
  - `phase = fields[4]`
  - `power = int(fields[5])`
- Accept phase `A`, `B`, `C` for normal aggregation; accept `0` or empty as inspection mode (respond
  but do not aggregate). Reject other phase values.
- Treat later response fields as implementation-defined unless verified with controlled experiments.

## 9) Suggested follow-up experiments

To resolve remaining unknown response fields:

1. Run controlled tests with exactly one battery changing power stepwise (e.g. -100, -200, -300).
2. Repeat with two batteries toggling one at a time.
3. Correlate each response field against known injected values.
4. Label fields only when correlation is stable across runs.
