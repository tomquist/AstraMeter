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

/// Atomic save with the same three fallbacks Python's
/// `_atomic_write_lines` uses (Docker bind-mounts and HA-addon overlayfs
/// reject rename(2)):
///   1. write `.tmp` then `rename(tmp → target)` — happy path.
///   2. on EBUSY / EPERM / EACCES: `copy_file(tmp → target)` then unlink
///      the tmp. Handles Docker bind-mounts that block rename but allow
///      open-for-write.
///   3. on second failure: `remove(target)` + `rename(tmp → target)`.
///      Handles overlayfs where the file lives in a read-only lower
///      layer.
pub async fn save_config_atomic(path: &std::path::Path, contents: &[u8]) -> Result<()> {
    let tmp = path.with_extension("ini.tmp");
    let bak = path.with_extension("ini.bak");
    tokio::fs::write(&tmp, contents)
        .await
        .map_err(|e| Error::transport(format!("write tmp: {e}")))?;
    if path.exists() {
        let _ = tokio::fs::rename(path, &bak).await;
    }
    match tokio::fs::rename(&tmp, path).await {
        Ok(()) => Ok(()),
        Err(e) if is_mount_restriction(&e) => {
            tracing::warn!("rename blocked ({e}); falling back to copy");
            if let Err(copy_err) = tokio::fs::copy(&tmp, path).await {
                tracing::warn!("copy fallback failed ({copy_err}); trying unlink+rename");
                let _ = tokio::fs::remove_file(path).await;
                if let Err(re) = tokio::fs::rename(&tmp, path).await {
                    return Err(Error::transport(format!(
                        "atomic save failed (rename={e}, copy={copy_err}, retry={re})"
                    )));
                }
            } else {
                let _ = tokio::fs::remove_file(&tmp).await;
            }
            Ok(())
        }
        Err(e) => Err(Error::transport(format!("rename tmp: {e}"))),
    }
}

fn is_mount_restriction(e: &std::io::Error) -> bool {
    use std::io::ErrorKind;
    matches!(
        e.kind(),
        ErrorKind::PermissionDenied
            | ErrorKind::ResourceBusy
            | ErrorKind::CrossesDevices
            | ErrorKind::Other
    )
}
