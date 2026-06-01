#pragma once

// Mirrors src/astrameter/ct002/balancer.py. Method and field names are
// preserved exactly so cross-language bug fixes map 1:1. See the Python
// source for narrative comments — the C++ port keeps only the comments
// that capture invariants a reader needs to safely modify the code.

#include <array>
#include <cstdint>
#include <functional>
#include <optional>
#include <set>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace esphome {
namespace ct002 {

inline constexpr double EFFICIENCY_HYSTERESIS_FACTOR = 1.2;
inline constexpr double SATURATION_GRACE_SECONDS = 90.0;
inline constexpr double SATURATION_STALL_TIMEOUT_SECONDS = 60.0;
inline constexpr double SATURATION_REFERENCE_DT = 1.0;
inline constexpr double SATURATION_LONG_GAP_SECONDS = 30.0;

bool is_ac_chargeable(const std::string &device_type);

struct BalancerConfig {
  bool fair_distribution{true};
  float balance_gain{0.2f};
  float balance_deadband{15.0f};
  float error_boost_threshold{150.0f};
  float error_boost_max{0.5f};
  float error_reduce_threshold{20.0f};
  float max_correction_per_step{80.0f};
  float max_target_step{0.0f};
  float min_efficient_power{0.0f};
  float probe_min_power{80.0f};
  float efficiency_rotation_interval{900.0f};
  float efficiency_fade_alpha{0.15f};
  float efficiency_saturation_threshold{0.4f};

  void clamp();
};

enum class ConsumerModeKind { AUTO, MANUAL, INACTIVE };
struct ConsumerMode {
  ConsumerModeKind kind{ConsumerModeKind::AUTO};
  float manual_value{0.0f};
};

struct BalancerConsumerState {
  std::optional<float> last_target;
  float fade_weight{1.0f};
  // Long-running EMA accumulator — double prevents small-bias drift on
  // steady signals over hours of runtime.
  double saturation_score{0.0};
  double saturation_grace_until{0.0};
  double saturation_grace_started_at{0.0};
  double last_saturation_update{0.0};
};

struct ProbeState {
  std::string candidate_id;
  std::vector<std::string> active_ids;
  std::vector<std::string> backup_ids;
  std::vector<std::string> restore_active_ids;
  double deadline{0.0};
  double started_at{0.0};
  int proof_samples{0};
  float requested_power_abs{0.0f};
};

// Per-consumer report from the UDP handler: device_type, phase ("A"/"B"/"C"),
// reported power. Matches the dict shape Python passes to compute_target.
struct ConsumerReport {
  std::string device_type;
  std::string phase{"A"};
  float power{0.0f};
  // Relative fair-share weight (1.0 = neutral). Mirrors the Python reports
  // dict's "weight" key, set live via the MQTT "Distribution Weight" entity.
  float weight{1.0f};
};

using ReportMap = std::unordered_map<std::string, ConsumerReport>;

class SaturationTracker {
 public:
  SaturationTracker(float alpha, float min_target, float decay_factor,
                    float stall_timeout_seconds, bool enabled,
                    std::function<double()> clock);

  void update(BalancerConsumerState &state, std::optional<float> last_target,
              float actual);
  double get(const BalancerConsumerState &state) const { return state.saturation_score; }
  void set_grace(BalancerConsumerState &state, double deadline);
  void clear(BalancerConsumerState &state);

 private:
  std::function<double()> clock_;
  bool enabled_;
  float alpha_;
  float min_target_;
  float decay_factor_;
  float stall_timeout_seconds_;
};

class LoadBalancer {
 public:
  LoadBalancer(BalancerConfig config, float saturation_alpha,
               float saturation_min_target, float saturation_decay_factor,
               float saturation_grace_seconds, float saturation_stall_timeout_seconds,
               bool saturation_enabled, std::function<double()> clock,
               std::function<void()> reset_fn);

