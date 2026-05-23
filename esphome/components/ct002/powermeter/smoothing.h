#pragma once

#include <optional>
#include <vector>

#include "base.h"

namespace esphome {
namespace ct002 {

// EMA smoother that filters per-phase power readings on the *total* and
// redistributes proportionally. Mirrors
// src/astrameter/powermeter/wrappers/smoothing.py.
class SmoothedPowermeter : public PowermeterWrapper {
 public:
  SmoothedPowermeter(Powermeter *wrapped, float alpha, float max_step);

  std::vector<float> get_powermeter_watts() override;
  void reset() override;

  // Exposed so MQTT insights can publish the smoothed value alongside raw.
  std::optional<double> smoothed_value() const { return this->value_; }

 protected:
  std::vector<float> distribute_(const std::vector<float> &raw_values, double raw_total);

  float alpha_;
  float max_step_;
  // EMA accumulator in double to avoid small-bias drift on steady signals.
  std::optional<double> value_;
  std::vector<float> last_sample_;
  std::optional<double> last_raw_total_;
};

// Stateless gate that zeros readings whose total magnitude is below the
// deadband threshold. Upstream EMA inertia keeps the signal continuous near
// the threshold, so this clean clamp is acceptable. Mirrors
// DeadbandPowermeter in smoothing.py.
class DeadbandPowermeter : public PowermeterWrapper {
 public:
  DeadbandPowermeter(Powermeter *wrapped, float deadband)
      : PowermeterWrapper(wrapped), deadband_(deadband) {}

  std::vector<float> get_powermeter_watts() override;

 protected:
  float deadband_;
};

}  // namespace ct002
}  // namespace esphome
