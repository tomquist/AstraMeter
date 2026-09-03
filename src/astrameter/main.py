import argparse
import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Sequence
from dataclasses import fields, replace
from typing import Any

from astrameter.cloud_reporting import (
    CloudReporter,
    CloudReporterConfig,
    CtMeasurement,
)
from astrameter.config import addon
from astrameter.config.config_loader import ClientFilter
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.logger import logger, setLogLevel
from astrameter.config.settings import (
    CT_DEVICE_TYPES,
    DEVICE_TYPES,
    AppConfig,
    ConfiguredPowermeter,
    CtSettings,
    GeneralSettings,
    MarstekSettings,
    is_ct,
)
from astrameter.ct002 import CT002
from astrameter.ct002.balancer import BalancerConfig
from astrameter.ct002.ct002 import CT002Request
from astrameter.marstek_api import (
    MarstekApiError,
    MarstekConfig,
    ensure_managed_fake_device,
)
from astrameter.meter_pool import powermeter_for, powermeter_name, read_fresh
from astrameter.mqtt_insights import (
    MarstekMqttBinding,
    MqttInsightsService,
    format_cd4_slave_csv,
    normalize_mac,
    ver_v_from_marstek_api_version,
)
from astrameter.power_units import three_phases
from astrameter.powermeter import Powermeter
from astrameter.shelly import Shelly
from astrameter.status import StatusRegistry, detect_config_mode
from astrameter.version_info import get_git_commit_sha, get_version
from astrameter.web_server import WebServer, parse_allowed_hosts

#: How long a read waits for a fresh push before serving the cached value.
FRESH_READING_TIMEOUT_S = 2.0


async def read_ct_powermeter(
    addr: tuple[str, int],
    powermeters: list[ConfiguredPowermeter],
) -> list[float] | None:
    """Pick the powermeter matching *addr* and return up to three phase values."""
    configured = powermeter_for(powermeters, addr[0])
    if configured is None:
        logger.debug("No powermeter found for client %s", addr[0])
        return None
    return three_phases(await read_fresh(configured))


async def _read_grid_phases(powermeters: list[ConfiguredPowermeter]) -> list[float]:
    """Raw three-phase reading from the meter that serves every client (or the
    first one), waiting briefly for a fresh push so an idle meter cannot pin
    the caller."""
    chosen = next(
        (c.powermeter for c in powermeters if c.client_filter.matches("0.0.0.0")), None
    )
    if chosen is None and powermeters:
        chosen = powermeters[0].powermeter
    if chosen is None:
        return [0.0, 0.0, 0.0]
    with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
        await asyncio.wait_for(
            chosen.wait_for_next_message(), timeout=FRESH_READING_TIMEOUT_S
        )
    return [float(v) for v in three_phases(await chosen.get_powermeter_watts_raw())]


async def check_powermeter(powermeter: Powermeter, client_filter: ClientFilter) -> None:
    """Prove the meter delivers a reading before any battery is served."""
    attempts = 4
    retry_delay_s = 5
    for attempt in range(1, attempts + 1):
        try:
            logger.debug("Testing powermeter (attempt %d/%d)", attempt, attempts)
            await powermeter.wait_for_message(timeout=30)
            values = await powermeter.get_powermeter_watts()
        except Exception as exc:
            logger.debug("Powermeter test attempt %d failed: %s", attempt, exc)
            if attempt == attempts:
                raise RuntimeError(
                    f"Failed to test powermeter after {attempts} attempts: {exc}"
                ) from exc
            logger.info("Retrying powermeter test in %d seconds...", retry_delay_s)
            await asyncio.sleep(retry_delay_s)
            continue
        logger.info(
            "Successfully fetched %s powermeter value (filter %s): %s",
            powermeter_name(powermeter),
            ", ".join(str(n) for n in client_filter.netmasks),
            " | ".join(f"{v}W" for v in values),
        )
        return


def _reset_all_powermeters(powermeters: Sequence[ConfiguredPowermeter]) -> None:
    for configured in powermeters:
        configured.powermeter.reset()


