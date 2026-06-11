#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "esphome/core/component.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/socket/socket.h"

#include "balancer.h"
#include "pid.h"
#include "sensor_backed.h"
#include "wrapper_base.h"

namespace esphome {
namespace ct002 {

// Per-consumer (battery) state mirrored from src/astrameter/ct002/ct002.py's
// `Consumer` dataclass. Lives in CT002Component::consumers_; mutated by
// _update_consumer_report and the MQTT/insights setters.
struct Consumer {
  std::string consumer_id;
  std::string phase{"A"};
  float power{0.0f};
  std::string device_type;
  std::string last_ip;
  double timestamp{0.0};
  std::optional<float> poll_interval;
  // Cached input value(s) from before_send-style hooks (manual injection
  // path used by MQTT insights). When unset the SensorBackedPowermeter feed
  // is used directly.
  std::optional<std::vector<float>> values;
  bool active{true};
  bool manual_enabled{false};
  float manual_target{0.0f};
  // "Participate" flag from the request's optional 7th field. ``0`` on the wire
  // means "do not aggregate me"; defaults to true when the field is absent.
  bool participates{true};
  // Relative fair-share weight (1.0 = neutral). Tuned live via the MQTT
  // "Distribution Weight" entity; mirrors Python's Consumer.distribution_weight.
  float distribution_weight{1.0f};
  // Per-device MIN_DC_OUTPUT override (W); unset = inherit global. Tuned live
  // via the MQTT "Min DC Output" entity; mirrors Python's Consumer.min_dc_output.
  std::optional<float> min_dc_output;
  // Net AC power the balancer last instructed this consumer to be at —
  // distinct from `power` (what the consumer reports). The cross-talk
  // *_chrg_power / *_dchrg_power fields aggregate THIS, not `power`,
  // so PV-passthrough doesn't masquerade as discharge (issue #376).
  float last_instructed_power{0.0f};
};

class CT002Component : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // Configuration setters (called from to_code()).
  void set_power_sensor_l1(sensor::Sensor *s) { this->power_sensor_l1_ = s; }
  void set_power_sensor_l2(sensor::Sensor *s) { this->power_sensor_l2_ = s; }
  void set_power_sensor_l3(sensor::Sensor *s) { this->power_sensor_l3_ = s; }
  void set_ct_type(const std::string &v) { this->ct_type_ = v; }
  void set_ct_mac(const std::string &v) { this->ct_mac_ = v; }
  void set_wifi_rssi(int v) { this->wifi_rssi_ = v; }
  void set_udp_port(uint16_t v) { this->udp_port_ = v; }
  void set_active_control(bool v) { this->active_control_ = v; }
  void set_max_sensor_age_ms(uint32_t v) { this->max_sensor_age_ms_ = v; }
#ifdef USE_CT002_TEST_HOOKS
  // Enable the test-control UDP server on this port. Only compiled when the
  // YAML sets `test_control_port:` (which adds the USE_CT002_TEST_HOOKS
  // define) — never present in production firmware. The control channel
  // lets a host-platform e2e test inject grid power and drive a mock clock
  // so time-gated behaviour (saturation / probe / eviction / dedup) is
  // deterministic. See test_hooks.cpp.
  void set_control_port(uint16_t v) { this->control_port_ = v; }
#endif

  // Filter pipeline configuration (each call enables that filter; absence
  // means the wrapper is not in the pipeline, matching Python's
  // fallback=0 → wrapper skipped semantics).
  void enable_hampel(size_t window, float n_sigma, float min_threshold);
  void enable_smoothing(float alpha, float max_step);
  void enable_deadband(float deadband);
  void enable_pid(float kp, float ki, float kd, float output_max, PidMode mode);

  // Balancer configuration setter (called once from to_code() after
  // populating a BalancerConfig).
  void set_balancer_config(const BalancerConfig &cfg) { this->balancer_cfg_ = cfg; }
  void set_balancer_saturation(float alpha, float min_target, float decay_factor,
                              float grace_seconds, float stall_timeout_seconds, bool enabled) {
    this->saturation_alpha_ = alpha;
    this->saturation_min_target_ = min_target;
    this->saturation_decay_factor_ = decay_factor;
    this->saturation_grace_seconds_ = grace_seconds;
    this->saturation_stall_timeout_seconds_ = stall_timeout_seconds;
    this->saturation_enabled_ = enabled;
  }

