//! AstraMeter host entry point.

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use astrameter_config::Config;
use astrameter_platform::Platform;
use astrameter_powermeters::{register_all, PowermeterRegistry};
use astrameter_web::{AppState, ReloadCommand, Status};
use parking_lot::Mutex;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    let cli = parse_cli();
    let env_filter = if let Some(level) = &cli.log_level {
        EnvFilter::try_new(format!("astrameter={level}")).unwrap_or_else(|_| EnvFilter::new("info"))
    } else {
        EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into())
    };
    tracing_subscriber::fmt().with_env_filter(env_filter).init();

    let config_path = cli.config.clone();

    tracing::info!(
        version = astrameter_core::VERSION,
        config = %config_path.display(),
        "astrameter starting"
    );

    let platform = Arc::new(astrameter_platform_std::build_platform());
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);
    let registry = Arc::new(reg);

    let (reload_tx, mut reload_rx) = tokio::sync::mpsc::channel::<ReloadCommand>(8);
    let status = Arc::new(Mutex::new(Status {
        healthy: false,
        ..Default::default()
    }));
    let app_state = AppState {
        config_path: config_path.clone(),
        reload_tx: Arc::new(reload_tx),
        status: status.clone(),
    };

    // Web server settings honour [GENERAL].ENABLE_WEB_SERVER /
    // WEB_SERVER_PORT / WEB_CONFIG_ENABLED, matching Python.
    let (web_enabled, web_port, config_editor_enabled) = if config_path.exists() {
        let raw = tokio::fs::read_to_string(&config_path)
            .await
            .unwrap_or_default();
        let cfg = Config::parse(&raw).ok();
        let g = cfg.as_ref().and_then(|c| c.section("GENERAL"));
        (
            g.as_ref()
                .map(|s| s.get_bool("ENABLE_WEB_SERVER", true).unwrap_or(true))
                .unwrap_or(true),
            g.as_ref()
                .map(|s| s.get_int("WEB_SERVER_PORT", 52500).unwrap_or(52500))
                .unwrap_or(52500) as u16,
            g.as_ref()
                .map(|s| s.get_bool("WEB_CONFIG_ENABLED", true).unwrap_or(true))
                .unwrap_or(true),
        )
    } else {
        (true, 52500, true)
    };
    if config_editor_enabled {
        tracing::warn!(
            "Web config editor is ENABLED and unauthenticated. Disable via \
             WEB_CONFIG_ENABLED=false or restrict the listener via firewall."
        );
    }
    if web_enabled {
        let web_state = app_state.clone();
        tokio::spawn(async move {
            let app = if config_editor_enabled {
                axum::Router::new()
                    .merge(astrameter_web::health::axum_router::build(
                        web_state.clone(),
                    ))
                    .merge(astrameter_web::config_ui::axum_router::build(web_state))
            } else {
                astrameter_web::health::axum_router::build(web_state)
            };
            let addr: SocketAddr = format!("0.0.0.0:{web_port}").parse().expect("web addr");
            match tokio::net::TcpListener::bind(addr).await {
                Ok(listener) => {
                    tracing::info!("web server listening on {addr}");
                    if let Err(e) = axum::serve(listener, app).await {
                        tracing::error!("web server: {e}");
                    }
                }
                Err(e) => tracing::error!("web bind {addr}: {e}"),
            }
        });
    }

    let supervisor = Arc::new(Supervisor::new(platform.clone(), registry.clone()));
    if config_path.exists() {
        match supervisor.start_from_file(&config_path).await {
            Ok(()) => {
                let mut s = status.lock();
                s.healthy = true;
                s.last_reload_ok = Some(true);
            }
            Err(e) => {
                tracing::error!("initial supervisor start: {e}");
                let mut s = status.lock();
                s.last_reload_ok = Some(false);
                s.last_error = Some(e.to_string());
            }
        }
    } else {
        tracing::warn!(
            "config file not found at {} — services idle until a config is submitted",
            config_path.display()
        );
    }

    // Reload loop (Supervisor receives ReloadCommand from web routes).
    let sup = supervisor.clone();
    let status_for_loop = status.clone();
    let path_for_loop = config_path.clone();
    tokio::spawn(async move {
        while let Some(_cmd) = reload_rx.recv().await {
            tracing::info!("supervisor: hot-reload requested");
            sup.stop().await;
            match sup.start_from_file(&path_for_loop).await {
                Ok(()) => {
                    let mut s = status_for_loop.lock();
                    s.healthy = true;
                    s.last_reload_ok = Some(true);
                    s.last_error = None;
                }
                Err(e) => {
                    tracing::error!("reload failed: {e}");
                    let mut s = status_for_loop.lock();
                    s.healthy = false;
                    s.last_reload_ok = Some(false);
                    s.last_error = Some(e.to_string());
                }
            }
        }
    });

    // Honor SIGTERM as well as Ctrl-C (matches Python: SIGTERM raises
    // KeyboardInterrupt). Plain `tokio::signal::ctrl_c()` alone won't
    // catch SIGTERM, which is what `systemd stop` and Docker's stop
    // command send.
    #[cfg(unix)]
    {
        use tokio::signal::unix::{signal, SignalKind};
        let mut term = signal(SignalKind::terminate())
            .map_err(|e| anyhow::anyhow!("install SIGTERM handler: {e}"))?;
        tokio::select! {
            _ = tokio::signal::ctrl_c() => {
                tracing::info!("received Ctrl-C, shutting down");
            }
            _ = term.recv() => {
                tracing::info!("received SIGTERM, shutting down");
            }
        }
    }
    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c()
            .await
            .context("waiting for ctrl-c")?;
    }
    supervisor.stop().await;
    Ok(())
}

