//! Multi-battery load split with efficiency rotation and saturation
//! detection. Faithful port of `src/astrameter/ct002/balancer.py`.
//!
//! Architecture mirrors the Python source line-by-line:
//! - [`BalancerConfig`] holds tuning knobs (with the same clamping rules).
//! - [`SaturationTracker`] runs a time-weighted EMA on per-consumer
//!   "can-follow-target" health, with grace periods and stall timeouts.
//! - [`LoadBalancer`] owns the auto-pool pipeline: inactive steering,
//!   manual override, efficiency deprioritization with priority rotation,
//!   EMA fade transitions, fair-share distribution with balance correction,
//!   probe-based handoffs, and phase-aware splitting.
//!
//! The Python `dict[str, dict]` consumer-report shape is modelled by
//! [`ConsumerReport`]; the public surface uses HashMap<String, ConsumerReport>
//! everywhere a Python reader would expect `dict[str, dict]`.

use parking_lot::Mutex;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;

// ---------------------------------------------------------------------------
// Constants (mirroring Python module-level constants)
// ---------------------------------------------------------------------------

const EFFICIENCY_HYSTERESIS_FACTOR: f64 = 1.2;
pub const SATURATION_GRACE_SECONDS: f64 = 90.0;
pub const SATURATION_STALL_TIMEOUT_SECONDS: f64 = 60.0;
const SATURATION_REFERENCE_DT: f64 = 1.0;
const SATURATION_LONG_GAP_SECONDS: f64 = 30.0;

pub const AC_CHARGEABLE_DEVICE_PREFIXES: &[&str] = &["HMG", "VNS"];

pub fn is_ac_chargeable(device_type: &str) -> bool {
    if device_type.is_empty() {
        return false;
    }
    let upper = device_type.to_ascii_uppercase();
    AC_CHARGEABLE_DEVICE_PREFIXES
        .iter()
        .any(|p| upper.starts_with(p))
}

// ---------------------------------------------------------------------------
// Reports (replaces Python dict[str, dict])
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct ConsumerReport {
    pub power: i32,
    /// One of 'A', 'B', 'C'. Defaults to 'A' on missing/unknown.
    pub phase: char,
    pub device_type: String,
}

pub type Reports = HashMap<String, ConsumerReport>;

fn report_power(reports: &Reports, cid: &str) -> i32 {
    reports.get(cid).map(|r| r.power).unwrap_or(0)
}

fn report_phase(reports: &Reports, cid: &str) -> char {
    reports
        .get(cid)
        .map(|r| match r.phase.to_ascii_uppercase() {
            c @ ('A' | 'B' | 'C') => c,
            _ => 'A',
        })
        .unwrap_or('A')
}

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy)]
pub struct BalancerConfig {
    pub fair_distribution: bool,
    pub balance_gain: f64,
    pub balance_deadband: f64,
    pub error_boost_threshold: f64,
    pub error_boost_max: f64,
    pub error_reduce_threshold: f64,
    pub max_correction_per_step: f64,
    pub max_target_step: f64,
    pub min_efficient_power: f64,
    pub probe_min_power: f64,
    pub efficiency_rotation_interval: f64,
    pub efficiency_fade_alpha: f64,
    pub efficiency_saturation_threshold: f64,
}

impl Default for BalancerConfig {
    fn default() -> Self {
        Self {
            fair_distribution: true,
            balance_gain: 0.2,
            balance_deadband: 15.0,
            error_boost_threshold: 150.0,
            error_boost_max: 0.5,
            error_reduce_threshold: 20.0,
            max_correction_per_step: 80.0,
            max_target_step: 0.0,
            min_efficient_power: 0.0,
            probe_min_power: 80.0,
            efficiency_rotation_interval: 900.0,
            efficiency_fade_alpha: 0.15,
            efficiency_saturation_threshold: 0.4,
        }
    }
}

impl BalancerConfig {
    pub fn clamped(mut self) -> Self {
        self.balance_gain = self.balance_gain.clamp(0.0, 1.0);
        self.balance_deadband = self.balance_deadband.max(0.0);
        self.error_boost_threshold = self.error_boost_threshold.max(0.0);
        self.error_boost_max = self.error_boost_max.max(0.0);
        self.error_reduce_threshold = self.error_reduce_threshold.max(0.0);
        self.max_correction_per_step = self.max_correction_per_step.max(0.0);
        self.max_target_step = self.max_target_step.max(0.0);
        self.min_efficient_power = self.min_efficient_power.max(0.0);
        self.probe_min_power = self.probe_min_power.max(0.0);
        self.efficiency_rotation_interval = self.efficiency_rotation_interval.max(1.0);
        self.efficiency_fade_alpha = self.efficiency_fade_alpha.clamp(0.01, 1.0);
        self.efficiency_saturation_threshold = self.efficiency_saturation_threshold.clamp(0.0, 1.0);
        self
    }
}

// ---------------------------------------------------------------------------
// Consumer mode
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ConsumerMode {
    Auto,
    Manual(f64),
    Inactive,
}

impl ConsumerMode {
    pub fn is_manual(self) -> bool {
        matches!(self, ConsumerMode::Manual(_))
    }
    pub fn is_inactive(self) -> bool {
        matches!(self, ConsumerMode::Inactive)
    }
}

// ---------------------------------------------------------------------------
// Per-consumer state
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct BalancerConsumerState {
    pub last_target: Option<f64>,
    pub fade_weight: f64,
    pub saturation_score: f64,
    pub saturation_grace_until: f64,
    pub saturation_grace_started_at: f64,
    pub last_saturation_update: f64,
}

impl Default for BalancerConsumerState {
    fn default() -> Self {
        Self {
            last_target: None,
            fade_weight: 1.0,
            saturation_score: 0.0,
            saturation_grace_until: 0.0,
            saturation_grace_started_at: 0.0,
            last_saturation_update: 0.0,
        }
    }
}

#[derive(Debug, Clone)]
struct ProbeState {
    candidate_id: String,
    active_ids: Vec<String>,
    backup_ids: Vec<String>,
    restore_active_ids: Vec<String>,
    deadline: f64,
    #[allow(dead_code)]
    started_at: f64,
    proof_samples: u32,
    requested_power_abs: f64,
}

// ---------------------------------------------------------------------------
// Saturation tracker
// ---------------------------------------------------------------------------

pub struct SaturationTracker {
    enabled: bool,
    alpha: f64,
    min_target: f64,
    decay_factor: f64,
    stall_timeout_seconds: f64,
    clock: ClockFn,
}

type ClockFn = Arc<dyn Fn() -> f64 + Send + Sync>;

fn default_clock() -> ClockFn {
    Arc::new(|| {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0)
    })
}

