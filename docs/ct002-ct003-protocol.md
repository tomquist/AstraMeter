# CT002 / CT003 UDP Protocol

This document summarizes the CT002/CT003 protocol based on community reverse‑engineering and the reference scripts in:
- https://github.com/rweijnen/marstek-venus-e-firmware-notes
- https://github.com/d-shmt/hass_marstek-smart-meter

Capture-based findings for issue #111 are documented in:
- [ct002-capture-analysis.md](ct002-capture-analysis.md)

The CT002 and CT003 share the **same protocol**. The only difference is the CT type value:
- **CT002:** `HME-4`
- **CT003:** `HME-3`

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
Because the length field is part of the packet, you must compute it iteratively until the digit count matches.

### Checksum
The checksum is a 2‑character ASCII hex string (lowercase in observed traffic). It is the XOR of all bytes from the
**start of SOH up to and including ETX**.

Pseudo‑code:

```python
xor = 0
for b in payload_without_checksum:
    xor ^= b
checksum = f"{xor:02x}".encode("ascii")
```

## Request Fields

Request payload fields (consumer → CT):

1. **meter_dev_type** — device type of the requester (copied through into the response)
2. **meter_mac_code** — battery MAC (12 hex chars, from Marstek app device management)
3. **hhm_dev_type** — CT type (`HME-4` or `HME-3`)
4. **hhm_mac_code** — CT MAC (12 hex chars, from Marstek app device management)
5. **phase** — phase identifier (`A`, `B`, `C`) observed in real traffic; any other value (observed: `0`, empty, `D`) means inspection mode
6. **phase_power** — signed integer watts for the phase in field 5

This mapping is based on real packet captures. Older public scripts may show `0|0` placeholders,
but observed live traffic carries `phase|power` in these two fields.

### Inspection mode (any phase other than `A`/`B`/`C`)

When a device sends a phase that is not `A`, `B`, or `C` (observed markers
include `0`, empty, and `D` from newer Marstek firmwares), it is in
**inspection mode** — determining which
phase it is connected to. The emulator:

- **Responds** to the request (so the device can continue its phase detection)
- **Does not** add the device's reported power to the per-phase aggregates (the device has not yet
  committed to a phase)

### Example Request (human readable)

```text
<SOH><STX>53|HMG-50|AABBCCDDEEFF|HME-4|112233445566|B|-217<ETX>xx
```

## Response Fields

Response payload fields (CT → consumer):

1. **meter_dev_type** — echoes request field 1
2. **meter_mac_code** — echoes request field 2
3. **hhm_dev_type** — CT type (`HME-4` or `HME-3`)
4. **hhm_mac_code** — CT MAC
5. **A_phase_power** — integer watts for phase A
6. **B_phase_power** — integer watts for phase B
7. **C_phase_power** — integer watts for phase C
8. **total_power** — integer watts (sum of phases)
9. **A_chrg_nb** — set to `1` if phase A aggregate is non-zero, else `0`
10. **B_chrg_nb** — set to `1` if phase B aggregate is non-zero, else `0`
11. **C_chrg_nb** — set to `1` if phase C aggregate is non-zero, else `0`
12. **ABC_chrg_nb** — currently always `0`
13. **wifi_rssi** — configured RSSI value
14. **info_idx** — incrementing response index (`0..255`, wraps)
15. **x_chrg_power** — currently `0`
16. **A_chrg_power** — phase A aggregate when negative, else `0`
17. **B_chrg_power** — phase B aggregate when negative, else `0`
18. **C_chrg_power** — phase C aggregate when negative, else `0`
19. **ABC_chrg_power** — currently `0`
20. **x_dchrg_power** — currently `0`
21. **A_dchrg_power** — phase A aggregate when positive, else `0`
22. **B_dchrg_power** — phase B aggregate when positive, else `0`
23. **C_dchrg_power** — phase C aggregate when positive, else `0`
24. **ABC_dchrg_power** — currently `0`

This list describes current emulator behavior as implemented.

## Multi‑consumer behavior

The emulator tracks per‑consumer `phase` + `phase_power` from the request fields (only when phase is
`A`, `B`, or `C`; inspection-mode requests carrying any other phase value — observed: `0`, empty, `D` — are responded to but not aggregated).
If a consumer stops sending updates for a while, its reported values are evicted after a configurable
TTL (`CONSUMER_TTL`).

### Relay mode (ACTIVE_CONTROL = False)

When active control is disabled, the emulator behaves like a passive relay:
- Forwards per-phase aggregates based on the latest known reports from all known consumers,
  grouped by their reported phase.
- Uses sign split for forwarded aggregates:
  - negative phase sums -> `A/B/C_chrg_power` (fields 16-18)
  - positive phase sums -> `A/B/C_dchrg_power` (fields 21-23)

Capture analysis shows this aggregate + sign-split model matches observed traffic closely. In this
mode, each battery decides its own charge/discharge based on the relayed aggregates.

### Active control mode (ACTIVE_CONTROL = True, default)

When active control is enabled, the emulator becomes the control authority:
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

## CT MAC behavior

If `CT_MAC` is configured, the CT only responds when the request `hhm_mac_code` matches `CT_MAC`.
If `CT_MAC` is empty, the emulator accepts requests for any CT MAC and echoes the request CT MAC
back in responses.
