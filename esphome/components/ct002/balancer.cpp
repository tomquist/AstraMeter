#include "balancer.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>

namespace esphome {
namespace ct002 {

namespace {

std::string to_upper(const std::string &s) {
  std::string up;
  up.reserve(s.size());
  for (char c : s) up.push_back(static_cast<char>(std::toupper(c)));
  return up;
}

bool starts_with(const std::string &s, const char *prefix) {
  const size_t plen = std::strlen(prefix);
  return s.size() >= plen && s.compare(0, plen, prefix) == 0;
}

// Clamp a report's efficiency-rotation window weight to [0, 1] (mirrors
// Python's _efficiency_window_weight). Missing reports map to neutral 1.0.
float efficiency_window_weight_of(const ReportMap &reports, const std::string &cid) {
  auto it = reports.find(cid);
  if (it == reports.end()) return 1.0f;
  return std::max(0.0f, std::min(1.0f, it->second.efficiency_window_weight));
}

template <typename Set>
Set set_difference(const Set &a, const Set &b) {
  Set out;
  for (const auto &x : a) {
    if (b.find(x) == b.end()) {
      out.insert(x);
    }
  }
  return out;
}

template <typename Set>
Set set_union(const Set &a, const Set &b) {
  Set out(a);
  for (const auto &x : b) out.insert(x);
  return out;
}

std::string serialize_cache_key(const std::vector<float> &sample_id,
                                const std::vector<std::string> &priority) {
  std::string key;
  key.reserve(sample_id.size() * 8 + priority.size() * 16);
  for (float v : sample_id) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.6g|", v);
    key += buf;
  }
  key += "::";
  for (const auto &p : priority) {
    key += p;
    key.push_back('|');
  }
  return key;
}

}  // namespace

DeviceCapabilities device_capabilities(const std::string &device_type) {
  const std::string dt = to_upper(device_type);
  // Venus A/D: built-in inverter + AC input + extra DC input. Checked before
  // the generic VNS branch ("VNSA".startswith("VNS")).
  if (starts_with(dt, "VNSA") || starts_with(dt, "VNSD")) return {true, true, true};
  // Other Venus (HMG*, VNSE3, ...): built-in inverter + AC input, no DC input.
  if (starts_with(dt, "HMG") || starts_with(dt, "VNS")) return {true, true, false};
  // Jupiter: DC battery with a built-in inverter.
  if (starts_with(dt, "HMN") || starts_with(dt, "HMM") || starts_with(dt, "JPLS"))
    return {true, false, true};
  // B2500 family: DC input feeding a SEPARATE inverter (no built-in inverter).
  if (starts_with(dt, "HMA") || starts_with(dt, "HMJ") || starts_with(dt, "HMK"))
    return {false, false, true};
  // Unknown / future / empty: assume a modern AC-coupled battery.
  return {true, true, false};
}

bool is_ac_chargeable(const std::string &device_type) {
  return device_capabilities(device_type).has_ac_input;
}

bool needs_dc_output_floor(const std::string &device_type) {
  const DeviceCapabilities caps = device_capabilities(device_type);
  return !caps.has_ac_input && !caps.has_builtin_inverter;
}

void BalancerConfig::clamp() {
  auto clamp_v = [](float &v, float lo, float hi) {
    v = std::max(lo, std::min(hi, v));
  };
  clamp_v(balance_gain, 0.0f, 1.0f);
  if (balance_deadband < 0.0f) balance_deadband = 0.0f;
  if (error_boost_threshold < 0.0f) error_boost_threshold = 0.0f;
  if (error_boost_max < 0.0f) error_boost_max = 0.0f;
  if (error_reduce_threshold < 0.0f) error_reduce_threshold = 0.0f;
  if (max_correction_per_step < 0.0f) max_correction_per_step = 0.0f;
  if (max_target_step < 0.0f) max_target_step = 0.0f;
  if (min_efficient_power < 0.0f) min_efficient_power = 0.0f;
  if (probe_min_power < 0.0f) probe_min_power = 0.0f;
  if (efficiency_rotation_interval < 1.0f) efficiency_rotation_interval = 1.0f;
  clamp_v(efficiency_fade_alpha, 0.01f, 1.0f);
  efficiency_saturation_threshold =
      std::max(0.0, std::min(1.0, efficiency_saturation_threshold));
  clamp_v(efficiency_demand_alpha, 0.01f, 1.0f);
  if (min_dc_output < 0.0f) min_dc_output = 0.0f;
  if (pace_base_step < 0.0f) pace_base_step = 0.0f;
  if (pace_max_step < pace_base_step) pace_max_step = pace_base_step;
  clamp_v(osc_damp_max, 0.0f, 1.0f);
  clamp_v(osc_damp_alpha, 0.0f, 1.0f);
  clamp_v(osc_damp_decay, 0.0f, 1.0f);
  if (osc_damp_threshold < 0.0f) osc_damp_threshold = 0.0f;
  clamp_v(grid_predict_trust, 0.0f, 1.0f);
  if (concentrate_deadband < 0.0f) concentrate_deadband = 0.0f;
  if (import_trim_w < 0.0f) import_trim_w = 0.0f;
}

// -------------------------------------------------------------------------
// SaturationTracker
// -------------------------------------------------------------------------

SaturationTracker::SaturationTracker(double alpha, float min_target, double decay_factor,
                                     float stall_timeout_seconds, bool enabled,
                                     std::function<double()> clock)
    : clock_(std::move(clock)),
      enabled_(enabled),
      alpha_(std::max(0.01, std::min(1.0, alpha))),
      min_target_(std::max(1.0f, min_target)),
      decay_factor_(std::max(0.0, std::min(1.0, decay_factor))),
      stall_timeout_seconds_(std::max(0.0f, stall_timeout_seconds)) {}

void SaturationTracker::update(BalancerConsumerState &state,
                               std::optional<float> last_target, float actual) {
  if (!this->enabled_ || !last_target.has_value()) return;
  const double now = this->clock_();
  const double target_abs = std::fabs(*last_target);

  if (state.saturation_grace_until > 0.0) {
    if (now < state.saturation_grace_until) {
      if (std::fabs(actual) >= this->min_target_) {
        state.saturation_grace_until = 0.0;
        state.saturation_grace_started_at = 0.0;
        state.last_saturation_update = 0.0;
      } else if (target_abs >= this->min_target_ &&
                 state.saturation_grace_started_at > 0.0 &&
                 (now - state.saturation_grace_started_at) >= this->stall_timeout_seconds_) {
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

  const int target_sign = (*last_target > 0.0f) ? 1 : (*last_target < 0.0f ? -1 : 0);
  const int actual_sign = (actual > 0.0f) ? 1 : (actual < 0.0f ? -1 : 0);
  const bool sign_reversing =
      (target_sign != 0 && actual_sign != 0 && target_sign != actual_sign);

  double prev_t = state.last_saturation_update;
  if (prev_t <= 0.0) prev_t = now - SATURATION_REFERENCE_DT;
  double dt = std::max(0.0, now - prev_t);
  state.last_saturation_update = now;
  if (dt == 0.0) return;
  if (dt > SATURATION_LONG_GAP_SECONDS) return;

  const double ratio = dt / SATURATION_REFERENCE_DT;
  if (target_abs < this->min_target_ || sign_reversing) {
    const double prev = state.saturation_score;
    if (prev > 0.0) {
      const double decayed = prev * std::pow(this->decay_factor_, ratio);
      state.saturation_score = (decayed < 0.001) ? 0.0 : decayed;
    }
    return;
  }
  const double inst_saturation = (std::fabs(actual) < this->min_target_) ? 1.0 : 0.0;
  const double alpha_eff = 1.0 - std::pow(1.0 - this->alpha_, ratio);
  const double prev = state.saturation_score;
  state.saturation_score = alpha_eff * inst_saturation + (1.0 - alpha_eff) * prev;
}

void SaturationTracker::set_grace(BalancerConsumerState &state, double deadline) {
  state.saturation_grace_until = deadline;
  state.saturation_grace_started_at = this->clock_();
  state.last_saturation_update = 0.0;
}

void SaturationTracker::clear(BalancerConsumerState &state) {
  state.saturation_score = 0.0;
  state.saturation_grace_until = 0.0;
  state.saturation_grace_started_at = 0.0;
  state.last_saturation_update = 0.0;
}

// -------------------------------------------------------------------------
// LoadBalancer
// -------------------------------------------------------------------------

LoadBalancer::LoadBalancer(BalancerConfig config, double saturation_alpha,
                           float saturation_min_target, double saturation_decay_factor,
                           float saturation_grace_seconds,
                           float saturation_stall_timeout_seconds,
                           bool saturation_enabled, std::function<double()> clock,
                           std::function<void()> reset_fn)
    : clock_(std::move(clock)),
      cfg_(config),
      saturation_(saturation_alpha, saturation_min_target, saturation_decay_factor,
                  saturation_stall_timeout_seconds, saturation_enabled,
                  [this]() { return this->clock_(); }),
      saturation_grace_seconds_(std::max(0.0f, saturation_grace_seconds)),
      reset_fn_(std::move(reset_fn)),
      last_rotation_(this->clock_()),
      probe_timeout_seconds_(std::max(0.0f, saturation_grace_seconds)),
      probe_success_threshold_(std::max(1.0f, saturation_min_target)) {
  this->cfg_.clamp();
}

BalancerConsumerState &LoadBalancer::get_consumer_(const std::string &consumer_id) {
  return this->consumers_[consumer_id];
}

void LoadBalancer::invalidate_efficiency_cache_() {
  this->cache_sample_.reset();
  this->cache_result_.clear();
}

std::unordered_set<std::string> LoadBalancer::probe_participants_() const {
  std::unordered_set<std::string> out;
  if (!this->probe_state_) return out;
  for (const auto &c : this->probe_state_->active_ids) out.insert(c);
  for (const auto &c : this->probe_state_->backup_ids) out.insert(c);
  return out;
}

float LoadBalancer::effective_probe_min_power_() const {
  return std::max(this->probe_success_threshold_, this->cfg_.probe_min_power);
}

float LoadBalancer::next_probe_requested_abs_(float current_requested_abs,
                                              float ceiling) const {
  ceiling = std::max(0.0f, ceiling);
  const float base_step = std::max(1.0f, this->probe_success_threshold_ * 0.25f);
  if (current_requested_abs <= 0.0f) return std::min(ceiling, base_step);
  return std::min(ceiling,
                  std::max(current_requested_abs + base_step, current_requested_abs * 1.35f));
}

void LoadBalancer::clear_probe_state_(const std::string &) {
  if (!this->probe_state_) return;
  this->probe_state_.reset();
  this->invalidate_efficiency_cache_();
}

void LoadBalancer::clear_post_probe_fade_() {
  this->post_probe_fade_until_ = 0.0;
  this->post_probe_fade_ids_.clear();
}

void LoadBalancer::set_consumer_grace_(const std::string &consumer_id, double deadline) {
  this->saturation_.set_grace(this->get_consumer_(consumer_id), deadline);
}

void LoadBalancer::clear_consumer_grace_(const std::string &consumer_id) {
  auto &state = this->get_consumer_(consumer_id);
  state.saturation_grace_until = 0.0;
  state.saturation_grace_started_at = 0.0;
}

void LoadBalancer::begin_probe_(const std::string &candidate_id,
                                std::vector<std::string> active_ids,
                                std::vector<std::string> backup_ids,
                                std::vector<std::string> restore_active_ids, double now) {
  const double deadline = now + this->probe_timeout_seconds_;
  ProbeState p;
  p.candidate_id = candidate_id;
  p.active_ids = active_ids;
  p.backup_ids = backup_ids;
  p.restore_active_ids = std::move(restore_active_ids);
  p.deadline = deadline;
  p.started_at = now;
  this->probe_state_ = std::move(p);
  std::unordered_set<std::string> all;
  for (auto &c : active_ids) all.insert(c);
  for (auto &c : backup_ids) all.insert(c);
  for (const auto &cid : all) this->get_consumer_(cid).fade_weight = 1.0f;
  this->clear_post_probe_fade_();
  this->saturation_.clear(this->get_consumer_(candidate_id));
  this->set_consumer_grace_(candidate_id, deadline);
  this->invalidate_efficiency_cache_();
}

void LoadBalancer::commit_probe_(const ReportMap &reports, double now, float actual) {
  if (!this->probe_state_) return;
  ProbeState probe = *this->probe_state_;
  std::vector<std::string> participants;
  for (const auto &c : probe.active_ids) {
    if (reports.find(c) != reports.end()) participants.push_back(c);
  }
  for (const auto &c : probe.backup_ids) {
    if (reports.find(c) != reports.end()) participants.push_back(c);
  }
  double total_actual = 0.0;
  for (const auto &cid : participants) {
    auto it = reports.find(cid);
    if (it != reports.end()) total_actual += std::fabs(it->second.power);
  }
  if (total_actual > 0.0) {
    for (const auto &cid : participants) {
      auto it = reports.find(cid);
      const double share = it != reports.end() ? std::fabs(it->second.power) : 0.0;
      this->get_consumer_(cid).fade_weight = share / total_actual;
    }
  } else {
    const size_t active_count = std::max<size_t>(1, probe.active_ids.size());
    for (const auto &cid : probe.active_ids)
      this->get_consumer_(cid).fade_weight = 1.0 / active_count;
    for (const auto &cid : probe.backup_ids) this->get_consumer_(cid).fade_weight = 0.0f;
  }
  this->post_probe_fade_until_ = now + std::min(5.0f, this->probe_timeout_seconds_);
  this->post_probe_fade_ids_.clear();
  for (auto &p : participants) this->post_probe_fade_ids_.insert(p);
  this->clear_consumer_grace_(probe.candidate_id);
  this->probe_state_.reset();
  this->last_rotation_ = now;
  this->invalidate_efficiency_cache_();
  if (this->reset_fn_) this->reset_fn_();
  (void)actual;
}

void LoadBalancer::reject_probe_(double now, const std::string &) {
  if (!this->probe_state_) return;
  ProbeState probe = *this->probe_state_;
  auto &candidate_state = this->get_consumer_(probe.candidate_id);
  candidate_state.saturation_score = std::max(candidate_state.saturation_score, 1.0);
  candidate_state.fade_weight = 0.0f;
  for (const auto &cid : probe.restore_active_ids)
    this->get_consumer_(cid).fade_weight = 1.0f;
  this->clear_consumer_grace_(probe.candidate_id);
  this->clear_post_probe_fade_();
  std::unordered_set<std::string> restore_set(probe.restore_active_ids.begin(),
                                              probe.restore_active_ids.end());
  std::vector<std::string> remaining;
  for (const auto &cid : this->priority_) {
    if (restore_set.find(cid) == restore_set.end() && cid != probe.candidate_id) {
      remaining.push_back(cid);
    }
  }
  this->priority_.clear();
  for (const auto &cid : probe.restore_active_ids) this->priority_.push_back(cid);
  for (const auto &cid : remaining) this->priority_.push_back(cid);
  this->priority_.push_back(probe.candidate_id);
  this->probe_state_.reset();
  this->invalidate_efficiency_cache_();
  (void)now;
  if (this->reset_fn_) this->reset_fn_();
}

bool LoadBalancer::resolve_probe_state_(const ReportMap &reports, double now,
                                        float grid_total) {
  if (!this->probe_state_) return false;
  auto participants = this->probe_participants_();
  std::vector<std::string> missing;
  for (const auto &cid : participants) {
    if (reports.find(cid) == reports.end()) missing.push_back(cid);
  }
  if (!missing.empty()) {
    this->clear_probe_state_("participants disappeared");
    return true;
  }
  ProbeState &probe = *this->probe_state_;
  auto it = reports.find(probe.candidate_id);
  const float actual = (it != reports.end()) ? it->second.power : 0.0f;
  float desired_total = grid_total;
  for (const auto &r : reports) desired_total += r.second.power;
  const float probe_success_threshold = this->probe_success_threshold_;
  const int demand_sign =
      (desired_total > 0.0f) ? 1 : (desired_total < 0.0f ? -1 : 0);
  const int actual_sign = (actual > 0.0f) ? 1 : (actual < 0.0f ? -1 : 0);
  if (demand_sign != 0 && actual_sign == demand_sign &&
      std::fabs(actual) >= probe_success_threshold) {
    probe.proof_samples += 1;
  } else {
    probe.proof_samples = 0;
  }
  if (probe.proof_samples >= 2) {
    this->commit_probe_(reports, now, actual);
    return true;
  }
  if (now >= probe.deadline) {
    this->reject_probe_(now, "timeout before meaningful output");
    return true;
  }
  return false;
}

float LoadBalancer::compute_desired_contribution_(
    const std::string &consumer_id, const ReportMap &reports,
    const std::unordered_map<std::string, float> &weights, float desired_total) {
  float total_weight = 0.0f;
  for (const auto &r : reports) {
    auto it = weights.find(r.first);
    if (it != weights.end()) total_weight += it->second;
  }
  float fair_share;
  if (total_weight > 0.0f) {
    auto it = weights.find(consumer_id);
    const float w = (it != weights.end()) ? it->second : 0.0f;
    fair_share = desired_total * w / total_weight;
  } else {
    fair_share = desired_total / std::max<size_t>(1, reports.size());
  }
  const bool not_in_reports = reports.find(consumer_id) == reports.end();
  if (!this->cfg_.fair_distribution || not_in_reports ||
      (this->cfg_.balance_deadband > 0.0f &&
       std::fabs(desired_total) < this->cfg_.balance_deadband)) {
    return fair_share;
  }
  return this->balance_correction_(consumer_id, reports, weights, fair_share);
}

std::optional<std::array<float, 3>> LoadBalancer::compute_probe_target_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports,
    float grid_total, const std::unordered_map<std::string, float> &eff_part) {
  if (!this->probe_state_ || !consumer_id) return std::nullopt;
  ProbeState &probe = *this->probe_state_;
  const std::string &candidate_id = probe.candidate_id;
  if (reports.find(candidate_id) == reports.end()) return std::nullopt;
  ReportMap support_reports;
  for (const auto &c : probe.backup_ids) {
    auto it = reports.find(c);
    if (it != reports.end()) support_reports[c] = it->second;
  }
  for (const auto &c : probe.active_ids) {
    if (c == candidate_id) continue;
    auto it = reports.find(c);
    if (it != reports.end()) support_reports[c] = it->second;
  }
  if (*consumer_id != candidate_id &&
      support_reports.find(*consumer_id) == support_reports.end()) {
    return std::nullopt;
  }
  float desired_total = grid_total;
  for (const auto &r : reports) desired_total += r.second.power;
  auto &state = this->get_consumer_(*consumer_id);
  auto cand_it = reports.find(candidate_id);
  const float probe_actual = (cand_it != reports.end()) ? cand_it->second.power : 0.0f;
  const float probe_ceiling = std::max(std::fabs(desired_total), this->cfg_.probe_min_power);

  if (*consumer_id == candidate_id) {
    const float next_requested_abs =
        this->next_probe_requested_abs_(probe.requested_power_abs, probe_ceiling);
    float desired_probe = 0.0f;
    if (desired_total > 0.0f) {
      desired_probe = std::max(std::fabs(probe_actual), next_requested_abs);
    } else if (desired_total < 0.0f) {
      desired_probe = -std::max(std::fabs(probe_actual), next_requested_abs);
    } else if (probe.requested_power_abs > 0.0f) {
      desired_probe =
          std::max(0.0f, probe.requested_power_abs - this->probe_success_threshold_);
    }
    if (desired_total < 0.0f && desired_probe > 0.0f) desired_probe = -desired_probe;
    probe.requested_power_abs = std::fabs(desired_probe);
    const float reading = to_grid_reading(NetOutputW(desired_probe), probe_actual);
    state.last_target = reading;
    state.last_intent = desired_probe;
    ReportMap cand_only;
    cand_only[candidate_id] = cand_it->second;
    return split_by_phase_(reading, cand_only);
  }

  // Seed backup_weights from the per-consumer efficiency partition the
  // auto path already computed (Python: balancer.py:612-614). Falling back
  // to 1.0 mirrors `eff_part.get(cid, 1.0)`; the floor at 0.01 keeps the
  // fair-share denominator from going to zero when every backup is
  // saturated. The earlier placeholder value 0.01f wiped out efficiency
  // weighting during the probe window and made backups share equally
  // regardless of how well they could actually follow targets.
  std::unordered_map<std::string, float> backup_weights;
  for (const auto &r : support_reports) {
    auto it = eff_part.find(r.first);
    const float w = (it != eff_part.end()) ? it->second : 1.0f;
    backup_weights[r.first] = std::max(0.01f, w) * r.second.weight;
  }
  const float qualified_probe_actual = probe.proof_samples > 0 ? probe_actual : 0.0f;
  const float desired = this->compute_desired_contribution_(
      *consumer_id, support_reports, backup_weights, desired_total - qualified_probe_actual);
  auto sup_it = support_reports.find(*consumer_id);
  const float reported = (sup_it != support_reports.end()) ? sup_it->second.power : 0.0f;
  const float reading = to_grid_reading(NetOutputW(desired), reported);
  state.last_target = reading;
  state.last_intent = desired;
  return split_by_phase_(reading, support_reports, &backup_weights);
}

float LoadBalancer::effective_min_dc_output_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports) {
  if (!consumer_id) return 0.0f;
  auto it = reports.find(*consumer_id);
  if (it == reports.end()) return 0.0f;
  const ConsumerReport &report = it->second;
  if (report.min_dc_output) return std::max(0.0f, *report.min_dc_output);
  if (needs_dc_output_floor(report.device_type)) return this->cfg_.min_dc_output;
  return 0.0f;
}

std::array<float, 3> LoadBalancer::apply_min_dc_output_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports,
    std::array<float, 3> result) {
  // Hold an external-inverter DC battery at MIN_DC_OUTPUT discharge so its
  // DC-fed inverter doesn't sleep at 0 W. Mirrors balancer.py _apply_min_dc_output.
  if (!consumer_id) return result;
  auto it = reports.find(*consumer_id);
  if (it == reports.end()) return result;
  const float eff_min = this->effective_min_dc_output_(consumer_id, reports);
  if (eff_min <= 0.0f) return result;
  const ConsumerReport &report = it->second;
  // Respect an explicit park (weight 0): don't silently wake it.
  if (report.weight == 0.0f) return result;
  const float reported = report.power;
  // Use the consumer's FULL intended reading: split_by_phase preserves the
  // total, so the sum recovers it regardless of phase distribution.
  const float reading_self = result[0] + result[1] + result[2];
  const float net_self = reported + reading_self;
  // Floor whenever the commanded net output is below the floor — including
  // negative (charge) commands: a floor-eligible battery can't charge, so the
  // all-DC-under-surplus case (lone B2500, issue #425) must still be lifted.
  if (net_self >= eff_min) return result;
  const float reading = to_grid_reading(NetOutputW(eff_min), reported);
  std::string phase = report.phase.empty() ? "A" : report.phase;
  for (auto &c : phase) c = static_cast<char>(std::toupper(c));
  size_t idx = 0;
  if (phase == "B") idx = 1;
  else if (phase == "C") idx = 2;
  std::array<float, 3> out{0.0f, 0.0f, 0.0f};
  out[idx] = reading;
  auto &mdc_state = this->get_consumer_(*consumer_id);
  mdc_state.last_target = reading;
  mdc_state.last_intent = eff_min;
  return out;
}

// With paced=true the wind-down reading goes through the ramp-pacing cap:
// the battery firmware applies a charge-direction reading in full in one
// cycle, so an unpaced wind-down dumps a discharging consumer's whole output
// as a one-poll step disturbance on the pool (see balancer.py).
std::array<float, 3> LoadBalancer::steer_to_zero_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports, bool paced) {
  float reported = 0.0f;
  std::string phase = "A";
  if (consumer_id) {
    auto it = reports.find(*consumer_id);
    if (it != reports.end()) {
      reported = it->second.power;
      phase = it->second.phase.empty() ? "A" : it->second.phase;
    }
  }
  float reading = to_grid_reading(NetOutputW(0.0f), reported);
  if (paced && consumer_id) reading = this->pace_reading_(*consumer_id, reading, reported);
  if (consumer_id) {
    auto &stz_state = this->get_consumer_(*consumer_id);
    stz_state.last_target = paced ? reading : 0.0f;
    stz_state.last_intent = 0.0f;
  }
  if (reading == 0.0f) return {0.0f, 0.0f, 0.0f};
  for (auto &c : phase) c = static_cast<char>(std::toupper(c));
  std::array<float, 3> result{0.0f, 0.0f, 0.0f};
  size_t idx = 0;
  if (phase == "A") idx = 0;
  else if (phase == "B") idx = 1;
  else if (phase == "C") idx = 2;
  result[idx] = reading;
  return result;
}