  // Observability (MQTT insights and future automation hooks read these).
  size_t reporting_consumer_count() const;

  // ── MQTT-insights integration API ────────────────────────────────────
  // Snapshot of one consumer's state for publish-time JSON building.
  // Mirrors src/astrameter/mqtt_insights/service.py::_handle_ct002_event's
  // `consumer_state` dict. Returned by value (small POD-ish; per-event
  // copies are negligible vs the MQTT publish cost).
  struct ConsumerSnapshot {
    std::string consumer_id;
    std::string phase;
    std::string device_type;
    std::string last_ip;
    float reported_power{0.0f};
    bool active{true};
    bool auto_target{true};
    std::optional<float> manual_target;
    float distribution_weight{1.0f};
    std::optional<float> min_dc_output;
    std::optional<float> poll_interval;
    double timestamp{0.0};
    // Cross-phase grid power last observed at the pipeline head (post-
    // filters, pre-balancer). Mirrors Python's `grid_power.{l1,l2,l3}`.
    std::array<float, 3> grid_power{0.0f, 0.0f, 0.0f};
    // Per-phase balancer-issued targets from the most recent reply.
    std::array<float, 3> target{0.0f, 0.0f, 0.0f};
    // Saturation (0..1) of this consumer's phase, from the LoadBalancer.
    float saturation{0.0f};
    std::optional<float> last_target;
    // Device-level total input grid power (post-filter, pre-balancer) —
    // mirrors Python's smooth_target. Same for every consumer in a given
    // poll cycle (it's a device-wide value), carried on the snapshot so
    // mqtt_insights doesn't need a second ct002 accessor.
    float smooth_target{0.0f};
  };
  ConsumerSnapshot snapshot_consumer(const std::string &consumer_id) const;
  std::vector<std::string> reporting_consumer_ids() const;

  // Read-only view of the most recent grid_power values (for the Marstek
  // MQTT responder's get_values()). Returns up to 3 phases; values that
  // age past max_sensor_age_ms_ are zeroed (mirrors Python's
  // SensorBackedPowermeter behaviour).
  std::vector<float> latest_grid_power() const;
  size_t connected_slave_count() const;

  // Configured ct_type/ct_mac forwarded to the Marstek MQTT topics.
  const std::string &ct_type() const { return this->ct_type_; }
  const std::string &ct_mac() const { return this->ct_mac_; }
  int wifi_rssi() const { return this->wifi_rssi_; }
  // Used by mqtt_insights for the device-level "active_control" entity so
  // HA reflects the configured state instead of always reading "running".
  bool active_control() const { return this->active_control_; }
  // TTL (seconds) after which a silent consumer is evicted from the
  // tracking map. Defaults to 120 s, matching Python's consumer_ttl.
  void set_consumer_ttl_seconds(uint32_t v) { this->consumer_ttl_seconds_ = v; }
  // Dedup window (ms). Repeat polls from the same consumer within this
  // window are dropped. 0 (default) disables dedup. Mirrors Python's
  // dedupe_time_window (default 0.0).
  void set_dedupe_window_ms(uint32_t v) { this->dedupe_window_ms_ = v; }

  // Reporting-row shape that mirrors src/astrameter/ct002/__init__.py's
  // `ReportingConsumerRow` — used by the Marstek cd=4 slave list and by
  // mqtt_insights when it needs `device_type`/`consumer_id`/`last_ip`/`phase`
  // for one published row. Keep field names aligned with Python.
  struct ReportingConsumerRow {
    std::string consumer_id;
    std::string device_type;
    std::string last_ip;
    std::string phase;
  };
  std::vector<ReportingConsumerRow> reporting_consumer_rows() const;

