//! Timer wrapping tokio. esp-idf-svc provides the necessary pthread
//! layer for tokio to schedule timers correctly.

use astrameter_platform::Timer;
use async_trait::async_trait;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub struct TokioTimer;

#[async_trait]
impl Timer for TokioTimer {
    async fn sleep(&self, dur: Duration) {
        tokio::time::sleep(dur).await;
    }

    fn monotonic_secs(&self) -> f64 {
        thread_local! {
            static EPOCH: Instant = Instant::now();
        }
        EPOCH.with(|e| e.elapsed().as_secs_f64())
    }

    fn unix_secs(&self) -> f64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    }
}
