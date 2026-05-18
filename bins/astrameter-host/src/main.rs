//! AstraMeter host entry point.

use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use astrameter_config::Config;
use astrameter_platform::Platform;
use astrameter_powermeters::{register_all, PowermeterRegistry};
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

    if !config_path.exists() {
        tracing::warn!(
            "config file not found at {} — running idle. Pass a path as the \
             first CLI argument, or place config.ini in the working directory.",
            config_path.display()
        );
        return tokio::signal::ctrl_c().await.context("waiting for ctrl-c");
    }

    let config = astrameter_config::load_file(&config_path)?;
    let mut reg = PowermeterRegistry::new();
    register_all(&mut reg);

    let supervisor = Supervisor::new(platform.clone(), reg);
    supervisor.start(&config).await?;

    tokio::signal::ctrl_c()
        .await
        .context("waiting for ctrl-c")?;
    supervisor.shutdown().await;
    Ok(())
}

/// Minimal supervisor: builds powermeters from a [`Config`], runs them in
/// parallel, and logs a sample every 10 s. Phase 7 expands this into the full
/// hot-reload Supervisor described in the plan.
struct Supervisor {
    platform: Arc<Platform>,
    registry: PowermeterRegistry,
    cancel: tokio_util::sync::CancellationToken,
}

impl Supervisor {
    fn new(platform: Arc<Platform>, registry: PowermeterRegistry) -> Self {
        Self {
            platform,
            registry,
            cancel: tokio_util::sync::CancellationToken::new(),
        }
    }

    async fn start(&self, config: &Config) -> Result<()> {
        for section_name in config.sections().collect::<Vec<_>>() {
            let section = match config.section(section_name) {
                Some(s) => s,
                None => continue,
            };
            let Some(factory) = self.registry.lookup(section_name) else {
                continue;
            };
            // Skip MQTT_INSIGHTS via prefix-precedence: registered MQTT prefix
            // would match it too, so an explicit guard keeps insights out.
            if section_name.starts_with("MQTT_INSIGHTS") {
                continue;
            }
            tracing::info!(section = section_name, "instantiating powermeter");
            let meter = match factory(&section, self.platform.clone()) {
                Ok(m) => m,
                Err(e) => {
                    tracing::error!(section = section_name, "failed to instantiate: {e}");
                    continue;
                }
            };
            let cancel = self.cancel.clone();
            let name = section_name.to_string();
            tokio::spawn(async move {
                if let Err(e) = meter.start().await {
                    tracing::error!(section = %name, "start() failed: {e}");
                    return;
                }
                loop {
                    tokio::select! {
                        _ = cancel.cancelled() => break,
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
        }
        Ok(())
    }

    async fn shutdown(&self) {
        self.cancel.cancel();
        // Best-effort grace period for spawned tasks to drop.
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    }
}
