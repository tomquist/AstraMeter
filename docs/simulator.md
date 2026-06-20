# Simulator (`astra-sim`)

The project includes a standalone battery and powermeter simulator (`astra-sim`)
that lets you test the CT002 emulator without real hardware. It simulates N
batteries speaking the CT002 UDP protocol and exposes an HTTP endpoint that
astrameter reads as a powermeter.

## Install

```bash
pip install 'astrameter[sim]'
# or with uv:
uv pip install 'astrameter[sim]'
```

## Quick start

**Terminal 1** — Start the simulator (1 battery, single-phase, with TUI):

```bash
astra-sim run --batteries 1 --phases 1
```

**Terminal 2** — Start astrameter with the matching config:

```bash
astra-sim config > config.ini   # generate a config snippet
astrameter -c config.ini
```

The generated `config.ini` looks like:

```ini
[GENERAL]
DEVICE_TYPE = ct002

[CT002]
UDP_PORT = 12345
ACTIVE_CONTROL = True

[JSON_HTTP]
URL = http://localhost:8080/power
JSON_PATHS = $.phase_a
```

For three-phase setups, use `JSON_PATHS = $.phase_a,$.phase_b,$.phase_c`.

## Multi-battery 3-phase setup

```bash
# 3 batteries distributed across 3 phases
astra-sim run --batteries 3 --phases 3

# Custom base load and initial SOC
astra-sim run --batteries 2 --phases 3 --base-load 500,300,200 --soc 0.8
```

## JSON config file

For full control, use a JSON config file:

```bash
astra-sim run -c sim_config.json
```

Example `sim_config.json`:

```json
{
  "ct": {
    "mac": "112233445566",
    "host": "127.0.0.1",
    "port": 12345
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "powermeter": {
    "base_load": [100, 100, 100],
    "loads": [
      {"name": "LED lights", "power": 30, "phase": "A"},
      {"name": "TV + entertainment", "power": 80, "phase": "B"},
      {"name": "Router + NAS", "power": 40, "phase": "A"},
      {"name": "Microwave", "power": 800, "phase": "A"},
      {"name": "Washing machine", "power": 400, "phase": "B"}
    ],
    "solar_max": 2000,
    "solar_phases": ["A"]
  },
  "power_update_delay_ticks": 0,
  "batteries": [
    {"mac": "02B250000001", "phase": "A", "capacity_wh": 2560, "initial_soc": 0.5},
    {"mac": "02B250000002", "phase": "B", "capacity_wh": 2560, "initial_soc": 0.8}
  ]
}
```

Optional top-level `power_update_delay_ticks` (or per-battery
`power_update_delay_ticks`) delays how many simulator ticks pass before the
battery applies each new CT-derived power setpoint (`reported_power +
grid_reading` from the response; `0` = immediate). The same delay can be set from
the CLI with `astra-sim run --power-update-delay N` (also supported on `astra-sim
start`). With a non-zero delay, `GET /status` and the TUI expose **`target`** as
the latest CT-requested watts and **`applied_target`** as the setpoint the battery
is ramping toward after the delay. When delay is `0`, both match.

A more complete example simulating a European 3-phase household with rooftop
solar, multiple appliances, and 4 batteries (two on the heaviest phase):

