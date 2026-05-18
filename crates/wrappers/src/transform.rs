use crate::SharedMeter;
use astrameter_core::{Error, Powermeter, Result};
use async_trait::async_trait;
use parking_lot::Mutex;
use std::time::Duration;

/// Applies `value * multiplier + offset` per phase. Direct port of
/// `wrappers/transform.py`.
pub struct TransformedPowermeter {
    inner: SharedMeter,
    offsets: Vec<f64>,
    multipliers: Vec<f64>,
    warned: Mutex<Warned>,
}

#[derive(Default)]
struct Warned {
    offsets: bool,
    multipliers: bool,
}

impl TransformedPowermeter {
    pub fn new(inner: SharedMeter, offsets: Vec<f64>, multipliers: Vec<f64>) -> Result<Self> {
        if offsets.is_empty() {
            return Err(Error::config("offsets must be non-empty"));
        }
        if multipliers.is_empty() {
            return Err(Error::config("multipliers must be non-empty"));
        }
        Ok(Self {
            inner,
            offsets,
            multipliers,
            warned: Mutex::new(Warned::default()),
        })
    }

    fn apply(&self, values: Vec<f64>) -> Vec<f64> {
        let mut out = Vec::with_capacity(values.len());
        for (i, v) in values.iter().enumerate() {
            let m = self.multipliers[i % self.multipliers.len()];
            let o = self.offsets[i % self.offsets.len()];
            out.push(v * m + o);
        }
        let mut warned = self.warned.lock();
        if self.offsets.len() > 1 && self.offsets.len() != values.len() {
            if !warned.offsets {
                tracing::warn!(
                    "POWER_OFFSET has {} values but powermeter returned {} phases",
                    self.offsets.len(),
                    values.len()
                );
                warned.offsets = true;
            }
        } else {
            warned.offsets = false;
        }
        if self.multipliers.len() > 1 && self.multipliers.len() != values.len() {
            if !warned.multipliers {
                tracing::warn!(
                    "POWER_MULTIPLIER has {} values but powermeter returned {} phases",
                    self.multipliers.len(),
                    values.len()
                );
                warned.multipliers = true;
            }
        } else {
            warned.multipliers = false;
        }
        out
    }
}

#[async_trait]
impl Powermeter for TransformedPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let v = self.inner.get_powermeter_watts().await?;
        Ok(self.apply(v))
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    struct Fake(Vec<f64>);
    #[async_trait]
    impl Powermeter for Fake {
        async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
            Ok(self.0.clone())
        }
    }

    #[tokio::test]
    async fn applies_per_phase() {
        let inner: SharedMeter = Arc::new(Fake(vec![100.0, 200.0, 300.0]));
        let t = TransformedPowermeter::new(inner, vec![1.0, 2.0, 3.0], vec![2.0]).unwrap();
        assert_eq!(
            t.get_powermeter_watts().await.unwrap(),
            vec![201.0, 402.0, 603.0]
        );
    }

    #[tokio::test]
    async fn single_offset_repeats() {
        let inner: SharedMeter = Arc::new(Fake(vec![50.0, 60.0]));
        let t = TransformedPowermeter::new(inner, vec![10.0], vec![2.0]).unwrap();
        assert_eq!(t.get_powermeter_watts().await.unwrap(), vec![110.0, 130.0]);
    }
}