def _build_ct002(
    ct: CtSettings,
    ct_type: str,
    device_id: str,
    debug_status: bool,
    reset_fn: Callable[[], None] | None,
) -> CT002:
    """Create the emulator a :class:`CtSettings` describes."""
    return CT002(
        udp_port=ct.udp_port,
        ct_type=ct_type,
        ct_mac=ct.ct_mac,
        wifi_rssi=ct.wifi_rssi,
        dedupe_time_window=ct.dedupe_time_window,
        consumer_ttl=ct.consumer_ttl,
        debug_status=debug_status,
        active_control=ct.active_control,
        balancer=_balancer_config(ct),
        saturation_detection=ct.saturation_detection,
        saturation_alpha=ct.saturation_alpha,
        min_target_for_saturation=ct.min_target_for_saturation,
        saturation_decay_factor=ct.saturation_decay_factor,
        saturation_grace_seconds=ct.saturation_grace_seconds,
        saturation_stall_timeout_seconds=ct.saturation_stall_timeout_seconds,
        device_id=device_id,
        reset_fn=reset_fn,
    )


def _balancer_config(ct: CtSettings) -> BalancerConfig:
    """Every balancer knob is a ``CtSettings`` field of the same name, so a
    new one reaches the balancer as soon as both sides declare it."""
    return BalancerConfig(
        **{f.name: getattr(ct, f.name) for f in fields(BalancerConfig)}
    )


def _log_ct_settings(ct: CtSettings, device_type: str, device_id: str) -> None:
    if 0 < ct.min_dc_output < ct.min_target_for_saturation:
        logger.warning(
            "MIN_DC_OUTPUT (%gW) is below MIN_TARGET_FOR_SATURATION (%dW): a "
            "floored battery's target never clears the saturation gate, so an "
            "empty/full unit can't be detected. Consider MIN_DC_OUTPUT >= %d.",
            ct.min_dc_output,
            ct.min_target_for_saturation,
            ct.min_target_for_saturation,
        )
    logger.debug("%s settings for %s: %s", device_type.upper(), device_id, ct)
    if not ct.active_control:
        logger.debug("CT control model: relay (forward consumer aggregates)")
        return
    extras = []
    if ct.fair_distribution:
        extras.append("fair distribution")
    if ct.saturation_detection:
        extras.append("saturation detection")
    if ct.min_efficient_power > 0:
        extras.append(f"efficiency optimization ({ct.min_efficient_power}W)")
    logger.info("Active control enabled: %s", " + ".join(["load split", *extras]))


def _forward_events(
    insights: MqttInsightsService, device: CT002 | Shelly
) -> Callable[[str, str, dict[str, Any]], None]:
    """Route a device's per-battery events to MQTT Insights.  A battery that
    the device evicted arrives as ``{"_removed": True}``."""
    on_update: Callable[[str, str, dict[str, Any]], None]
    on_removed: Callable[[str, str], None]
    if isinstance(device, CT002):
        on_update = insights.on_ct002_response
        on_removed = insights.on_ct002_consumer_removed
    else:
        on_update = insights.on_shelly_response
        on_removed = insights.on_shelly_battery_removed

    def listener(device_id: str, battery_id: str, data: dict) -> None:
        if data.get("_removed"):
            on_removed(device_id, battery_id)
        else:
            on_update(device_id, battery_id, data)

    return listener


def _build_device(
    device_type: str,
    ct: CtSettings | None,
    general: GeneralSettings,
    powermeters: list[ConfiguredPowermeter],
    device_id: str,
) -> CT002 | Shelly:
    if ct is not None:
        ct_type = DEVICE_TYPES[device_type].ct_type
        debug_status = ct.debug_status or os.environ.get(
            "DEBUG_STATUS", ""
        ).lower() in ("1", "true", "yes")
        _log_ct_settings(ct, device_type, device_id)
        device = _build_ct002(
            ct,
            ct_type,
            device_id,
            debug_status,
            lambda: _reset_all_powermeters(powermeters),
        )

        async def update_readings(
            addr: tuple[str, int],
            _request: CT002Request | None = None,
            _consumer_id: str | None = None,
        ) -> list[float] | None:
            return await read_ct_powermeter(addr, powermeters)

        device.before_send = update_readings
        return device
    udp_port = (
        DEVICE_TYPES[device_type].udp_port if device_type in DEVICE_TYPES else None
    )
    if udp_port is not None:
        logger.debug("Shelly settings: device id %s, type %s", device_id, device_type)
        return Shelly(
            powermeters=powermeters,
            device_id=device_id,
            device_type=device_type,
            udp_port=udp_port,
            dedupe_time_window=general.dedupe_time_window,
        )
    raise ValueError(f"Unsupported device type: {device_type}")