std::array<float, 3> LoadBalancer::split_by_phase_(
    float target, const ReportMap &reports,
    const std::unordered_map<std::string, float> *weights) {
  std::array<float, 3> phase_effective{0.0f, 0.0f, 0.0f};
  for (const auto &r : reports) {
    std::string phase = r.second.phase.empty() ? "A" : r.second.phase;
    for (auto &c : phase) c = static_cast<char>(std::toupper(c));
    size_t idx = 0;
    if (phase == "A") idx = 0;
    else if (phase == "B") idx = 1;
    else if (phase == "C") idx = 2;
    else idx = 0;
    float w = 1.0f;
    if (weights) {
      auto it = weights->find(r.first);
      if (it != weights->end()) w = it->second;
    }
    phase_effective[idx] += w;
  }
  const float total = phase_effective[0] + phase_effective[1] + phase_effective[2];
  if (total <= 0.0f) return {target, 0.0f, 0.0f};
  return {target * (phase_effective[0] / total), target * (phase_effective[1] / total),
          target * (phase_effective[2] / total)};
}

std::array<float, 3> LoadBalancer::compute_target(
    const std::optional<std::string> &consumer_id, ConsumerMode mode,
    const ReportMap &all_reports, float grid_total,
    const std::unordered_set<std::string> &inactive,
    const std::unordered_set<std::string> &manual,
    const std::vector<float> &sample_id) {
  if (mode.kind == ConsumerModeKind::INACTIVE) {
    return this->steer_to_zero_(consumer_id, all_reports);
  }
  ReportMap active_reports;
  for (const auto &r : all_reports) {
    if (inactive.find(r.first) == inactive.end()) active_reports[r.first] = r.second;
  }

  BalancerConsumerState *state = nullptr;
  if (consumer_id) state = &this->get_consumer_(*consumer_id);
  std::optional<float> last_target = state ? state->last_target : std::optional<float>{};

  if (consumer_id && state && active_reports.find(*consumer_id) != active_reports.end() &&
      mode.kind != ConsumerModeKind::MANUAL) {
    const auto probe_set = this->probe_participants_();
    if (probe_set.find(*consumer_id) == probe_set.end() &&
        this->deprioritized_.find(*consumer_id) == this->deprioritized_.end()) {
      const float actual = active_reports[*consumer_id].power;
      this->saturation_.update(*state, last_target, actual);
    }
  }

  if (mode.kind == ConsumerModeKind::MANUAL && consumer_id && state) {
    const float reported = active_reports.count(*consumer_id)
                               ? active_reports[*consumer_id].power
                               : 0.0f;
    const float reading = to_grid_reading(NetOutputW(mode.manual_value), reported);
    state->last_target = reading;
    state->last_intent = mode.manual_value;
    return split_by_phase_(reading, active_reports);
  }

  ReportMap auto_reports;
  for (const auto &r : active_reports) {
    if (manual.find(r.first) == manual.end()) auto_reports[r.first] = r.second;
  }
  auto result =
      this->compute_auto_target_(consumer_id, auto_reports, grid_total, sample_id);
  return this->apply_min_dc_output_(consumer_id, auto_reports, result);
}

