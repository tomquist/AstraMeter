use crate::SharedMeter;
use astrameter_core::{Error, Powermeter, Result};
use async_trait::async_trait;
use parking_lot::Mutex;
use std::collections::VecDeque;
use std::time::Duration;

const MAD_SCALE: f64 = 1.4826;

/// Rolling-median outlier filter on sum-of-phases. Port of
/// `wrappers/hampel.py`.
pub struct HampelPowermeter {
    inner: SharedMeter,
    window_size: usize,
    n_sigma: f64,
    min_threshold: f64,
    window: Mutex<VecDeque<f64>>,
}

impl HampelPowermeter {
    pub fn new(
        inner: SharedMeter,
        window: usize,
        n_sigma: f64,
        min_threshold: f64,
    ) -> Result<Self> {
        if window < 1 {
            return Err(Error::config(format!(
                "Hampel window must be >= 1, got {window}"
            )));
        }
        if n_sigma < 0.0 {
            return Err(Error::config(format!(
                "Hampel n_sigma must be >= 0, got {n_sigma}"
            )));
        }
        if min_threshold < 0.0 {
            return Err(Error::config(format!(
                "Hampel min_threshold must be >= 0, got {min_threshold}"
            )));
        }
        Ok(Self {
            inner,
            window_size: window,
            n_sigma,
            min_threshold,
            window: Mutex::new(VecDeque::with_capacity(window)),
        })
    }
}

fn median(slice: &[f64]) -> f64 {
    let mut v: Vec<f64> = slice.to_vec();
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = v.len();
    if n == 0 {
        return 0.0;
    }
    if n % 2 == 0 {
        (v[n / 2 - 1] + v[n / 2]) / 2.0
    } else {
        v[n / 2]
    }
}

#[async_trait]
impl Powermeter for HampelPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let raw = self.inner.get_powermeter_watts().await?;
        if raw.is_empty() {
            return Ok(raw);
        }
        let raw_total: f64 = raw.iter().sum();

        let mut window = self.window.lock();
        if window.len() == self.window_size {
            window.pop_front();
        }
        window.push_back(raw_total);

        if window.len() < self.window_size {
            return Ok(raw);
        }

        let snapshot: Vec<f64> = window.iter().copied().collect();
        let med = median(&snapshot);
        let deviations: Vec<f64> = snapshot.iter().map(|x| (x - med).abs()).collect();
        let mad = median(&deviations);
        let threshold = (self.n_sigma * MAD_SCALE * mad).max(self.min_threshold);

        if threshold <= 0.0 || (raw_total - med).abs() <= threshold {
            return Ok(raw);
        }

        *window.back_mut().unwrap() = med;
        drop(window);
        tracing::debug!(
            "Hampel: outlier rejected raw={:.2} median={:.2} threshold={:.2}",
            raw_total,
            med,
            threshold
        );

        if raw_total.abs() < 1e-9 {
            let n = raw.len() as f64;
            return Ok(vec![med / n; raw.len()]);
        }
        let ratio = med / raw_total;
        Ok(raw.iter().map(|v| v * ratio).collect())
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
        self.window.lock().clear();
        self.inner.reset()
    }
}
