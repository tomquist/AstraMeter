//! AstraMeter host entry point.
//!
//! Phase 0: this is a skeleton — it initializes logging, prints the version,
//! and exits. Phase 1 onward wires in config loading, the powermeter
//! registry, services, and the Supervisor.

use anyhow::Result;
use tracing_subscriber::EnvFilter;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();

    tracing::info!(version = astrameter_core::VERSION, "astrameter starting");

    // TODO(phase-1): parse CLI flags, load config.ini, spawn the Supervisor.

    tracing::info!("phase-0 skeleton: nothing to do, exiting");
    Ok(())
}