#[derive(Debug)]
struct Cli {
    config: PathBuf,
    log_level: Option<String>,
}

/// Minimal CLI parity with the Python argparse interface — accepts `-c`,
/// `--config`, `-log`, `--loglevel`. `--skip-powermeter-test`,
/// `--throttle-interval`, and `--device-ids` are accepted but ignored
/// (their config-file counterparts still work).
fn parse_cli() -> Cli {
    let mut args = std::env::args().skip(1);
    let mut config: Option<PathBuf> = None;
    let mut log_level: Option<String> = None;
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-c" | "--config" => {
                config = args.next().map(PathBuf::from);
            }
            "-log" | "--loglevel" => {
                log_level = args.next();
            }
            "-t" | "--skip-powermeter-test" => {}
            "--throttle-interval" => {
                let _ = args.next();
            }
            "-d" | "--device-types" => {
                let _ = args.next();
            }
            "--device-ids" => {
                let _ = args.next();
            }
            "-h" | "--help" => {
                eprintln!(
                    "Usage: astrameter [--config PATH] [--loglevel LEVEL]\n\
                     \n\
                     Options:\n\
                     \x20 -c, --config PATH       Path to config.ini (default: ./config.ini)\n\
                     \x20 -log, --loglevel LEVEL  debug|info|warning|error (default: info)"
                );
                std::process::exit(0);
            }
            other if !other.starts_with('-') && config.is_none() => {
                config = Some(PathBuf::from(other));
            }
            other => {
                eprintln!("unknown arg: {other}");
            }
        }
    }
    // LOG_LEVEL env var as a fallback before RUST_LOG (Python compat).
    if log_level.is_none() {
        if let Ok(v) = std::env::var("LOG_LEVEL") {
            if !v.is_empty() {
                log_level = Some(v);
            }
        }
    }
    Cli {
        config: config.unwrap_or_else(|| PathBuf::from("config.ini")),
        log_level,
    }
}

/// Supervisor with hot-reload. Keeps a set of running meter tasks under a
/// shared cancellation token so the whole tree can be torn down and rebuilt
/// from a fresh config without restarting the process.
struct Supervisor {
    platform: Arc<Platform>,
    registry: Arc<PowermeterRegistry>,
    inner: tokio::sync::Mutex<SupervisorInner>,
}

#[derive(Default)]
struct SupervisorInner {
    cancel: Option<tokio_util::sync::CancellationToken>,
    handles: Vec<tokio::task::JoinHandle<()>>,
    emulators: Vec<EmulatorHandle>,
    insights: Option<InsightsHandle>,
    meters: Vec<Arc<dyn astrameter_core::Powermeter>>,
}