// -------------------------------------------------------------------------
// Lifecycle
// -------------------------------------------------------------------------

void LoadBalancer::remove_consumer(const std::string &consumer_id) {
  this->consumers_.erase(consumer_id);
  this->deprioritized_.erase(consumer_id);
  auto it = std::find(this->priority_.begin(), this->priority_.end(), consumer_id);
  if (it != this->priority_.end()) {
    this->priority_.erase(it);
    this->invalidate_efficiency_cache_();
  }
  const auto probe_set = this->probe_participants_();
  if (probe_set.find(consumer_id) != probe_set.end())
    this->clear_probe_state_("consumer removed");
}

void LoadBalancer::detach_from_auto_pool(const std::string &consumer_id) {
  this->deprioritized_.erase(consumer_id);
  this->priority_.erase(
      std::remove(this->priority_.begin(), this->priority_.end(), consumer_id),
      this->priority_.end());
  this->consumers_.erase(consumer_id);
  this->invalidate_efficiency_cache_();
  const auto probe_set = this->probe_participants_();
  if (probe_set.find(consumer_id) != probe_set.end())
    this->clear_probe_state_("consumer detached");
}

void LoadBalancer::reset_consumer(const std::string &consumer_id) {
  auto &state = this->get_consumer_(consumer_id);
  state.last_target.reset();
  state.last_intent.reset();
  state.pace_cap = 0.0f;
  state.pace_sign = 0;
  state.pace_prev_reported.reset();
  state.pace_last_at = 0.0;
  state.osc_score = 0.0f;
  state.osc_last_sign = 0;
  state.saturation_score = 0.0;
  const double grace =
      this->clock_() + std::min(static_cast<double>(this->saturation_grace_seconds_),
                                static_cast<double>(this->cfg_.efficiency_rotation_interval));
  this->saturation_.set_grace(state, grace);
}

