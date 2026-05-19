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

        // Apply the full Python config_loader wrapper chain
        // (transform/throttle/hampel/smoothing/deadband/pid + NETMASK
        // ClientFilter + WAIT_FOR_NEXT_MESSAGE) before spawning meters.
        let bound = astrameter_powermeters::read_all_powermeter_configs(
            config,
            &self.registry,
            self.platform.clone(),
        )?;
        if bound.is_empty() {
            tracing::warn!("config has no recognised powermeter sections");
        }
        for bp in bound {
            tracing::info!(section = %bp.section, "instantiating powermeter");
            let cancel_clone = cancel.clone();
            let name = bp.section.clone();
            let meter = bp.meter;
            let h = tokio::spawn(async move {
                if let Err(e) = meter.start().await {
                    tracing::error!(section = %name, "start() failed: {e}");
                    return;
                }
                loop {
                    tokio::select! {
                        _ = cancel_clone.cancelled() => break,
                        _ = tokio::time::sleep(std::time::Duration::from_secs(10)) => {
                            match meter.get_powermeter_watts().await {
                                Ok(v) => tracing::info!(section = %name, watts = ?v, "sample"),
                                Err(e) => tracing::warn!(section = %name, "read failed: {e}"),
                            }
                        }
                    }
                }
                let _ = meter.stop().await;
            });
            handles.push(h);
        }
        let mut inner = self.inner.lock().await;
        inner.cancel = Some(cancel);
        inner.handles = handles;
        Ok(())
    }

    async fn stop(&self) {
        let mut inner = self.inner.lock().await;
        if let Some(token) = inner.cancel.take() {
            token.cancel();
        }
        for h in inner.handles.drain(..) {
            let _ = tokio::time::timeout(std::time::Duration::from_secs(5), h).await;
        }
    }
}
