//! Health + web config editor.
//!
//! Two routes:
//!   * `GET  /health`           — health check (Python `web_server.py`).
//!   * `GET  /api/config`       — return current INI contents.
//!   * `POST /api/config`       — atomically replace INI + signal supervisor.
//!   * `POST /api/restart`      — trigger a supervisor reload.

#![forbid(unsafe_code)]

pub mod config_ui;
pub mod health;

use std::path::PathBuf;
use std::sync::Arc;

use astrameter_core::{Error, Result};
use parking_lot::Mutex;

/// Shared state passed to web handlers and the supervisor.
#[derive(Clone)]
pub struct AppState {
    pub config_path: PathBuf,
    pub reload_tx: Arc<tokio::sync::mpsc::Sender<ReloadCommand>>,
    pub status: Arc<Mutex<Status>>,
}

#[derive(Debug, Clone, Default)]
pub struct Status {
    pub healthy: bool,
    pub last_reload_ok: Option<bool>,
    pub last_error: Option<String>,
}

#[derive(Debug)]
pub enum ReloadCommand {
    ApplyNewConfig,
}

/// Atomic save: write to `.tmp`, rename current to `.bak`, rename `.tmp` to current.
pub async fn save_config_atomic(path: &std::path::Path, contents: &[u8]) -> Result<()> {
    let tmp = path.with_extension("ini.tmp");
    let bak = path.with_extension("ini.bak");
    tokio::fs::write(&tmp, contents)
        .await
        .map_err(|e| Error::transport(format!("write tmp: {e}")))?;
    if path.exists() {
        let _ = tokio::fs::rename(path, &bak).await;
    }
    tokio::fs::rename(&tmp, path)
        .await
        .map_err(|e| Error::transport(format!("rename tmp: {e}")))?;
    Ok(())
}