```json
{
  "ct": {
    "mac": "AABBCCDDEEFF",
    "host": "127.0.0.1",
    "port": 12345
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "powermeter": {
    "base_load": [120, 80, 60],
    "base_noise": 30,
    "loads": [
      {"name": "LED lights",        "power":   30, "phase": "A"},
      {"name": "Router + NAS",      "power":   40, "phase": "A"},
      {"name": "Coffee machine",    "power":  200, "phase": "A"},
      {"name": "TV + entertainment","power":   80, "phase": "B"},
      {"name": "Washing machine",   "power":  400, "phase": "B"},
      {"name": "Laptop charger",    "power":   65, "phase": "B"},
      {"name": "Microwave",         "power":  800, "phase": "A"},
      {"name": "Fridge/freezer",    "power":  120, "phase": "C"},
      {"name": "Vacuum cleaner",    "power":  600, "phase": "C"}
    ],
    "solar_max": 5000,
    "solar_phases": ["A", "B", "C"]
  },
  "batteries": [
    {
      "mac": "02B250000001",
      "phase": "A",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.9,
      "ramp_rate": 150,
      "poll_interval": 1.0
    },
    {
      "mac": "02B250000002",
      "phase": "A",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.7
    },
    {
      "mac": "02B250000003",
      "phase": "B",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 5120,
      "initial_soc": 0.4
    },
    {
      "mac": "02B250000004",
      "phase": "C",
      "max_charge_power": 800,
      "max_discharge_power": 800,
      "capacity_wh": 2560,
      "initial_soc": 0.2
    }
  ],
  "auto_mode": true,
  "auto_interval": [15, 45],
  "log_interval": 10
}
```

This configuration demonstrates:

- **Phase imbalance**: Kitchen loads (coffee machine, microwave) are concentrated
  on phase A with two batteries to compensate; entertainment/laundry on B;
  fridge/cleaning on C
- **Two batteries on one phase**: Batteries `0001` and `0002` both serve phase A —
  CT002's fair distribution algorithm splits the target between them
- **Mixed capacities**: Battery `0003` has a larger 5.12 kWh capacity (simulating
  a newer model)
- **Varied SOC**: Batteries start at different charge levels (90%, 70%, 40%, 20%)
  to test saturation timing
- **3-phase solar**: 5 kWp rooftop system balanced across all three phases — even
  moderate production exceeds the base load, causing grid export (negative
  readings) and battery charging
- **Custom ramp rate**: Battery `0001` ramps at 150 W/s instead of the default
  200 W/s
- **Auto mode**: Randomly toggles loads and solar every 15–45 seconds for
  hands-free testing

## Interactive controls

When running with the TUI (`astra-sim run`, without `--no-tui`), you can interact
with the simulation using keyboard shortcuts displayed on screen. The TUI shows
live battery state (power, SOC, targets), grid readings per phase, and active
loads. If `power_update_delay_ticks` is non-zero, the battery table adds **Req**
(CT request) and **Appl** (delayed setpoint) columns so you can see the latency
effect; otherwise a single **Target** column shows the setpoint.

Without the TUI, you can control the simulation via the HTTP API:

```bash
# Toggle a load on/off (1-based index)
astra-sim load toggle 1

# Set solar production (watts)
astra-sim solar set 800
astra-sim solar set off

# Set a battery's SOC (for testing saturation)
astra-sim battery 02B250000001 soc 0.0

# Show full status
astra-sim status
```

## Daemon mode

Run the simulator in the background and attach/detach the TUI:

```bash
# Start headless daemon
astra-sim start -c sim_config.json

# Attach TUI to running daemon
astra-sim attach

# Stop daemon
astra-sim stop
```

## Custom ports

If you need non-default ports (e.g. to avoid conflicts):

```bash
# Simulator on custom ports
astra-sim run --batteries 2 --phases 3 --ct-port 54321 --http-port 9090

# Generate matching astrameter config
astra-sim config --ct-port 54321 --http-port 9090 > config.ini
```

## Headless mode

For CI or scripted testing, run without the TUI:

```bash
astra-sim run --batteries 2 --phases 3 --no-tui
```

## How it works

The simulator is fully decoupled from astrameter — it communicates purely over
the network:

- **Battery simulators** send UDP requests to astrameter's CT002 emulator using
  the same protocol as real Marstek batteries
- **Powermeter simulator** serves an HTTP JSON endpoint (`GET /power`) that
  astrameter reads via its `[JSON_HTTP]` powermeter config
- Grid power is computed as:
  `grid = base_load + active_loads + noise - solar - battery_output`
- When solar exceeds consumption, grid goes negative (export) and batteries charge
- Batteries track SOC and saturate at 0%/100%