impl Supervisor {
    fn new(platform: Arc<Platform>, registry: Arc<PowermeterRegistry>) -> Self {
        Self {
            platform,
            registry,
            inner: tokio::sync::Mutex::new(SupervisorInner::default()),
        }
    }

    async fn start_from_file(&self, path: &std::path::Path) -> Result<()> {
        let raw = tokio::fs::read_to_string(path)
            .await
            .with_context(|| format!("read {}", path.display()))?;
        let cfg = Config::parse(&raw)?;
        self.start(&cfg).await
    }

    async fn start(&self, config: &Config) -> Result<()> {
        let cancel = tokio_util::sync::CancellationToken::new();
        let mut handles: Vec<tokio::task::JoinHandle<()>> = Vec::new();
        let mut emulator_handles: Vec<EmulatorHandle> = Vec::new();
        let mut insights_handle: Option<InsightsHandle> = None;

        // Apply the full Python config_loader wrapper chain.
        let bound = astrameter_powermeters::read_all_powermeter_configs(
            config,
            &self.registry,
            self.platform.clone(),
        )?;
        if bound.is_empty() {
            tracing::warn!("config has no recognised powermeter sections");
        }

        // Start each meter so push-based meters connect immediately. We hand
        // out clones of `Arc<dyn Powermeter>` to the emulators below.
        for bp in &bound {
            let _ = bp.meter.start().await;
        }
        // Periodic sample-logger (matches the Python `astrameter` startup
        // sanity check). Keeps last-known value alive for diagnostics.
        for bp in &bound {
            let cancel_clone = cancel.clone();
            let name = bp.section.clone();
            let meter = bp.meter.clone();
            let h = tokio::spawn(async move {
                loop {
                    tokio::select! {
                        _ = cancel_clone.cancelled() => break,
                        _ = tokio::time::sleep(std::time::Duration::from_secs(10)) => {
                            match meter.get_powermeter_watts().await {
                                Ok(v) => tracing::info!(section = %name, watts = ?v, "sample"),
                                Err(e) => tracing::debug!(section = %name, "read: {e}"),
                            }
                        }
                    }
                }
            });
            handles.push(h);
        }

        // Resolve [GENERAL].DEVICE_TYPE — supports comma-separated lists
        // for multi-device emulation (Python main.py `_resolve_device_config`).
        let device_type_raw = config
            .section("GENERAL")
            .and_then(|s| s.get_opt_string("DEVICE_TYPE"))
            .unwrap_or_else(|| "ct002".to_string());
        let device_types: Vec<String> = device_type_raw
            .split(',')
            .map(|s| s.trim().to_lowercase())
            .filter(|s| !s.is_empty())
            .collect();
        let global_dedupe = config
            .section("GENERAL")
            .map(|s| s.get_float("DEDUPE_TIME_WINDOW", 0.0))
            .unwrap_or(Ok(0.0))
            .unwrap_or(0.0);

        let mut ct002_for_handlers: Option<Arc<astrameter_emulator_ct002::server::Ct002Emulator>> =
            None;
        for dt in &device_types {
            match dt.as_str() {
                "ct002" | "ct003" => {
                    let h = self.start_ct002(config, dt, &bound, &cancel).await?;
                    if let EmulatorHandle::Ct002(ref e) = h {
                        ct002_for_handlers = Some(e.clone());
                    }
                    emulator_handles.push(h);
                }
                "shellypro3em_old" => {
                    let h = self
                        .start_shelly(config, dt, 1010, global_dedupe, &bound, &cancel)
                        .await?;
                    emulator_handles.push(h);
                }
                "shellypro3em_new" => {
                    let h = self
                        .start_shelly(config, dt, 2220, global_dedupe, &bound, &cancel)
                        .await?;
                    emulator_handles.push(h);
                }
                // `shellypro3em` is shorthand for both legacy ports
                // (1010 + 2220), matching Python's expansion.
                "shellypro3em" => {
                    let h_old = self
                        .start_shelly(
                            config,
                            "shellypro3em_old",
                            1010,
                            global_dedupe,
                            &bound,
                            &cancel,
                        )
                        .await?;
                    emulator_handles.push(h_old);
                    let h_new = self
                        .start_shelly(
                            config,
                            "shellypro3em_new",
                            2220,
                            global_dedupe,
                            &bound,
                            &cancel,
                        )
                        .await?;
                    emulator_handles.push(h_new);
                }
                "shellyemg3" => {
                    let h = self
                        .start_shelly(config, dt, 2222, global_dedupe, &bound, &cancel)
                        .await?;
                    emulator_handles.push(h);
                }
                "shellyproem50" => {
                    let h = self
                        .start_shelly(config, dt, 2223, global_dedupe, &bound, &cancel)
                        .await?;
                    emulator_handles.push(h);
                }
                other => {
                    tracing::warn!(
                        "DEVICE_TYPE={other:?} not recognised; emulator not started. \
                         Supported: ct002, ct003, shellypro3em_old, shellypro3em_new, \
                         shellypro3em, shellyemg3, shellyproem50"
                    );
                }
            }
        }

        // MQTT Insights, if configured.
        if let Some(section) = config
            .sections()
            .find(|s| s.starts_with("MQTT_INSIGHTS"))
            .and_then(|n| config.section(n))
        {
            match self
                .start_insights(
                    config,
                    &section,
                    &bound,
                    ct002_for_handlers.clone(),
                    device_types
                        .iter()
                        .find(|dt| dt.as_str() == "ct002" || dt.as_str() == "ct003")
                        .map(|s| s.as_str()),
                    &cancel,
                )
                .await
            {
                Ok(h) => insights_handle = Some(h),
                Err(e) => tracing::error!("MQTT Insights failed: {e}"),
            }
        }

        // Marstek cloud auto-register, if [MARSTEK] enabled.
        if let Some(marstek) = config
            .sections()
            .find(|s| s.starts_with("MARSTEK"))
            .and_then(|n| config.section(n))
        {
            if marstek.get_bool("ENABLE", false).unwrap_or(false) {
                let base_url = marstek.get_string("BASE_URL", "https://eu.hamedata.com");
                let mailbox = marstek.get_string("MAILBOX", "");
                let password = marstek.get_string("PASSWORD", "");
                if !mailbox.is_empty() && !password.is_empty() {
                    // Register the first CT-class device-type we have.
                    let device_type = device_types
                        .iter()
                        .find(|dt| dt == &"ct002" || dt == &"ct003")
                        .cloned()
                        .unwrap_or_else(|| device_types.first().cloned().unwrap_or_default());
                    let http = self.platform.http.clone();
                    handles.push(tokio::spawn(async move {
                        let client = astrameter_marstek_api::MarstekClient::new(http);
                        let cfg =
                            astrameter_marstek_api::MarstekConfig::new(base_url, mailbox, password);
                        match client.ensure_managed_fake_device(&cfg, &device_type).await {
                            Ok(Some(d)) => {
                                tracing::info!("Marstek registration ok: {d:?}");
                            }
                            Ok(None) => {
                                tracing::info!("Marstek: nothing to register for {device_type}");
                            }
                            Err(e) => {
                                tracing::warn!("Marstek registration failed: {e}");
                            }
                        }
                    }));
                }
            }
        }

        let mut inner = self.inner.lock().await;
        inner.cancel = Some(cancel);
        inner.handles = handles;
        inner.emulators = emulator_handles;
        inner.insights = insights_handle;
        inner.meters = bound.into_iter().map(|bp| bp.meter).collect();
        Ok(())
    }