async def _bind_marstek_responder(
    insights: MqttInsightsService,
    device: CT002,
    device_id: str,
    powermeters: list[ConfiguredPowermeter],
    marstek_mac: str,
    marstek_ver_v: int | None,
) -> None:
    """Answer Marstek app polls over MQTT, if a managed MAC gives hame-relay
    something to route the replies back to."""
    if not marstek_mac:
        logger.info(
            "Marstek MQTT responder not wired for %s: no managed MAC "
            "available. Enable [MARSTEK] with MAILBOX/PASSWORD to use "
            "this feature, or set MARSTEK_MQTT_ENABLED=false to silence "
            "this notice.",
            device_id,
        )
        return
    await insights.register_marstek(
        MarstekMqttBinding(
            device_id=device_id,
            ct_type=device.ct_type,
            mac=marstek_mac,
            get_values=lambda: _read_grid_phases(powermeters),
            get_connected_slave_count=device.reporting_consumer_count,
            get_cd4_slave_csv=lambda: format_cd4_slave_csv(
                device.reporting_consumer_rows()
            ),
            wifi_rssi=device.wifi_rssi,
            ver_v=(
                marstek_ver_v
                if marstek_ver_v is not None
                else ver_v_from_marstek_api_version(None)
            ),
        )
    )


def _ct_measurement(
    device: CT002, phases: list[float], mqtt_connected: bool
) -> CtMeasurement:
    """What a real CT reports to the cloud: the grid phases plus the charge and
    discharge power aggregated per phase bucket."""
    ap, bp, cp = (round(p) for p in phases)
    buckets = device.reporting_phase_buckets()

    def charge(bucket: str) -> int:
        return buckets[bucket].chrg_power

    def discharge(bucket: str) -> int:
        return buckets[bucket].dchrg_power

    return CtMeasurement(
        ap=ap,
        bp=bp,
        cp=cp,
        dp=ap + bp + cp,
        rssi=device.wifi_rssi,
        slv=device.reporting_consumer_count(),
        udp=1,
        mqtt=1 if mqtt_connected else 0,
        cz=charge("x"),
        ca=charge("A"),
        cb=charge("B"),
        cc=charge("C"),
        cd=charge("ABC"),
        dz=discharge("x"),
        da=discharge("A"),
        db=discharge("B"),
        dc=discharge("C"),
        dd=discharge("ABC"),
    )


def _start_cloud_reporting(
    device: CT002,
    ct: CtSettings,
    device_id: str,
    powermeters: list[ConfiguredPowermeter],
    report_id: str,
    mqtt_connected: bool,
    registry: StatusRegistry | None,
) -> asyncio.Task[None] | None:
    """Opt-in reporting to hamedata.com the way a real CT does: a handshake,
    then a periodic report with live grid and bucket data."""
    if not report_id:
        logger.warning(
            "CLOUD_REPORTING enabled for %s but no device id is available; "
            "set CT_MAC, or enable the Marstek account so the registered "
            "device id is used. Cloud reporting disabled.",
            device_id,
        )
        return None

    async def gather() -> CtMeasurement:
        return _ct_measurement(
            device, await _read_grid_phases(powermeters), mqtt_connected
        )

    reporter = CloudReporter(
        CloudReporterConfig(
            ct_type=device.ct_type,
            device_id=report_id,
            host=ct.cloud_reporting_host,
            interval_seconds=ct.cloud_reporting_interval,
        ),
        gather,
    )
    if registry is not None:
        registry.cloud_reporters[device_id] = reporter
    return asyncio.create_task(reporter.run())


