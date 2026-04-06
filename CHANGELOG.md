# Changelog

## Next
- **Breaking:** Rebrand project from "B2500 Meter" to "AstraMeter" (formerly b2500-meter). Package renamed to `astrameter`, CLI commands are now `astrameter` and `astra-sim`. Docker image moved from `ghcr.io/tomquist/b2500-meter` to `ghcr.io/tomquist/astrameter` (the legacy `ghcr.io/tomquist/b2500-meter` image is still published in parallel for backward compatibility). Home Assistant users must update their app repository URL to `https://github.com/tomquist/astrameter#main`.
- Added CT002/CT003 emulation for steering multiple Marstek storage devices over the Marstek CT UDP protocol, with opt-in efficiency optimization that concentrates power on fewer batteries at low demand and rotates fairly over time (`MIN_EFFICIENT_POWER`, `EFFICIENCY_ROTATION_INTERVAL`, and related tuning options)
- Added MQTT Insights: optional `[MQTT_INSIGHTS]` section publishes internal state (grid power, targets, saturation, consumer topology) to MQTT with Home Assistant Device Discovery, per-consumer active/pause control, manual target override, and Shelly battery offline availability; auto-configured in the HA app when Mosquitto is installed
- Added HomeWizard P1 powermeter support via the device WebSocket API with optional `VERIFY_SSL` ([#231](https://github.com/tomquist/astrameter/pull/231), [#254](https://github.com/tomquist/astrameter/pull/254))
- Added SMA Energy Meter / Sunny Home Manager support via Speedwire multicast with auto-detection and per-phase readings ([#252](https://github.com/tomquist/astrameter/pull/252))
- Added SML powermeter support for smart meters over a local serial port (IR head), with optional per-phase OBIS overrides ([#229](https://github.com/tomquist/astrameter/pull/229))
- Added multi-phase support for Tasmota (`JSON_POWER_MQTT_LABEL`) and MQTT (`TOPICS` / `JSON_PATHS`) powermeters ([#136](https://github.com/tomquist/astrameter/issues/136), [#280](https://github.com/tomquist/astrameter/pull/280))
- Added `POWER_OFFSET` and `POWER_MULTIPLIER` transforms for any powermeter, including per-phase calibration, sign flipping, and phase nulling ([#250](https://github.com/tomquist/astrameter/pull/250))
- Added optional Marstek cloud auto-registration for managed fake CT devices at startup ([#237](https://github.com/tomquist/astrameter/pull/237))
- Switched the Home Assistant powermeter integration from REST polling to the WebSocket API ([#232](https://github.com/tomquist/astrameter/pull/232))
- Added `LOG_LEVEL` environment variable support for Docker and CLI runs ([#174](https://github.com/tomquist/astrameter/pull/174))
- Added timestamps to application log lines ([#260](https://github.com/tomquist/astrameter/pull/260))
- CI-built container images embed `GIT_COMMIT_SHA`; startup logs the git commit and `/health` JSON includes `git_commit` when set
- Fixed Modbus `UNIT_ID` handling and clarified Home Assistant entity ID configuration in the docs ([#191](https://github.com/tomquist/astrameter/pull/191), [#195](https://github.com/tomquist/astrameter/pull/195))
- Added battery activity info logs for Shelly emulation to report detection, inactivity, and reconnection events ([#241](https://github.com/tomquist/astrameter/pull/241))
- Reduced throttling output noise by replacing unconditional `print` calls in `ThrottledPowermeter` with structured logging (`logger.debug` for routine wait/fetch/cache messages; failures remain at error level) ([#251](https://github.com/tomquist/astrameter/pull/251))
- Improved Shelly UDP server robustness by adding socket timeouts to avoid hangs during shutdown and testing ([#233](https://github.com/tomquist/astrameter/pull/233))

### Breaking
- The Home Assistant app no longer publishes images for 32-bit ARM (`armhf` / `armv7`). Installations must use a 64-bit Home Assistant OS or supervisor environment (`amd64` or `aarch64`), consistent with Home Assistant dropping 32-bit support.
- **CT001 emulation removed** (Python `ct001` package and the `nodered.json` flow). Use `ct002` or `ct003` for multiple storage devices; use a Shelly `DEVICE_TYPE` otherwise (replacing `ct001`). Drop obsolete `[GENERAL]` options `DISABLE_SUM_PHASES`, `DISABLE_ABSOLUTE_VALUES`, and `POLL_INTERVAL` if present. The Home Assistant app no longer offers `poll_interval` or `disable_absolute_values`; remove those keys from saved app configuration if validation fails after upgrade ([#258](https://github.com/tomquist/astrameter/pull/258)).
- **From-source / contributor workflow:** Pipenv, `Pipfile`, and running `python main.py` from the repo root are removed—use **uv** and the **`astrameter`** command (or `uv run astrameter`) per [CONTRIBUTING.md](CONTRIBUTING.md).

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
