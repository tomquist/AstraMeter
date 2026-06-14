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

// Ramp pacing (issue #458): the pacing cap doubles per reference second, and
// only when the battery's reported output moved at least
// PACE_TRACKING_DELTA_W (scaled by the consumer's poll cadence) in the
// commanded direction since the previous paced poll. The threshold sits
// below the battery firmware's guaranteed 10 W minimum step on a constant
// reading (it would deadlock a step response otherwise), and the caps are W
// per PACE_REFERENCE_DT so fast pollers cannot integrate per-poll readings
// into a higher W/s slew. Mirrors balancer.py.
inline constexpr float PACE_TRACKING_DELTA_W = 5.0f;
inline constexpr float PACE_GROWTH_FACTOR = 2.0f;
inline constexpr double PACE_REFERENCE_DT = 1.0;

// Adaptive grid-state predictor (see BalancerConfig::grid_predict_trust and
// LoadBalancer::predict_control_grid_). The meter trust is bounded to
// [PRED_TRUST_MIN, PRED_TRUST_MAX] and adapted per fresh meter sample whose
// innovation clears PRED_INNOVATION_GATE_W. The raise is a small additive step
// (trust climbs only under a sustained same-sign innovation run — a genuine
// lasting disturbance) while the shrink is a hard multiplicative cut (a single
// sign flip, the signature of latency-driven hunting, collapses it). Mirrors
// balancer.py.
inline constexpr float PRED_TRUST_MIN = 0.15f;
inline constexpr float PRED_TRUST_MAX = 0.6f;
inline constexpr float PRED_TRUST_RAISE_STEP = 0.08f;
inline constexpr float PRED_TRUST_SHRINK = 0.4f;
inline constexpr float PRED_INNOVATION_GATE_W = 40.0f;

inline constexpr double EFFICIENCY_HYSTERESIS_FACTOR = 1.2;
inline constexpr double SATURATION_GRACE_SECONDS = 90.0;
inline constexpr double SATURATION_STALL_TIMEOUT_SECONDS = 60.0;
inline constexpr double SATURATION_REFERENCE_DT = 1.0;
inline constexpr double SATURATION_LONG_GAP_SECONDS = 30.0;

// Device capabilities — the single source of truth for every device-type
// decision (mirrors balancer.py device_capabilities). All downstream policy
// (AC-charge eligibility, the MIN_DC_OUTPUT wake floor) is derived from these.
struct DeviceCapabilities {
  bool has_builtin_inverter{false};
  bool has_ac_input{false};
  bool has_dc_input{false};
};

DeviceCapabilities device_capabilities(const std::string &device_type);

bool is_ac_chargeable(const std::string &device_type);

// True iff the battery depends on a sleep-prone external inverter (no built-in
// inverter and no AC input — the B2500 family). Mirrors _needs_dc_output_floor.
bool needs_dc_output_floor(const std::string &device_type);

// Absolute net-output target in watts: the single currency of all control
// logic (mirrors balancer.py NetOutputW). Sign convention, defined once:
//   +  =  net discharge (export to grid / serve load)
//   -  =  net charge     (import from grid)
// A distinct type so a net-output target can never be silently mixed with a
// grid-meter reading (the relative delta a battery adds to its own output).
struct NetOutputW {
  float value{0.0f};
  explicit NetOutputW(float v = 0.0f) : value(v) {}
};

// Single boundary between the control currency (NetOutputW, an absolute net
// output) and the grid-meter reading a battery integrates via
// new_output = reported + reading. Returns target - reported so the battery
// lands on the absolute target; positive = grid import (raise net output).
// Callers phase-split the scalar result (see LoadBalancer::split_by_phase_).
inline float to_grid_reading(NetOutputW target, float reported) {
  return target.value - reported;
}

struct BalancerConfig {
  bool fair_distribution{true};
  float balance_gain{0.2f};
  // Kept above the battery firmware's own +-20 W input deadband so the
  // balancer never chases share errors the battery would ignore (issue #458).
  float balance_deadband{25.0f};
  float error_boost_threshold{150.0f};
  float error_boost_max{0.5f};
  float error_reduce_threshold{20.0f};
  float max_correction_per_step{80.0f};
  float max_target_step{0.0f};
  // Ramp pacing for the auto path (issue #458): per-poll cap on the sent
  // reading, starting at the firmware ramp's first-step gain and growing
  // toward pace_max_step only while the battery is observed tracking.
  // pace_base_step = 0 disables. See balancer.py for the tuning rationale.
  float pace_base_step{50.0f};
  float pace_max_step{200.0f};
  // Oscillation-gated damping (issue #473): under meter latency the gain-1
  // grid-following residual limit-cycles. An EMA of how often a consumer's
  // residual reverses sign scales the residual down by up to osc_damp_max; a
  // genuine step holds one sign (score ~0, full gain), only a hunt is damped.
  // osc_damp_max = 0 disables. See balancer.py for the tuning rationale.
  float osc_damp_max{0.8f};
  float osc_damp_alpha{0.15f};
  float osc_damp_decay{0.1f};
  // Only residuals below this magnitude are damped; a larger one is a genuine
  // demand step that reacts at full gain. See balancer.py.
  float osc_damp_threshold{450.0f};
  float min_efficient_power{0.0f};
  float probe_min_power{80.0f};
  float efficiency_rotation_interval{900.0f};
  float efficiency_fade_alpha{0.15f};
  // double (compared against the double saturation_score EMA) so the swap
  // decision matches the canonical Python double math; a float 0.4f sits a few
  // 1e-8 above 0.4 and flips the comparison on a knife-edge score, diverging the
  // deprioritized set from Python.
  double efficiency_saturation_threshold{0.4};
  // Minimum net discharge (W) to keep an external-inverter DC battery awake.
  // 0 disables. See issue #425 and balancer.py.
  float min_dc_output{0.0f};
  // Adaptive grid-state predictor: act on a predicted grid that credits the
  // pool's freshly-reported output between meter refreshes and trusts each
  // fresh meter sample by an online-learned amount, compensating for meter
  // latency without per-meter tuning. 0 disables (act on the raw meter); any
  // positive value only seeds the self-adapting trust. See balancer.py.
  float grid_predict_trust{0.5f};