async def run_device(
    device_type: str,
    config: AppConfig,
    general: GeneralSettings,
    powermeters: list[ConfiguredPowermeter],
    device_id: str = "",
    insights: MqttInsightsService | None = None,
    marstek_mac: str = "",
    marstek_ver_v: int | None = None,
    registry: StatusRegistry | None = None,
) -> None:
    """Run one emulated device until it stops, wiring it to the optional
    integrations (MQTT Insights, the Marstek responder, cloud reporting)."""
    logger.debug("Starting device: %s", device_type)
    ct = config.ct(device_type) if is_ct(device_type) else None
    device = _build_device(device_type, ct, general, powermeters, device_id)
    if insights:
        device.event_listener = _forward_events(insights, device)

    try:
        await device.start()
    except Exception:
        # One device failing to start (a port conflict, say) must not take
        # down the others.  It stays absent from the dashboard rather than
        # reporting zeros.
        logger.exception("Device %s (%s) failed to start", device_type, device_id)
        try:
            await device.stop()
        except Exception:
            logger.exception(
                "Device %s (%s) cleanup also failed", device_type, device_id
            )
        return

    if registry is not None:
        registry.register_device(device_id, device_type, device)

    cloud_task = None
    if isinstance(device, CT002) and ct is not None:
        if insights:
            # Only now, so MQTT commands never reach a device that failed to
            # come up.
            insights.register_device(device_id, device)
            if insights.marstek_mqtt_enabled:
                await _bind_marstek_responder(
                    insights, device, device_id, powermeters, marstek_mac, marstek_ver_v
                )
        if ct.cloud_reporting:
            # The cloud knows the CT by the MAC registered in the Marstek
            # account when there is one, else by the locally set CT_MAC.
            cloud_task = _start_cloud_reporting(
                device,
                ct,
                device_id,
                powermeters,
                marstek_mac or ct.ct_mac,
                insights is not None,
                registry,
            )

    try:
        await device.wait()
    finally:
        if registry is not None:
            registry.unregister_device(device_id)
        if cloud_task is not None:
            cloud_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cloud_task
        if insights and isinstance(device, CT002):
            insights.unregister_device(device_id)
            with contextlib.suppress(Exception):
                await insights.unregister_marstek(device_id)
        try:
            await device.stop()
        except Exception:
            logger.exception("Device %s (%s) failed to stop", device_type, device_id)


async def async_main(
    config: AppConfig,
    general: GeneralSettings,
    device_types: list[str],
    device_ids: list[str],
    skip_test: bool,
    managed_marstek: dict[str, tuple[str, int]] | None = None,
    registry: StatusRegistry | None = None,
) -> None:
    managed_marstek = managed_marstek or {}

    powermeters: list[ConfiguredPowermeter] = []
    insights: MqttInsightsService | None = None

    try:
        # Create powermeters
        powermeters = config.powermeters(general)
        if registry is not None:
            registry.powermeters = [c.powermeter for c in powermeters]
            registry.bump()

        # Start powermeter lifecycle
        for configured in powermeters:
            await configured.powermeter.start()

        if not skip_test:
            for configured in powermeters:
                await check_powermeter(configured.powermeter, configured.client_filter)

        # MQTT Insights (optional)
        insights_cfg = config.mqtt_insights()
        if insights_cfg:
            insights = MqttInsightsService(
                insights_cfg, powermeters=[c.powermeter for c in powermeters]
            )
            await insights.start()
            logger.info("MQTT Insights service started")
        if registry is not None:
            registry.insights = insights
            registry.bump()

        if not device_types:
            logger.warning("No runnable device types configured after filtering.")
            return

        await asyncio.gather(
            *(
                run_device(
                    device_type,
                    config,
                    general,
                    powermeters,
                    device_id,
                    insights,
                    managed_marstek.get(device_type, ("", None))[0],
                    managed_marstek.get(device_type, ("", None))[1],
                    registry=registry,
                )
                for device_type, device_id in zip(
                    device_types, device_ids, strict=False
                )
            )
        )
    finally:
        # Best-effort shutdown: each resource gets a stop attempt even if
        # an earlier one fails.
        if insights:
            try:
                await insights.stop()
                logger.info("MQTT Insights service stopped")
            except Exception:
                logger.exception("Error stopping MQTT Insights service")
        for configured in powermeters:
            try:
                await configured.powermeter.stop()
            except Exception:
                logger.exception("Error stopping powermeter %s", configured.powermeter)