impl SaturationTracker {
    pub fn new(
        alpha: f64,
        min_target: f64,
        decay_factor: f64,
        stall_timeout_seconds: f64,
        enabled: bool,
        clock: ClockFn,
    ) -> Self {
        Self {
            enabled,
            alpha: alpha.clamp(0.01, 1.0),
            min_target: min_target.max(1.0),
            decay_factor: decay_factor.clamp(0.0, 1.0),
            stall_timeout_seconds: stall_timeout_seconds.max(0.0),
            clock,
        }
    }

    pub fn update(&self, state: &mut BalancerConsumerState, last_target: Option<f64>, actual: i32) {
        if !self.enabled {
            return;
        }
        let Some(last_target) = last_target else {
            return;
        };
        let now = (self.clock)();
        let target_abs = last_target.abs();

        if state.saturation_grace_until > 0.0 {
            if now < state.saturation_grace_until {
                if (actual.unsigned_abs() as f64) >= self.min_target {
                    state.saturation_grace_until = 0.0;
                    state.saturation_grace_started_at = 0.0;
                    state.last_saturation_update = 0.0;
                } else if target_abs >= self.min_target
                    && state.saturation_grace_started_at > 0.0
                    && now - state.saturation_grace_started_at >= self.stall_timeout_seconds
                {
                    state.saturation_score = 1.0;
                    state.saturation_grace_until = 0.0;
                    state.saturation_grace_started_at = 0.0;
                    state.last_saturation_update = 0.0;
                    return;
                } else {
                    return;
                }
            } else {
                state.saturation_grace_until = 0.0;
                state.saturation_grace_started_at = 0.0;
                state.last_saturation_update = 0.0;
            }
        }

        let target_sign = signum_i(last_target);
        let actual_sign = signum_i(actual as f64);
        let sign_reversing = target_sign != 0 && actual_sign != 0 && target_sign != actual_sign;

        let prev_t = if state.last_saturation_update <= 0.0 {
            now - SATURATION_REFERENCE_DT
        } else {
            state.last_saturation_update
        };
        let dt = (now - prev_t).max(0.0);
        state.last_saturation_update = now;
        if dt == 0.0 || dt > SATURATION_LONG_GAP_SECONDS {
            return;
        }
        let ratio = dt / SATURATION_REFERENCE_DT;

        if target_abs < self.min_target || sign_reversing {
            let prev = state.saturation_score;
            if prev > 0.0 {
                let decayed = prev * self.decay_factor.powf(ratio);
                state.saturation_score = if decayed < 0.001 { 0.0 } else { decayed };
            }
            return;
        }
        let inst_saturation: f64 = if (actual.unsigned_abs() as f64) < self.min_target {
            1.0
        } else {
            0.0
        };
        let alpha_eff = 1.0 - (1.0 - self.alpha).powf(ratio);
        let prev = state.saturation_score;
        state.saturation_score = alpha_eff * inst_saturation + (1.0 - alpha_eff) * prev;
    }

    pub fn set_grace(&self, state: &mut BalancerConsumerState, deadline: f64) {
        state.saturation_grace_until = deadline;
        state.saturation_grace_started_at = (self.clock)();
        state.last_saturation_update = 0.0;
    }

    pub fn clear(state: &mut BalancerConsumerState) {
        state.saturation_score = 0.0;
        state.saturation_grace_until = 0.0;
        state.saturation_grace_started_at = 0.0;
        state.last_saturation_update = 0.0;
    }
}

fn signum_i(v: f64) -> i32 {
    if v > 0.0 {
        1
    } else if v < 0.0 {
        -1
    } else {
        0
    }
}

// ---------------------------------------------------------------------------
// Load balancer
// ---------------------------------------------------------------------------

pub type ResetFn = Arc<dyn Fn() + Send + Sync>;

pub struct LoadBalancer {
    cfg: BalancerConfig,
    saturation: SaturationTracker,
    saturation_grace_seconds: f64,
    reset_fn: Option<ResetFn>,
    state: Mutex<LbInner>,
    clock: ClockFn,
    probe_timeout_seconds: f64,
    probe_success_threshold: f64,
}

struct LbInner {
    consumers: HashMap<String, BalancerConsumerState>,
    deprioritized: HashSet<String>,
    priority: Vec<String>,
    last_rotation: f64,
    cache_sample: Option<(Vec<f64>, Vec<String>)>,
    cache_result: Option<HashMap<String, f64>>,
    probe_state: Option<ProbeState>,
    post_probe_fade_until: f64,
    post_probe_fade_ids: HashSet<String>,
    all_dc_surplus_warned: bool,
}