void LoadBalancer::force_rotation(const std::unordered_set<std::string> &current_pool) {
  std::vector<std::string> new_priority;
  for (const auto &cid : this->priority_) {
    if (current_pool.find(cid) != current_pool.end()) new_priority.push_back(cid);
  }
  std::vector<std::string> sorted_pool(current_pool.begin(), current_pool.end());
  std::sort(sorted_pool.begin(), sorted_pool.end());
  for (const auto &cid : sorted_pool) {
    if (std::find(new_priority.begin(), new_priority.end(), cid) == new_priority.end())
      new_priority.push_back(cid);
  }
  this->priority_ = std::move(new_priority);
  std::unordered_set<std::string> new_dep;
  for (const auto &c : this->deprioritized_)
    if (current_pool.count(c)) new_dep.insert(c);
  this->deprioritized_ = std::move(new_dep);

  if (this->priority_.size() < 2) return;
  const std::string first = this->priority_.front();
  this->priority_.erase(this->priority_.begin());
  this->priority_.push_back(first);
  this->last_rotation_ = this->clock_();
  this->probe_state_.reset();
  this->invalidate_efficiency_cache_();
  for (auto it = this->consumers_.begin(); it != this->consumers_.end();) {
    if (current_pool.count(it->first)) {
      it->second.fade_weight = 1.0f;
      ++it;
    } else {
      it = this->consumers_.erase(it);
    }
  }
}

double LoadBalancer::get_saturation(const std::string &consumer_id) const {
  auto it = this->consumers_.find(consumer_id);
  return (it != this->consumers_.end()) ? it->second.saturation_score : 0.0;
}

std::optional<float> LoadBalancer::get_last_target(const std::string &consumer_id) const {
  auto it = this->consumers_.find(consumer_id);
  return (it != this->consumers_.end()) ? it->second.last_target : std::optional<float>{};
}

std::optional<float> LoadBalancer::get_last_intent(const std::string &consumer_id) const {
  auto it = this->consumers_.find(consumer_id);
  return (it != this->consumers_.end()) ? it->second.last_intent : std::optional<float>{};
}

// -------------------------------------------------------------------------
// Auto-target pipeline
// -------------------------------------------------------------------------

