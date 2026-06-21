# Home Assistant Add-on Installation

The Home Assistant add-on is the easiest way to run AstraMeter if you already
use Home Assistant. It provides a user-friendly configuration interface and
integrates seamlessly with your installation.

> **Tip:** Prefer a guided setup? The
> [config generator](https://astrameter.com/generator.html) can
> produce a ready-to-paste Home Assistant add-on options block — pick the
> "Home Assistant add-on" target.

## 1. Add the repository to Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23main)

## 2. Install the add-on

- Click on "Add-on Store" in the bottom right corner
- The AstraMeter add-on should appear in the store
- Click on it and then click "Install"

## 3. Configure the add-on

You can configure the add-on in two ways.

### A) Using the add-on configuration interface

- After installation, go to the add-on's Configuration tab
- For single-phase monitoring:
  - Set the `Power Input Entity ID` and optionally the `Power Output Entity ID`
    to the entity IDs of your power sensors
- For three-phase monitoring:
  - Set the `Power Input Entity ID` to a comma-separated list of three entity IDs
    (one for each phase)
  - If using calculated power, also set the `Power Output Entity ID` to a
    comma-separated list of three entity IDs
  - Example: `sensor.phase1,sensor.phase2,sensor.phase3`
- Set `Device Types` (comma-separated list) to the device types you want to
  emulate:
  - `ct002`: CT002 emulator (Marstek CT002 protocol)
  - `ct003`: CT003 emulator (same protocol as CT002)
  - `shellypro3em`: Shelly Pro 3EM emulator (uses both ports 1010 and 2220 for
    compatibility with all B2500 firmware versions)
  - `shellypro3em_old`: Shelly Pro 3EM emulator using port 1010 (for B2500
    firmware up to v224)
  - `shellypro3em_new`: Shelly Pro 3EM emulator using port 2220 (for B2500
    firmware v226+)
  - `shellyemg3`: Shelly EM gen3 emulator
  - `shellyproem50`: Shelly Pro EM50 emulator

  **Tip:** Use `ct002`/`ct003` for multiple devices; use a Shelly type (e.g.
  `shellypro3em` or `_old`/`_new`) otherwise.
- Optional signal-conditioning filters are also available as Configuration
  fields (all optional, off by default): power offset/multiplier, smoothing
  (EMA), deadband, the Hampel outlier filter (see
  [General Configuration](../configuration.md#general-configuration)), and the
  [PID Controller](../configuration.md#pid-controller). Leave them empty to keep
  them disabled.
- Click "Save" to apply the configuration

### B) Using a custom configuration file (advanced)

- Create a `config.ini` file based on the examples in the
  [Configuration reference](../configuration.md)
- Place the file in `/addon_configs/a0ef98c5_b2500_meter/` (path uses the legacy
  slug `b2500_meter` for in-place upgrade compatibility). You can do that via the
  "File editor" add-on in Home Assistant. Make sure to disable the "Enforce
  Basepath" setting in the File editor add-on config to access the
  `/addon_configs` folder.
- In the add-on configuration, set `Custom Config` to the filename (e.g.,
  `config.ini` without the path)
- When using a custom configuration file, other configuration options will be
  ignored

## 4. Start the add-on

- Go to the add-on's Info tab
- Click "Start" to run the add-on

When the add-on is running, switch your Marstek battery to "Self-Adaptation"
mode to enable the powermeter functionality.

## Pre-release builds (`next`)

CI publishes **pre-release** container images from the **`develop`** branch with
the **`next`** tag on GitHub Container Registry. These track the latest changes
before a stable release and **may be less stable** than **`latest`** — use them
to try fixes early or to validate the add-on before it lands on **`main`**.

1. Add the repository pointing at the **`develop`** branch (same flow as above,
   but use this URL):

   `https://github.com/tomquist/astrameter#develop`

   [![Add develop repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftomquist%2Fastrameter%23develop)

2. Install or update the **AstraMeter** add-on from the store. Supervisor will
   pull the **`next`**-tagged image (`ghcr.io/tomquist/astrameter-addon:next`).

To return to stable releases, remove this repository and add the normal URL
without `#develop` (step 1 above), then reinstall or wait for an update to the
**`latest`** track.
