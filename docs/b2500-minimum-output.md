# B2500 minimum output

A Marstek B2500 can't deliver less than a minimum power. If it is asked for
less, it stays at a few watts. The minimum depends on the micro-inverter you
select for the battery in the Marstek app (microinverter setup: manufacturer,
model and quantity), not on the inverter that is actually connected.

## Minimum by selected inverter

| Inverter selected in the Marstek app | Per output | Per battery, both outputs connected | Per battery, grouped (one output) |
|---|---:|---:|---:|
| A Marstek model, for example MST-MI0800W | 20 W | 40 W | 20 W |
| Any other manufacturer, for example Hoymiles, APsystems or TSUN | 40 W | 80 W | 40 W |

The app sends the selected model to the battery, and the battery sets its
minimum from it: 40 W for a Marstek model and 80 W for any other model, split
evenly across the two outputs.

Because the battery only checks the selected model, selecting a Marstek model
also lowers the minimum when an inverter from another manufacturer is
connected. Not every inverter keeps running at 20 W per input. Users report
that APsystems EZ1-M works, and that a Hoymiles HMS-1600-4T needed a newer
Hoymiles firmware to run reliably at that power ([#390](https://github.com/tomquist/astrameter/issues/390), [#402](https://github.com/tomquist/astrameter/issues/402)).

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

## Sources

User reports in [#179](https://github.com/tomquist/astrameter/issues/179),
[#206](https://github.com/tomquist/astrameter/issues/206),
[#390](https://github.com/tomquist/astrameter/issues/390),
[#515](https://github.com/tomquist/astrameter/issues/515) and
[#600](https://github.com/tomquist/astrameter/issues/600).