std::array<float, 3> LoadBalancer::compute_auto_target_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports,
    float grid_total, const std::vector<float> &sample_id) {
  // Predicted grid the residual/fair-share control acts on (compensates for
  // meter latency; see predict_control_grid_). Updated on every call so the
  // estimate stays continuous across the probe / fading / charge-blind early
  // returns, which keep using the raw meter for their categorical decisions;
  // only the steady residual loop below acts on the prediction.
  // The trim integrates a steady-state bias, so it must only act on genuinely
  // fresh meter samples (closed-loop feedback). A frozen / stale meter repeats
  // its sample_id; without this gate the trim would wind a blind bias the meter
  // can never correct (e.g. through a probe handoff).
  const bool trim_fresh = sample_id != this->trim_sample_id_;
  this->trim_sample_id_ = sample_id;
  const float control_grid = this->apply_import_trim_(
      this->predict_control_grid_(reports, grid_total, sample_id), trim_fresh);

  std::unordered_map<std::string, float> saturation;
  for (const auto &c : this->consumers_)
    saturation[c.first] = static_cast<float>(c.second.saturation_score);
  const size_t num_consumers = std::max<size_t>(1, reports.size());
  std::unordered_map<std::string, float> eff_part;
  for (const auto &r : reports) {
    const float s = saturation.count(r.first) ? saturation[r.first] : 0.0f;
    eff_part[r.first] = std::max(0.01f, 1.0f - s);
  }

  bool ac_charging = false;
  bool any_ac_chargeable = false;
  for (const auto &r : reports) {
    const bool ac = is_ac_chargeable(r.second.device_type);
    if (ac) any_ac_chargeable = true;
    if (ac && r.second.power < 0.0f) ac_charging = true;
  }
  const bool in_charge_territory =
      any_ac_chargeable && (grid_total < 0.0f || (grid_total == 0.0f && ac_charging));
  std::unordered_set<std::string> charge_blind;
  if (in_charge_territory) {
    for (const auto &r : reports) {
      if (!is_ac_chargeable(r.second.device_type)) charge_blind.insert(r.first);
    }
  }
  for (const auto &cid : charge_blind) eff_part[cid] = 0.0f;

  auto efficiency_adjustments =
      this->compute_efficiency_deprioritized_(reports, sample_id, grid_total);
  std::unordered_set<std::string> all_ids;
  for (const auto &r : reports) all_ids.insert(r.first);
  auto faded_adjustments = this->fade_efficiency_weights_(efficiency_adjustments, all_ids);
  bool any_fading = false;
  for (const auto &kv : faded_adjustments)
    if (kv.second > 0.0f && kv.second < 1.0f) {
      any_fading = true;
      break;
    }

  auto probe_target = this->compute_probe_target_(consumer_id, reports, grid_total, eff_part);
  if (probe_target.has_value()) return *probe_target;

  const bool all_dc_under_surplus =
      (grid_total < 0.0f) && !reports.empty() && !any_ac_chargeable;
  if (all_dc_under_surplus && !this->all_dc_surplus_warned_) {
    this->all_dc_surplus_warned_ = true;
  } else if (!all_dc_under_surplus) {
    this->all_dc_surplus_warned_ = false;
  }

  if (consumer_id && charge_blind.count(*consumer_id)) {
    return this->steer_to_zero_(consumer_id, reports, /*paced=*/true);
  }

  if (any_fading && consumer_id) {
    auto &state = this->get_consumer_(*consumer_id);
    const double fade_w = state.fade_weight;
    auto it = reports.find(*consumer_id);
    const float reported = (it != reports.end()) ? it->second.power : 0.0f;
    if (fade_w == 0.0) return this->steer_to_zero_(consumer_id, reports, /*paced=*/true);
    float total_battery = 0.0f;
    for (const auto &r : reports) total_battery += r.second.power;
    const double demand = static_cast<double>(total_battery) + grid_total;
    double total_fade = 0.0;
    for (const auto &r : reports) total_fade += this->get_consumer_(r.first).fade_weight;
    const double desired = (total_fade > 0.0) ? demand * fade_w / total_fade : 0.0;
    float reading = to_grid_reading(NetOutputW(desired), reported);
    reading = this->pace_reading_(*consumer_id, reading, reported);
    state.last_target = reading;
    state.last_intent = desired;
    return split_by_phase_(reading, reports, &eff_part);
  }

  for (const auto &kv : faded_adjustments) {
    if (eff_part.count(kv.first) && kv.second == 0.0f) eff_part[kv.first] = 0.0f;
  }
  if (!faded_adjustments.empty() && consumer_id) {
    auto it = faded_adjustments.find(*consumer_id);
    if (it != faded_adjustments.end() && it->second == 0.0f) {
      return this->steer_to_zero_(consumer_id, reports, /*paced=*/true);
    }
  }

  // Fold the per-battery user weight into the effectiveness map so the
  // fair-share split honours the configured ratio. `eff_part` stays the pure
  // health/saturation map (used for participation/probing); the weighted
  // `share_part` only drives the proportional distribution. With neutral
  // weights (all 1.0) share_part == eff_part and the math is unchanged.
  std::unordered_map<std::string, float> share_part;
  for (const auto &kv : eff_part) {
    float w = 1.0f;
    auto rit = reports.find(kv.first);
    if (rit != reports.end()) w = rit->second.weight;
    share_part[kv.first] = kv.second * w;
  }
  float total_effective = 0.0f;
  for (const auto &kv : share_part) total_effective += kv.second;
  float fair_share;
  if (consumer_id && reports.count(*consumer_id)) {
    const float w = share_part.count(*consumer_id) ? share_part[*consumer_id] : 1.0f;
    fair_share = (total_effective > 0.0f) ? (control_grid / total_effective) * w
                                          : control_grid / num_consumers;
  } else {
    fair_share = control_grid / num_consumers;
  }

  // Deadband concentration (concentrate_deadband): a small grid error split N
  // ways can drop each battery's share below the firmware's ~20 W input
  // deadband, so none move and the pool tolerates ~N* the offset. Hand the whole
  // correction to the most-active battery (deterministic, with an id tiebreak so
  // it matches balancer.py) so it clears the deadband; bypass balance correction
  // for this tick. Acts on control_grid like the rest of the residual loop. Only
  // over participating batteries (not charge-blind / faded-out) and only when
  // they're all on the same phase (control_grid sums phases, so on a multi-phase
  // pool concentrating it over-corrects one phase and hunts). Gated on
  // fair_distribution. Mirrors balancer.py _compute_auto_target.
  bool concentrate = false;
  std::vector<const std::string *> conc_ids;
  bool conc_single_phase = true;
  bool consumer_in_conc = false;
  {
    std::string first_phase;
    bool have_first = false;
    for (const auto &kv : reports) {
      if (charge_blind.count(kv.first)) continue;
      auto ep = eff_part.find(kv.first);
      if (ep == eff_part.end() || ep->second <= 0.1f) continue;
      if (kv.second.weight <= 0.0f) continue;  // explicit zero share takes none
      conc_ids.push_back(&kv.first);
      if (consumer_id && kv.first == *consumer_id) consumer_in_conc = true;
      const std::string ph = kv.second.phase.empty() ? "A" : to_upper(kv.second.phase);
      if (!have_first) {
        first_phase = ph;
        have_first = true;
      } else if (ph != first_phase) {
        conc_single_phase = false;
      }
    }
  }
  if (this->cfg_.fair_distribution && this->cfg_.concentrate_deadband > 0.0f &&
      conc_ids.size() > 1 && conc_single_phase && consumer_in_conc &&
      std::fabs(control_grid) > 0.0f &&
      std::fabs(control_grid) < this->cfg_.concentrate_deadband) {
    const std::string *designated = nullptr;
    float best_abs = -1.0f;
    for (const auto *cid : conc_ids) {
      const float a = std::fabs(reports.at(*cid).power);
      if (a > best_abs || (a == best_abs && designated && *cid > *designated)) {
        best_abs = a;
        designated = cid;
      }
    }
    fair_share = (designated && *consumer_id == *designated) ? control_grid : 0.0f;
    concentrate = true;
  }

  // fair_share / balance_correction_ produce the residual: this consumer's
  // slice of the grid imbalance to fold into its current output. The absolute
  // net-output target is "what I report now plus my residual" (NetOutputW wrap
  // below).
  float residual;
  if (!this->cfg_.fair_distribution || !consumer_id ||
      reports.find(*consumer_id) == reports.end() || concentrate) {
    residual = fair_share;
  } else if (eff_part.count(*consumer_id)) {
    residual = this->balance_correction_(*consumer_id, reports, eff_part, fair_share);
  } else {
    residual = fair_share;
  }
  if ((control_grid < 0.0f && residual > 0.0f) ||
      (control_grid > 0.0f && residual < 0.0f)) {
    residual = 0.0f;
  }
  if (consumer_id) {
    residual = this->damp_oscillation_(*consumer_id, residual);
  }
  float reported = 0.0f;
  if (consumer_id) {
    auto it = reports.find(*consumer_id);
    if (it != reports.end()) reported = it->second.power;
  }
  float reading = to_grid_reading(NetOutputW(reported + residual), reported);
  if (consumer_id) {
    reading = this->pace_reading_(*consumer_id, reading, reported);
    auto &auto_state = this->get_consumer_(*consumer_id);
    auto_state.last_target = reading;
    auto_state.last_intent = reported + residual;
  }
  return split_by_phase_(reading, reports, &eff_part);
}

