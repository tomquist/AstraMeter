# Changelog

## Next

- **Fixed** the system reacting slowly or hunting back and forth after a load or solar change. Multi-battery control settles faster and with less overshoot, especially with batteries that poll at different speeds or run with efficiency optimization enabled ([#469](https://github.com/tomquist/astrameter/issues/469)); and when the meter the controller acts on lags reality (e.g. a Home Assistant push sensor with measurement delay), active control no longer hunts indefinitely with the grid never settling at zero — per-battery corrections are now **oscillation-damped**: the balancer detects when a battery's command keeps reversing direction (the signature of hunting, not a real load change) and bleeds the loop gain that sustains it, while leaving genuine load/solar reactions at full speed ([#473](https://github.com/tomquist/astrameter/issues/473)). Tune or disable the damper with the new `OSC_DAMP_MAX` / `OSC_DAMP_ALPHA` / `OSC_DAMP_DECAY` / `OSC_DAMP_THRESHOLD` options (`osc_damp_*` on ESPHome).

- **Changed** active control to work *with* the battery firmware's own control loop instead of fighting it ([#458](https://github.com/tomquist/astrameter/issues/458)): per-battery commands are now **ramp-paced** — each poll's delta is capped starting at the firmware ramp's ~50 W first step and growing toward 200 W only while the battery demonstrably follows — and the balance deadband default was raised from 15 W to 25 W (above the battery's own ±20 W input deadband). In simulation across realistic household scenarios this cuts opposite-direction overshoot by ~80% and battery power churn by ~33% for ~10% slower settling; tune or disable with the new `PACE_BASE_STEP` / `PACE_MAX_STEP` options (`pace_base_step` / `pace_max_step` on ESPHome). A new evaluation harness (`python -m astrameter.simulator.evaluation`) measures reaction/oscillation/energy metrics per scenario, and CI posts a before/after comparison on every PR.
- **Changed** the CT002/CT003 emulator (Python and ESPHome) to behave more like a real CT: with active control off (`ACTIVE_CONTROL = False`), batteries now see each other's reported power, and batteries still detecting their phase or running in combined mode no longer skew the phase-A share split; a battery that goes silent now drops out after missing ~2 of its own poll cycles instead of after a fixed 120 s — set `CONSUMER_TTL` / `consumer_ttl` to keep a fixed window ([#457](https://github.com/tomquist/astrameter/issues/457), [#460](https://github.com/tomquist/astrameter/issues/460), [#462](https://github.com/tomquist/astrameter/issues/462)).
- **Changed** the bundled **battery simulator** and CT002/CT003 emulator to match real Marstek behavior more closely: the simulator now steers with the gain-scheduled, accelerating self-consumption controller real Venus-class hardware runs (replacing the old `output + grid` integral rule), including the firmware's pre-ramp input-conditioning gate — a >50 W single-sample spike filter, a ±20 W deadband, and a small-residual-import hold — so it no longer jitters against sub-threshold or transient grid noise near the null point; the non-physical `idle_on_cross_phase_discharge` simulator option (which modelled cross-phase coordination real firmware doesn't implement) was removed; relay mode (`ACTIVE_CONTROL = False`) now forwards the real per-phase battery count so several batteries on one phase each take their 1/N share instead of all chasing the full residual; the optional request `participate` field (the 7th field newer batteries such as the B2500 send) is now honored — a battery sending `participate=0` is excluded from per-phase aggregation and active-control distribution (absent/non-zero preserves prior behavior); and the simulator can now model a **DC-coupled B2500** (set a battery's `meter_dev_type` to an `HMA*`/`HMJ*`/`HMK*` type), which steers its DC output into an external microinverter with the B2500's integer hysteresis law and never charges from AC, instead of the Venus ramp.
- **Added** the AVM **FRITZ!Smart Energy 250** smart-meter read head as a power source (`[FRITZ]`). AstraMeter logs into the FRITZ!Box over the AHA-HTTP-Interface (`login_sid.lua` + `getdevicelistinfos`) and reads the read head's signed grid power by AIN — positive = import, negative = feed-in. See [docs/powermeters.md](docs/powermeters.md#fritzsmart-energy-250).
- **Added** the **Min DC Output** option to the Home Assistant add-on's Configuration tab, so it no longer requires a custom config file to set. The web config generator's add-on target now emits it too ([#446](https://github.com/tomquist/astrameter/issues/446)).
- **Fixed** the per-battery **AstraMeter Consumer** device in Home Assistant getting merged into the battery's own device (e.g. the one from hm2mqtt), so its controls and sensors disappeared onto the wrong device. AstraMeter no longer advertises the battery's MAC as a device *connection* — which HA uses as a global, cross-integration identity and would merge on — and instead keeps the consumer as its own device, linked to the meter via `via_device`. The merge was non-deterministic (it depended on MQTT discovery order), which is why it hit only some batteries ([#438](https://github.com/tomquist/astrameter/issues/438)).
- **Fixed** the web config generator producing invalid ESPHome YAML for HTTP/JSON power sources (e.g. HomeWizard P1, Shelly, Tasmota). The `url`, `capture_response`, and `on_response` keys were indented as siblings of the `http_request.get:` action instead of nested under it, so ESPHome rejected the file with `Cannot have two actions in one item` ([#477](https://github.com/tomquist/astrameter/issues/477)).
- **Fixed** the HomeWizard powermeter's MQTT Insights **Online** sensor flapping on/off while the P1 meter is in a broken state. A stalled dongle keeps accepting the WebSocket and replays a single cached reading on every watchdog reconnect, which briefly reset the freshness window; AstraMeter now treats the source as online only while readings flow as a continuous stream, so the sensor stays off until live data resumes ([#427](https://github.com/tomquist/astrameter/issues/427)).


## 2.1.2

- **Added** a **Min DC Output** option that keeps a DC battery's inverter (e.g. the Marstek B2500) from switching off at 0 W and falling asleep under high PV surplus. Set it globally (`MIN_DC_OUTPUT`) or per battery from Home Assistant; off by default ([#425](https://github.com/tomquist/astrameter/issues/425)).
- **Fixed** a phantom empty "Unnamed Device" that kept reappearing under the MQTT integration in Home Assistant, even after deleting it. AstraMeter now publishes a proper top-level **AstraMeter** device — with **Status**, **Version**, and **Consumer Count** entities — that the meter devices are grouped under. The hub is now published in standalone/Docker too (keyed on a base-topic fallback when `ADDON_SLUG` isn't set), so devices group there as well ([#421](https://github.com/tomquist/astrameter/issues/421)).
- **Fixed** batteries drifting or losing their phase reference during a brief power-meter dropout (e.g. a Home Assistant sensor going `unavailable`). The CT002/CT003 emulator now sends a zero adjustment so each battery holds its current output while the meter is down, instead of re-driving control from the last-known reading — which could wind a battery up or feed stale per-phase values into a Venus phase self-diagnosis ([#403](https://github.com/tomquist/astrameter/issues/403)).
- **Added** a per-powermeter diagnostic device in MQTT Insights: each configured power source gets an "AstraMeter Powermeter `<Section>`" device (grouped under the AstraMeter hub) with an **Online** connectivity sensor that flips off when the source stops delivering fresh, usable readings (a stalled or disconnected push stream, or a polling source whose reads start failing), plus **Power**/**L1**/**L2**/**L3** sensors showing the latest per-phase readings and their total. A phase that merely stops changing stays online; tune or disable the publish cadence with `POWERMETER_HEALTH_INTERVAL` ([#427](https://github.com/tomquist/astrameter/issues/427)).


## 2.1.1

- **Changed** transient meter-read failures (in the Shelly emulator and the CT002/CT003 emulator, e.g. when an HTTP source times out) now log a single-line warning instead of a full stack trace; the traceback is included only when running at `LOG_LEVEL = DEBUG` ([#404](https://github.com/tomquist/astrameter/issues/404)).
- **Added** the Hampel outlier filter and PID controller as optional fields in the Home Assistant add-on Configuration tab (alongside the existing smoothing/deadband options) — all optional and off unless you set them. The web config generator gained a "Home Assistant add-on" target that produces a ready-to-paste add-on options block including these filters.
- **Fixed** the power sensors published via MQTT Insights now carry `state_class: measurement`, so the instantly-updated grid/target/reported power entities can be used as power sources in the Home Assistant energy dashboard ([#416](https://github.com/tomquist/astrameter/issues/416)).
- **Fixed** the ESPHome MQTT Insights `device_id` now defaults to `device-1` (matching the Python add-on) instead of the ESPHome `ct002:` component id, so both stacks publish the same Home Assistant discovery node (e.g. `astrameter_ct002_device-1`). Existing ESPHome users who want to keep their previous device can set `device_id:` explicitly under `mqtt_insights:`.


## 2.1.0

- **Added** a per-battery **Distribution Weight** Home Assistant entity (default `1.0`) that biases how the balancer splits load across multiple batteries — e.g. set `1.5` and `1.0` for a ~60:40 split so a smaller battery no longer saturates first ([#412](https://github.com/tomquist/astrameter/discussions/412)). The per-battery controls (manual target, auto/active toggles, distribution weight) now each use a dedicated retained MQTT command topic, so Home Assistant restores their values across an AstraMeter restart.
- **Fixed** intermittent CT stalls when an upstream HTTP power meter was slow to respond: the read timeout for the polling HTTP sources (Shelly, AMIS Reader, emlog, ESPHome, ioBroker, generic JSON-HTTP, SHRDZM, Tasmota, VZLogger) is now 2s with a 1s connect timeout (was 10s), so an unresponsive meter fails fast and the next battery poll can recover instead of pinning a request handler ([#404](https://github.com/tomquist/astrameter/issues/404)).
- **Added** a project website (`web/`, deployable to GitHub Pages): a landing page introducing AstraMeter (features, supported devices and power meters, installation options, FAQ) plus a beginner-friendly config generator that walks you through a few questions and produces a ready-to-use Python `config.ini` or ESPHome YAML, with a live preview, save/load/share, and step-by-step ESPHome flashing guidance ([#399](https://github.com/tomquist/astrameter/pull/399)).
- **Added** experimental ESPHome external component `ct002` to run the CT002/CT003 emulator directly on an ESP32. See `esphome.example.yaml` and the per-meter grid-power sensor reference in `docs/esphome-powermeters.md` (which also lists meters not yet supported on the ESP, e.g. Enphase Envoy and the SMA Energy Meter). Per-source `config.ini` documentation moved out of the README into `docs/powermeters.md` ([#385](https://github.com/tomquist/astrameter/pull/385), [#397](https://github.com/tomquist/astrameter/pull/397)).
- **Added** Modbus UDP support via a `TRANSPORT = TCP|UDP` option in the `[MODBUS]` section (defaults to `TCP`) ([#387](https://github.com/tomquist/astrameter/pull/387)).
- **Fixed** Home Assistant powermeter timing out on startup with "Timeout waiting for Home Assistant state" when the configured sensor hasn't changed value since AstraMeter started ([#382](https://github.com/tomquist/astrameter/pull/382), [#383](https://github.com/tomquist/astrameter/pull/383)).
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
