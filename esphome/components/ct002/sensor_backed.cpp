#include "sensor_backed.h"

#ifndef CT002_HOST_TEST
#include "esphome/core/hal.h"
#endif

namespace esphome {
namespace ct002 {

#ifndef CT002_HOST_TEST
static uint32_t now_ms() { return ::esphome::millis(); }
#else
static uint32_t now_ms() { return 0; }  // Static for host tests; harness drives input directly.
#endif

SensorBackedPowermeter::SensorBackedPowermeter(uint8_t num_phases,
                                               const std::array<float, 3> *raw_values,
                                               const std::array<uint32_t, 3> *raw_stamp_ms,
                                               uint32_t max_sensor_age_ms)
    : num_phases_(num_phases),
      raw_values_(raw_values),
      raw_stamp_ms_(raw_stamp_ms),
      max_sensor_age_ms_(max_sensor_age_ms) {}

std::vector<float> SensorBackedPowermeter::get_powermeter_watts() {
  if (this->max_sensor_age_ms_ > 0) {
    const uint32_t now = now_ms();
    uint32_t max_stamp = 0;
    for (uint8_t i = 0; i < this->num_phases_; ++i) {
      if ((*this->raw_stamp_ms_)[i] > max_stamp) max_stamp = (*this->raw_stamp_ms_)[i];
    }
    // Unsigned subtraction wraps correctly across the 49.7-day boundary
    // (matches the rollover-safe pattern documented in the plan).
    if (max_stamp != 0 && (now - max_stamp) > this->max_sensor_age_ms_) {
      return {};  // unavailable
    }
  }
  std::vector<float> out;
  out.reserve(this->num_phases_);
  for (uint8_t i = 0; i < this->num_phases_; ++i) {
    out.push_back((*this->raw_values_)[i]);
  }
  return out;
}

}  // namespace ct002
}  // namespace esphome
