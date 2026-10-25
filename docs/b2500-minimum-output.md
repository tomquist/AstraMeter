# B2500 minimum output

A Marstek B2500 can't deliver less than a minimum power. If it is asked for
less, it stays at a few watts. The minimum depends on the micro-inverter you
select for the battery in the Marstek app (microinverter setup: manufacturer,
model and quantity), not on the inverter that is actually connected.

## Minimum by selected inverter

| Inverter selected in the Marstek app | Per output | Per battery, both outputs connected | Per battery, grouped (one output) |
|---|---:|---:|---:|
| Any Marstek model, or TSUN Tsol-MP3000 | 20 W | 40 W | 20 W |
| Any other model | 40 W | 80 W | 40 W |

[All models](#all-models) lists every inverter the app offers.

The app sends the selected model to the battery, and the battery sets its
minimum from it, split evenly across the two outputs. Because the battery only
checks the selected model, selecting a Marstek model also lowers the minimum
when an inverter from another manufacturer is connected. Not every inverter
keeps running at 20 W per input. Users report that APsystems EZ1-M works, and
that a Hoymiles HMS-1600-4T needed a newer Hoymiles firmware to run reliably at
that power ([#390](https://github.com/tomquist/astrameter/issues/390),
[#402](https://github.com/tomquist/astrameter/issues/402)).

## Grouping (parallel mode)

Grouping B2500 batteries in the Marstek app ("Combination") doesn't lower the
minimum. A group is meant for setups where several B2500 feed one shared
inverter, each through a single output. Adding a battery to a group switches
it to parallel mode:

- the second output is switched off,
- the first output receives the whole setpoint,
- the maximum power is halved, for example from 800 W to 400 W.

The minimum of the output that stays on doesn't change, so a grouped battery
needs 20 W or 40 W, as in the last column above. Don't group batteries that
have both outputs connected, because the second output stops delivering power.

## What to set in AstraMeter

- `MIN_EFFICIENT_POWER`: with two or more B2500, set it to at least the minimum
  per battery from the table. See
  [My second battery never starts](faq.md#my-second-battery-never-starts-or-only-some-of-my-batteries-run).
- `MIN_DC_OUTPUT`: if you use it, set it to at least the minimum per battery.
  A lower value keeps the battery near 0 W. See
  [What does MIN_DC_OUTPUT do?](faq.md#my-b2500s-inverter-switches-off-during-solar-surplus-what-does-min_dc_output-do)

## All models

The inverters the Marstek app offered for a B2500 in October 2026. Marstek
adds models from time to time, so the app may list models that are missing
here.

| Manufacturer | Model | Minimum per battery |
|---|---|---:|
| APsystems | AP-EcoFlow | 80 W |
| APsystems | AP-SMI-360 | 80 W |
| APsystems | AP-systems DS3-L | 80 W |
| APsystems | AP-systems DS3-M | 80 W |
| APsystems | AP-systems DS3-S-600 | 80 W |
| APsystems | AP-systems YC600 | 80 W |
| APsystems | AP-systems-EZ1-M | 80 W |
| APsystems | AP-wvc-800 | 80 W |
| APsystems | AP-ZX-3134 | 80 W |
| APsystems | AP-ZX-3518 | 80 W |
| Bosswerk | Bosswerk MI300 | 80 W |
| Bosswerk | Bosswerk MI600 | 80 W |
| Deye | Deye M80G3-EU-800 | 80 W |
| Deye | Deye SUN-M100G3-EU-Q0 | 80 W |
| Deye | Deye SUN-M160G4-EU-Q0 | 80 W |
| Deye | DEYE SUN-M80G4-EU-Q0 | 80 W |
| Deye | Deye SUN1000G3-EU-230 | 80 W |
| Deye | Deye SUN1600G3-EU-230 | 80 W |
| Deye | Deye SUN2000G3-EU-230 | 80 W |
| Deye | Deye SUN300G3-EU-230 | 80 W |
| Deye | Deye SUN600G3-EU-220 | 80 W |
| Deye | Deye SUN600G3-EU-230 | 80 W |
| Deye | Deye SUN800G3-EU-230 | 80 W |
| EcoFlow | EcoFlow EFWN511 | 80 W |
| EcoFlow | Ecoflow PowerStream-1000 | 80 W |
| EcoFlow | Ecoflow PowerStream-600 | 80 W |
| EcoFlow | Ecoflow PowerStream-800 | 80 W |
| EcoFlow | EW VN2T08EU-01 | 80 W |
| Envertech | ENVERTEC EVT300 | 80 W |
| Envertech | ENVERTEC EVT360 | 80 W |
| Envertech | ENVERTEC EVT400 | 80 W |
| Envertech | ENVERTEC EVT500 | 80 W |
| Envertech | ENVERTEC EVT560 | 80 W |
| Envertech | ENVERTEC EVT720 | 80 W |
| Envertech | ENVERTEC EVT800 | 80 W |
| Envertech | ENVERTECH EVT800 | 80 W |
| Growatt | Growatt 2500TL-XH | 80 W |
| Growatt | Growatt Mic 600 TL-X | 80 W |
| Growatt | Growatt NEO 800 TL-X | 80 W |
| Growatt | Growatt NEO 800M-X | 80 W |
| Growatt | Growatt Other | 80 W |
| HERF | HERF-1000 | 80 W |
| HERF | HERF-1200 | 80 W |
| HERF | HERF-1800 | 80 W |
| HERF | HERF-600 | 80 W |
| HERF | HERF-800 | 80 W |
| Hoymiles | Hoymiles HM-1200 | 80 W |
| Hoymiles | Hoymiles HM-1500 | 80 W |
| Hoymiles | Hoymiles HM-1800 | 80 W |
| Hoymiles | Hoymiles HM-300 | 80 W |
| Hoymiles | Hoymiles HM-400 | 80 W |
| Hoymiles | Hoymiles HM-400-1A | 80 W |
| Hoymiles | Hoymiles HM-600 | 80 W |
| Hoymiles | Hoymiles HM-700 | 80 W |
| Hoymiles | Hoymiles HMS-1500-4T | 80 W |
| Hoymiles | Hoymiles HMS-1600-4T | 80 W |
| Hoymiles | Hoymiles HMS-1800-4T | 80 W |
| Hoymiles | Hoymiles HMS-2000-4T | 80 W |
| Hoymiles | Hoymiles HMS-800-2T | 80 W |
| Hoymiles | Hoymiles HMT-2250-6T | 80 W |
| KKT | KKT Kolbe BKW1000-02 | 80 W |
| KKT | KKT Kolbe BKW600-02 | 80 W |
| KKT | KKT Kolbe BKW800-02 | 80 W |
| Marstek | MARSTEK MST-MI0350 | 40 W |
| Marstek | MARSTEK MST-MI0500 | 40 W |
| Marstek | MARSTEK MST-MI0600W | 40 W |
| Marstek | MARSTEK MST-MI0700 | 40 W |
| Marstek | MARSTEK MST-MI0800W | 40 W |
| Marstek | MARSTEK MST-MI0900 | 40 W |
| Marstek | MARSTEK MST-MI1000W | 40 W |
| Marstek | MARSTEK MST-MI2000 | 40 W |
| Micro (WVC) | Micro WVC-1000 | 80 W |
| Micro (WVC) | Micro WVC-600 | 80 W |
| Micro (WVC) | Micro WVC-700 | 80 W |
| Micro (WVC) | Micro WVC-800 | 80 W |
| NEP | NEP BDM-1000 | 80 W |
| NEP | NEP BDM-600 | 80 W |
| NEP | NEP BDM-800 | 80 W |
| Solarepic | Solar 600-2T | 80 W |
| Solarepic | Solar 800-2T | 80 W |
| Solarepic | Solarepic WVC-800 | 80 W |
| Solax | Solax micro-X1 1000W | 80 W |
| SOSSEN | SOSSEN-1000 | 80 W |
| SOSSEN | SOSSEN-600 | 80 W |
| SOSSEN | SOSSEN-800 | 80 W |
| TSUN | TSUN TSOL-M1600 | 80 W |
| TSUN | TSUN TSOL-M350 | 80 W |
| TSUN | Tsun TSOL-MS1800 | 80 W |
| TSUN | Tsun TSOL-MS600 | 80 W |
| TSUN | TSUN TSOL-MS800 | 80 W |
| TSUN | Tsun-Tsol-MP3000 | 40 W |
| TSUN | Tsun-Tsol-MS2000 | 80 W |
| TUYA | TUYA WVC-800 | 80 W |

## Sources

The inverter list in the Marstek app (October 2026) and user reports in
[#179](https://github.com/tomquist/astrameter/issues/179),
[#206](https://github.com/tomquist/astrameter/issues/206),
[#390](https://github.com/tomquist/astrameter/issues/390),
[#515](https://github.com/tomquist/astrameter/issues/515) and
[#600](https://github.com/tomquist/astrameter/issues/600).
