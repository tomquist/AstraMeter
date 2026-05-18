//! Timer trait — sleep and wall-clock now.

use async_trait::async_trait;
use std::time::Duration;

#[async_trait]
pub trait Timer: Send + Sync {
    /// Sleep at least `dur`.
    async fn sleep(&self, dur: Duration);

    /// Monotonic seconds since some implementation-defined epoch. Always
    /// monotonically increasing within a process; do not assume wall-clock
    /// meaning.
    fn monotonic_secs(&self) -> f64;

    /// Wall-clock seconds since Unix epoch. May go backwards across NTP
    /// adjustments; on ESP32 returns 0 until SNTP has resolved.
    fn unix_secs(&self) -> f64;
}
