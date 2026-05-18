use core::time::Duration;

use async_trait::async_trait;

use crate::Result;

/// A power meter source. Mirrors the Python `Powermeter` ABC in
/// `src/astrameter/powermeter/base.py`.
///
/// Polling implementations override `get_powermeter_watts`. Push-based
/// implementations additionally override `start`, `stop`, and
/// `wait_for_next_message`.
#[async_trait]
pub trait Powermeter: Send + Sync {
    /// Return the latest per-phase watts after the section's wrapper pipeline.
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>>;

    /// Per-phase watts before section/global processing wrappers. Defaults to
    /// the same values as `get_powermeter_watts` for sources with no inner
    /// pipeline.
    async fn get_powermeter_watts_raw(&self) -> Result<Vec<f64>> {
        self.get_powermeter_watts().await
    }

    /// Block until the source has received at least one measurement.
    async fn wait_for_message(&self, _timeout: Duration) -> Result<()> {
        Ok(())
    }

    /// Block until a *new* measurement arrives (push-based powermeters).
    async fn wait_for_next_message(&self, _timeout: Duration) -> Result<()> {
        Ok(())
    }

    /// Lifecycle hook for push-based powermeters that need a background task.
    async fn start(&self) -> Result<()> {
        Ok(())
    }

    /// Stop background work. Must be idempotent — the Supervisor may call this
    /// during a hot config reload.
    async fn stop(&self) -> Result<()> {
        Ok(())
    }

    /// Reset cached state (used by some wrappers between reloads).
    fn reset(&self) {}
}
