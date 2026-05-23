#include "ct002.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace ct002 {

static const char *const TAG = "ct002";

void CT002Component::setup() {
  this->num_phases_ = (this->power_sensor_l2_ != nullptr) ? 3 : 1;

  auto cache_l1 = [this](float value) {
    this->raw_values_[0] = value;
    this->raw_stamp_ms_[0] = millis();
  };
  this->power_sensor_l1_->add_on_state_callback(cache_l1);

  if (this->power_sensor_l2_ != nullptr) {
    auto cache_l2 = [this](float value) {
      this->raw_values_[1] = value;
      this->raw_stamp_ms_[1] = millis();
    };
    this->power_sensor_l2_->add_on_state_callback(cache_l2);
  }
  if (this->power_sensor_l3_ != nullptr) {
    auto cache_l3 = [this](float value) {
      this->raw_values_[2] = value;
      this->raw_stamp_ms_[2] = millis();
    };
    this->power_sensor_l3_->add_on_state_callback(cache_l3);
  }

  ESP_LOGCONFIG(TAG, "CT002 setup: %u phase(s), ct_type=%s, udp_port=%u",
                this->num_phases_, this->ct_type_.c_str(), this->udp_port_);
}

void CT002Component::loop() {
  // UDP server pump lands in a subsequent commit.
}

void CT002Component::dump_config() {
  ESP_LOGCONFIG(TAG, "CT002 Component:");
  ESP_LOGCONFIG(TAG, "  Phases: %u", this->num_phases_);
  ESP_LOGCONFIG(TAG, "  CT Type: %s", this->ct_type_.c_str());
  ESP_LOGCONFIG(TAG, "  CT MAC: %s", this->ct_mac_.empty() ? "(mirror)" : this->ct_mac_.c_str());
  ESP_LOGCONFIG(TAG, "  UDP Port: %u", this->udp_port_);
  ESP_LOGCONFIG(TAG, "  Active Control: %s", YESNO(this->active_control_));
  ESP_LOGCONFIG(TAG, "  Max Sensor Age: %u ms", this->max_sensor_age_ms_);
}

}  // namespace ct002
}  // namespace esphome
