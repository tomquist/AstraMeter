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

constexpr const char *AC_CHARGEABLE_PREFIXES[] = {"HMG", "VNS"};

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

bool is_ac_chargeable(const std::string &device_type) {
  if (device_type.empty()) return false;
  std::string up;
  up.reserve(device_type.size());
  for (char c : device_type) up.push_back(static_cast<char>(std::toupper(c)));
  for (const char *prefix : AC_CHARGEABLE_PREFIXES) {
    const size_t plen = std::strlen(prefix);
    if (up.size() >= plen && up.compare(0, plen, prefix) == 0) return true;
  }
  return false;
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
  clamp_v(efficiency_saturation_threshold, 0.0f, 1.0f);
}

// -------------------------------------------------------------------------
// SaturationTracker
// -------------------------------------------------------------------------

SaturationTracker::SaturationTracker(float alpha, float min_target, float decay_factor,
                                     float stall_timeout_seconds, bool enabled,
                                     std::function<double()> clock)
    : clock_(std::move(clock)),
      enabled_(enabled),
      alpha_(std::max(0.01f, std::min(1.0f, alpha))),
      min_target_(std::max(1.0f, min_target)),
      decay_factor_(std::max(0.0f, std::min(1.0f, decay_factor))),
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

LoadBalancer::LoadBalancer(BalancerConfig config, float saturation_alpha,
                           float saturation_min_target, float saturation_decay_factor,
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
      const float share = it != reports.end() ? std::fabs(it->second.power) : 0.0f;
      this->get_consumer_(cid).fade_weight = static_cast<float>(share / total_actual);
    }
  } else {
    const size_t active_count = std::max<size_t>(1, probe.active_ids.size());
    for (const auto &cid : probe.active_ids)
      this->get_consumer_(cid).fade_weight = 1.0f / active_count;
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
    const float target = desired_probe - probe_actual;
    state.last_target = target;
    ReportMap cand_only;
    cand_only[candidate_id] = cand_it->second;
    return split_by_phase_(target, cand_only);
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
  const float target = desired - reported;
  state.last_target = target;
  return split_by_phase_(target, support_reports, &backup_weights);
}

std::array<float, 3> LoadBalancer::steer_to_zero_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports) {
  if (consumer_id) this->get_consumer_(*consumer_id).last_target = 0.0f;
  float reported = 0.0f;
  std::string phase = "A";
  if (consumer_id) {
    auto it = reports.find(*consumer_id);
    if (it != reports.end()) {
      reported = it->second.power;
      phase = it->second.phase.empty() ? "A" : it->second.phase;
    }
  }
  if (reported == 0.0f) return {0.0f, 0.0f, 0.0f};
  for (auto &c : phase) c = static_cast<char>(std::toupper(c));
  std::array<float, 3> result{0.0f, 0.0f, 0.0f};
  size_t idx = 0;
  if (phase == "A") idx = 0;
  else if (phase == "B") idx = 1;
  else if (phase == "C") idx = 2;
  result[idx] = -reported;
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
    const float target = mode.manual_value - reported;
    state->last_target = target;
    return split_by_phase_(target, active_reports);
  }

  ReportMap auto_reports;
  for (const auto &r : active_reports) {
    if (manual.find(r.first) == manual.end()) auto_reports[r.first] = r.second;
  }
  return this->compute_auto_target_(consumer_id, auto_reports, grid_total, sample_id);
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

// -------------------------------------------------------------------------
// Auto-target pipeline
// -------------------------------------------------------------------------