    async fn start_ct002(
        &self,
        config: &Config,
        device_type: &str,
        bound: &[astrameter_powermeters::BoundPowermeter],
        _cancel: &tokio_util::sync::CancellationToken,
    ) -> Result<EmulatorHandle> {
        use astrameter_emulator_ct002::balancer::BalancerConfig;
        use astrameter_emulator_ct002::server::{BoundMeter, Ct002Emulator, Ct002Settings};
        let section_name = if device_type == "ct003" && config.section("CT003").is_some() {
            "CT003"
        } else {
            "CT002"
        };
        let section = config
            .section(section_name)
            .ok_or_else(|| anyhow::anyhow!("section [{section_name}] missing"))?;
        let udp_port = section.get_int("UDP_PORT", 12345)? as u16;

        // Full Python `[CT002]`/`[CT003]` knob set.
        let balancer_cfg = BalancerConfig {
            fair_distribution: section.get_bool("FAIR_DISTRIBUTION", true)?,
            balance_gain: section.get_float("BALANCE_GAIN", 0.2)?,
            balance_deadband: section.get_float("BALANCE_DEADBAND", 15.0)?,
            error_boost_threshold: section.get_float("ERROR_BOOST_THRESHOLD", 150.0)?,
            error_boost_max: section.get_float("ERROR_BOOST_MAX", 0.5)?,
            error_reduce_threshold: section.get_float("ERROR_REDUCE_THRESHOLD", 20.0)?,
            max_correction_per_step: section.get_float("MAX_CORRECTION_PER_STEP", 80.0)?,
            max_target_step: section.get_float("MAX_TARGET_STEP", 0.0)?,
            min_efficient_power: section.get_float("MIN_EFFICIENT_POWER", 0.0)?,
            probe_min_power: section.get_float("PROBE_MIN_POWER", 80.0)?,
            efficiency_rotation_interval: section
                .get_float("EFFICIENCY_ROTATION_INTERVAL", 900.0)?,
            efficiency_fade_alpha: section.get_float("EFFICIENCY_FADE_ALPHA", 0.15)?,
            efficiency_saturation_threshold: section
                .get_float("EFFICIENCY_SATURATION_THRESHOLD", 0.4)?,
        };
        let ct_type = if device_type == "ct003" {
            "HME-3".to_string()
        } else {
            "HME-4".to_string()
        };
        let settings = Ct002Settings {
            ct_type,
            ct_mac: section.get_string("CT_MAC", ""),
            wifi_rssi: section.get_int("WIFI_RSSI", -50)? as i32,
            dedupe_time_window: std::time::Duration::from_secs_f64(
                section.get_float("DEDUPE_TIME_WINDOW", 0.0)?.max(0.0),
            ),
            consumer_ttl: std::time::Duration::from_secs_f64(
                section.get_float("CONSUMER_TTL", 600.0)?.max(0.0),
            ),
            debug_status: section.get_bool("DEBUG_STATUS", false)?,
            active_control: section.get_bool("ACTIVE_CONTROL", true)?,
            saturation_alpha: section.get_float("SATURATION_ALPHA", 0.2)?,
            min_target_for_saturation: section.get_float("MIN_TARGET_FOR_SATURATION", 10.0)?,
            saturation_decay_factor: section.get_float("SATURATION_DECAY_FACTOR", 0.9)?,
            saturation_grace_seconds: section.get_float(
                "SATURATION_GRACE_SECONDS",
                astrameter_emulator_ct002::balancer::SATURATION_GRACE_SECONDS,
            )?,
            saturation_stall_timeout_seconds: section.get_float(
                "SATURATION_STALL_TIMEOUT_SECONDS",
                astrameter_emulator_ct002::balancer::SATURATION_STALL_TIMEOUT_SECONDS,
            )?,
            saturation_detection: section.get_bool("SATURATION_DETECTION", true)?,
        };

        let meters: Vec<BoundMeter> = bound
            .iter()
            .map(|bp| BoundMeter {
                meter: bp.meter.clone(),
                filter: bp.client_filter.clone(),
                wait_for_next: bp.wait_for_next_message,
            })
            .collect();
        let device_id = section.get_string("DEVICE_ID", section_name);
        let emu = Arc::new(Ct002Emulator::with_settings(
            udp_port,
            device_id,
            settings,
            balancer_cfg,
            meters,
            self.platform.clone(),
        ));
        emu.start().await?;
        Ok(EmulatorHandle::Ct002(emu))
    }

