//! Multi-battery load split.
//!
//! **Simplified port.** The Python `balancer.py` is 1,270 LOC and implements
//! several optional control modes — efficiency rotation, fair-share gain
//! control with deadband, time-weighted saturation detection with grace
//! periods, manual MQTT target override, ARP-based consumer discovery, and
//! probe-based fades. This Rust port currently exposes a single
//! [`Balancer::split`] function that distributes grid power **equally**
//! across discovered batteries. It is correct enough for relay-mode
//! deployments where `ACTIVE_CONTROL=False` was previously chosen, but
//! users running active control should keep the Python implementation
//! until this module is fleshed out and snapshot-tested against captured
//! Python decisions.
//!
//! See `src/astrameter/ct002/balancer.py` for the full Python reference.

use parking_lot::Mutex;
use std::collections::HashMap;
use std::time::Instant;

#[derive(Debug, Clone)]
pub struct BalancerConfig {
    pub active_control: bool,
    pub fair_distribution: bool,
    pub balance_deadband: f64,
}

impl Default for BalancerConfig {
    fn default() -> Self {
        Self {
            active_control: true,
            fair_distribution: true,
            balance_deadband: 15.0,
        }
    }
}

#[derive(Debug, Clone)]
pub struct BatteryState {
    pub last_seen: Instant,
    pub last_reported_power: f64,
}

#[derive(Default)]
pub struct Balancer {
    cfg: BalancerConfig,
    state: Mutex<BalancerState>,
}

#[derive(Default)]
struct BalancerState {
    batteries: HashMap<String, BatteryState>,
}

impl Balancer {
    pub fn new(cfg: BalancerConfig) -> Self {
        Self {
            cfg,
            state: Mutex::new(BalancerState::default()),
        }
    }

    /// Per-battery setpoint for a given total grid demand (positive = import).
    /// The returned vec is in the same order as `battery_ids`.
    pub fn split(&self, grid_power_w: f64, battery_ids: &[String]) -> Vec<f64> {
        let mut st = self.state.lock();
        let now = Instant::now();
        for id in battery_ids {
            st.batteries.entry(id.clone()).or_insert(BatteryState {
                last_seen: now,
                last_reported_power: 0.0,
            });
        }
        if !self.cfg.active_control {
            // Relay mode: forward the same total to every battery; they
            // negotiate among themselves.
            return vec![grid_power_w; battery_ids.len()];
        }
        // Equal split, with deadband zero.
        if grid_power_w.abs() < self.cfg.balance_deadband {
            return vec![0.0; battery_ids.len()];
        }
        let per = grid_power_w / battery_ids.len() as f64;
        vec![per; battery_ids.len()]
    }
}
