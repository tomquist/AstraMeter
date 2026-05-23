#pragma once

#include <array>
#include <cstdint>
#include <string>

#include "esphome/core/component.h"
#include "esphome/components/sensor/sensor.h"

namespace esphome {
namespace ct002 {

// Component skeleton for the CT002/CT003 grid-meter emulator. The full port
// (UDP server, balancer, filter pipeline, optional MQTT/Marstek sub-blocks)
// lands in subsequent commits. This first iteration carries only the
// configuration surface, the protocol primitives (protocol.{h,cpp}), and a
// sensor-input cache so downstream wiring can be exercised end-to-end.
class CT002Component : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_power_sensor_l1(sensor::Sensor *s) { this->power_sensor_l1_ = s; }
  void set_power_sensor_l2(sensor::Sensor *s) { this->power_sensor_l2_ = s; }
  void set_power_sensor_l3(sensor::Sensor *s) { this->power_sensor_l3_ = s; }

  void set_ct_type(const std::string &ct_type) { this->ct_type_ = ct_type; }
  void set_ct_mac(const std::string &ct_mac) { this->ct_mac_ = ct_mac; }
  void set_wifi_rssi(int rssi) { this->wifi_rssi_ = rssi; }
  void set_udp_port(uint16_t port) { this->udp_port_ = port; }
  void set_active_control(bool active) { this->active_control_ = active; }
  void set_max_sensor_age_ms(uint32_t ms) { this->max_sensor_age_ms_ = ms; }

 protected:
  sensor::Sensor *power_sensor_l1_{nullptr};
  sensor::Sensor *power_sensor_l2_{nullptr};
  sensor::Sensor *power_sensor_l3_{nullptr};

  std::array<float, 3> raw_values_{0.0f, 0.0f, 0.0f};
  std::array<uint32_t, 3> raw_stamp_ms_{0, 0, 0};
  uint8_t num_phases_{0};

  std::string ct_type_{"HME-4"};
  std::string ct_mac_;  // empty → mirror incoming MAC; else 12-hex lowercase
  int wifi_rssi_{-50};
  uint16_t udp_port_{12345};
  bool active_control_{true};
  uint32_t max_sensor_age_ms_{30000};
};

}  // namespace ct002
}  // namespace esphome
