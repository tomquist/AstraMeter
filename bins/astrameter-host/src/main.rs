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
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    let config_path = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("config.ini"));

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

    // Web server (host-only).
    let web_state = app_state.clone();
    tokio::spawn(async move {
        let app = axum::Router::new()
            .merge(astrameter_web::health::axum_router::build(
                web_state.clone(),
            ))
            .merge(astrameter_web::config_ui::axum_router::build(web_state));
        let addr: SocketAddr = "0.0.0.0:8080".parse().unwrap();
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

    tokio::signal::ctrl_c()
        .await
        .context("waiting for ctrl-c")?;
    supervisor.stop().await;
    Ok(())
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

        // Resolve [GENERAL].DEVICE_TYPE and instantiate the right emulator.
        let device_type = config
            .section("GENERAL")
            .and_then(|s| s.get_opt_string("DEVICE_TYPE"))
            .unwrap_or_else(|| "ct002".to_string())
            .to_lowercase();
        let global_dedupe = config
            .section("GENERAL")
            .map(|s| s.get_float("DEDUPE_TIME_WINDOW", 0.0))
            .unwrap_or(Ok(0.0))
            .unwrap_or(0.0);

        match device_type.as_str() {
            "ct002" | "ct003" => {
                let h = self
                    .start_ct002(config, &device_type, &bound, &cancel)
                    .await?;
                emulator_handles.push(h);
            }
            "shellypro3em_old" => {
                let h = self
                    .start_shelly(config, "shellypro3em", 1010, global_dedupe, &bound, &cancel)
                    .await?;
                emulator_handles.push(h);
            }
            "shellypro3em_new" | "shellypro3em" => {
                let h = self
                    .start_shelly(config, "shellypro3em", 2220, global_dedupe, &bound, &cancel)
                    .await?;
                emulator_handles.push(h);
            }
            "shellyemg3" => {
                let h = self
                    .start_shelly(config, "shellyemg3", 2222, global_dedupe, &bound, &cancel)
                    .await?;
                emulator_handles.push(h);
            }
            "shellyproem50" => {
                let h = self
                    .start_shelly(
                        config,
                        "shellyproem50",
                        2223,
                        global_dedupe,
                        &bound,
                        &cancel,
                    )
                    .await?;
                emulator_handles.push(h);
            }
            other => {
                tracing::warn!(
                    "DEVICE_TYPE={other:?} not recognised; emulator not started. \
                     Supported: ct002, ct003, shellypro3em_old, shellypro3em_new, \
                     shellyemg3, shellyproem50"
                );
            }
        }

        // MQTT Insights, if configured.
        if let Some(section) = config
            .sections()
            .find(|s| s.starts_with("MQTT_INSIGHTS"))
            .and_then(|n| config.section(n))
        {
            match self.start_insights(&section, &bound, &cancel).await {
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
                    let device_type = device_type.clone();
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
        use astrameter_emulator_ct002::server::{BoundMeter, Ct002Emulator};
        let section_name = if device_type == "ct003" && config.section("CT003").is_some() {
            "CT003"
        } else {
            "CT002"
        };
        let section = config
            .section(section_name)
            .ok_or_else(|| anyhow::anyhow!("section [{section_name}] missing"))?;
        let udp_port = section.get_int("UDP_PORT", 12345)? as u16;
        let ct_mac = section.get_string("CT_MAC", "");
        let meters: Vec<BoundMeter> = bound
            .iter()
            .map(|bp| BoundMeter {
                meter: bp.meter.clone(),
                filter: bp.client_filter.clone(),
                wait_for_next: bp.wait_for_next_message,
            })
            .collect();
        let emu = Arc::new(Ct002Emulator::new(
            udp_port,
            ct_mac,
            meters,
            astrameter_emulator_ct002::balancer::BalancerConfig::default(),
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
        section: &astrameter_config::Section<'_>,
        bound: &[astrameter_powermeters::BoundPowermeter],
        _cancel: &tokio_util::sync::CancellationToken,
    ) -> Result<InsightsHandle> {
        use astrameter_insights_mqtt::{InsightsService, MqttInsightsConfig};
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
                        m.get_powermeter_watts().await
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
