# Direct Installation

A manual installation on Windows, macOS, or Linux is the most flexible option,
suited to development or custom setups. It requires a Python environment.

## Prerequisites

1. **Python:** Use Python **3.10 or newer** (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).
   You can download Python from the
   [official Python website](https://www.python.org/downloads/).
2. **Configuration:** Create a `config.ini` file in the root directory of the
   project and add the appropriate configuration as described in the
   [Configuration reference](../configuration.md).

## Installation steps

1. **Open a terminal / command prompt**
   - Windows: Press `Win + R`, type `cmd`, press Enter
   - macOS: Press `Cmd + Space`, type `Terminal`, press Enter
   - Linux: Use your preferred terminal emulator

2. **Navigate to the project directory**
   ```bash
   cd path/to/astrameter
   ```

3. **Install [uv](https://docs.astral.sh/uv/getting-started/installation/)**
   (dependency manager).

4. **Install dependencies and run**
   ```bash
   uv sync
   uv run astrameter
   ```
   With dev tools (tests, ruff, mypy): `uv sync --extra dev`. See
   [CONTRIBUTING.md](../../CONTRIBUTING.md) for the full workflow.

All commands above work across Windows, macOS, and Linux. The only difference is
how you open your terminal.

When the script is running, switch your Marstek battery to "Self-Adaptation"
mode to enable the powermeter functionality.

## Autostart on boot (Linux)

Use systemd to create a service:

1. Create a unit file (e.g., `/etc/systemd/system/astrameter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable astrameter && sudo systemctl start astrameter`

## Binding the Shelly HTTP port

With a `shellypro3em` device type, AstraMeter also serves a Shelly HTTP surface
on port 80 so batteries that discover meters by themselves can talk to it.
Ports below 1024 need extra privileges on Linux, so a direct install running as
an ordinary user will log one error naming the port and carry on without it —
batteries that poll a configured address are unaffected.

To serve it, either grant the capability once:

```bash
sudo setcap cap_net_bind_service=+ep "$(readlink -f "$(which python3)")"
```

or, if you run AstraMeter under systemd, add it to the unit instead:

```ini
[Service]
AmbientCapabilities=CAP_NET_BIND_SERVICE
```

Or set `TCP_PORT` to a port above 1024, which needs no privileges — though some
battery models have port 80 hardcoded and will only find the meter there. See
[Shelly discovery and the HTTP surface](../shelly-tcp.md).