    async fn start_shelly(
        &self,
        _config: &Config,
        device_id: &str,
        port: u16,
        dedupe_secs: f64,
        bound: &[astrameter_powermeters::BoundPowermeter],
        _cancel: &tokio_util::sync::CancellationToken,
    ) -> Result<EmulatorHandle> {
        use astrameter_emulator_shelly::{BoundMeter, ShellyEmulator};
        let meters: Vec<BoundMeter> = bound
            .iter()
            .map(|bp| BoundMeter {
                meter: bp.meter.clone(),
                filter: bp.client_filter.clone(),
                wait_for_next: bp.wait_for_next_message,
            })
            .collect();
        let emu = Arc::new(ShellyEmulator::new(
            port,
            device_id.to_string(),
            meters,
            std::time::Duration::from_secs_f64(dedupe_secs.max(0.0)),
            self.platform.clone(),
        ));
        emu.start().await?;
        Ok(EmulatorHandle::Shelly(emu))
    }

    async fn start_insights(
        &self,
        config: &Config,
        section: &astrameter_config::Section<'_>,
        bound: &[astrameter_powermeters::BoundPowermeter],
        ct002: Option<Arc<astrameter_emulator_ct002::server::Ct002Emulator>>,
        ct002_device_type: Option<&str>,
        _cancel: &tokio_util::sync::CancellationToken,
    ) -> Result<InsightsHandle> {
        use astrameter_insights_mqtt::{
            CommandHandlers, InsightsService, MarstekBinding, MqttInsightsConfig,
        };
        let (broker, port, username, password, tls) = match section.get_opt_string("URI") {
            Some(uri) => {
                let parts = astrameter_config::parse_mqtt_uri(&uri)?;
                (
                    parts.host,
                    parts.port,
                    parts.username,
                    parts.password,
                    parts.tls,
                )
            }
            None => (
                section.get_string("BROKER", "localhost"),
                section.get_int("PORT", 1883)? as u16,
                section.get_opt_string("USERNAME"),
                section.get_opt_string("PASSWORD"),
                section.get_bool("TLS", false)?,
            ),
        };
        let cfg = MqttInsightsConfig {
            broker,
            port,
            username,
            password,
            tls,
            base_topic: section.get_string("BASE_TOPIC", "astrameter"),
            ha_discovery: section.get_bool("HA_DISCOVERY", true)?,
            ha_discovery_prefix: section.get_string("HA_DISCOVERY_PREFIX", "homeassistant"),
            addon_slug: section.get_opt_string("ADDON_SLUG"),
            marstek_mqtt_enabled: section.get_bool("MARSTEK_MQTT_ENABLED", true)?,
            marstek_mqtt_interval: section.get_float("MARSTEK_MQTT_INTERVAL", 300.0)?,
        };
        let service = Arc::new(InsightsService::new(cfg, self.platform.clone()));
        if let Some(ct) = ct002.clone() {
            let ct_act = ct.clone();
            let ct_mt = ct.clone();
            let ct_at = ct.clone();
            let ct_fr = ct.clone();
            service.set_command_handlers(CommandHandlers {
                set_active: Some(Arc::new(move |_dev: &str, consumer: &str, active: bool| {
                    ct_act.set_consumer_active(consumer, active);
                })),
                set_manual_target: Some(Arc::new(
                    move |_dev: &str, consumer: &str, target: f64| {
                        ct_mt.set_consumer_manual_target(consumer, target);
                    },
                )),
                set_auto_target: Some(Arc::new(move |_dev: &str, consumer: &str, auto: bool| {
                    ct_at.set_consumer_auto_target(consumer, auto);
                })),
                force_rotation: Some(Arc::new(move |_dev: &str| {
                    ct_fr.force_efficiency_rotation();
                })),
            });
        }
        // Register a MarstekBinding for the CT002/CT003 emulator (if any).
        // This drives App/ctrl poll responses on `hame_energy/...` and
        // `marstek_energy/...` topics plus the periodic broadcast loop.
        if let (Some(ct), Some(dev_type)) = (ct002.as_ref(), ct002_device_type) {
            let section_name = if dev_type == "ct003" && config.section("CT003").is_some() {
                "CT003"
            } else {
                "CT002"
            };
            if let Some(cs) = config.section(section_name) {
                let ct_mac_raw = cs.get_string("CT_MAC", "");
                let mac_norm = astrameter_insights_mqtt::marstek::normalize_mac(&ct_mac_raw);
                let ct_type = if dev_type == "ct003" {
                    "HME-3".to_string()
                } else {
                    "HME-4".to_string()
                };
                let device_id = cs.get_string("DEVICE_ID", section_name);
                let wifi_rssi = cs.get_int("WIFI_RSSI", -50).unwrap_or(-50);
                if !mac_norm.is_empty() {
                    let ct_for_count = ct.clone();
                    let ct_for_csv = ct.clone();
                    service.add_marstek_binding(MarstekBinding {
                        device_id,
                        ct_type,
                        mac: mac_norm,
                        wifi_rssi,
                        ver_v: astrameter_insights_mqtt::marstek::DEFAULT_VER_V,
                        ble_s: 0,
                        fc4_v: astrameter_insights_mqtt::marstek::DEFAULT_FC4_V.to_string(),
                        get_connected_slave_count: Some(Arc::new(move || {
                            ct_for_count.reporting_consumer_count() as i64
                        })),
                        get_cd4_slave_csv: Some(Arc::new(move || {
                            ct_for_csv.reporting_consumer_csv()
                        })),
                    });
                } else if !ct_mac_raw.is_empty() {
                    tracing::warn!(
                        "[{section_name}] CT_MAC={ct_mac_raw:?} could not be normalised; \
                         Marstek MQTT binding skipped"
                    );
                }
            }
        }
        // The meter-watts callback maps a device id to whichever bound meter
        // matches `0.0.0.0` (i.e. accepts any caller); falls back to the
        // first meter so single-meter configs still work.
        let meters: Vec<(String, Arc<dyn astrameter_core::Powermeter>)> = bound
            .iter()
            .map(|bp| (bp.section.clone(), bp.meter.clone()))
            .collect();
        let meters_arc = Arc::new(meters);
        let meters_cb = meters_arc.clone();
        service
            .start(move |_device_id: &str| {
                let meters_cb = meters_cb.clone();
                Box::pin(async move {
                    if let Some((_, m)) = meters_cb.first() {
                        // Marstek wire format echoes raw powermeter watts so
                        // wrapper smoothing/PID don't interfere with how the
                        // Marstek app interprets totals (matches Python's
                        // `binding.get_values = pm.get_powermeter_watts_raw`).
                        m.get_powermeter_watts_raw().await
                    } else {
                        Ok(vec![0.0, 0.0, 0.0])
                    }
                })
            })
            .await?;
        Ok(InsightsHandle(service))
    }
}

