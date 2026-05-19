//! Async battery simulator — port of `simulator/battery.py`.
//!
//! Speaks the CT002 UDP protocol: builds requests with the current
//! output power, sends them to the configured CT002 emulator, parses
//! the response, and adjusts its own target to drive the reported
//! grid reading toward zero.

use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;
use rand::Rng;
use tokio::net::UdpSocket;

use crate::protocol;

/// Snapshot of one battery's externally-visible state.
#[derive(Debug, Clone, serde::Serialize)]
pub struct BatteryState {
    pub mac: String,
    pub phase: String,
    pub power: i64,
    pub target: i64,
    pub applied_target: i64,
    pub power_update_delay_ticks: i64,
    pub soc: f64,
    pub max_charge: i64,
    pub max_discharge: i64,
}

/// Battery simulator. Public methods are thread-safe; the physics state
/// is behind a `parking_lot::Mutex` so the HTTP control endpoints and
/// the `step` loop can race without unsoundness.
pub struct BatterySimulator {
    pub mac: String,
    pub phase: char,
    pub ct_mac: String,
    pub ct_host: String,
    pub ct_port: u16,
    pub meter_dev_type: String,
    pub ct_dev_type: String,
    pub min_power_threshold: f64,
    pub ramp_rate: f64,
    pub poll_interval: f64,
    pub startup_delay: f64,
    pub inspection_count: u32,
    pub time_scale: f64,
    state: Mutex<State>,
}

struct State {
    current_power: f64,
    soc: f64,
    capacity_wh: f64,
    target_power: f64,
    requested_target: f64,
    request_count: u32,
    startup_elapsed: f64,
    step_index: i64,
    pending_targets: Vec<(i64, f64)>,
    max_charge_power: i64,
    max_discharge_power: i64,
    power_update_delay_ticks: i64,
}

#[derive(Clone)]
pub struct BatteryConfig {
    pub mac: String,
    pub phase: char,
    pub ct_mac: String,
    pub ct_host: String,
    pub ct_port: u16,
    pub meter_dev_type: String,
    pub ct_dev_type: String,
    pub max_charge_power: i64,
    pub max_discharge_power: i64,
    pub capacity_wh: f64,
    pub initial_soc: f64,
    pub ramp_rate: f64,
    pub poll_interval: f64,
    pub min_power_threshold: f64,
    pub startup_delay: f64,
    pub inspection_count: u32,
    pub time_scale: f64,
    pub power_update_delay_ticks: i64,
}

impl BatterySimulator {
    pub fn new(cfg: BatteryConfig) -> anyhow::Result<Arc<Self>> {
        if protocol::phase_field_index(cfg.phase).is_none() {
            anyhow::bail!("Invalid phase {:?}", cfg.phase);
        }
        Ok(Arc::new(Self {
            mac: cfg.mac.to_uppercase(),
            phase: cfg.phase,
            ct_mac: cfg.ct_mac,
            ct_host: cfg.ct_host,
            ct_port: cfg.ct_port,
            meter_dev_type: cfg.meter_dev_type,
            ct_dev_type: cfg.ct_dev_type,
            min_power_threshold: cfg.min_power_threshold,
            ramp_rate: cfg.ramp_rate,
            poll_interval: cfg.poll_interval,
            startup_delay: cfg.startup_delay.max(0.0),
            inspection_count: cfg.inspection_count,
            time_scale: cfg.time_scale.max(0.1),
            state: Mutex::new(State {
                current_power: 0.0,
                soc: cfg.initial_soc.clamp(0.0, 1.0),
                capacity_wh: cfg.capacity_wh,
                target_power: 0.0,
                requested_target: 0.0,
                request_count: 0,
                startup_elapsed: 0.0,
                step_index: 0,
                pending_targets: Vec::new(),
                max_charge_power: cfg.max_charge_power,
                max_discharge_power: cfg.max_discharge_power,
                power_update_delay_ticks: cfg.power_update_delay_ticks.max(0),
            }),
        }))
    }

    pub fn current_power(&self) -> f64 {
        self.state.lock().current_power
    }

    pub fn snapshot(&self) -> BatteryState {
        let s = self.state.lock();
        BatteryState {
            mac: self.mac.clone(),
            phase: self.phase.to_string(),
            power: s.current_power.round() as i64,
            target: s.requested_target.round() as i64,
            applied_target: s.target_power.round() as i64,
            power_update_delay_ticks: s.power_update_delay_ticks,
            soc: (s.soc * 10000.0).round() / 10000.0,
            max_charge: s.max_charge_power,
            max_discharge: s.max_discharge_power,
        }
    }

    pub fn set_soc(&self, soc: f64) {
        self.state.lock().soc = soc.clamp(0.0, 1.0);
    }

    pub fn set_max_charge(&self, w: i64) {
        self.state.lock().max_charge_power = w.max(0);
    }

    pub fn set_max_discharge(&self, w: i64) {
        self.state.lock().max_discharge_power = w.max(0);
    }