impl LoadBalancer {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        config: BalancerConfig,
        saturation_alpha: f64,
        saturation_min_target: f64,
        saturation_decay_factor: f64,
        saturation_grace_seconds: f64,
        saturation_stall_timeout_seconds: f64,
        saturation_enabled: bool,
        clock: Option<ClockFn>,
        reset_fn: Option<ResetFn>,
    ) -> Self {
        let clock = clock.unwrap_or_else(default_clock);
        let now = (clock)();
        let saturation = SaturationTracker::new(
            saturation_alpha,
            saturation_min_target,
            saturation_decay_factor,
            saturation_stall_timeout_seconds,
            saturation_enabled,
            clock.clone(),
        );
        let probe_timeout_seconds = saturation_grace_seconds.max(0.0);
        let probe_success_threshold = saturation_min_target.max(1.0);
        Self {
            cfg: config.clamped(),
            saturation,
            saturation_grace_seconds: saturation_grace_seconds.max(0.0),
            reset_fn,
            state: Mutex::new(LbInner {
                consumers: HashMap::new(),
                deprioritized: HashSet::new(),
                priority: Vec::new(),
                last_rotation: now,
                cache_sample: None,
                cache_result: None,
                probe_state: None,
                post_probe_fade_until: 0.0,
                post_probe_fade_ids: HashSet::new(),
                all_dc_surplus_warned: false,
            }),
            clock,
            probe_timeout_seconds,
            probe_success_threshold,
        }
    }

    fn now(&self) -> f64 {
        (self.clock)()
    }

    // ------------------------------------------------------------------
    // Observability
    // ------------------------------------------------------------------

    pub fn get_saturation(&self, consumer_id: &str) -> f64 {
        self.state
            .lock()
            .consumers
            .get(consumer_id)
            .map(|s| s.saturation_score)
            .unwrap_or(0.0)
    }

    pub fn get_last_target(&self, consumer_id: &str) -> Option<f64> {
        self.state
            .lock()
            .consumers
            .get(consumer_id)
            .and_then(|s| s.last_target)
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    pub fn remove_consumer(&self, consumer_id: &str) {
        let mut s = self.state.lock();
        s.consumers.remove(consumer_id);
        s.deprioritized.remove(consumer_id);
        if let Some(pos) = s.priority.iter().position(|c| c == consumer_id) {
            s.priority.remove(pos);
            s.cache_sample = None;
            s.cache_result = None;
        }
        let probe_match = s
            .probe_state
            .as_ref()
            .map(|p| {
                p.active_ids.iter().any(|c| c == consumer_id)
                    || p.backup_ids.iter().any(|c| c == consumer_id)
            })
            .unwrap_or(false);
        if probe_match {
            tracing::info!("Efficiency: ending probe (consumer removed)");
            s.probe_state = None;
            s.cache_sample = None;
            s.cache_result = None;
        }
    }

    pub fn detach_from_auto_pool(&self, consumer_id: &str) {
        let mut s = self.state.lock();
        s.deprioritized.remove(consumer_id);
        s.priority.retain(|c| c != consumer_id);
        s.consumers.remove(consumer_id);
        s.cache_sample = None;
        s.cache_result = None;
        let probe_match = s
            .probe_state
            .as_ref()
            .map(|p| {
                p.active_ids.iter().any(|c| c == consumer_id)
                    || p.backup_ids.iter().any(|c| c == consumer_id)
            })
            .unwrap_or(false);
        if probe_match {
            tracing::info!("Efficiency: ending probe (consumer detached)");
            s.probe_state = None;
        }
    }

    pub fn reset_consumer(&self, consumer_id: &str) {
        let now = self.now();
        let mut s = self.state.lock();
        let state = s.consumers.entry(consumer_id.to_string()).or_default();
        state.last_target = None;
        state.saturation_score = 0.0;
        let grace = now
            + self
                .saturation_grace_seconds
                .min(self.cfg.efficiency_rotation_interval);
        self.saturation.set_grace(state, grace);
    }

    pub fn force_rotation(&self, current_pool: &HashSet<String>) {
        let now = self.now();
        let mut s = self.state.lock();
        s.priority.retain(|c| current_pool.contains(c));
        let mut new_ids: Vec<String> = current_pool
            .iter()
            .filter(|id| !s.priority.contains(id))
            .cloned()
            .collect();
        new_ids.sort();
        s.priority.extend(new_ids);
        s.deprioritized.retain(|c| current_pool.contains(c));
        if s.priority.len() < 2 {
            return;
        }
        let head = s.priority.remove(0);
        s.priority.push(head);
        s.last_rotation = now;
        s.probe_state = None;
        s.cache_sample = None;
        s.cache_result = None;
        let known: Vec<String> = s.consumers.keys().cloned().collect();
        for cid in known {
            if current_pool.contains(&cid) {
                s.consumers.entry(cid).or_default().fade_weight = 1.0;
            } else {
                s.consumers.remove(&cid);
            }
        }
        tracing::info!(
            "Efficiency: forced rotation, new order: {:?}",
            short_ids(&s.priority)
        );
    }

    // ------------------------------------------------------------------
    // Primary interface
    // ------------------------------------------------------------------

    #[allow(clippy::too_many_arguments)]
    pub fn compute_target(
        &self,
        consumer_id: Option<&str>,
        consumer_mode: ConsumerMode,
        all_reports: &Reports,
        grid_total: f64,
        inactive: &HashSet<String>,
        manual: &HashSet<String>,
        sample_id: Vec<f64>,
    ) -> [f64; 3] {
        if consumer_mode.is_inactive() {
            return self.steer_to_zero(consumer_id, all_reports);
        }
        let active_reports: Reports = all_reports
            .iter()
            .filter(|(cid, _)| !inactive.contains(*cid))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();

        // Update saturation (with skip conditions matching Python).
        {
            let probe_participants = self.probe_participants();
            let mut s = self.state.lock();
            if let Some(cid) = consumer_id {
                let in_active = active_reports.contains_key(cid);
                let in_probe = probe_participants.contains(cid);
                let in_deprio = s.deprioritized.contains(cid);
                if in_active && !consumer_mode.is_manual() && !in_probe && !in_deprio {
                    let actual = report_power(&active_reports, cid);
                    let state = s.consumers.entry(cid.to_string()).or_default();
                    let last_target = state.last_target;
                    self.saturation.update(state, last_target, actual);
                }
            }
        }

        if let ConsumerMode::Manual(manual_value) = consumer_mode {
            if let Some(cid) = consumer_id {
                let reported = report_power(&active_reports, cid);
                let target = manual_value - reported as f64;
                {
                    let mut s = self.state.lock();
                    s.consumers.entry(cid.to_string()).or_default().last_target = Some(target);
                }
                return Self::split_by_phase(target, &active_reports, None);
            }
        }

        let reports: Reports = active_reports
            .iter()
            .filter(|(cid, _)| !manual.contains(*cid))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        self.compute_auto_target(consumer_id, &reports, grid_total, sample_id)
    }

    // ------------------------------------------------------------------
    // Auto-target pipeline
    // ------------------------------------------------------------------

    fn compute_auto_target(
        &self,
        consumer_id: Option<&str>,
        reports: &Reports,
        grid_total: f64,
        sample_id: Vec<f64>,
    ) -> [f64; 3] {
        let saturation: HashMap<String, f64> = {
            let s = self.state.lock();
            s.consumers
                .iter()
                .map(|(k, v)| (k.clone(), v.saturation_score))
                .collect()
        };
        let num_consumers = reports.len().max(1);
        let mut eff_part: HashMap<String, f64> = reports
            .keys()
            .map(|cid| {
                (
                    cid.clone(),
                    (1.0 - saturation.get(cid).copied().unwrap_or(0.0)).max(0.01),
                )
            })
            .collect();

        // AC-chargeable gating (issue #338 / #359).
        let ac_charging = reports
            .values()
            .any(|r| is_ac_chargeable(&r.device_type) && r.power < 0);
        let any_ac_chargeable = reports.values().any(|r| is_ac_chargeable(&r.device_type));
        let in_charge_territory =
            any_ac_chargeable && (grid_total < 0.0 || (grid_total == 0.0 && ac_charging));
        let charge_blind: HashSet<String> = if in_charge_territory {
            reports
                .iter()
                .filter(|(_, r)| !is_ac_chargeable(&r.device_type))
                .map(|(cid, _)| cid.clone())
                .collect()
        } else {
            HashSet::new()
        };
        for cid in &charge_blind {
            eff_part.insert(cid.clone(), 0.0);
        }

        let efficiency_adjustments =
            self.compute_efficiency_deprioritized(reports, &sample_id, grid_total);
        let report_ids: HashSet<String> = reports.keys().cloned().collect();
        let faded_adjustments = self.fade_efficiency_weights(&efficiency_adjustments, &report_ids);
        let any_fading = faded_adjustments.values().any(|w| *w > 0.0 && *w < 1.0);

        if let Some(probe_target) =
            self.compute_probe_target(consumer_id, reports, grid_total, &eff_part)
        {
            return probe_target;
        }

        // Degenerate all-DC under surplus (issue #338).
        let all_dc_under_surplus = grid_total < 0.0 && !reports.is_empty() && !any_ac_chargeable;
        {
            let mut s = self.state.lock();
            if all_dc_under_surplus && !s.all_dc_surplus_warned {
                let mut device_types: Vec<String> = reports
                    .values()
                    .map(|r| {
                        if r.device_type.is_empty() {
                            "?".to_string()
                        } else {
                            r.device_type.clone()
                        }
                    })
                    .collect();
                device_types.sort();
                device_types.dedup();
                tracing::info!(
                    "CT002: {:.0} W surplus but no AC-chargeable battery reporting; types: {:?}",
                    -grid_total,
                    device_types,
                );
                s.all_dc_surplus_warned = true;
            } else if !all_dc_under_surplus {
                s.all_dc_surplus_warned = false;
            }
        }

        if let Some(cid) = consumer_id {
            if charge_blind.contains(cid) {
                return self.steer_to_zero(Some(cid), reports);
            }
        }

        if any_fading {
            if let Some(cid) = consumer_id {
                let (fade_w, reported, total_fade, total_battery) = {
                    let s = self.state.lock();
                    let fade_w = s.consumers.get(cid).map(|c| c.fade_weight).unwrap_or(1.0);
                    let reported = report_power(reports, cid);
                    let mut total_fade = 0.0;
                    let mut total_battery = 0;
                    for rid in reports.keys() {
                        total_fade += s.consumers.get(rid).map(|c| c.fade_weight).unwrap_or(1.0);
                        total_battery += report_power(reports, rid);
                    }
                    (fade_w, reported, total_fade, total_battery)
                };
                if fade_w == 0.0 {
                    return self.steer_to_zero(Some(cid), reports);
                }
                let demand = total_battery as f64 + grid_total;
                let desired = if total_fade > 0.0 {
                    demand * fade_w / total_fade
                } else {
                    0.0
                };
                let target = desired - reported as f64;
                {
                    let mut s = self.state.lock();
                    s.consumers.entry(cid.to_string()).or_default().last_target = Some(target);
                }
                return Self::split_by_phase(target, reports, Some(&eff_part));
            }
        }

        // Non-fading path.
        for (cid, fade_w) in &faded_adjustments {
            if eff_part.contains_key(cid) && *fade_w == 0.0 {
                eff_part.insert(cid.clone(), 0.0);
            }
        }
        if !faded_adjustments.is_empty() {
            if let Some(cid) = consumer_id {
                if faded_adjustments.get(cid) == Some(&0.0) {
                    return self.steer_to_zero(Some(cid), reports);
                }
            }
        }

        let total_effective: f64 = eff_part.values().sum();
        let fair_share = match consumer_id {
            Some(cid) if reports.contains_key(cid) && total_effective > 0.0 => {
                (grid_total / total_effective) * eff_part.get(cid).copied().unwrap_or(1.0)
            }
            _ => grid_total / num_consumers as f64,
        };

        let mut target = match consumer_id {
            Some(cid) if self.cfg.fair_distribution && reports.contains_key(cid) => {
                if eff_part.contains_key(cid) {
                    self.balance_correction(cid, reports, &eff_part, fair_share)
                } else {
                    fair_share
                }
            }
            _ => fair_share,
        };

        if (grid_total < 0.0 && target > 0.0) || (grid_total > 0.0 && target < 0.0) {
            target = 0.0;
        }

        if let Some(cid) = consumer_id {
            let mut s = self.state.lock();
            s.consumers.entry(cid.to_string()).or_default().last_target = Some(target);
        }
        Self::split_by_phase(target, reports, Some(&eff_part))
    }

    fn balance_correction(
        &self,
        consumer_id: &str,
        reports: &Reports,
        eff_part: &HashMap<String, f64>,
        fair_share: f64,
    ) -> f64 {
        let cfg = &self.cfg;
        let actual_self = report_power(reports, consumer_id);
        let participating: Vec<&String> = reports
            .keys()
            .filter(|cid| eff_part.get(*cid).copied().unwrap_or(1.0) > 0.1)
            .collect();
        if participating.is_empty() {
            return fair_share;
        }
        let actual_total: i32 = participating
            .iter()
            .map(|cid| report_power(reports, cid))
            .sum();
        let actual_avg = actual_total as f64 / participating.len() as f64;
        let error = actual_avg - actual_self as f64;
        let err_abs = error.abs();
        if cfg.balance_deadband > 0.0 && err_abs < cfg.balance_deadband {
            return fair_share;
        }
        let mut gain = cfg.balance_gain;
        if cfg.error_reduce_threshold > 0.0 && err_abs < cfg.error_reduce_threshold {
            gain *= err_abs / cfg.error_reduce_threshold;
        } else if cfg.error_boost_threshold > 0.0 && cfg.error_boost_max > 0.0 {
            let boost = (err_abs / cfg.error_boost_threshold).min(1.0) * cfg.error_boost_max;
            gain *= 1.0 + boost;
        }
        let mut correction = gain * error;
        if cfg.max_correction_per_step > 0.0 {
            let cap = cfg.max_correction_per_step;
            correction = correction.clamp(-cap, cap);
        }
        let mut target = fair_share + correction;
        if cfg.max_target_step > 0.0 {
            let lo = actual_self as f64 - cfg.max_target_step;
            let hi = actual_self as f64 + cfg.max_target_step;
            target = target.clamp(lo, hi);
        }
        target
    }

    // ------------------------------------------------------------------
    // Efficiency deprioritization
    // ------------------------------------------------------------------

    fn compute_efficiency_deprioritized(
        &self,
        reports: &Reports,
        sample_id: &[f64],
        grid_total: f64,
    ) -> HashMap<String, f64> {
        let cfg = self.cfg;
        if cfg.min_efficient_power <= 0.0 || reports.len() < 2 {
            let mut s = self.state.lock();
            s.probe_state = None;
            s.deprioritized.clear();
            s.cache_sample = None;
            s.cache_result = None;
            return HashMap::new();
        }
        let now = self.now();
        let current: HashSet<String> = reports.keys().cloned().collect();
        let grace = now
            + self
                .saturation_grace_seconds
                .min(cfg.efficiency_rotation_interval);

        let mut new_ids = Vec::new();
        {
            let mut s = self.state.lock();
            s.priority.retain(|c| current.contains(c));
            s.deprioritized.retain(|c| current.contains(c));
            let mut current_sorted: Vec<String> = current.iter().cloned().collect();
            current_sorted.sort();
            for cid in current_sorted {
                if !s.priority.contains(&cid) {
                    s.priority.push(cid.clone());
                    new_ids.push(cid);
                }
            }
        }
        for cid in &new_ids {
            let mut s = self.state.lock();
            let st = s.consumers.entry(cid.clone()).or_default();
            self.saturation.set_grace(st, grace);
        }

        let (previous_active, priority_snapshot) = {
            let s = self.state.lock();
            let slots = (s.priority.len() - s.deprioritized.len())
                .max(0)
                .min(s.priority.len());
            let pa: Vec<String> = s.priority.iter().take(slots).cloned().collect();
            (pa, s.priority.clone())
        };

        let probe_resolved = self.resolve_probe_state(reports, now, grid_total);
        let probe_active = self.state.lock().probe_state.is_some();

        if !probe_active && !probe_resolved {
            let mut s = self.state.lock();
            if !s.priority.is_empty() && now - s.last_rotation >= cfg.efficiency_rotation_interval {
                s.last_rotation = now;
                let head = s.priority.remove(0);
                s.priority.push(head);
                s.cache_sample = None;
                s.cache_result = None;
            }
        }

        if !probe_active
            && !probe_resolved
            && cfg.efficiency_saturation_threshold > 0.0
            && self.state.lock().cache_sample.is_some()
        {
            let mut s = self.state.lock();
            let slots_est =
                (s.priority.len() as i64 - s.deprioritized.len() as i64).max(0) as usize;
            let head: Vec<String> = s.priority.iter().take(slots_est).cloned().collect();
            for cid in head {
                if let Some(state) = s.consumers.get(&cid) {
                    if state.saturation_score >= cfg.efficiency_saturation_threshold {
                        s.cache_sample = None;
                        s.cache_result = None;
                        break;
                    }
                }
            }
        }

        let cache_key = (sample_id.to_vec(), priority_snapshot.clone());
        {
            let s = self.state.lock();
            if s.cache_sample
                .as_ref()
                .map(|c| c == &cache_key)
                .unwrap_or(false)
            {
                return s.cache_result.clone().unwrap_or_default();
            }
        }

        // Demand estimate.
        let priority_now: Vec<String> = self.state.lock().priority.clone();
        let total_battery_power: i32 = priority_now
            .iter()
            .map(|cid| report_power(reports, cid))
            .sum();
        let abs_target = (total_battery_power as f64 + grid_total).abs();
        let n = priority_now.len();
        let per_consumer = if n == 0 { 0.0 } else { abs_target / n as f64 };

        let was_limiting = !self.state.lock().deprioritized.is_empty();
        let enter_limiting = if was_limiting {
            per_consumer < cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR
        } else {
            per_consumer < cfg.min_efficient_power
        };

        let slots = if enter_limiting && n > 1 {
            ((abs_target / cfg.min_efficient_power) as usize).clamp(1, n.saturating_sub(1))
        } else {
            n
        };

        let mut deprioritized: HashSet<String> = priority_now.iter().skip(slots).cloned().collect();
        let mut result: HashMap<String, f64> =
            deprioritized.iter().map(|c| (c.clone(), 0.0)).collect();
        let pre_swap_active: HashSet<String> = priority_now.iter().take(slots).cloned().collect();

        // Transition active->: clear deprio state on its way back.
        let (old_deprio, prev_active_in_state) = {
            let s = self.state.lock();
            (s.deprioritized.clone(), Vec::<String>::new())
        };
        let _ = prev_active_in_state;
        for cid in old_deprio.difference(&deprioritized) {
            let mut s = self.state.lock();
            if let Some(state) = s.consumers.get_mut(cid) {
                SaturationTracker::clear(state);
                self.saturation.set_grace(state, grace);
            }
        }

        if !probe_active && !probe_resolved {
            let swapped = self.maybe_force_swap_saturated(slots, now);
            if swapped {
                let priority_after: Vec<String> = self.state.lock().priority.clone();
                deprioritized = priority_after.iter().skip(slots).cloned().collect();
                result = deprioritized.iter().map(|c| (c.clone(), 0.0)).collect();
                let new_active: HashSet<String> =
                    priority_after.iter().take(slots).cloned().collect();
                for cid in new_active.difference(&pre_swap_active) {
                    let mut s = self.state.lock();
                    if let Some(st) = s.consumers.get_mut(cid) {
                        SaturationTracker::clear(st);
                        self.saturation.set_grace(st, grace);
                    }
                }
            }
        }

        let final_active: Vec<String> = self
            .state
            .lock()
            .priority
            .iter()
            .take(slots)
            .cloned()
            .collect();
        if !probe_active && !probe_resolved && !previous_active.is_empty() {
            let promoted: Vec<String> = final_active
                .iter()
                .filter(|c| !previous_active.contains(c))
                .cloned()
                .collect();
            let backups: Vec<String> = previous_active
                .iter()
                .filter(|c| !final_active.contains(c))
                .cloned()
                .collect();
            if !promoted.is_empty() && !backups.is_empty() {
                self.begin_probe(
                    &promoted[0],
                    final_active.clone(),
                    backups,
                    previous_active.clone(),
                    now,
                );
            }
        }

        for cid in deprioritized.difference(&old_deprio) {
            let mut s = self.state.lock();
            if let Some(state) = s.consumers.get_mut(cid) {
                SaturationTracker::clear(state);
            }
            tracing::info!(
                "Efficiency: deprioritizing consumer {} (demand {:.0}W, {} active)",
                short(cid),
                abs_target,
                slots
            );
        }
        for cid in old_deprio.difference(&deprioritized) {
            tracing::info!(
                "Efficiency: activating consumer {} (demand {:.0}W, {} active)",
                short(cid),
                abs_target,
                slots
            );
        }

        {
            let mut s = self.state.lock();
            s.deprioritized = deprioritized;
            s.cache_sample = Some((sample_id.to_vec(), self.state_priority_clone()));
            s.cache_result = Some(result.clone());
        }
        result
    }

    fn state_priority_clone(&self) -> Vec<String> {
        // Helper to avoid borrowing the lock twice when assigning cache_sample.
        self.state.lock().priority.clone()
    }

    fn maybe_force_swap_saturated(&self, slots: usize, now: f64) -> bool {
        let cfg = self.cfg;
        let mut s = self.state.lock();
        if cfg.efficiency_saturation_threshold <= 0.0 || slots >= s.priority.len() {
            return false;
        }
        let threshold = cfg.efficiency_saturation_threshold;
        let mut saturated_idx: Option<usize> = None;
        for i in 0..slots {
            let cid = &s.priority[i];
            if let Some(state) = s.consumers.get(cid) {
                if state.saturation_score >= threshold {
                    saturated_idx = Some(i);
                    break;
                }
            }
        }
        let Some(sat_idx) = saturated_idx else {
            return false;
        };
        let mut healthy_idx: Option<usize> = None;
        for i in slots..s.priority.len() {
            let cid = &s.priority[i];
            let is_healthy = match s.consumers.get(cid) {
                None => true,
                Some(state) => state.saturation_score < threshold,
            };
            if is_healthy {
                healthy_idx = Some(i);
                break;
            }
        }
        let Some(h_idx) = healthy_idx else {
            return false;
        };
        let sat_score = s
            .consumers
            .get(&s.priority[sat_idx])
            .map(|st| st.saturation_score)
            .unwrap_or(0.0);
        tracing::info!(
            "Efficiency: {} cannot follow target (sat={:.2}), rotating to {}",
            short(&s.priority[sat_idx]),
            sat_score,
            short(&s.priority[h_idx])
        );
        s.priority.swap(sat_idx, h_idx);
        s.last_rotation = now;
        true
    }

    fn fade_efficiency_weights(
        &self,
        raw_adjustments: &HashMap<String, f64>,
        consumer_ids: &HashSet<String>,
    ) -> HashMap<String, f64> {
        let alpha = self.cfg.efficiency_fade_alpha;
        let mut result = HashMap::new();
        let frozen = self.probe_participants();
        let now = self.now();
        let post_probe_active = now < self.state.lock().post_probe_fade_until;
        for cid in consumer_ids {
            let mut s = self.state.lock();
            let state = s.consumers.entry(cid.clone()).or_default();
            if frozen.contains(cid) {
                state.fade_weight = 1.0;
                continue;
            }
            let goal = raw_adjustments.get(cid).copied().unwrap_or(1.0);
            let prev = state.fade_weight;
            let mut effective_alpha = alpha;
            if post_probe_active && s.post_probe_fade_ids.contains(cid) {
                effective_alpha = alpha.min(0.25);
            }
            let mut new = prev + effective_alpha * (goal - prev);
            if (new - goal).abs() < 0.05 {
                new = goal;
            }
            s.consumers.entry(cid.clone()).or_default().fade_weight = new;
            if new < 1.0 {
                result.insert(cid.clone(), new);
            }
        }
        if !post_probe_active {
            let mut s = self.state.lock();
            s.post_probe_fade_until = 0.0;
            s.post_probe_fade_ids.clear();
        }
        // Drop consumers no longer in pool & not in priority.
        let mut s = self.state.lock();
        let priority: HashSet<String> = s.priority.iter().cloned().collect();
        let to_drop: Vec<String> = s
            .consumers
            .keys()
            .filter(|c| !consumer_ids.contains(*c) && !priority.contains(*c))
            .cloned()
            .collect();
        for c in to_drop {
            s.consumers.remove(&c);
        }
        result
    }

    // ------------------------------------------------------------------
    // Probe
    // ------------------------------------------------------------------

    fn probe_participants(&self) -> HashSet<String> {
        let s = self.state.lock();
        match &s.probe_state {
            Some(p) => p
                .active_ids
                .iter()
                .chain(p.backup_ids.iter())
                .cloned()
                .collect(),
            None => HashSet::new(),
        }
    }

    fn begin_probe(
        &self,
        candidate_id: &str,
        active_ids: Vec<String>,
        backup_ids: Vec<String>,
        restore_active_ids: Vec<String>,
        now: f64,
    ) {
        let deadline = now + self.probe_timeout_seconds;
        {
            let mut s = self.state.lock();
            let participants: HashSet<String> = active_ids
                .iter()
                .chain(backup_ids.iter())
                .cloned()
                .collect();
            for cid in &participants {
                s.consumers.entry(cid.clone()).or_default().fade_weight = 1.0;
            }
            s.post_probe_fade_until = 0.0;
            s.post_probe_fade_ids.clear();
            if let Some(state) = s.consumers.get_mut(candidate_id) {
                SaturationTracker::clear(state);
            }
            let cand_state = s.consumers.entry(candidate_id.to_string()).or_default();
            self.saturation.set_grace(cand_state, deadline);
            s.probe_state = Some(ProbeState {
                candidate_id: candidate_id.to_string(),
                active_ids: active_ids.clone(),
                backup_ids: backup_ids.clone(),
                restore_active_ids,
                deadline,
                started_at: now,
                proof_samples: 0,
                requested_power_abs: 0.0,
            });
            s.cache_sample = None;
            s.cache_result = None;
        }
        tracing::info!(
            "Efficiency: probing consumer {} with backups {:?} until {:.1}s",
            short(candidate_id),
            short_ids(&backup_ids),
            self.probe_timeout_seconds
        );
    }

    fn commit_probe(&self, reports: &Reports, now: f64, actual: i32) {
        let mut s = self.state.lock();
        let probe = match s.probe_state.take() {
            Some(p) => p,
            None => return,
        };
        let participants: Vec<String> = probe
            .active_ids
            .iter()
            .chain(probe.backup_ids.iter())
            .filter(|c| reports.contains_key(*c))
            .cloned()
            .collect();
        let total_actual: i32 = participants
            .iter()
            .map(|cid| report_power(reports, cid).unsigned_abs() as i32)
            .sum();
        if total_actual > 0 {
            for cid in &participants {
                let share = report_power(reports, cid).unsigned_abs() as f64;
                s.consumers.entry(cid.clone()).or_default().fade_weight =
                    share / total_actual as f64;
            }
        } else {
            let active_count = probe.active_ids.len().max(1) as f64;
            for cid in &probe.active_ids {
                s.consumers.entry(cid.clone()).or_default().fade_weight = 1.0 / active_count;
            }
            for cid in &probe.backup_ids {
                s.consumers.entry(cid.clone()).or_default().fade_weight = 0.0;
            }
        }
        s.post_probe_fade_until = now + 5.0_f64.min(self.probe_timeout_seconds);
        s.post_probe_fade_ids = participants.iter().cloned().collect();
        if let Some(state) = s.consumers.get_mut(&probe.candidate_id) {
            state.saturation_grace_until = 0.0;
            state.saturation_grace_started_at = 0.0;
        }
        s.last_rotation = now;
        s.cache_sample = None;
        s.cache_result = None;
        let cid_short = short(&probe.candidate_id);
        drop(s);
        tracing::info!(
            "Efficiency: probe succeeded for {} at {:.0}W",
            cid_short,
            actual
        );
        if let Some(reset) = &self.reset_fn {
            (reset)();
        }
    }

    fn reject_probe(&self, now: f64, reason: &str) {
        let _ = now;
        let mut s = self.state.lock();
        let probe = match s.probe_state.take() {
            Some(p) => p,
            None => return,
        };
        if let Some(state) = s.consumers.get_mut(&probe.candidate_id) {
            state.saturation_score = state.saturation_score.max(1.0);
            state.fade_weight = 0.0;
        }
        for cid in &probe.restore_active_ids {
            s.consumers.entry(cid.clone()).or_default().fade_weight = 1.0;
        }
        if let Some(state) = s.consumers.get_mut(&probe.candidate_id) {
            state.saturation_grace_until = 0.0;
            state.saturation_grace_started_at = 0.0;
        }
        s.post_probe_fade_until = 0.0;
        s.post_probe_fade_ids.clear();
        let restore_set: HashSet<String> = probe.restore_active_ids.iter().cloned().collect();
        let remaining: Vec<String> = s
            .priority
            .iter()
            .filter(|cid| !restore_set.contains(*cid) && **cid != probe.candidate_id)
            .cloned()
            .collect();
        let mut new_priority = probe.restore_active_ids.clone();
        new_priority.extend(remaining);
        new_priority.push(probe.candidate_id.clone());
        s.priority = new_priority;
        s.cache_sample = None;
        s.cache_result = None;
        let cid_short = short(&probe.candidate_id);
        let backups_short: Vec<String> = probe.backup_ids.iter().map(|c| short(c)).collect();
        drop(s);
        tracing::info!(
            "Efficiency: probe rejected for {} ({}), restoring backups {:?}",
            cid_short,
            reason,
            backups_short
        );
        if let Some(reset) = &self.reset_fn {
            (reset)();
        }
    }

    fn resolve_probe_state(&self, reports: &Reports, now: f64, grid_total: f64) -> bool {
        let probe_clone = self.state.lock().probe_state.clone();
        let Some(mut probe) = probe_clone else {
            return false;
        };
        let participants: HashSet<String> = probe
            .active_ids
            .iter()
            .chain(probe.backup_ids.iter())
            .cloned()
            .collect();
        let missing: Vec<String> = participants
            .iter()
            .filter(|c| !reports.contains_key(*c))
            .cloned()
            .collect();
        if !missing.is_empty() {
            let mut s = self.state.lock();
            tracing::info!(
                "Efficiency: ending probe (participants disappeared: {:?})",
                missing.iter().map(|c| short(c)).collect::<Vec<_>>()
            );
            s.probe_state = None;
            s.cache_sample = None;
            s.cache_result = None;
            return true;
        }
        let actual = report_power(reports, &probe.candidate_id);
        let desired_total: f64 = reports.values().map(|r| r.power as f64).sum::<f64>() + grid_total;
        let probe_success_threshold = self.probe_success_threshold;
        let demand_sign = signum_i(desired_total);
        let actual_sign = signum_i(actual as f64);
        if demand_sign != 0
            && actual_sign == demand_sign
            && (actual.unsigned_abs() as f64) >= probe_success_threshold
        {
            probe.proof_samples += 1;
        } else {
            probe.proof_samples = 0;
        }
        self.state.lock().probe_state = Some(probe.clone());
        if probe.proof_samples >= 2 {
            self.commit_probe(reports, now, actual);
            return true;
        }
        if now >= probe.deadline {
            self.reject_probe(now, "timeout before meaningful output");
            return true;
        }
        false
    }

    fn compute_probe_target(
        &self,
        consumer_id: Option<&str>,
        reports: &Reports,
        grid_total: f64,
        eff_part: &HashMap<String, f64>,
    ) -> Option<[f64; 3]> {
        let probe = self.state.lock().probe_state.clone()?;
        let consumer_id = consumer_id?;
        let candidate_id = probe.candidate_id.clone();
        if !reports.contains_key(&candidate_id) {
            return None;
        }
        let support_reports: Reports = probe
            .backup_ids
            .iter()
            .chain(probe.active_ids.iter().filter(|c| **c != candidate_id))
            .filter_map(|c| reports.get(c).map(|r| (c.clone(), r.clone())))
            .collect();
        if consumer_id != candidate_id && !support_reports.contains_key(consumer_id) {
            return None;
        }
        let desired_total: f64 = reports.values().map(|r| r.power as f64).sum::<f64>() + grid_total;
        let probe_actual = report_power(reports, &candidate_id);
        let probe_ceiling = desired_total.abs().max(self.cfg.probe_min_power);
        let cand_id_owned = candidate_id.clone();

        if consumer_id == candidate_id {
            let next_requested_abs =
                self.next_probe_requested_abs(probe.requested_power_abs, probe_ceiling);
            let mut desired_probe = 0.0;
            if desired_total > 0.0 {
                desired_probe = (probe_actual.unsigned_abs() as f64).max(next_requested_abs);
            } else if desired_total < 0.0 {
                desired_probe = -(probe_actual.unsigned_abs() as f64).max(next_requested_abs);
            } else if probe.requested_power_abs > 0.0 {
                desired_probe = (probe.requested_power_abs - self.probe_success_threshold).max(0.0);
            }
            if desired_total < 0.0 && desired_probe > 0.0 {
                desired_probe = -desired_probe;
            }
            {
                let mut s = self.state.lock();
                if let Some(p) = s.probe_state.as_mut() {
                    p.requested_power_abs = desired_probe.abs();
                }
            }
            let target = desired_probe - probe_actual as f64;
            {
                let mut s = self.state.lock();
                s.consumers
                    .entry(consumer_id.to_string())
                    .or_default()
                    .last_target = Some(target);
            }
            let one_report: Reports = std::iter::once((
                cand_id_owned.clone(),
                reports.get(&cand_id_owned).cloned().unwrap_or_default(),
            ))
            .collect();
            return Some(Self::split_by_phase(target, &one_report, None));
        }

        let backup_weights: HashMap<String, f64> = support_reports
            .keys()
            .map(|cid| {
                (
                    cid.clone(),
                    eff_part.get(cid).copied().unwrap_or(1.0).max(0.01),
                )
            })
            .collect();
        let qualified_probe_actual = if probe.proof_samples > 0 {
            probe_actual
        } else {
            0
        };
        let desired = self.compute_desired_contribution(
            consumer_id,
            &support_reports,
            &backup_weights,
            desired_total - qualified_probe_actual as f64,
        );
        let reported = report_power(&support_reports, consumer_id);
        let target = desired - reported as f64;
        {
            let mut s = self.state.lock();
            s.consumers
                .entry(consumer_id.to_string())
                .or_default()
                .last_target = Some(target);
        }
        Some(Self::split_by_phase(
            target,
            &support_reports,
            Some(&backup_weights),
        ))
    }

    fn next_probe_requested_abs(&self, current_requested_abs: f64, ceiling: f64) -> f64 {
        let ceiling = ceiling.max(0.0);
        let base_step = (self.probe_success_threshold * 0.25).max(1.0);
        if current_requested_abs <= 0.0 {
            ceiling.min(base_step)
        } else {
            ceiling.min((current_requested_abs + base_step).max(current_requested_abs * 1.35))
        }
    }

    fn compute_desired_contribution(
        &self,
        consumer_id: &str,
        reports: &Reports,
        weights: &HashMap<String, f64>,
        desired_total: f64,
    ) -> f64 {
        let total_weight: f64 = reports
            .keys()
            .map(|cid| weights.get(cid).copied().unwrap_or(0.0))
            .sum();
        let fair_share = if total_weight > 0.0 {
            desired_total * weights.get(consumer_id).copied().unwrap_or(0.0) / total_weight
        } else {
            desired_total / reports.len().max(1) as f64
        };
        if !self.cfg.fair_distribution
            || !reports.contains_key(consumer_id)
            || (self.cfg.balance_deadband > 0.0 && desired_total.abs() < self.cfg.balance_deadband)
        {
            return fair_share;
        }
        self.balance_correction(consumer_id, reports, weights, fair_share)
    }

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    fn steer_to_zero(&self, consumer_id: Option<&str>, reports: &Reports) -> [f64; 3] {
        if let Some(cid) = consumer_id {
            let mut s = self.state.lock();
            s.consumers.entry(cid.to_string()).or_default().last_target = Some(0.0);
        }
        let reported = match consumer_id {
            Some(cid) => report_power(reports, cid),
            None => 0,
        };
        if reported == 0 {
            return [0.0, 0.0, 0.0];
        }
        let phase = match consumer_id {
            Some(cid) => report_phase(reports, cid),
            None => 'A',
        };
        let mut out = [0.0_f64, 0.0, 0.0];
        let idx = match phase {
            'A' => 0,
            'B' => 1,
            'C' => 2,
            _ => 0,
        };
        out[idx] = -(reported as f64);
        out
    }

    fn split_by_phase(
        target: f64,
        reports: &Reports,
        weights: Option<&HashMap<String, f64>>,
    ) -> [f64; 3] {
        let mut phase_effective = [0.0_f64; 3];
        for (cid, report) in reports {
            let phase = match report.phase.to_ascii_uppercase() {
                'A' => 0,
                'B' => 1,
                'C' => 2,
                _ => 0,
            };
            let w = weights.and_then(|w| w.get(cid).copied()).unwrap_or(1.0);
            phase_effective[phase] += w;
        }
        let total: f64 = phase_effective.iter().sum();
        if total <= 0.0 {
            return [target, 0.0, 0.0];
        }
        [
            target * phase_effective[0] / total,
            target * phase_effective[1] / total,
            target * phase_effective[2] / total,
        ]
    }
}

