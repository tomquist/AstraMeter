# How a battery is told which CT to follow

A Marstek battery in automatic mode follows one CT: it stores that CT's type
and MAC, and polls it over UDP (see
[ct002-ct003-protocol.md](ct002-ct003-protocol.md)). This page describes how
the Marstek app hands a battery that choice, so a battery can be pointed at an
emulated CT without going through the Marstek cloud.

The account plays no part in the battery's side of the link. It only fills the
list the app lets you pick from. The battery never checks a CT against an
account, and AstraMeter answers polls for any CT MAC while `CT_MAC` / `ct_mac`
is empty.

> **Status.** Everything here is read from the Android app's code. None of it
> has been sent to a battery from anything but the app. Treat it as a map of
> what to try, not as a tested interface.

## Three ways the app does it

| Way | When the app uses it |
|---|---|
| Bluetooth command to the battery | The phone is connected to the battery over Bluetooth |
| MQTT command to the battery | Otherwise; published on `hame_energy/<battery type>/App/<battery MAC>/ctrl` |
| Cloud "CT group" | Newer Venus models and the Mars SE, for a CT that is in the account (see [below](#newer-venus-models-and-the-mars-se)) |

The first two carry the same information: a CT type code, the CT's MAC, and on
some firmware the CT's device type name.

## CT type codes

| CT | Type code | Device types in the account |
|---|---|---|
| CT001 | 0 | `HME-1` |
| Shelly Pro 3EM | 1 | |
| P1 meter | 2 | |
| CT002 | 3 | `HME-2`, `HME-4`, `TPM-CN` |
| CT003 | 4 | `HME-3`, `HME-5` |

The *type name* sent to some firmware is the CT's device type from the
account, for example `HME-4` for a CT002. An emulated CT002 uses code `3` and
name `HME-4`; a CT003 uses `4` and `HME-3`.

## Per battery family

| Battery family | Bluetooth command | MQTT command |
|---|---|---|
| B2500 (device types starting `HMA`, `HMB`, `HMK`, `HMF`, `HMJ`; the V6000 uses the same screen) | `0x31` | `cd=27,meter=<code>,mac=<CT MAC>[,ct_dev=<name>]` |
| Venus (older screens) and devices of type `HMHL` | `0x18` | `cd=18,meter=<code>,mac=<CT MAC>[,ct_dev=<name>]` |
| Jupiter | `0x15` | `cd=16,meter=<code>,mac=<CT MAC>[,ct_dev=<name>]` |
| Venus E mini | `0x18` | `cd=18,ct_t=<code>,mac=<CT MAC>` |
| Venus G | `0x18` | `cd=18,meter=<code>,mac=<CT MAC>,ct_type=<…>` |
| Venus X | `0x18` | `cd=18,meter=<code>,mac=<CT MAC>` |

- `ct_dev` (and the name in the Bluetooth frame below) is only sent to firmware
  the app considers new enough. The B2500 always gets the name in its
  Bluetooth frame.
- `<CT MAC>` is 12 lowercase hex characters, the CT's MAC as the battery
  polls it.
- The app tells Venus and Jupiter apart by model rather than by a type prefix,
  so this page names no prefixes for them.
- The value the Venus G sends as `ct_type` was not decoded.

## Bluetooth frame

The frame uses Marstek's usual Bluetooth framing, the same as a CT002/CT003's:
`0x73`, the length of the whole frame, `0x23`, the command, the payload, and
the XOR of every byte before it.

For the B2500, the older Venus screens, `HMHL` devices and the Jupiter:

```text
73 LEN 23 CMD CODE MAC[12 ASCII] [NAME] XOR
```

- **NAME** on the B2500: the device type name, zero-padded to 13 bytes.
- **NAME** on the others: the device type name as plain ASCII, only when the
  battery's firmware is new enough; otherwise left out.

The newer Venus modules send `CODE` followed by the MAC as text, inside the
same framing. The Venus G adds a second text field after the MAC, presumably
the type name.

Two examples for a CT002 with MAC `02b25012abcd`:

```text
Venus, no name:  73 12 23 18 03 30 32 62 32 35 30 31 32 61 62 63 64 09
B2500:           73 1f 23 31 03 30 32 62 32 35 30 31 32 61 62 63 64
                 48 4d 45 2d 34 00 00 00 00 00 00 00 00 74
```

## Newer Venus models and the Mars SE

On the Venus E mini, G and X, and on the Mars SE, the app links a battery to a
CT from your account through the cloud: it posts a "Venus CT group" to
`ems-agent/v1/device-group/venus-ct`. The group holds the CT's ID, name, type
and MAC, and the battery's MAC, name, type and phase. The cloud then sets up
the battery. The app does this only on firmware it considers new enough;
older firmware goes through a Bluetooth binding flow instead.

The direct commands in the table above are still in the app for these models,
and the Mars SE screen sends the shared command for a CT that is not in the
account. A battery on newer firmware may still accept the direct command, but
that has not been tried.

## Not covered

- **Automatic mode.** The battery also has to be in automatic (self-adaptation)
  mode. The app sets the work mode with a separate command, which this page
  does not decode.
- **Phase.** The cloud group carries the battery's phase, and the newer
  modules have a separate command for it. Neither is decoded here.
- **Acknowledgement.** The app waits for the battery to confirm the change; the
  reply format is not covered.
