# Changelog

## Next
- Added MQTT Insights: optional `[MQTT_INSIGHTS]` section publishes internal state (grid power, targets, saturation, consumer topology) to MQTT with Home Assistant Device Discovery and per-consumer active/pause control, manual target override, auto/manual target toggle, force efficiency rotation button, and Shelly battery offline availability; auto-configured in HA add-on when Mosquitto is installed
- Added 3-phase support for Tasmota powermeter: comma-separated `JSON_POWER_MQTT_LABEL` / input / output labels ([#136](https://github.com/tomquist/b2500-meter/issues/136))
- Added opt-in efficiency optimization for multi-battery setups: concentrates power on fewer batteries at low demand to avoid inefficient low-power operation, with hysteresis, time-based rotation for fairness, probe-based handoffs that keep the previous battery online until the promoted battery shows meaningful output, smooth fade transitions after a successful probe, and saturation-aware forced rotation when an active battery is stuck at near-zero output (`MIN_EFFICIENT_POWER`, `EFFICIENCY_ROTATION_INTERVAL`, `EFFICIENCY_FADE_ALPHA`, `EFFICIENCY_SATURATION_THRESHOLD`, `SATURATION_DECAY_FACTOR`, `SATURATION_GRACE_SECONDS`, `SATURATION_STALL_TIMEOUT_SECONDS`). `EFFICIENCY_SATURATION_THRESHOLD` now defaults to 0.4 (previously 0, disabled) so full/empty batteries are swapped out automatically; batteries that produce meaningful output but lag behind a fluctuating target are no longer falsely flagged as saturated; newly promoted batteries now get an explicit probe window that keeps grid tracking stable while slow real-world ramp-up is evaluated, while mid-interval saturation detection still catches batteries that stop following their target after the handoff.
- Added multi-phase support for MQTT powermeter: multiple topics (`TOPICS`) or multiple JSON paths (`JSON_PATHS`) from a single topic ([#208](https://github.com/tomquist/b2500-meter/issues/208), [#280](https://github.com/tomquist/b2500-meter/pull/280))
- Added support for emulating a *CT002/CT003*, which is recommended to steer multiple devices
- Added HomeWizard P1 powermeter support via the device WebSocket API with token and serial configuration ([#231](https://github.com/tomquist/b2500-meter/pull/231)), including optional `VERIFY_SSL` to disable TLS certificate verification on trusted networks when needed ([#254](https://github.com/tomquist/b2500-meter/pull/254))
- Added SMA Energy Meter / Sunny Home Manager support via Speedwire multicast protocol with auto-detection and per-phase power readings ([#231](https://github.com/tomquist/b2500-meter/pull/252))
- Added SML powermeter support for power readings from a smart meter over a local serial port (Smart Message Language / IR head), with optional per-phase OBIS overrides ([#229](https://github.com/tomquist/b2500-meter/pull/229))
- Switched the Home Assistant powermeter integration from REST polling to the WebSocket API and improved missing/non-numeric sensor state handling ([#232](https://github.com/tomquist/b2500-meter/pull/232), [#193](https://github.com/tomquist/b2500-meter/pull/193))
- Added optional Marstek cloud auto-registration for managed fake `ct002` and `ct003` devices at startup ([#237](https://github.com/tomquist/b2500-meter/pull/237))
- Added battery activity info logs for Shelly emulation to report detection, inactivity, and reconnection events ([#241](https://github.com/tomquist/b2500-meter/pull/241))
- Added `POWER_OFFSET` and `POWER_MULTIPLIER` transforms for any powermeter, including per-phase calibration, sign flipping, and phase nulling support, plus improved validation and logging with support for `POWER_MULTIPLIER = 0`, one-time phase-mismatch warnings, and preserved exception chaining for invalid float lists ([#250](https://github.com/tomquist/b2500-meter/pull/250))
- Added `LOG_LEVEL` environment variable support for Docker and CLI runs ([#174](https://github.com/tomquist/b2500-meter/pull/174))
- Reduced throttling output noise by replacing unconditional `print` calls in `ThrottledPowermeter` with structured logging (`logger.debug` for routine wait/fetch/cache messages; failures remain at error level) ([#251](https://github.com/tomquist/b2500-meter/pull/251))
- Improved Shelly UDP server robustness by adding socket timeouts to avoid hangs during shutdown and testing ([#233](https://github.com/tomquist/b2500-meter/pull/233))
- Fixed Modbus `UNIT_ID` handling and clarified Home Assistant entity ID configuration in the docs ([#191](https://github.com/tomquist/b2500-meter/pull/191), [#195](https://github.com/tomquist/b2500-meter/pull/195))
- CI-built container images embed **`GIT_COMMIT_SHA`**; startup logs the git commit and `/health` JSON includes **`git_commit`** when set
- **Developer tooling:** Python **3.10+**; **uv** + **pyproject.toml** replace Pipenv; application code lives under **src/b2500_meter/**; **ruff**, **mypy**, and **pytest** (see [CONTRIBUTING.md](CONTRIBUTING.md)); CLI entry point **`b2500-meter`**; Docker and Home Assistant add-on images install the wheel and use the same command instead of `python main.py`.
- Added timestamps to application log lines ([#260](https://github.com/tomquist/b2500-meter/pull/260))

### Breaking
- The Home Assistant add-on no longer publishes images for 32-bit ARM (`armhf` / `armv7`). Installations must use a 64-bit Home Assistant OS or supervisor environment (`amd64` or `aarch64`), consistent with Home Assistant dropping 32-bit support.
- **CT001 emulation removed** (Python `ct001` package and the `nodered.json` flow). Use `ct002` or `ct003` for multiple storage devices; use a Shelly `DEVICE_TYPE` otherwise (replacing `ct001`). Drop obsolete `[GENERAL]` options `DISABLE_SUM_PHASES`, `DISABLE_ABSOLUTE_VALUES`, and `POLL_INTERVAL` if present. The Home Assistant add-on no longer offers `poll_interval` or `disable_absolute_values`; remove those keys from saved add-on configuration if validation fails after upgrade ([#258](https://github.com/tomquist/b2500-meter/pull/258)).
- **From-source / contributor workflow:** Pipenv, `Pipfile`, and running `python main.py` from the repo root are removed—use **uv** and the **`b2500-meter`** command (or `uv run b2500-meter`) per [CONTRIBUTING.md](CONTRIBUTING.md).

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