fn short(cid: &str) -> String {
    cid.chars().take(16).collect()
}

fn short_ids(ids: &[String]) -> Vec<String> {
    ids.iter().map(|c| short(c)).collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> BalancerConfig {
        BalancerConfig::default()
    }

    fn reports_of(items: &[(&str, i32, char, &str)]) -> Reports {
        items
            .iter()
            .map(|(id, p, ph, dt)| {
                (
                    (*id).to_string(),
                    ConsumerReport {
                        power: *p,
                        phase: *ph,
                        device_type: dt.to_string(),
                    },
                )
            })
            .collect()
    }

    fn make_lb(c: BalancerConfig) -> LoadBalancer {
        LoadBalancer::new(c, 0.2, 10.0, 0.9, 90.0, 60.0, true, None, None)
    }

    #[test]
    fn ac_chargeable_prefixes() {
        assert!(is_ac_chargeable("HMG-1"));
        assert!(is_ac_chargeable("VNSE3"));
        assert!(!is_ac_chargeable("B2500"));
        assert!(!is_ac_chargeable(""));
    }

    #[test]
    fn fair_share_two_ac_batteries() {
        let lb = make_lb(cfg());
        let reports = reports_of(&[("a", 0, 'A', "HMG-1"), ("b", 0, 'A', "HMG-2")]);
        let inactive = HashSet::new();
        let manual = HashSet::new();
        let target = lb.compute_target(
            Some("a"),
            ConsumerMode::Auto,
            &reports,
            200.0,
            &inactive,
            &manual,
            vec![1.0],
        );
        let sum: f64 = target.iter().sum();
        // Each consumer gets half of 200W under fair share.
        assert!((sum - 100.0).abs() < 1.0, "got {sum:?}");
    }

    #[test]
    fn inactive_consumer_steers_to_zero() {
        let lb = make_lb(cfg());
        let reports = reports_of(&[("a", 50, 'A', "HMG-1")]);
        let inactive = HashSet::new();
        let manual = HashSet::new();
        let out = lb.compute_target(
            Some("a"),
            ConsumerMode::Inactive,
            &reports,
            100.0,
            &inactive,
            &manual,
            vec![],
        );
        // reported=50 -> steer-to-zero returns -50 on phase A.
        assert_eq!(out, [-50.0, 0.0, 0.0]);
    }

    #[test]
    fn manual_override() {
        let lb = make_lb(cfg());
        let reports = reports_of(&[("a", 30, 'A', "HMG-1")]);
        let inactive = HashSet::new();
        let manual: HashSet<String> = std::iter::once("a".to_string()).collect();
        let out = lb.compute_target(
            Some("a"),
            ConsumerMode::Manual(200.0),
            &reports,
            500.0,
            &inactive,
            &manual,
            vec![],
        );
        // manual target = 200 - 30 reported = 170; goes to phase A.
        assert!((out[0] - 170.0).abs() < 0.01);
    }

    #[test]
    fn dc_battery_under_surplus_steered_to_zero() {
        let lb = make_lb(cfg());
        // grid_total < 0 (surplus) and reports include one AC + one DC.
        let reports = reports_of(&[("ac", 0, 'A', "HMG-1"), ("dc", 100, 'A', "B2500")]);
        let out = lb.compute_target(
            Some("dc"),
            ConsumerMode::Auto,
            &reports,
            -200.0,
            &HashSet::new(),
            &HashSet::new(),
            vec![-200.0],
        );
        // DC under surplus is steer-to-zero.
        assert_eq!(out, [-100.0, 0.0, 0.0]);
    }

    #[test]
    fn saturation_score_increments_when_actual_lags() {
        let mut state = BalancerConsumerState::default();
        let clock_now = std::cell::Cell::new(1000.0_f64);
        let clock: ClockFn = {
            let cn = std::sync::Arc::new(std::sync::Mutex::new(1000.0_f64));
            let cn2 = cn.clone();
            Arc::new(move || *cn2.lock().unwrap())
        };
        let _ = clock_now;
        let tracker = SaturationTracker::new(0.5, 10.0, 0.9, 60.0, true, clock);
        tracker.update(&mut state, Some(500.0), 0); // huge target, zero actual
        assert!(state.saturation_score > 0.0);
    }
}