    /// Apply a CT-derived target: with `power_update_delay_ticks == 0` it
    /// takes effect immediately; otherwise it is queued for `N` ticks
    /// later. Exposed for tests that drive the state machine directly.
    pub fn apply_ct_derived_target(&self, new_target: f64) {
        let mut s = self.state.lock();
        s.requested_target = new_target;
        if s.power_update_delay_ticks <= 0 {
            s.target_power = new_target;
            return;
        }
        let apply_at = s.step_index + s.power_update_delay_ticks;
        s.pending_targets.push((apply_at, new_target));
    }

    fn drain_pending(&self) {
        let mut s = self.state.lock();
        if s.power_update_delay_ticks <= 0 {
            return;
        }
        let cur = s.step_index;
        let mut remaining = Vec::new();
        // Walk the pending list once. Apply any whose `apply_at <= step_index`
        // directly to `target_power` (last in iteration order wins, mirroring
        // Python's `_drain_pending_power_targets`). Keep the rest.
        for (apply_at, target) in s.pending_targets.drain(..).collect::<Vec<_>>() {
            if apply_at <= cur {
                s.target_power = target;
            } else {
                remaining.push((apply_at, target));
            }
        }
        s.pending_targets = remaining;
    }

    fn update_power(&self, dt: f64) {
        let mut s = self.state.lock();
        let mut target = s.target_power;
        if target.abs() < self.min_power_threshold {
            target = 0.0;
        }
        if s.soc >= 1.0 && target < 0.0 {
            target = 0.0;
        }
        if s.soc <= 0.0 && target > 0.0 {
            target = 0.0;
        }
        // Startup delay.
        if self.startup_delay > 0.0 {
            let idle = s.current_power.abs() < self.min_power_threshold;
            let want_power = target.abs() >= self.min_power_threshold;
            if idle && want_power {
                s.startup_elapsed += dt;
                if s.startup_elapsed < self.startup_delay {
                    return;
                }
            } else {
                s.startup_elapsed = 0.0;
            }
        }
        let diff = target - s.current_power;
        let max_step = self.ramp_rate * dt;
        let step = if diff.abs() > max_step {
            if diff > 0.0 {
                max_step
            } else {
                -max_step
            }
        } else {
            diff
        };
        s.current_power += step;
        let max_d = s.max_discharge_power as f64;
        let max_c = -(s.max_charge_power as f64);
        s.current_power = s.current_power.clamp(max_c, max_d);
    }

    fn update_soc(&self, dt: f64) {
        let mut s = self.state.lock();
        if s.capacity_wh <= 0.0 {
            return;
        }
        let energy_wh = s.current_power * (dt / 3600.0);
        s.soc -= energy_wh / s.capacity_wh;
        s.soc = s.soc.clamp(0.0, 1.0);
    }

    /// Send a single CT002 request and apply the response's grid reading
    /// (sum of phases A+B+C) as the new target. Returns the parsed
    /// response fields or `None` if the request timed out / decoded badly.
    async fn send_request(&self) -> Option<Vec<String>> {
        let (req_count, current) = {
            let s = self.state.lock();
            (s.request_count, s.current_power)
        };
        let phase_field = if req_count < self.inspection_count {
            "0".to_string()
        } else {
            self.phase.to_string()
        };
        let fields = vec![
            self.meter_dev_type.clone(),
            self.mac.clone(),
            self.ct_dev_type.clone(),
            self.ct_mac.clone(),
            phase_field.clone(),
            current.round().to_string(),
        ];
        let payload = protocol::build_payload(&fields);

        let sock = match UdpSocket::bind("0.0.0.0:0").await {
            Ok(s) => s,
            Err(e) => {
                tracing::debug!("Battery {}: bind: {e}", self.mac);
                return None;
            }
        };
        let dst = format!("{}:{}", self.ct_host, self.ct_port);
        if let Err(e) = sock.send_to(&payload, &dst).await {
            tracing::debug!("Battery {}: send: {e}", self.mac);
            return None;
        }
        let mut buf = vec![0u8; 2048];
        let n = match tokio::time::timeout(Duration::from_secs(2), sock.recv_from(&mut buf)).await {
            Ok(Ok((n, _))) => n,
            _ => {
                tracing::debug!("Battery {}: recv timeout", self.mac);
                return None;
            }
        };
        self.state.lock().request_count += 1;
        let (parsed, err) = protocol::parse_message(&buf[..n]);
        if let Some(e) = err {
            tracing::debug!("Battery {}: bad response: {e}", self.mac);
            return None;
        }
        let fields = parsed?;
        // Extract grid reading: sum of phase A/B/C power fields (4,5,6).
        if phase_field != "0" {
            let pa: i64 = fields.get(4).and_then(|s| s.parse().ok()).unwrap_or(0);
            let pb: i64 = fields.get(5).and_then(|s| s.parse().ok()).unwrap_or(0);
            let pc: i64 = fields.get(6).and_then(|s| s.parse().ok()).unwrap_or(0);
            let grid_reading = (pa + pb + pc) as f64;
            self.apply_ct_derived_target(current + grid_reading);
        }
        Some(fields)
    }

