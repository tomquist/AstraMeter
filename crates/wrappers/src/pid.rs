use crate::SharedMeter;
use astrameter_core::{Error, Powermeter, Result};
use async_trait::async_trait;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PidMode {
    Bias,
    Replace,
}

impl PidMode {
    pub fn from_str_ci(s: &str) -> Result<Self> {
        match s.trim().to_ascii_lowercase().as_str() {
            "bias" => Ok(PidMode::Bias),
            "replace" => Ok(PidMode::Replace),
            other => Err(Error::config(format!(
                "PID mode must be 'bias' or 'replace', got {other:?}"
            ))),
        }
    }
}

/// PID controller wrapper. Port of `wrappers/pid.py`.
pub struct PidPowermeter {
    inner: SharedMeter,
    kp: f64,
    ki: f64,
    kd: f64,
    output_max: f64,
    mode: PidMode,
    state: Mutex<PidState>,
}

#[derive(Default)]
struct PidState {
    integral: f64,
    prev_error: Option<f64>,
    prev_time: Option<Instant>,
}

impl PidPowermeter {
    pub fn new(
        inner: SharedMeter,
        kp: f64,
        ki: f64,
        kd: f64,
        output_max: f64,
        mode: PidMode,
    ) -> Result<Self> {
        if output_max <= 0.0 {
            return Err(Error::config(format!(
                "PID output_max must be positive, got {output_max}"
            )));
        }
        Ok(Self {
            inner,
            kp,
            ki,
            kd,
            output_max,
            mode,
            state: Mutex::new(PidState::default()),
        })
    }
}

#[async_trait]
impl Powermeter for PidPowermeter {
    async fn get_powermeter_watts(&self) -> Result<Vec<f64>> {
        let mut state = self.state.lock().await;
        let raw_values = self.inner.get_powermeter_watts().await?;
        let now = Instant::now();
        let total: f64 = raw_values.iter().sum();
        let error = -total;
        let dt = match state.prev_time {
            None => {
                state.prev_error = Some(error);
                state.prev_time = Some(now);
                0.0
            }
            Some(t) => {
                let dt = now.duration_since(t).as_secs_f64();
                if dt <= 0.0 {
                    0.0
                } else {
                    dt
                }
            }
        };

        let p_term = self.kp * error;
        let d_term = match (dt, state.prev_error) {
            (dt, Some(prev_e)) if dt > 0.0 => self.kd * (error - prev_e) / dt,
            _ => 0.0,
        };

        if dt > 0.0 {
            let tentative_integral = state.integral + error * dt;
            let tentative_output = p_term + self.ki * tentative_integral + d_term;
            let unwinding = state.integral != 0.0 && state.integral * error < 0.0;
            if tentative_output.abs() <= self.output_max || unwinding {
                state.integral = tentative_integral;
            }
        }
        let i_term = self.ki * state.integral;

        state.prev_error = Some(error);
        state.prev_time = Some(now);
        drop(state);

        let mut pid_output = p_term + i_term + d_term;
        pid_output = pid_output.clamp(-self.output_max, self.output_max);

        let n = raw_values.len().max(1) as f64;
        let per_phase = pid_output / n;
        match self.mode {
            PidMode::Bias => Ok(raw_values.into_iter().map(|v| v + per_phase).collect()),
            PidMode::Replace => Ok(vec![per_phase; raw_values.len()]),
        }
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