def _build_managed_marstek(
    marstek: MarstekSettings, device_types: Sequence[str]
) -> dict[str, tuple[str, int]]:
    """Register managed fake CT devices with Marstek and return the MAC/ver map.

    Called both at startup and after a config-driven restart so the MAC/version
    wiring stays in sync with the (possibly reloaded) config and device_types.
    """
    managed_marstek: dict[str, tuple[str, int]] = {}
    if not marstek.enable:
        return managed_marstek

    if not marstek.mailbox or not marstek.password:
        logger.warning(
            "Marstek auto-registration is enabled, but the mailbox/password is missing; skipping it"
        )
        return managed_marstek

    marstek_cfg = MarstekConfig(
        base_url=marstek.base_url,
        mailbox=marstek.mailbox,
        password=marstek.password,
        timezone=marstek.timezone,
    )
    try:
        any_ct = False
        for dt in CT_DEVICE_TYPES:
            if dt in device_types:
                any_ct = True
                created = ensure_managed_fake_device(marstek_cfg, dt)
                if created is not None:
                    normalized = normalize_mac(str(created.get("mac", "")))
                    if normalized:
                        managed_marstek[dt] = (
                            normalized,
                            ver_v_from_marstek_api_version(created.get("version")),
                        )
        if any_ct:
            logger.info(
                "Managed fake CT registration completed. Fake CT devices appear as offline in the Marstek app CT list (this is expected)."
            )
            ct_names = [
                f"AstraMeter {dt.upper()}"
                for dt in CT_DEVICE_TYPES
                if dt in device_types
            ]
            logger.info(
                "Pairing hint: refresh the CT device list (or log out/in if needed), select %s, switch battery mode to Automatic, and choose that CT."
                " The CT should be selectable as soon as it appears in the device list.",
                (" / ".join(ct_names) if ct_names else "the managed AstraMeter CT"),
            )
            logger.info(
                "Credentials are only needed for one-time registration. You can remove MARSTEK mailbox/password from config now."
            )
    except MarstekApiError as exc:
        logger.error("Marstek auto-registration failed: %s", exc, exc_info=True)
    except Exception as exc:
        logger.error(
            "Unexpected Marstek auto-registration error: %s", exc, exc_info=True
        )
    return managed_marstek


def _addon_slug(args: argparse.Namespace) -> str | None:
    """This add-on's slug, so the dashboard can build ingress-relative links."""
    if not args.addon:
        return None
    return addon.SupervisorClient().addon_slug() or None


def _apply_cli_overrides(
    general: GeneralSettings, args: argparse.Namespace
) -> GeneralSettings:
    """Re-apply CLI flags that override configured values."""
    if args.throttle_interval is None:
        return general
    # Also reaches the power sources: they are built from these settings.
    return replace(
        general,
        signal=replace(general.signal, throttle_interval=args.throttle_interval),
    )


def _resolve_device_config(
    config: AppConfig, general: GeneralSettings, args: argparse.Namespace
) -> tuple[list[str], list[str], bool]:
    """Derive device_types, device_ids and skip_test from the config and CLI."""
    device_types = (
        args.device_types
        if args.device_types is not None
        else list(general.device_types)
    )
    skip_test = (
        args.skip_powermeter_test
        if args.skip_powermeter_test is not None
        else general.skip_powermeter_test
    )

    device_ids: list[str] = list(args.device_ids) if args.device_ids is not None else []
    if not device_ids:
        device_ids = list(general.device_ids)
    while len(device_ids) < len(device_types):
        device_type = device_types[len(device_ids)]
        prefix = (
            DEVICE_TYPES[device_type].id_prefix if device_type in DEVICE_TYPES else ""
        )
        if prefix:
            device_ids.append(f"{prefix}-ec4609c439c{len(device_ids) + 1}")
        else:
            device_ids.append(f"device-{len(device_ids) + 1}")

    if "shellypro3em" in device_types:
        shellypro3em_index = device_types.index("shellypro3em")
        device_types[shellypro3em_index] = "shellypro3em_old"
        device_types.append("shellypro3em_new")
        device_ids.append(device_ids[shellypro3em_index])

    ct_ports = [
        config.ct(device_type).udp_port
        for device_type in device_types
        if is_ct(device_type)
    ]
    if len(ct_ports) != len(set(ct_ports)):
        raise ValueError(
            "Multiple CT002/CT003 devices are configured with the same UDP port. "
            "Set UDP_PORT in [CT002]/[CT003] to avoid conflicts."
        )

    logger.info("Device Types: %s", device_types)
    logger.info("Device IDs: %s", device_ids)
    logger.info(f"Skip Test: {skip_test}")

    return device_types, device_ids, skip_test