    /// Execute one simulator iteration with explicit `dt`. Does NOT
    /// sleep — designed for deterministic testing.
    pub async fn step(self: &Arc<Self>, dt: f64) -> Option<Vec<String>> {
        {
            let mut s = self.state.lock();
            s.step_index += 1;
        }
        self.drain_pending();
        self.update_power(dt);
        self.update_soc(dt);
        self.send_request().await
    }

    /// Long-running poll loop with random jitter.
    pub async fn run(self: Arc<Self>, cancel: tokio_util_local::CancelFlag) {
        tracing::info!(
            "Battery {} started (phase={}, soc={:.0}%)",
            self.mac,
            self.phase,
            self.state.lock().soc * 100.0
        );
        let mut last = std::time::Instant::now();
        loop {
            if cancel.is_cancelled() {
                return;
            }
            let now = std::time::Instant::now();
            let dt = now.duration_since(last).as_secs_f64() * self.time_scale;
            last = now;
            let _ = self.step(dt).await;
            let jitter = rand::thread_rng().gen_range(-0.5..=0.5);
            let sleep = ((self.poll_interval + jitter) / self.time_scale).max(0.05);
            tokio::time::sleep(Duration::from_secs_f64(sleep)).await;
        }
    }
}

// Tiny inline cancellation helper to avoid pulling tokio_util just for this.
pub mod tokio_util_local {
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    #[derive(Clone, Default)]
    pub struct CancelFlag(Arc<AtomicBool>);

    impl CancelFlag {
        pub fn new() -> Self {
            Self::default()
        }
        pub fn cancel(&self) {
            self.0.store(true, Ordering::SeqCst);
        }
        pub fn is_cancelled(&self) -> bool {
            self.0.load(Ordering::SeqCst)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> BatteryConfig {
        BatteryConfig {
            mac: "AABBCCDDEEFF".into(),
            phase: 'A',
            ct_mac: "112233445566".into(),
            ct_host: "127.0.0.1".into(),
            ct_port: 0,
            meter_dev_type: "HMG-50".into(),
            ct_dev_type: "HME-4".into(),
            max_charge_power: 800,
            max_discharge_power: 800,
            capacity_wh: 2560.0,
            initial_soc: 0.5,
            ramp_rate: 200.0,
            poll_interval: 1.0,
            min_power_threshold: 20.0,
            startup_delay: 0.0,
            inspection_count: 0,
            time_scale: 1.0,
            power_update_delay_ticks: 0,
        }
    }

    #[test]
    fn soc_clamps_at_extremes() {
        let b = BatterySimulator::new(cfg()).unwrap();
        b.set_soc(5.0);
        assert_eq!(b.snapshot().soc, 1.0);
        b.set_soc(-1.0);
        assert_eq!(b.snapshot().soc, 0.0);
    }

    #[tokio::test]
    async fn ramps_toward_target_within_max_step() {
        let b = BatterySimulator::new(cfg()).unwrap();
        b.apply_ct_derived_target(500.0);
        // dt=1s, ramp_rate=200 → expect ~200W after one step (no UDP roundtrip).
        b.update_power(1.0);
        let snap = b.snapshot();
        assert!(snap.power == 200, "got {}", snap.power);
    }

    /// Ports `test_power_update_immediate_when_delay_zero`.
    #[test]
    fn power_update_immediate_when_delay_zero() {
        let b = BatterySimulator::new(cfg()).unwrap();
        b.state.lock().current_power = 100.0;
        b.apply_ct_derived_target(250.0);
        assert_eq!(b.state.lock().target_power, 250.0);
    }

    /// Ports `test_power_update_delayed_by_n_ticks`. With delay=2, the CT
    /// target observed at tick T applies at tick T+2.
    #[test]
    fn power_update_delayed_by_n_ticks() {
        let c = BatteryConfig {
            power_update_delay_ticks: 2,
            startup_delay: 0.0,
            min_power_threshold: 0.0,
            ramp_rate: 1e9,
            inspection_count: 0,
            ..cfg()
        };
        let b = BatterySimulator::new(c).unwrap();
        b.state.lock().current_power = 100.0;
        // Each step bumps step_index by 1 and then drains pending. Simulate
        // the Python test by enqueuing the target *before* the tick that
        // owns it, i.e. in step 1 enqueue (applies in step 3).
        b.state.lock().step_index = 1;
        b.apply_ct_derived_target(250.0);
        // step 2 — not yet
        b.state.lock().step_index = 2;
        b.drain_pending();
        assert_eq!(b.state.lock().target_power, 0.0);
        b.state.lock().step_index = 3;
        b.drain_pending();
        assert_eq!(b.state.lock().target_power, 250.0);
    }
}