  // Command path (called by mqtt_insights when an HA-discovery entity
  // is acted on). All are no-ops if consumer_id is unknown.
  void set_consumer_active(const std::string &consumer_id, bool active);
  void set_consumer_manual_target(const std::string &consumer_id, float target);
  void set_consumer_auto_target(const std::string &consumer_id, bool auto_target);
  void set_consumer_distribution_weight(const std::string &consumer_id, float weight);
  void set_consumer_min_dc_output(const std::string &consumer_id, float value);
  void force_balancer_rotation();

  // Listener registration — mqtt_insights subscribes once at setup() to
  // be notified after every successful UDP poll-reply round trip. Allows
  // the insights component to push fresh state without polling.
  using ConsumerEventCallback = std::function<void(const std::string &consumer_id)>;
  void add_consumer_event_listener(ConsumerEventCallback cb) {
    this->consumer_event_listeners_.push_back(std::move(cb));
  }
  using ConsumerRemovedCallback = std::function<void(const std::string &consumer_id)>;
  void add_consumer_removed_listener(ConsumerRemovedCallback cb) {
    this->consumer_removed_listeners_.push_back(std::move(cb));
  }

 protected:
  void start_udp_server_();
  void pump_udp_();
  void handle_request_(const uint8_t *data, size_t len, const std::string &addr_ip,
                       uint16_t addr_port);

  std::string consumer_key_(const std::string &meter_mac, const std::string &addr_ip,
                            uint16_t addr_port) const;
  Consumer &get_consumer_(const std::string &consumer_id);
  // Periodic cleanup driven by set_interval in setup(). Fires
  // consumer_removed_listeners_ and calls balancer_->remove_consumer for
  // every entry older than consumer_ttl_seconds_. Also purges the dedup
  // timestamp map (mirrors Python's _dedup.purge_older_than at the same
  // cadence).
  void evict_stale_consumers_();

  // Returns false if a poll from consumer_id arrived within
  // dedupe_window_ms_ of the last accepted one. The window is measured
  // from the last ACCEPTED request (dropped polls don't refresh the
  // timestamp), matching RequestDeduplicator.should_process.
  bool dedup_should_process_(const std::string &consumer_id);
  void update_consumer_report_(const std::string &consumer_id, const std::string &phase,
                              float power, const std::string &device_type,
                              const std::string &source_ip, bool participates = true);

  bool validate_ct_mac_(const std::vector<std::string> &request_fields) const;
  std::vector<std::string> build_response_fields_(
      const std::vector<std::string> &request_fields, const std::vector<float> &values);
  ReportMap collect_reports_for_balancer_() const;
  struct PhaseReports {
    std::array<float, 3> chrg_power{0.0f, 0.0f, 0.0f};
    std::array<float, 3> dchrg_power{0.0f, 0.0f, 0.0f};
    std::array<bool, 3> active{false, false, false};
    std::array<int, 3> count{0, 0, 0};
  };
  PhaseReports collect_reports_by_phase_() const;
  std::vector<float> compute_smooth_target_(const std::vector<float> &values,
                                            const std::string &consumer_id);
  // Monotonic seconds used for all time-gated logic (saturation, probe,
  // eviction, dedup, poll_interval). Instance method (not static) so the
  // test-hook mock clock can override it. Falls back to millis() in
  // production builds and whenever the mock clock is not engaged.
  double now_seconds_();
  // (Re)constructs balancer_ from balancer_cfg_ + saturation_* members.
  void build_balancer_();