  std::array<float, 3> compute_target(const std::optional<std::string> &consumer_id,
                                      ConsumerMode mode, const ReportMap &all_reports,
                                      float grid_total,
                                      const std::unordered_set<std::string> &inactive,
                                      const std::unordered_set<std::string> &manual,
                                      const std::vector<float> &sample_id);

  void remove_consumer(const std::string &consumer_id);
  void detach_from_auto_pool(const std::string &consumer_id);
  void reset_consumer(const std::string &consumer_id);
  void force_rotation(const std::unordered_set<std::string> &current_pool);

  double get_saturation(const std::string &consumer_id) const;
  std::optional<float> get_last_target(const std::string &consumer_id) const;

 protected:
  BalancerConsumerState &get_consumer_(const std::string &consumer_id);
  void invalidate_efficiency_cache_();
  std::unordered_set<std::string> probe_participants_() const;
  float effective_probe_min_power_() const;
  float next_probe_requested_abs_(float current_requested_abs, float ceiling) const;
  void clear_probe_state_(const std::string &reason);
  void clear_post_probe_fade_();
  void set_consumer_grace_(const std::string &consumer_id, double deadline);
  void clear_consumer_grace_(const std::string &consumer_id);

  void begin_probe_(const std::string &candidate_id,
                    std::vector<std::string> active_ids,
                    std::vector<std::string> backup_ids,
                    std::vector<std::string> restore_active_ids, double now);
  void commit_probe_(const ReportMap &reports, double now, float actual);
  void reject_probe_(double now, const std::string &reason);
  bool resolve_probe_state_(const ReportMap &reports, double now, float grid_total);

  float compute_desired_contribution_(const std::string &consumer_id,
                                      const ReportMap &reports,
                                      const std::unordered_map<std::string, float> &weights,
                                      float desired_total);
  std::optional<std::array<float, 3>> compute_probe_target_(
      const std::optional<std::string> &consumer_id, const ReportMap &reports,
      float grid_total, const std::unordered_map<std::string, float> &eff_part);

  std::array<float, 3> steer_to_zero_(const std::optional<std::string> &consumer_id,
                                      const ReportMap &reports);
  static std::array<float, 3> split_by_phase_(
      float target, const ReportMap &reports,
      const std::unordered_map<std::string, float> *weights = nullptr);

  std::array<float, 3> compute_auto_target_(const std::optional<std::string> &consumer_id,
                                            const ReportMap &reports, float grid_total,
                                            const std::vector<float> &sample_id);
  float balance_correction_(const std::string &consumer_id, const ReportMap &reports,
                            const std::unordered_map<std::string, float> &eff_part,
                            float fair_share);

  std::unordered_map<std::string, float> compute_efficiency_deprioritized_(
      const ReportMap &reports, const std::vector<float> &sample_id, float grid_total);
  bool maybe_force_swap_saturated_(std::vector<std::string> &priority, size_t slots,
                                   double now);
  std::unordered_map<std::string, float> fade_efficiency_weights_(
      const std::unordered_map<std::string, float> &raw_adjustments,
      const std::unordered_set<std::string> &consumer_ids);

  std::function<double()> clock_;
  BalancerConfig cfg_;
  SaturationTracker saturation_;
  float saturation_grace_seconds_;
  std::function<void()> reset_fn_;
  std::unordered_map<std::string, BalancerConsumerState> consumers_;
  std::unordered_set<std::string> deprioritized_;
  std::vector<std::string> priority_;
  double last_rotation_;

  // Efficiency cache. The Python side keys on (sample_id, tuple(priority_));
  // we serialize both into a single string to avoid templating an
  // unordered_map<pair<vector<float>, vector<string>>, ...>.
  std::optional<std::string> cache_sample_;
  std::unordered_map<std::string, float> cache_result_;

  std::optional<ProbeState> probe_state_;
  float probe_timeout_seconds_;
  float probe_success_threshold_;
  double post_probe_fade_until_{0.0};
  std::unordered_set<std::string> post_probe_fade_ids_;
  bool all_dc_surplus_warned_{false};
};

}  // namespace ct002
}  // namespace esphome
