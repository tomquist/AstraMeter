#pragma once

#include <array>
#include <cstdint>

#include "powermeter/base.h"

namespace esphome {
namespace ct002 {

// Bridge from ESPHome sensor::Sensor* state callbacks into the Powermeter
// interface used by the rest of the ct002 filter pipeline. Has no Python
// analog — Python reads power from polling sources, ESPHome push-delivers
// it via on_state callbacks. CT002 wires those callbacks to write into the
// raw_values_ array on this object; the wrapper pipeline pulls via
// get_powermeter_watts() each time it needs a fresh reading.
class SensorBackedPowermeter : public Powermeter {
 public:
  // num_phases is 1 or 3. raw_values and raw_stamp_ms are externally owned
  // (live on CT002Component) so the callbacks can write directly without an
  // extra indirection; SensorBackedPowermeter reads them on each call.
  SensorBackedPowermeter(uint8_t num_phases, const std::array<float, 3> *raw_values,
                        const std::array<uint32_t, 3> *raw_stamp_ms,
                        uint32_t max_sensor_age_ms);

  std::vector<float> get_powermeter_watts() override;
  std::vector<float> get_powermeter_watts_raw() override { return this->get_powermeter_watts(); }

 private:
  uint8_t num_phases_;
  const std::array<float, 3> *raw_values_;
  const std::array<uint32_t, 3> *raw_stamp_ms_;
  uint32_t max_sensor_age_ms_;
};

}  // namespace ct002
}  // namespace esphome
