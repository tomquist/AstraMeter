# Home Assistant Add-on Installation

If you already use Home Assistant, the add-on is the easiest way to run
AstraMeter. It gives you a simple configuration screen and fits right into your
installation.

> **Tip:** Want a guided setup? The
> [config generator](https://astrameter.com/generator.html) can
> produce a ready-to-paste Home Assistant add-on options block — pick the
> "Home Assistant add-on" target.

## 1. Add the repository to Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23main)

## 2. Install the add-on

- Click "Add-on Store" in the bottom right corner
- The AstraMeter add-on should appear in the store
- Click it, then click "Install"

## 3. Configure the add-on

You can configure the add-on in two ways.

### A) Using the add-on configuration interface

- After you install it, open the add-on's Configuration tab
- For single-phase monitoring:
  - Set `Power Input Entity ID`, and optionally `Power Output Entity ID`, to the
    entity IDs of your power sensors
- For three-phase monitoring:
  - Set `Power Input Entity ID` to a comma-separated list of three entity IDs,
    one per phase
  - If you use calculated power, also set `Power Output Entity ID` to a
    comma-separated list of three entity IDs
  - Example: `sensor.phase1,sensor.phase2,sensor.phase3`
- Set `Device Types` (comma-separated list) to the device types you want to
  emulate:
  - `ct002`: CT002 emulator (Marstek CT002 protocol)
  - `ct003`: CT003 emulator (same protocol as CT002)
  - `shellypro3em`: Shelly Pro 3EM emulator (uses both ports 1010 and 2220, so it
    works with every B2500 firmware version)
  - `shellypro3em_old`: Shelly Pro 3EM emulator using port 1010 (for B2500
    firmware up to v224)
  - `shellypro3em_new`: Shelly Pro 3EM emulator using port 2220 (for B2500
    firmware v226+)
  - `shellyemg3`: Shelly EM gen3 emulator
  - `shellyproem50`: Shelly Pro EM50 emulator

  **Tip:** Use `ct002`/`ct003` for multiple devices; use a Shelly type (e.g.
  `shellypro3em` or `_old`/`_new`) otherwise.
- The Configuration tab also has optional signal-conditioning filters, all off by
  default: power offset/multiplier, smoothing (EMA), deadband, the Hampel outlier
  filter (see
  [General Configuration](../configuration.md#general-configuration)), and the
  [PID Controller](../configuration.md#pid-controller). Leave them empty to keep
  them off.
- Click "Save" to apply the configuration

### B) Using a custom configuration file (advanced)

- Create a `config.ini` file based on the examples in the
  [Configuration reference](../configuration.md)
- Put the file in `/addon_configs/a0ef98c5_b2500_meter/` (the path keeps the
  legacy slug `b2500_meter` so in-place upgrades still work). You can do this
  with the "File editor" add-on in Home Assistant. Turn off the "Enforce
  Basepath" setting in the File editor add-on config first, or you can't reach
  the `/addon_configs` folder.
- In the add-on configuration, set `Custom Config` to the filename (e.g.,
  `config.ini` without the path)
- With a custom configuration file, the add-on ignores the other configuration
  options

### Getting a long debug log

The add-on's Log tab downloads at most 10,000 lines, about ten minutes at the
`debug` level, which is too short for a steering problem that takes hours to
show. To keep more, set `Log File` in the Configuration tab to a file name
such as `astrameter.log`. The add-on then also writes its log to
`/addon_configs/a0ef98c5_b2500_meter/<name>`, rotated at 20 MB with two older
files kept, so it takes at most 60 MB. Set `Log Level` to `debug`, let it run
until the problem shows, then fetch the file with the File editor or Samba
add-on and attach it to your report. Passwords and tokens are masked in the
file the same way they are in the Log tab. Clear the option to stop writing
the file; nothing else is needed.

The same works from a custom configuration file: `LOG_FILE = astrameter.log`
under `[GENERAL]`. Either way the name is taken inside that folder — a path
pointing elsewhere is refused and logged as a warning.

## 4. Start the add-on

- Open the add-on's Info tab
- Click "Start" to run the add-on

Once the add-on is running, switch your Marstek battery to "Self-Adaptation"
mode to turn on the powermeter functionality.

## Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with
the **`next`** tag on GitHub Container Registry. They carry the latest changes
from before a stable release, and **may be less stable** than **`latest`**. Use
them to try fixes early, or to check the add-on before it lands on **`main`**.

1. Add the repository pointing at the **`develop`** branch (same steps as above,
   but use this URL):

   `https://github.com/tomquist/astrameter#develop`

   [![Add develop repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23develop)

2. Install or update the **AstraMeter** add-on from the store. Supervisor pulls
   the **`next`**-tagged image (`ghcr.io/tomquist/astrameter-addon:next`).

To go back to stable releases, remove this repository and add the normal URL
without `#develop` (step 1 above). Then reinstall, or wait for an update on the
**`latest`** track.