// Scale residual down while the consumer is hunting (issue #473). Tracks an
// accumulating score of how often the residual reverses sign: a genuine load
// step holds one sign, so a single reversal barely moves the score and full
// gain is kept; a latency-driven limit cycle reverses every few polls, so the
// score accumulates toward 1 and shrinks the residual by up to osc_damp_max,
// bleeding the loop gain that sustains the hunt. Mirrors balancer.py
// _damp_oscillation.
float LoadBalancer::damp_oscillation_(const std::string &consumer_id, float residual) {
  if (this->cfg_.osc_damp_max <= 0.0f) return residual;
  auto &state = this->get_consumer_(consumer_id);
  int sign = (residual > 0.0f) ? 1 : (residual < 0.0f ? -1 : 0);
  // A residual past the threshold is a genuine demand step, not hunting: react
  // at full gain (and bleed any hunt memory) so a real load/solar change isn't
  // slowed just because the loop was hunting beforehand.
  if (this->cfg_.osc_damp_threshold > 0.0f &&
      std::fabs(residual) > this->cfg_.osc_damp_threshold) {
    state.osc_score *= 1.0f - this->cfg_.osc_damp_decay;
    if (sign != 0) state.osc_last_sign = sign;
    return residual;
  }
  if (sign != 0 && state.osc_last_sign != 0 && sign != state.osc_last_sign) {
    state.osc_score = std::min(1.0f, state.osc_score + this->cfg_.osc_damp_alpha);
  } else {
    state.osc_score *= 1.0f - this->cfg_.osc_damp_decay;
  }
  if (sign != 0) state.osc_last_sign = sign;
  return residual * (1.0f - this->cfg_.osc_damp_max * state.osc_score);
}

// Online grid-state observer that compensates for meter latency without
// per-meter tuning. Two signals see the same grid at different delays: the
// batteries' self-reported output (fresh) and the grid meter (latent). Every
// call the estimate is advanced by the pool's actual reported output change
// (grid moves opposite to net output), crediting an in-flight correction
// before the meter shows it — so the loop never re-issues (and winds up) a
// correction already delivered. On a fresh meter sample the estimate is pulled
// toward the reading by an adaptive trust: a sustained same-sign innovation run
// (a genuine disturbance) raises it additively so steps are tracked fast, while
// a sign flip (latency-driven hunting) shrinks it multiplicatively so the fast
// prediction dominates and the hunt is starved. Returns the raw meter when
// disabled. Mirrors balancer.py LoadBalancer._predict_control_grid.
float LoadBalancer::predict_control_grid_(const ReportMap &reports, float grid_total,
                                          const std::vector<float> &sample_id) {
  if (this->cfg_.grid_predict_trust <= 0.0f) return grid_total;
  float pool_output = 0.0f;
  for (const auto &r : reports) pool_output += r.second.power;
  if (!this->pred_grid_.has_value()) {
    this->pred_grid_ = grid_total;
    this->pred_pool_output_ = pool_output;
    this->pred_sample_id_ = sample_id;
    this->pred_trust_ =
        std::min(PRED_TRUST_MAX, std::max(PRED_TRUST_MIN, this->cfg_.grid_predict_trust));
    return grid_total;
  }
  *this->pred_grid_ -= pool_output - this->pred_pool_output_;
  this->pred_pool_output_ = pool_output;
  if (!this->pred_sample_id_.has_value() || *this->pred_sample_id_ != sample_id) {
    this->pred_sample_id_ = sample_id;
    const float innovation = grid_total - *this->pred_grid_;
    const int sign = (innovation > 0.0f) ? 1 : (innovation < 0.0f ? -1 : 0);
    if (std::fabs(innovation) >= PRED_INNOVATION_GATE_W && sign != 0) {
      if (this->pred_innov_sign_ == 0 || sign == this->pred_innov_sign_) {
        this->pred_trust_ = std::min(PRED_TRUST_MAX, this->pred_trust_ + PRED_TRUST_RAISE_STEP);
      } else {
        this->pred_trust_ = std::max(PRED_TRUST_MIN, this->pred_trust_ * PRED_TRUST_SHRINK);
      }
      this->pred_innov_sign_ = sign;
    }
    *this->pred_grid_ += this->pred_trust_ * innovation;
  }
  return *this->pred_grid_;
}

// Cover the small residual grid import the battery firmware leaves in steady
// state (deadband + small-import hold), recovering retail-priced
// self-consumption. Once the predicted grid has held inside the small-import
// band (0, IMPORT_TRIM_GATE_W) for IMPORT_TRIM_DWELL consecutive fresh samples — a
// genuine steady state, not a load step on its final approach to zero — add
// import_trim_w so the firmware discharges to cover it. The dwell requirement
// keeps the trim inert during transients (no added overshoot); the band gate
// keeps it clear of a saturated/empty pack (which leaves a larger import).
// Because the trim integrates a steady-state bias, it acts only on a fresh meter
// sample: a frozen / stale meter offers no feedback to bound it, so the dwell
// neither advances nor fires until a new reading arrives (the grid-state
// predictor keeps the loop balanced meanwhile). import_trim_w = 0 disables it.
// Mirrors balancer.py LoadBalancer::_apply_import_trim.
float LoadBalancer::apply_import_trim_(float control_grid, bool fresh) {
  const float trim = this->cfg_.import_trim_w;
  if (trim <= 0.0f || !fresh) return control_grid;
  if (control_grid > 0.0f && control_grid < IMPORT_TRIM_GATE_W) {
    this->steady_import_dwell_++;
  } else {
    this->steady_import_dwell_ = 0;
  }
  if (this->steady_import_dwell_ >= IMPORT_TRIM_DWELL) return control_grid + trim;
  return control_grid;
}

// Clamp the auto-path reading to the consumer's ramp-pacing cap (issue #458).
// The battery integrates the reading with its own accelerating ramp, stepping
// by at most min(GAIN[ramp], |reading|) per poll — so the reading we send is
// the only bound on its per-poll movement once the ramp has accelerated. The
// cap starts at pace_base_step, doubles toward pace_max_step only while the
// battery demonstrably tracks the command, follows the error back down as it
// shrinks, and resets to the base step on direction reversal. Only the
// The auto-pool paths are paced (regulation loop, fade transition, and the
// deprioritized/charge-blind wind-down to zero); probe / MIN_DC_OUTPUT floor /
// manual / inactive steer-to-zero bypass it (see balancer.py for the
// rationale). Caps are W per PACE_REFERENCE_DT; the per-poll clamp scales
// with the consumer's observed inter-poll time, clamped at 1.0.
float LoadBalancer::pace_reading_(const std::string &consumer_id, float reading,
                                  float reported) {
  const float base = this->cfg_.pace_base_step;
  if (base <= 0.0f) return reading;
  auto &state = this->get_consumer_(consumer_id);
  const double now = this->clock_();
  double dt = (state.pace_last_at > 0.0) ? now - state.pace_last_at : 0.0;
  if (dt <= 0.0) {
    // First paced poll, a non-advancing clock, or a backwards jump: assume
    // one reference period rather than starving the clamp.
    dt = PACE_REFERENCE_DT;
  }
  state.pace_last_at = now;
  const float dt_ratio = static_cast<float>(std::min(1.0, dt / PACE_REFERENCE_DT));
  // Reversals are paced too (bounds overshoot at zero crossings); consumers
  // needing the unpaced control intent (issue #376 cross-talk attribution)
  // read last_intent instead.
  const int sign = (reading > 0.0f) ? 1 : (reading < 0.0f ? -1 : 0);
  float cap = (state.pace_cap > 0.0f) ? state.pace_cap : base;
  // Floored at the base step: hysteresis-regulator devices (B2500) need a
  // minimum reading to clear their input hold window at all; the cadence
  // scale still bounds the grown cap (mirrors balancer.py).
  float limit = std::max(base, cap * dt_ratio);
  if (sign == 0 || sign != state.pace_sign) {
    cap = base;
  } else if (std::fabs(reading) > limit) {
    float moved = 0.0f;
    if (state.pace_prev_reported.has_value())
      moved = (reported - *state.pace_prev_reported) * sign;
    // The tracking threshold and growth rate scale with the same cadence
    // ratio: a fast poller is expected to have moved less between polls,
    // and its cap doubles per reference second, not per poll.
    if (moved >= PACE_TRACKING_DELTA_W * dt_ratio)
      cap = std::min(cap * std::pow(PACE_GROWTH_FACTOR, dt_ratio), this->cfg_.pace_max_step);
  } else {
    cap = std::max(base, std::fabs(reading) / dt_ratio);
  }
  // Enforce the pace_max_step contract: the grow branch already clamps, but the
  // else branch back-computes cap as fabs(reading) / dt_ratio, which a fast poll
  // (small dt_ratio) can inflate past the max — and a later normal-cadence poll
  // would then slew beyond pace_max_step.
  cap = std::min(cap, this->cfg_.pace_max_step);
  state.pace_cap = cap;
  state.pace_sign = sign;
  state.pace_prev_reported = reported;
  limit = std::max(base, cap * dt_ratio);
  return std::max(-limit, std::min(limit, reading));
}