  // Configuration.
  sensor::Sensor *power_sensor_l1_{nullptr};
  sensor::Sensor *power_sensor_l2_{nullptr};
  sensor::Sensor *power_sensor_l3_{nullptr};
  std::string ct_type_{"HME-4"};
  std::string ct_mac_;
  int wifi_rssi_{-50};
  uint16_t udp_port_{12345};
  bool active_control_{true};
  uint32_t max_sensor_age_ms_{30000};
  // Eviction interval for stale consumers (Python: consumer_ttl=120s).
  // Stored in seconds; the cleanup loop runs every 5s and evicts any
  // consumer whose last `timestamp` is older than this value.
  uint32_t consumer_ttl_seconds_{120};
  uint32_t dedupe_window_ms_{0};
  // Last-accepted-poll timestamp (monotonic seconds) per consumer_id,
  // for the dedup gate. Purged alongside consumer eviction.
  std::unordered_map<std::string, double> dedup_last_;
  // Per-consumer EMA alpha for the poll_interval diagnostic (Python:
  // POLL_INTERVAL_EMA_ALPHA=0.3). Same value here so the published
  // MQTT-insights poll_interval matches between stacks.
  static constexpr float POLL_INTERVAL_EMA_ALPHA = 0.3f;
  BalancerConfig balancer_cfg_;
  float saturation_alpha_{0.15f};
  float saturation_min_target_{20.0f};
  float saturation_decay_factor_{0.995f};
  float saturation_grace_seconds_{90.0f};
  float saturation_stall_timeout_seconds_{60.0f};
  bool saturation_enabled_{true};

  // Sensor input cache (written by per-sensor on_state callbacks).
  std::array<float, 3> raw_values_{0.0f, 0.0f, 0.0f};
  std::array<uint32_t, 3> raw_stamp_ms_{0, 0, 0};
  uint8_t num_phases_{0};

  // Pipeline: head is SensorBackedPowermeter, then optional wrappers.
  std::vector<std::unique_ptr<Powermeter>> pipeline_;
  Powermeter *pipeline_head_{nullptr};

  // Pending wrapper configs captured before setup() — applied at setup()
  // time when the SensorBackedPowermeter exists.
  struct HampelCfg { size_t window; float n_sigma; float min_threshold; };
  struct SmoothingCfg { float alpha; float max_step; };
  struct PidCfg { float kp, ki, kd, output_max; PidMode mode; };
  std::optional<HampelCfg> hampel_cfg_;
  std::optional<SmoothingCfg> smoothing_cfg_;
  std::optional<float> deadband_threshold_;
  std::optional<PidCfg> pid_cfg_;

  // Balancer + saturation tracker (created in setup()).
  std::unique_ptr<LoadBalancer> balancer_;

  // Consumers.
  std::unordered_map<std::string, Consumer> consumers_;
  uint8_t info_idx_counter_{0};

  // Last per-phase grid_power and balancer-issued target observed during
  // the most recent compute_smooth_target_ call. Read by snapshot_consumer
  // and latest_grid_power. Mirrors Python's per-consumer caches but is
  // shared here because ESPHome has at most one ct002 device → one balancer
  // run at a time, so per-consumer storage would just duplicate.
  std::array<float, 3> last_grid_power_{0.0f, 0.0f, 0.0f};
  std::array<float, 3> last_target_{0.0f, 0.0f, 0.0f};
  std::optional<float> last_smooth_target_;

  // Event listeners — invoked after every successful UDP poll-reply round
  // trip (event) and after consumer eviction (removed). mqtt_insights
  // registers callbacks via add_consumer_event_listener / removed_listener.
  std::vector<ConsumerEventCallback> consumer_event_listeners_;
  std::vector<ConsumerRemovedCallback> consumer_removed_listeners_;

  // UDP socket.
  std::unique_ptr<socket::Socket> socket_;

#ifdef USE_CT002_TEST_HOOKS
  // Test-only control channel (see test_hooks.cpp). Gated entirely behind
  // the build flag so production firmware carries none of it.
  void start_control_server_();
  void pump_control_();
  void handle_control_command_(const std::string &cmd, const struct sockaddr_storage &from,
                               socklen_t from_len);
  // Set one balancer/saturation config field by name and rebuild the
  // balancer. Returns false for an unknown key. Used by the `cfg` control
  // command so e2e tests can run scenarios under varied settings.
  bool apply_cfg_(const std::string &key, double value);
  uint16_t control_port_{0};
  std::unique_ptr<socket::Socket> control_socket_{nullptr};
  bool mock_clock_enabled_{false};
  double mock_clock_seconds_{0.0};
#endif
};

}  // namespace ct002
}  // namespace esphome