std::array<float, 3> LoadBalancer::compute_auto_target_(
    const std::optional<std::string> &consumer_id, const ReportMap &reports,
    float grid_total, const std::vector<float> &sample_id) {
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
    return this->steer_to_zero_(consumer_id, reports);
  }

  if (any_fading && consumer_id) {
    auto &state = this->get_consumer_(*consumer_id);
    const float fade_w = state.fade_weight;
    auto it = reports.find(*consumer_id);
    const float reported = (it != reports.end()) ? it->second.power : 0.0f;
    if (fade_w == 0.0f) return this->steer_to_zero_(consumer_id, reports);
    float total_battery = 0.0f;
    for (const auto &r : reports) total_battery += r.second.power;
    const float demand = total_battery + grid_total;
    float total_fade = 0.0f;
    for (const auto &r : reports) total_fade += this->get_consumer_(r.first).fade_weight;
    const float desired = (total_fade > 0.0f) ? demand * fade_w / total_fade : 0.0f;
    const float target = desired - reported;
    state.last_target = target;
    return split_by_phase_(target, reports, &eff_part);
  }

  for (const auto &kv : faded_adjustments) {
    if (eff_part.count(kv.first) && kv.second == 0.0f) eff_part[kv.first] = 0.0f;
  }
  if (!faded_adjustments.empty() && consumer_id) {
    auto it = faded_adjustments.find(*consumer_id);
    if (it != faded_adjustments.end() && it->second == 0.0f) {
      return this->steer_to_zero_(consumer_id, reports);
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
    fair_share = (total_effective > 0.0f) ? (grid_total / total_effective) * w
                                          : grid_total / num_consumers;
  } else {
    fair_share = grid_total / num_consumers;
  }

  float target;
  if (!this->cfg_.fair_distribution || !consumer_id ||
      reports.find(*consumer_id) == reports.end()) {
    target = fair_share;
  } else if (eff_part.count(*consumer_id)) {
    target = this->balance_correction_(*consumer_id, reports, eff_part, fair_share);
  } else {
    target = fair_share;
  }
  if ((grid_total < 0.0f && target > 0.0f) || (grid_total > 0.0f && target < 0.0f)) {
    target = 0.0f;
  }
  if (consumer_id) this->get_consumer_(*consumer_id).last_target = target;
  return split_by_phase_(target, reports, &eff_part);
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

  const size_t prev_slots = std::max<size_t>(
      0, std::min(this->priority_.size(),
                  this->priority_.size() >= this->deprioritized_.size()
                      ? this->priority_.size() - this->deprioritized_.size()
                      : 0));
  std::vector<std::string> previous_active(this->priority_.begin(),
                                           this->priority_.begin() + prev_slots);
  const bool probe_resolved = this->resolve_probe_state_(reports, now, grid_total);
  const bool probe_active = this->probe_state_.has_value();

  if (!probe_active && !probe_resolved && !this->priority_.empty() &&
      (now - this->last_rotation_) >= cfg.efficiency_rotation_interval) {
    this->last_rotation_ = now;
    const std::string first = this->priority_.front();
    this->priority_.erase(this->priority_.begin());
    this->priority_.push_back(first);
    this->invalidate_efficiency_cache_();
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

  const std::string cache_key = serialize_cache_key(sample_id, this->priority_);
  if (this->cache_sample_.has_value() && *this->cache_sample_ == cache_key) {
    return this->cache_result_;
  }

  float total_battery_power = 0.0f;
  for (const auto &cid : this->priority_) {
    auto it = reports.find(cid);
    if (it != reports.end()) total_battery_power += it->second.power;
  }
  const float abs_target = std::fabs(total_battery_power + grid_total);
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
  const float threshold = cfg.efficiency_saturation_threshold;
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
    const float goal = goal_it != raw_adjustments.end() ? goal_it->second : 1.0f;
    const float prev = state.fade_weight;
    float effective_alpha = alpha;
    if (post_probe_active && this->post_probe_fade_ids_.find(cid) != this->post_probe_fade_ids_.end()) {
      effective_alpha = std::min(alpha, 0.25f);
    }
    float new_w = prev + effective_alpha * (goal - prev);
    if (std::fabs(new_w - goal) < 0.05f) new_w = goal;
    state.fade_weight = new_w;
    if (new_w < 1.0f) result[cid] = new_w;
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