float LoadBalancer::balance_correction_(const std::string &consumer_id,
                                        const ReportMap &reports,
                                        const std::unordered_map<std::string, float> &eff_part,
                                        float fair_share) {
  const auto &cfg = this->cfg_;
  auto self_it = reports.find(consumer_id);
  const float actual_self = (self_it != reports.end()) ? self_it->second.power : 0.0f;
  std::vector<std::string> participating;
  for (const auto &r : reports) {
    auto it = eff_part.find(r.first);
    const float v = (it != eff_part.end()) ? it->second : 1.0f;
    if (v > 0.1f) participating.push_back(r.first);
  }
  if (participating.empty()) return fair_share;
  float actual_total = 0.0f;
  for (const auto &cid : participating) {
    auto it = reports.find(cid);
    if (it != reports.end()) actual_total += it->second.power;
  }
  // Pull each battery toward its weight-proportional share of the pool's total
  // output rather than the plain average, so the configured ratio is the steady
  // state. Participation is decided by eff_part above, so a healthy battery with
  // a small weight is not dropped. Neutral weights reduce to the plain average.
  float total_weight = 0.0f;
  std::unordered_map<std::string, float> weights;
  for (const auto &cid : participating) {
    float w = 1.0f;
    auto it = reports.find(cid);
    if (it != reports.end()) w = it->second.weight;
    weights[cid] = w;
    total_weight += w;
  }
  float target_share;
  if (total_weight > 0.0f) {
    const float wself = weights.count(consumer_id) ? weights[consumer_id] : 0.0f;
    target_share = actual_total * wself / total_weight;
  } else {
    target_share = actual_total / participating.size();
  }
  const float error = target_share - actual_self;
  const float err_abs = std::fabs(error);
  if (cfg.balance_deadband > 0.0f && err_abs < cfg.balance_deadband) return fair_share;
  float gain = cfg.balance_gain;
  if (cfg.error_reduce_threshold > 0.0f && err_abs < cfg.error_reduce_threshold) {
    gain = gain * (err_abs / cfg.error_reduce_threshold);
  } else if (cfg.error_boost_threshold > 0.0f && cfg.error_boost_max > 0.0f) {
    const float boost =
        std::min(err_abs / cfg.error_boost_threshold, 1.0f) * cfg.error_boost_max;
    gain = gain * (1.0f + boost);
  }
  float correction = gain * error;
  if (cfg.max_correction_per_step > 0.0f) {
    const float cap = cfg.max_correction_per_step;
    correction = std::max(-cap, std::min(cap, correction));
  }
  float target = fair_share + correction;
  if (cfg.max_target_step > 0.0f) {
    const float lo = actual_self - cfg.max_target_step;
    const float hi = actual_self + cfg.max_target_step;
    target = std::max(lo, std::min(hi, target));
  }
  return target;
}

// -------------------------------------------------------------------------
// Efficiency deprioritization
// -------------------------------------------------------------------------

