# Changelog

## Next

- **Fixed** intermittent CT stalls when an upstream HTTP power meter was slow to respond: the read timeout for the polling HTTP sources (Shelly, AMIS Reader, emlog, ESPHome, ioBroker, generic JSON-HTTP, SHRDZM, Tasmota, VZLogger) is now 2s with a 1s connect timeout (was 10s), so an unresponsive meter fails fast and the next battery poll can recover instead of pinning a request handler ([#404](https://github.com/tomquist/astrameter/issues/404)).
- **Added** a project website (`web/`, deployable to GitHub Pages): a landing page introducing AstraMeter (features, supported devices and power meters, installation options, FAQ) plus a beginner-friendly config generator that walks you through a few questions and produces a ready-to-use Python `config.ini` or ESPHome YAML, with a live preview, save/load/share, and step-by-step ESPHome flashing guidance.
- **Added** experimental ESPHome external component `ct002` to run the CT002/CT003 emulator directly on an ESP32. See `esphome.example.yaml` and the per-meter grid-power sensor reference in `docs/esphome-powermeters.md` (which also lists meters not yet supported on the ESP, e.g. Enphase Envoy and the SMA Energy Meter). Per-source `config.ini` documentation moved out of the README into `docs/powermeters.md`.
- **Added** Modbus UDP support via a `TRANSPORT = TCP|UDP` option in the `[MODBUS]` section (defaults to `TCP`).
- **Fixed** Home Assistant powermeter timing out on startup with "Timeout waiting for Home Assistant state" when the configured sensor hasn't changed value since AstraMeter started.
- **Fixed** `DEVICE_TYPE = shellypro3em_new` (and `shellypro3em_old`) generating an invalid `device-N` source id that the B2500 rejected, so the CT was never detected. These variants now default to a proper `shellypro3em-*` id like the combined `shellypro3em` type ([#389](https://github.com/tomquist/astrameter/issues/389)).
- **Fixed** multi-Venus setups where a battery passing PV through to the grid (full SoC with "feed excess to grid" enabled) caused other batteries on different phases to stop charging. AstraMeter now populates the CT002 cross-talk `*_chrg_power` / `*_dchrg_power` fields from the per-battery instruction it sent rather than from the battery's reported AC output, so involuntary PV-passthrough no longer looks like a battery discharge to the rest of the fleet ([#376](https://github.com/tomquist/astrameter/issues/376)).

## 2.0.2

- **Fixed** EMA smoother slowing down across zero-crossings when `SMOOTH_TARGET_ALPHA` was configured above 0.5: the sign-flip "catchup" branch capped the effective alpha at 0.5, which actually reduced responsiveness for users who picked a larger alpha (e.g. 1.0 for near-instant tracking). The catchup boost now never drops below the configured alpha ([#371](https://github.com/tomquist/astrameter/issues/371)).

## 2.0.1

- **Fixed** false "Home Assistant sensor is stale" errors for sensors that update infrequently or push only on value changes — including constant readings (e.g. solar production at night) and push-based integrations. The Home Assistant powermeter now treats a sensor as stale only when Home Assistant itself marks it `unavailable`/`unknown` or the websocket connection is lost ([#363](https://github.com/tomquist/astrameter/issues/363)).

## 2.0.0

### Breaking
- **Rebranded** project from "B2500 Meter" to "AstraMeter" (formerly b2500-meter). Package renamed to `astrameter`, CLI commands are now `astrameter` and `astra-sim`. Docker image moved from `ghcr.io/tomquist/b2500-meter` to `ghcr.io/tomquist/astrameter` (the legacy `ghcr.io/tomquist/b2500-meter` image is still published in parallel for backward compatibility). Home Assistant users must update their app repository URL to `https://github.com/tomquist/astrameter#main` ([#302](https://github.com/tomquist/astrameter/pull/302), [#304](https://github.com/tomquist/astrameter/pull/304)).
- **Removed CT001 emulation** (Python `ct001` package and the `nodered.json` flow). Use `ct002`/`ct003` for multiple storage devices, or a Shelly `DEVICE_TYPE` otherwise. Drop obsolete `[GENERAL]` options `DISABLE_SUM_PHASES`, `DISABLE_ABSOLUTE_VALUES`, and `POLL_INTERVAL` if present. The Home Assistant app no longer offers `poll_interval` or `disable_absolute_values`; remove those keys from saved app configuration if validation fails after upgrade ([#258](https://github.com/tomquist/astrameter/pull/258)).
- **Changed Shelly emulator default:** event-driven powermeters (MQTT, SMA Speedwire, HomeWizard WS, HA WS) now block each Shelly response for up to 2 s waiting for a fresh push sample, then fall back to the cached value on timeout. Set `WAIT_FOR_NEXT_MESSAGE = False` under `[GENERAL]` or the powermeter section to restore the previous immediate-read behavior ([#322](https://github.com/tomquist/astrameter/pull/322), [#330](https://github.com/tomquist/astrameter/pull/330)).
- **Changed API:** the `Powermeter` base class is now async. Out-of-tree powermeter subclasses must implement `async get_powermeter_watts()`; the synchronous legacy interface has been removed ([#273](https://github.com/tomquist/astrameter/pull/273), [#274](https://github.com/tomquist/astrameter/pull/274), [#275](https://github.com/tomquist/astrameter/pull/275), [#276](https://github.com/tomquist/astrameter/pull/276), [#277](https://github.com/tomquist/astrameter/pull/277), [#278](https://github.com/tomquist/astrameter/pull/278), [#279](https://github.com/tomquist/astrameter/pull/279), [#282](https://github.com/tomquist/astrameter/pull/282)).
- **Removed** 32-bit ARM (`armhf` / `armv7`) Home Assistant images. Installations must use a 64-bit Home Assistant OS or supervisor environment (`amd64` or `aarch64`), consistent with Home Assistant dropping 32-bit support.
- **Removed** from-source / contributor workflow: Pipenv, `Pipfile`, and running `python main.py` from the repo root are gone — use **uv** and the **`astrameter`** command (or `uv run astrameter`) per [CONTRIBUTING.md](CONTRIBUTING.md).

### Added
- **Added** CT002/CT003 emulation for steering multiple Marstek storage devices over the Marstek CT UDP protocol. Active control is on by default (`ACTIVE_CONTROL = True`): the emulator smooths the grid reading, splits the target across batteries with a 15 W `BALANCE_DEADBAND`, and runs time-weighted saturation detection with handoff — set `ACTIVE_CONTROL = False` for relay mode (raw meter values forwarded, batteries decide). Includes fair-share balancing (`FAIR_DISTRIBUTION`, `BALANCE_GAIN`), manual target override and forced rotation via MQTT, ARP-based consumer discovery, and an opt-in efficiency mode that concentrates power on fewer batteries at low demand (`MIN_EFFICIENT_POWER`, `EFFICIENCY_ROTATION_INTERVAL`, probe-based fades, `SATURATION_GRACE_SECONDS`) ([#283](https://github.com/tomquist/astrameter/pull/283), [#284](https://github.com/tomquist/astrameter/pull/284), [#287](https://github.com/tomquist/astrameter/pull/287), [#289](https://github.com/tomquist/astrameter/pull/289), [#291](https://github.com/tomquist/astrameter/pull/291), [#293](https://github.com/tomquist/astrameter/pull/293), [#294](https://github.com/tomquist/astrameter/pull/294), [#296](https://github.com/tomquist/astrameter/pull/296), [#298](https://github.com/tomquist/astrameter/pull/298), [#301](https://github.com/tomquist/astrameter/pull/301), [#303](https://github.com/tomquist/astrameter/pull/303), [#310](https://github.com/tomquist/astrameter/pull/310), [#311](https://github.com/tomquist/astrameter/pull/311), [#320](https://github.com/tomquist/astrameter/pull/320), [#321](https://github.com/tomquist/astrameter/pull/321)).
- **Added** MQTT Insights: optional `[MQTT_INSIGHTS]` section publishes internal state (grid power, targets, saturation, consumer topology, EMA poll interval) to MQTT with Home Assistant Device Discovery; per-consumer active/pause + manual target control; Shelly battery offline availability; auto-configured in the HA app when Mosquitto is installed ([#292](https://github.com/tomquist/astrameter/pull/292), [#294](https://github.com/tomquist/astrameter/pull/294), [#297](https://github.com/tomquist/astrameter/pull/297), [#300](https://github.com/tomquist/astrameter/pull/300), [#306](https://github.com/tomquist/astrameter/pull/306)).
- **Added** optional Marstek MQTT responder alongside MQTT Insights (HA is the main use case): when `[MARSTEK]` is configured, AstraMeter can answer CT002/CT003 poll traffic on the same broker using the managed cloud MAC; with [hame-relay](https://github.com/tomquist/hame-relay) **≥ 1.3.5** on that broker the Marstek mobile app shows live readings (see README, MQTT Insights). On by default; set `MARSTEK_MQTT_ENABLED = false` in `[MQTT_INSIGHTS]` to disable only this add-on.
- **Added** opt-in web-based configuration editor (`WEB_CONFIG_ENABLED = True` in `[GENERAL]`) accessible at `http://<host>:52500/config`; supports editing all config sections and keys with type-appropriate inputs, comment preservation, and a Save & Restart button ([#319](https://github.com/tomquist/astrameter/pull/319)).
- **Added** HomeWizard P1 powermeter via the device WebSocket API, with optional `VERIFY_SSL` ([#231](https://github.com/tomquist/astrameter/pull/231), [#254](https://github.com/tomquist/astrameter/pull/254)).
- **Added** Enphase IQ Gateway (Envoy) powermeter via the local HTTPS `production.json` API, with optional Enlighten-cloud token acquisition and automatic refresh on 401, and auto-detection of single- vs three-phase readings ([#245](https://github.com/tomquist/astrameter/pull/245)).
- **Added** SMA Energy Meter / Sunny Home Manager support via Speedwire multicast with device auto-detection and per-phase readings ([#252](https://github.com/tomquist/astrameter/pull/252)).
- **Added** SML powermeter for smart meters over a local serial port (IR head), with optional per-phase OBIS overrides ([#229](https://github.com/tomquist/astrameter/pull/229)).
- **Added** multi-phase support to the MQTT powermeter via `TOPICS` / `JSON_PATHS` ([#280](https://github.com/tomquist/astrameter/pull/280), [issue #136](https://github.com/tomquist/b2500-meter/issues/136)).
- **Added** multi-phase support to the Tasmota powermeter via comma-separated `JSON_POWER_MQTT_LABEL` ([#281](https://github.com/tomquist/astrameter/pull/281)).
- **Added** multi-phase support to the VZLogger powermeter via comma-separated `UUID` values; phases are fetched in parallel ([#332](https://github.com/tomquist/astrameter/pull/332)).
- **Added** PID controller support for any powermeter via `PID_KP`, `PID_KI`, `PID_KD`, `PID_OUTPUT_MAX`, and `PID_MODE` (global or per-section), with built-in anti-windup ([#315](https://github.com/tomquist/astrameter/pull/315)).
- **Added** per-powermeter EMA smoothing and deadband wrappers — `SMOOTH_TARGET_ALPHA`, `MAX_SMOOTH_STEP`, `DEADBAND` (global `[GENERAL]` fallback with per-section override; off by default) ([#331](https://github.com/tomquist/astrameter/pull/331)).
- **Added** optional Hampel outlier filter for noisy powermeter sources (MQTT / HTTP / WiFi glitches): `HAMPEL_WINDOW`, `HAMPEL_N_SIGMA`, `HAMPEL_MIN_THRESHOLD` (global `[GENERAL]` fallback with per-section override; off by default; sits in the wrapper chain after throttling and before EMA smoothing) ([#334](https://github.com/tomquist/astrameter/pull/334)).
- **Added** `POWER_OFFSET` and `POWER_MULTIPLIER` transforms for any powermeter, including per-phase calibration, sign flipping, and phase nulling; the Home Assistant app exposes both as optional advanced fields ([#250](https://github.com/tomquist/astrameter/pull/250), [#308](https://github.com/tomquist/astrameter/pull/308)).
- **Added** `DEDUPE_TIME_WINDOW` to the Shelly emulator to drop burst-repeat requests from the same battery IP; the value can also be set under `[GENERAL]` to apply regardless of which device type is emulated ([#333](https://github.com/tomquist/astrameter/pull/333)).
- **Added** MQTT broker configuration via a single `BROKER_URI` (auth and TLS schemes) alongside the existing host/port/user/pass keys ([#309](https://github.com/tomquist/astrameter/pull/309)).
- **Added** optional Marstek cloud auto-registration for managed fake CT devices at startup under `[MARSTEK]` ([#237](https://github.com/tomquist/astrameter/pull/237)).
- **Added** `LOG_LEVEL` environment variable support for Docker and CLI runs ([#174](https://github.com/tomquist/astrameter/pull/174)).
- **Added** timestamps to application log lines ([#260](https://github.com/tomquist/astrameter/pull/260)).
- **Added** `GIT_COMMIT_SHA` embedding in CI-built container images; startup logs the git commit and `/health` JSON includes `git_commit` when set ([#273](https://github.com/tomquist/astrameter/pull/273)).
- **Added** `exc_info` to logger warnings for better debugging ([#307](https://github.com/tomquist/astrameter/pull/307)).

### Changed
- **Switched** the Home Assistant powermeter integration from REST polling to the WebSocket API ([#232](https://github.com/tomquist/astrameter/pull/232)).
- **Expanded** Shelly emulation logs to report battery detection, inactivity, and reconnection events ([#241](https://github.com/tomquist/astrameter/pull/241)).
- **Reduced** throttling output noise by replacing unconditional `print` calls in `ThrottledPowermeter` with structured logging (`logger.debug` for routine wait/fetch/cache messages; failures remain at error level) ([#251](https://github.com/tomquist/astrameter/pull/251)).
- **Improved** Shelly UDP server robustness by adding socket timeouts to avoid hangs during shutdown and testing ([#233](https://github.com/tomquist/astrameter/pull/233)).
- **Upgraded** `JSON_PATHS` parsing in the JSON HTTP and MQTT powermeters to the `jsonpath-ng` extended syntax, so values that arrive with a unit suffix (e.g. openHAB `Number:Power` returning `"331.74 W"`) can be sanitized inside the path with `` `split(...)` `` or `` `sub(/regex/, replacement)` `` instead of failing the float conversion ([#349](https://github.com/tomquist/astrameter/pull/349)).

### Fixed
- **Fixed** Modbus `UNIT_ID` handling and clarified Home Assistant entity ID configuration in the docs ([#191](https://github.com/tomquist/astrameter/pull/191), [#195](https://github.com/tomquist/astrameter/pull/195)).

## 1.0.8
- Added support for Modbus holding registers through new `REGISTER_TYPE` configuration option ([#173](https://github.com/tomquist/b2500-meter/pull/173))
- Improved Shelly emulator with threaded UDP handling for better performance under concurrent requests when throttle interval is used ([#168](https://github.com/tomquist/b2500-meter/pull/168))
- Enhanced TQ Energy Manager with signed power calculation using separate import/export OBIS codes ([#153](https://github.com/tomquist/b2500-meter/pull/153))
- Fixed powermeter test results to log at info level instead of debug level ([#165](https://github.com/tomquist/b2500-meter/pull/165))

## 1.0.7
- Added support for TQ Energy Manager devices through new TQ EM powermeter integration
- Added generic JSON HTTP powermeter integration with JSONPath support for flexible data extraction
- Fixed health check service port from 8124 to 52500

## 1.0.6
- Modbus: Support powermeters spanning multiple registers
- Modbus: Allow changing endianess
- Add dedicated health service module with endpoints on port 52500
- Implement multi-layer auto-restart: supervisor watchdog, startup retries, health checks

## 1.0.5
- Added throttling of powermeter readings for slow data sources to prevent oscillation.

## 1.0.4

### Added
- Added support for Shelly Pro 3EM on port 2220 (B2500 firmware version >=226)
- Added backward compatibility for Shelly Pro 3EM devices through shellypro3em_old (port 1010) and shellypro3em_new (port 2220) device types

## 1.0.3

### Added
- Support for three-phase power monitoring in Home Assistant integration
- Support for multiple powermeters (not through the HomeAssistant addon at this point)
- Allow providing custom config file in HA Addon

## 1.0.0 - Initial Release

- Initial release of B2500 Meter
- Support for emulating a CT001, Shelly Pro 3EM, Shelly EM gen3 and Shelly Pro EM50 for Marstek/Hame storages
- Support for various power meter integrations:
  - Shelly devices (1PM, Plus1PM, EM, 3EM, 3EMPro)
  - Tasmota devices
  - Home Assistant
  - MQTT
  - Modbus
  - ESPHome
  - VZLogger
  - AMIS Reader
  - IoBroker
  - Emlog
  - Shrdzm
  - Script execution