  void clamp();
};

enum class ConsumerModeKind { AUTO, MANUAL, INACTIVE };
struct ConsumerMode {
  ConsumerModeKind kind{ConsumerModeKind::AUTO};
  float manual_value{0.0f};
};

struct BalancerConsumerState {
  std::optional<float> last_target;
  // Absolute net-output target (NetOutputW currency) intended for this
  // consumer, recorded *before* wire pacing. The cross-talk chrg/dchrg
  // attribution uses it to filter involuntary outputs (issue #376).
  std::optional<float> last_intent;
  // Long-running EMA weight — double (like saturation_score) so the fade
  // trajectory and its snap-to-goal threshold match the canonical Python double
  // math poll-for-poll; float drifts enough to flip the snap on a different poll.
  double fade_weight{1.0};
  // Ramp-pacing state (see BalancerConfig::pace_base_step): current per-poll
  // cap, sign of the last paced reading, and the battery's reported power at
  // the last pacing step (tracking detection).
  float pace_cap{0.0f};
  int pace_sign{0};
  std::optional<float> pace_prev_reported{};
  double pace_last_at{0.0};
  // Oscillation-gated damping (see BalancerConfig::osc_damp_max): accumulated
  // reversal score and the sign of the last non-zero residual that fed it.
  float osc_score{0.0f};
  int osc_last_sign{0};
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
  // Per-device MIN_DC_OUTPUT override (W); unset = inherit the global setting.
  // Mirrors the Python reports dict's "min_dc_output" key. Default-initialized
  // so aggregate ``ConsumerReport{...}`` init stays warning-clean.
  std::optional<float> min_dc_output{};
};

using ReportMap = std::unordered_map<std::string, ConsumerReport>;

class SaturationTracker {
 public:
  SaturationTracker(double alpha, float min_target, double decay_factor,
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
  // double (like the saturation_score it drives) so the EMA matches the
  // canonical Python double math; a float alpha/decay sits ~1e-8 off the double
  // value and drifts the score across the swap threshold on a knife-edge.
  double alpha_;
  float min_target_;
  double decay_factor_;
  float stall_timeout_seconds_;
};

class LoadBalancer {
 public:
  LoadBalancer(BalancerConfig config, double saturation_alpha,
               float saturation_min_target, double saturation_decay_factor,
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
  // Absolute net-output target intended pre-pacing (see BalancerConsumerState).
  std::optional<float> get_last_intent(const std::string &consumer_id) const;

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

  float effective_min_dc_output_(const std::optional<std::string> &consumer_id,
                                 const ReportMap &reports);
  std::array<float, 3> apply_min_dc_output_(const std::optional<std::string> &consumer_id,
                                            const ReportMap &reports,
                                            std::array<float, 3> result);

  std::array<float, 3> steer_to_zero_(const std::optional<std::string> &consumer_id,
                                      const ReportMap &reports, bool paced = false);
  static std::array<float, 3> split_by_phase_(
      float target, const ReportMap &reports,
      const std::unordered_map<std::string, float> *weights = nullptr);

  std::array<float, 3> compute_auto_target_(const std::optional<std::string> &consumer_id,
                                            const ReportMap &reports, float grid_total,
                                            const std::vector<float> &sample_id);
  float balance_correction_(const std::string &consumer_id, const ReportMap &reports,
                            const std::unordered_map<std::string, float> &eff_part,
                            float fair_share);
  float pace_reading_(const std::string &consumer_id, float reading, float reported);
  float damp_oscillation_(const std::string &consumer_id, float residual);
  float predict_control_grid_(const ReportMap &reports, float grid_total,
                              const std::vector<float> &sample_id);

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

  // Adaptive grid-state predictor state (see predict_control_grid_).
  // pred_grid_ is the estimate the control path acts on; pred_pool_output_ is
  // the pool's last-seen reported output (its per-call delta advances the
  // estimate); pred_sample_id_ flags a genuinely fresh meter reading;
  // pred_trust_ is the online-adapted meter trust and pred_innov_sign_ the sign
  // of the last significant innovation that drove it.
  std::optional<float> pred_grid_{};
  float pred_pool_output_{0.0f};
  std::optional<std::vector<float>> pred_sample_id_{};
  float pred_trust_{0.0f};
  int pred_innov_sign_{0};
};

}  // namespace ct002
}  // namespace esphome
