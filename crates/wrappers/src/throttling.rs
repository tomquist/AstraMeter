use crate::SharedMeter;
use astrameter_core::{Powermeter, Result};
use async_trait::async_trait;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::Mutex;

/// Coalesce concurrent fetches and enforce `throttle_interval` between
/// reads. Port of `wrappers/throttling.py`.
pub struct ThrottledPowermeter {
    inner: SharedMeter,
    throttle: Duration,
    state: Mutex<State>,
}

struct State {
    last_update: Option<std::time::Instant>,
    last_values: Option<Vec<f64>>,
    pending: Option<tokio::sync::broadcast::Sender<Vec<f64>>>,
}

impl ThrottledPowermeter {
    pub fn new(inner: SharedMeter, throttle: Duration) -> Self {
        Self {
            inner,
            throttle,
            state: Mutex::new(State {
                last_update: None,
                last_values: None,
                pending: None,
            }),
        }
    }
}

#[async_trait]
impl Powermeter for ThrottledPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        if self.throttle.is_zero() {
            return self.inner.get_powermeter_watts().await;
        }

        // Try to coalesce: if a fetch is in flight, await its result.
        let mut state = self.state.lock().await;
        if let Some(tx) = &state.pending {
            let mut rx = tx.subscribe();
            drop(state);
            return rx
                .recv()
                .await
                .map_err(|e| astrameter_core::Error::Other(format!("throttle coalesce: {e}")));
        }

        let (tx, _) = tokio::sync::broadcast::channel(1);
        state.pending = Some(tx.clone());

        // Compute remaining sleep.
        let remaining = state
            .last_update
            .map(|t| self.throttle.saturating_sub(t.elapsed()))
            .unwrap_or_default();
        let cached = state.last_values.clone();
        drop(state);

        if !remaining.is_zero() {
            tracing::debug!("throttling: waiting {:.2?} before fresh fetch", remaining);
            tokio::time::sleep(remaining).await;
        }

        let result = self.inner.get_powermeter_watts().await;
        let mut state = self.state.lock().await;
        state.last_update = Some(std::time::Instant::now());
        state.pending = None;
        match result {
            Ok(v) => {
                state.last_values = Some(v.clone());
                let _ = tx.send(v.clone());
                Ok(v)
            }
            Err(e) => {
                if let Some(cached_values) = cached {
                    tracing::warn!("throttle: error fetching, using cached: {e}");
                    let _ = tx.send(cached_values.clone());
                    Ok(cached_values)
                } else {
                    Err(e)
                }
            }
        }
    }

    async fn get_powermeter_watts_raw(&self) -> Result<Vec<f64>> {
        // Raw reads skip throttle coalescing; Marstek MQTT should mirror
        // the sensor cadence, not the control loop.
        self.inner.get_powermeter_watts_raw().await
    }

    async fn wait_for_message(&self, t: Duration) -> Result<()> {
        self.inner.wait_for_message(t).await
    }

    async fn wait_for_next_message(&self, t: Duration) -> Result<()> {
        self.inner.wait_for_next_message(t).await
    }

    async fn start(&self) -> Result<()> {
        self.inner.start().await
    }

    async fn stop(&self) -> Result<()> {
        self.inner.stop().await
    }

    fn reset(&self) {
        self.inner.reset()
    }
}

// Suppress unused-import lint when nothing else in this module uses Arc.
#[allow(dead_code)]
fn _arc_in_scope() -> Option<Arc<()>> {
    None
}