def _load_config(
    args: argparse.Namespace, options: addon.Options | None = None
) -> AppConfig:
    """Pick the configuration backend the command line asks for.

    ``--addon`` takes the settings from the Home Assistant add-on options (and
    the Supervisor); otherwise they come from the ``--config`` file.

    Whatever the backend has to fetch remotely is resolved here, while we are
    still outside the event loop — this runs at startup *and* on a config
    restart, so neither path can leave a blocking lookup to the running loop.
    """
    if args.addon:
        config: AppConfig = addon.load_config(
            addon.load_options() if options is None else options
        )
    else:
        config = IniAppConfig.from_file(args.config)
    config.prefetch()
    return config


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Power meter device emulator")
    parser.add_argument(
        "-c", "--config", default="config.ini", help="Path to the configuration file"
    )
    parser.add_argument(
        "--addon",
        action="store_true",
        help="Run as the Home Assistant add-on: take the configuration from the "
        "add-on options and the Supervisor API instead of a config file",
    )
    parser.add_argument(
        "-t", "--skip-powermeter-test", action="store_true", default=None
    )
    parser.add_argument(
        "-d",
        "--device-types",
        nargs="+",
        choices=list(DEVICE_TYPES),
        help="List of device types to emulate",
    )
    parser.add_argument("--device-ids", nargs="+", help="List of device IDs")
    parser.add_argument(
        "-log",
        "--loglevel",
        default=os.environ.get("LOG_LEVEL", "warning"),
        help="Provide logging level. Example --loglevel debug. Can also be set via LOG_LEVEL env var",
    )

    parser.add_argument(
        "--throttle-interval",
        type=float,
        help="Throttling interval in seconds to prevent control instability",
    )
    return parser


def _adopt_config(
    registry: StatusRegistry,
    config: AppConfig,
    general: GeneralSettings,
    args: argparse.Namespace,
    web_server: WebServer | None = None,
) -> None:
    """Point the status registry at the configuration now in force.

    Start-up and a config restart run this same copy, so a field that has to
    follow the configuration cannot be wired up on one path and forgotten on
    the other.
    """
    # The dashboard can add or remove `custom_config`, so the source
    # the next cycle runs from may not be the one this one used.
    registry.app_config = config
    registry.config_path = config.path
    # The web server outlives every cycle, so the settings it reads per
    # request have to be re-pointed at the configuration just loaded —
    # otherwise editing them in the dashboard and restarting from it
    # appears to do nothing until the process itself is restarted.
    # Only the per-request gates: `dashboard_enabled` decides which
    # routes `build_app` registers, and those were built once at
    # start-up, so changing it here would disagree with the route table.
    registry.allow_write = general.dashboard_allow_write
    registry.direct_access = general.dashboard_direct_access
    if web_server is not None:
        web_server.allowed_hosts = parse_allowed_hosts(general.dashboard_allowed_hosts)
    registry.config_mode = detect_config_mode(addon=args.addon, config_path=config.path)


def main() -> None:
    args = _build_arg_parser().parse_args()

    # In add-on mode the log level is an add-on option, so the options have to
    # be read before the logger is configured — everything the config backend
    # logs while talking to the Supervisor honours the user's level that way.
    addon_options = addon.load_options() if args.addon else {}
    log_level = str(addon.get_option(addon_options, "log_level", args.loglevel))
    setLogLevel(log_level)
    logger.info("started astrameter application")
    _sha = get_git_commit_sha()
    if _sha:
        logger.info("Git commit: %s", _sha)
    else:
        logger.debug(
            "Git commit not logged (set GIT_COMMIT_SHA at image build for CI images)"
        )

    config = _load_config(args, addon_options)

    if args.addon:
        # Home Assistant is the power source, so give it a chance to finish
        # booting before the first reading is attempted.
        addon.wait_for_home_assistant(addon.SupervisorClient())

    general = _apply_cli_overrides(config.general(), args)
    logger.info("Effective configuration: %s", general)

    registry = StatusRegistry(
        config_path=config.path,
        log_level=log_level,
        version=get_version(),
        git_commit=_sha,
        addon_slug=_addon_slug(args),
        web_port=general.web_server_port,
        dashboard_enabled=general.dashboard,
    )
    _adopt_config(registry, config, general, args)

    # Map SIGTERM to KeyboardInterrupt so asyncio.run cancels tasks and
    # runs finally-cleanup the same way it does for SIGINT (Ctrl+C).
    signal.signal(signal.SIGTERM, signal.default_int_handler)

    try:
        asyncio.run(_supervise(config, general, args, registry))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        logger.error("%s", exc)
        exit(1)