std::unordered_map<std::string, float> LoadBalancer::compute_efficiency_deprioritized_(
    const ReportMap &reports, const std::vector<float> &sample_id, float grid_total) {
  const auto &cfg = this->cfg_;
  if (cfg.min_efficient_power <= 0.0f || reports.size() < 2) {
    this->probe_state_.reset();
    this->deprioritized_.clear();
    this->invalidate_efficiency_cache_();
    return {};
  }
  const double now = this->clock_();
  std::unordered_set<std::string> current;
  for (const auto &r : reports) current.insert(r.first);
  this->priority_.erase(std::remove_if(this->priority_.begin(), this->priority_.end(),
                                       [&](const std::string &c) { return !current.count(c); }),
                        this->priority_.end());
  std::unordered_set<std::string> new_dep;
  for (const auto &d : this->deprioritized_)
    if (current.count(d)) new_dep.insert(d);
  this->deprioritized_ = std::move(new_dep);

  const double grace =
      now + std::min(static_cast<double>(this->saturation_grace_seconds_),
                     static_cast<double>(cfg.efficiency_rotation_interval));
  std::vector<std::string> sorted_current(current.begin(), current.end());
  std::sort(sorted_current.begin(), sorted_current.end());
  for (const auto &cid : sorted_current) {
    if (std::find(this->priority_.begin(), this->priority_.end(), cid) ==
        this->priority_.end()) {
      this->priority_.push_back(cid);
      this->set_consumer_grace_(cid, grace);
    }
  }

  // Sink low/zero efficiency-window-weight batteries to the back of the priority
  // order so they fall into the deprioritized tail first while limiting. A
  // *stable* descending sort preserves the fair-wear rotation cycle within each
  // weight tier. Mirrors Python's self._priority.sort(...).
  std::stable_sort(this->priority_.begin(), this->priority_.end(),
                   [&](const std::string &a, const std::string &b) {
                     return efficiency_window_weight_of(reports, a) >
                            efficiency_window_weight_of(reports, b);
                   });

  const size_t prev_slots = std::max<size_t>(
      0, std::min(this->priority_.size(),
                  this->priority_.size() >= this->deprioritized_.size()
                      ? this->priority_.size() - this->deprioritized_.size()
                      : 0));
  std::vector<std::string> previous_active(this->priority_.begin(),
                                           this->priority_.begin() + prev_slots);
  const bool probe_resolved = this->resolve_probe_state_(reports, now, grid_total);
  const bool probe_active = this->probe_state_.has_value();

  // The active head holds its slot for efficiency_rotation_interval scaled by
  // its efficiency window weight, so a lower-weight battery rotates out sooner
  // (weight 0 -> threshold 0 -> rotates out on the next tick).
  if (!probe_active && !probe_resolved && !this->priority_.empty()) {
    const float head_weight =
        efficiency_window_weight_of(reports, this->priority_.front());
    if ((now - this->last_rotation_) >=
        cfg.efficiency_rotation_interval * head_weight) {
      this->last_rotation_ = now;
      const std::string first = this->priority_.front();
      this->priority_.erase(this->priority_.begin());
      this->priority_.push_back(first);
      this->invalidate_efficiency_cache_();
    }
  }

  if (!probe_active && !probe_resolved && cfg.efficiency_saturation_threshold > 0.0f &&
      this->cache_sample_.has_value()) {
    const size_t slots_est = this->priority_.size() - this->deprioritized_.size();
    for (size_t i = 0; i < slots_est && i < this->priority_.size(); ++i) {
      auto it = this->consumers_.find(this->priority_[i]);
      if (it != this->consumers_.end() &&
          it->second.saturation_score >= cfg.efficiency_saturation_threshold) {
        this->invalidate_efficiency_cache_();
        break;
      }
    }
  }

  std::string cache_key = serialize_cache_key(sample_id, this->priority_);
  if (this->cache_sample_.has_value() && *this->cache_sample_ == cache_key) {
    return this->cache_result_;
  }

  // Estimate household demand (|total_battery_power + grid_total| == true house
  // load) and low-pass filter it so meter noise can't thrash the active-set size
  // across the min_efficient_power threshold. The regulation loop still acts on
  // the raw grid, so tracking is unaffected. Mirrors balancer.py.
  float total_battery_power = 0.0f;
  for (const auto &cid : this->priority_) {
    auto it = reports.find(cid);
    if (it != reports.end()) total_battery_power += it->second.power;
  }
  const float raw_abs_target = std::fabs(total_battery_power + grid_total);
  const float demand_alpha = cfg.efficiency_demand_alpha;
  if (!this->demand_ema_.has_value() || demand_alpha >= 1.0f) {
    this->demand_ema_ = raw_abs_target;
  } else {
    this->demand_ema_ = *this->demand_ema_ + demand_alpha * (raw_abs_target - *this->demand_ema_);
  }
  const float abs_target = *this->demand_ema_;
  const size_t n = this->priority_.size();
  const float per_consumer = (n > 0) ? abs_target / n : 0.0f;

  const bool was_limiting = !this->deprioritized_.empty();
  const bool enter_limiting =
      was_limiting
          ? per_consumer < (cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR)
          : per_consumer < cfg.min_efficient_power;

  size_t slots;
  if (enter_limiting && n > 1) {
    slots = std::max<size_t>(
        1, std::min<size_t>(n - 1, static_cast<size_t>(abs_target / cfg.min_efficient_power)));
    if (was_limiting && prev_slots >= 1 && prev_slots < slots) {
      // Growing the active set while limiting takes the same 20% margin as
      // exiting limiting entirely.  Without it, demand sitting at an exact
      // multiple of min_efficient_power (e.g. ~300 W base load with a 150 W
      // floor) toggles a unit active/deprioritized on every meter-noise tick,
      // keeping the fade EMA permanently mid-transition and the pool hunting
      // (issue #469).  Shrinking stays immediate, mirroring how entering
      // limiting is immediate.
      const size_t grown = static_cast<size_t>(
          abs_target / (cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR));
      slots = std::max(prev_slots, std::min<size_t>(n - 1, grown));
    }
  } else {
    slots = n;
  }

  std::unordered_set<std::string> deprioritized;
  for (size_t i = slots; i < this->priority_.size(); ++i)
    deprioritized.insert(this->priority_[i]);
  std::unordered_map<std::string, float> result;
  for (const auto &cid : deprioritized) result[cid] = 0.0f;
  std::unordered_set<std::string> pre_swap_active;
  for (size_t i = 0; i < slots && i < this->priority_.size(); ++i)
    pre_swap_active.insert(this->priority_[i]);

  for (const auto &cid : this->deprioritized_) {
    if (deprioritized.find(cid) == deprioritized.end()) {
      this->saturation_.clear(this->get_consumer_(cid));
      this->set_consumer_grace_(cid, grace);
    }
  }

  if (!probe_active && !probe_resolved &&
      this->maybe_force_swap_saturated_(this->priority_, slots, now)) {
    deprioritized.clear();
    for (size_t i = slots; i < this->priority_.size(); ++i)
      deprioritized.insert(this->priority_[i]);
    result.clear();
    for (const auto &cid : deprioritized) result[cid] = 0.0f;
    // The swap reordered priority_; recompute the cache key from the *post*-swap
    // order so the next same-sample tick hits the cache, matching Python
    // (_compute_efficiency_deprioritized). Storing the pre-swap key here caused
    // the next tick to miss the cache and re-run the swap/probe machinery,
    // diverging from the canonical stack.
    cache_key = serialize_cache_key(sample_id, this->priority_);
    for (size_t i = 0; i < slots && i < this->priority_.size(); ++i) {
      if (pre_swap_active.find(this->priority_[i]) == pre_swap_active.end()) {
        this->saturation_.clear(this->get_consumer_(this->priority_[i]));
        this->set_consumer_grace_(this->priority_[i], grace);
      }
    }
  }

  std::vector<std::string> final_active(this->priority_.begin(),
                                        this->priority_.begin() +
                                            std::min(slots, this->priority_.size()));
  if (!probe_active && !probe_resolved && !previous_active.empty()) {
    std::vector<std::string> promoted;
    for (const auto &cid : final_active) {
      if (std::find(previous_active.begin(), previous_active.end(), cid) ==
          previous_active.end()) {
        promoted.push_back(cid);
      }
    }
    std::vector<std::string> backups;
    for (const auto &cid : previous_active) {
      if (std::find(final_active.begin(), final_active.end(), cid) == final_active.end())
        backups.push_back(cid);
    }
    if (!promoted.empty() && !backups.empty()) {
      this->begin_probe_(promoted[0], final_active, backups, previous_active, now);
    }
  }

  for (const auto &cid : deprioritized) {
    if (this->deprioritized_.find(cid) == this->deprioritized_.end()) {
      auto it = this->consumers_.find(cid);
      if (it != this->consumers_.end()) this->saturation_.clear(it->second);
    }
  }

  this->deprioritized_ = deprioritized;
  this->cache_sample_ = cache_key;
  this->cache_result_ = result;
  return result;
}

bool LoadBalancer::maybe_force_swap_saturated_(std::vector<std::string> &priority,
                                               size_t slots, double now) {
  const auto &cfg = this->cfg_;
  if (cfg.efficiency_saturation_threshold <= 0.0f || slots >= priority.size()) return false;
  const double threshold = cfg.efficiency_saturation_threshold;
  std::optional<size_t> saturated_idx;
  for (size_t i = 0; i < slots; ++i) {
    auto it = this->consumers_.find(priority[i]);
    if (it != this->consumers_.end() && it->second.saturation_score >= threshold) {
      saturated_idx = i;
      break;
    }
  }
  if (!saturated_idx) return false;
  std::optional<size_t> healthy_idx;
  for (size_t i = slots; i < priority.size(); ++i) {
    auto it = this->consumers_.find(priority[i]);
    if (it == this->consumers_.end() || it->second.saturation_score < threshold) {
      healthy_idx = i;
      break;
    }
  }
  if (!healthy_idx) return false;
  std::swap(priority[*saturated_idx], priority[*healthy_idx]);
  this->last_rotation_ = now;
  return true;
}

std::unordered_map<std::string, float> LoadBalancer::fade_efficiency_weights_(
    const std::unordered_map<std::string, float> &raw_adjustments,
    const std::unordered_set<std::string> &consumer_ids) {
  const float alpha = this->cfg_.efficiency_fade_alpha;
  std::unordered_map<std::string, float> result;
  const auto frozen = this->probe_participants_();
  const double now = this->clock_();
  const bool post_probe_active = now < this->post_probe_fade_until_;
  for (const auto &cid : consumer_ids) {
    auto &state = this->get_consumer_(cid);
    if (frozen.find(cid) != frozen.end()) {
      state.fade_weight = 1.0f;
      continue;
    }
    auto goal_it = raw_adjustments.find(cid);
    const double goal = goal_it != raw_adjustments.end() ? goal_it->second : 1.0;
    const double prev = state.fade_weight;
    double effective_alpha = alpha;
    if (post_probe_active && this->post_probe_fade_ids_.find(cid) != this->post_probe_fade_ids_.end()) {
      effective_alpha = std::min<double>(alpha, 0.25);
    }
    double new_w = prev + effective_alpha * (goal - prev);
    if (std::fabs(new_w - goal) < 0.05) new_w = goal;
    state.fade_weight = new_w;
    if (new_w < 1.0) result[cid] = static_cast<float>(new_w);
  }
  if (!post_probe_active) this->clear_post_probe_fade_();
  // Cleanup consumers no longer in the pool and not in priority_.
  std::unordered_set<std::string> in_priority(this->priority_.begin(), this->priority_.end());
  for (auto it = this->consumers_.begin(); it != this->consumers_.end();) {
    if (consumer_ids.find(it->first) == consumer_ids.end() &&
        in_priority.find(it->first) == in_priority.end()) {
      it = this->consumers_.erase(it);
    } else {
      ++it;
    }
  }
  return result;
}

}  // namespace ct002
}  // namespace esphome
