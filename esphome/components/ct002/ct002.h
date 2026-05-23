#pragma once

#include <array>
#include <cstdint>
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
#include "powermeter/base.h"
#include "sensor_backed.h"

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

 protected:
  void start_udp_server_();
  void pump_udp_();
  void handle_request_(const uint8_t *data, size_t len, const std::string &addr_ip,
                       uint16_t addr_port);

  std::string consumer_key_(const std::string &meter_mac, const std::string &addr_ip,
                            uint16_t addr_port) const;
  Consumer &get_consumer_(const std::string &consumer_id);
  void update_consumer_report_(const std::string &consumer_id, const std::string &phase,
                              float power, const std::string &device_type,
                              const std::string &source_ip);

  bool validate_ct_mac_(const std::vector<std::string> &request_fields) const;
  std::vector<std::string> build_response_fields_(
      const std::vector<std::string> &request_fields, const std::vector<float> &values);
  ReportMap collect_reports_for_balancer_() const;
  struct PhaseReports {
    std::array<float, 3> chrg_power{0.0f, 0.0f, 0.0f};
    std::array<float, 3> dchrg_power{0.0f, 0.0f, 0.0f};
    std::array<bool, 3> active{false, false, false};
  };
  PhaseReports collect_reports_by_phase_() const;
  std::vector<float> compute_smooth_target_(const std::vector<float> &values,
                                            const std::string &consumer_id);
  static double now_seconds_();

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

  // UDP socket.
  std::unique_ptr<socket::Socket> socket_;
};

}  // namespace ct002
}  // namespace esphome