/// Type-erased handle for an emulator task so the supervisor can stop both
/// kinds uniformly during reload.
enum EmulatorHandle {
    Ct002(Arc<astrameter_emulator_ct002::server::Ct002Emulator>),
    Shelly(Arc<astrameter_emulator_shelly::ShellyEmulator>),
}

impl EmulatorHandle {
    async fn stop(&self) {
        match self {
            EmulatorHandle::Ct002(e) => e.stop().await,
            EmulatorHandle::Shelly(e) => e.stop().await,
        }
    }
}

struct InsightsHandle(Arc<astrameter_insights_mqtt::InsightsService>);

impl InsightsHandle {
    async fn stop(&self) {
        self.0.stop().await;
    }
}

impl Supervisor {
    async fn stop(&self) {
        let mut inner = self.inner.lock().await;
        if let Some(token) = inner.cancel.take() {
            token.cancel();
        }
        // Stop emulators / insights / meters in reverse-construction order
        // so the meters drain their push tasks before the emulators close.
        if let Some(i) = inner.insights.take() {
            i.stop().await;
        }
        for e in inner.emulators.drain(..) {
            e.stop().await;
        }
        for m in inner.meters.drain(..) {
            let _ = m.stop().await;
        }
        for h in inner.handles.drain(..) {
            let _ = tokio::time::timeout(std::time::Duration::from_secs(5), h).await;
        }
    }
}
