# Direct Installation

A manual installation on Windows, macOS, or Linux is the most flexible option.
It suits development or custom setups. You need a Python environment.

## Prerequisites

1. **Python:** Use Python **3.10 or newer** (see [CONTRIBUTING.md](../../CONTRIBUTING.md)).
   Download Python from the
   [official Python website](https://www.python.org/downloads/).
2. **Configuration:** Create a `config.ini` file in the root directory of the
   project. Fill it in as described in the
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
   To add the dev tools (tests, ruff, mypy), run `uv sync --extra dev`. See
   [CONTRIBUTING.md](../../CONTRIBUTING.md) for the full workflow.

All the commands above work on Windows, macOS, and Linux. Only the way you open
your terminal differs.

Once the script is running, switch your Marstek battery to "Self-Adaptation"
mode to turn on the powermeter functionality.

## Autostart on boot (Linux)

Use systemd to create a service:

1. Create a unit file (e.g., `/etc/systemd/system/astrameter.service`)
2. Set `ExecStart` to your startup command
3. Enable and start: `sudo systemctl enable astrameter && sudo systemctl start astrameter`