async def _start_web_server(
    config: AppConfig, general: GeneralSettings, registry: StatusRegistry
) -> WebServer | None:
    """The web server that outlives every device cycle, or ``None`` for none.

    A server that cannot be started is not fatal: the devices still run, they
    just have no dashboard.
    """
    if not general.enable_web_server:
        return None
    logger.info("Starting web server...")
    web_server = None
    try:
        web_server = WebServer(
            port=general.web_server_port,
            config_path=config.path,
            enable_web_config=general.web_config_enabled,
            status=registry,
            allowed_hosts=general.dashboard_allowed_hosts,
        )
        if not await web_server.start():
            logger.error("Failed to start web server")
            return None
    except Exception:
        logger.exception("Web server failed to initialize")
        if web_server:
            await web_server.stop()
        return None
    return web_server


def _reload_config(
    args: argparse.Namespace, config: AppConfig, general: GeneralSettings
) -> tuple[AppConfig, GeneralSettings]:
    """The configuration to run the next cycle from, re-read from its source.

    The dashboard is what writes the file this re-reads, so a bad write must
    not be fatal: letting the error out would stop the web server in the
    caller's `finally` and take away the only surface that can repair the
    configuration. Keep running the last good one.
    """
    try:
        new_config = _load_config(args)
        return new_config, _apply_cli_overrides(new_config.general(), args)
    except Exception:
        logger.exception(
            "Could not reload the configuration; continuing with the previous one"
        )
        return config, general


async def _supervise(
    config: AppConfig,
    general: GeneralSettings,
    args: argparse.Namespace,
    registry: StatusRegistry,
) -> None:
    """Own the one asyncio loop for the process lifetime.

    The web server is started here, *outside* the per-cycle ``async_main``,
    so the dashboard URL and the add-on watchdog endpoint survive a restart.
    An ``AppRunner`` is bound to the loop that created it, so a long-lived
    server is only possible with exactly one loop per process — which is why
    the restart is an :class:`asyncio.Event` rather than a re-``asyncio.run``.
    """
    loop = asyncio.get_running_loop()
    restart = asyncio.Event()

    def _on_sigusr1() -> None:
        registry.restart_pending = True
        registry.bump()
        restart.set()

    loop.add_signal_handler(signal.SIGUSR1, _on_sigusr1)

    web_server = await _start_web_server(config, general, registry)

    try:
        while True:
            restart.clear()
            registry.restart_pending = False
            device_types, device_ids, skip_test = _resolve_device_config(
                config, general, args
            )
            # Marstek registration is blocking HTTP with retries; off-loop so a
            # slow or unreachable cloud cannot stall /health for ~40 s.
            managed_marstek = await asyncio.to_thread(
                _build_managed_marstek, config.marstek(), device_types
            )
            registry.managed_marstek = managed_marstek
            registry.bump()

            cycle = asyncio.create_task(
                async_main(
                    config,
                    general,
                    device_types,
                    device_ids,
                    skip_test,
                    managed_marstek,
                    registry,
                )
            )
            waiter = asyncio.create_task(restart.wait())
            try:
                await asyncio.wait({cycle, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await waiter

            if cycle.done():
                # A finished cycle means the devices exited on their own; let
                # the exception (if any) propagate as before.
                await cycle
                if not restart.is_set():
                    break

            # Restart requested: unwind this cycle fully before starting the
            # next one, so no device task, powermeter or MQTT client leaks
            # into it.
            logger.info("Restarting service…")
            cycle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cycle
            registry.reset_cycle()

            # Re-read the configuration off-loop: a backend may have to ask
            # the Supervisor for part of it, and prefetch() is blocking.
            config, general = await asyncio.to_thread(
                _reload_config, args, config, general
            )
            _adopt_config(registry, config, general, args, web_server)
            registry.bump()
    finally:
        loop.remove_signal_handler(signal.SIGUSR1)
        if web_server:
            logger.info("Stopping web server...")
            try:
                await asyncio.wait_for(web_server.stop(), timeout=5.0)
            except TimeoutError:
                logger.warning("Web server stop timed out")
            except Exception:
                logger.exception("Error stopping web server")


if __name__ == "__main__":
    main()
