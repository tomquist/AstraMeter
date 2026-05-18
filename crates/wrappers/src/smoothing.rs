use crate::SharedMeter;
use astrameter_core::{Powermeter, Result};
use async_trait::async_trait;
use parking_lot::Mutex;
use std::time::Duration;

/// EMA smoothing on sum-of-phases, distributed back per phase proportionally.
/// Port of `wrappers/smoothing.py::SmoothedPowermeter`.
pub struct SmoothedPowermeter {
    inner: SharedMeter,
    alpha: f64,
    max_step: f64,
    state: Mutex<State>,
}

#[derive(Default)]
struct State {
    value: Option<f64>,
    last_sample: Option<Vec<f64>>,
    last_raw_total: Option<f64>,
}

impl SmoothedPowermeter {
    pub fn new(inner: SharedMeter, alpha: f64, max_step: f64) -> Self {
        Self {
            inner,
            alpha,
            max_step,
            state: Mutex::new(State::default()),
        }
    }

    pub fn smoothed_value(&self) -> Option<f64> {
        self.state.lock().value
    }

    fn distribute(&self, raw: &[f64], raw_total: f64) -> Vec<f64> {
        let value = match self.state.lock().value {
            Some(v) => v,
            None => return raw.to_vec(),
        };
        if raw_total == 0.0 {
            return raw.to_vec();
        }
        let ratio = value / raw_total;
        raw.iter().map(|v| v * ratio).collect()
    }
}

#[async_trait]
impl Powermeter for SmoothedPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let raw = self.inner.get_powermeter_watts().await?;
        let raw_total: f64 = raw.iter().sum();
        let mut state = self.state.lock();

        if state.value.is_none() {
            state.value = Some(raw_total);
            state.last_sample = Some(raw.clone());
            state.last_raw_total = Some(raw_total);
            drop(state);
            return Ok(self.distribute(&raw, raw_total));
        }

        if state.last_sample.as_deref() == Some(&raw[..]) && state.last_raw_total == Some(raw_total)
        {
            drop(state);
            return Ok(self.distribute(&raw, raw_total));
        }
        state.last_sample = Some(raw.clone());
        state.last_raw_total = Some(raw_total);

        let prev = state.value.unwrap();
        let mut catchup_alpha = self.alpha;
        if (raw_total > 0.0) != (prev > 0.0) {
            catchup_alpha = self.alpha.max((self.alpha * 4.0).min(0.5));
        }
        let mut delta = catchup_alpha * (raw_total - prev);
        if self.max_step > 0.0 {
            delta = delta.clamp(-self.max_step, self.max_step);
        }
        state.value = Some(prev + delta);
        drop(state);
        Ok(self.distribute(&raw, raw_total))
    }

    async fn get_powermeter_watts_raw(&self) -> Result<Vec<f64>> {
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
        *self.state.lock() = State::default();
        self.inner.reset();
    }
}

/// Returns zeros when |sum| < deadband. Stateless. Port of
/// `wrappers/smoothing.py::DeadbandPowermeter`.
pub struct DeadbandPowermeter {
    inner: SharedMeter,
    deadband: f64,
}

impl DeadbandPowermeter {
    pub fn new(inner: SharedMeter, deadband: f64) -> Self {
        Self { inner, deadband }
    }
}

#[async_trait]
impl Powermeter for DeadbandPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let values = self.inner.get_powermeter_watts().await?;
        let total: f64 = values.iter().sum();
        if self.deadband > 0.0 && total.abs() < self.deadband {
            return Ok(vec![0.0; values.len()]);
        }
        Ok(values)
    }

    async fn get_powermeter_watts_raw(&self) -> Result<Vec<f64>> {
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
