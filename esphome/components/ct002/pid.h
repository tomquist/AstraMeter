#pragma once

#include <functional>
#include <optional>

#include "wrapper_base.h"

namespace esphome {
namespace ct002 {

enum class PidMode { BIAS, REPLACE };

// PID controller that steers the reported power toward zero (grid balance).
// Mirrors src/astrameter/powermeter/wrappers/pid.py.
//
// Error convention: error = -measurement. Positive grid import produces a
// negative error, causing the PID to reduce the reported value and motivate
// the storage device to cover the import. Output is clamped to
// [-output_max, +output_max]; the integral term has anti-windup (paused
// while output is saturated, unless the integral is unwinding).
class PidPowermeter : public PowermeterWrapper {
 public:
  PidPowermeter(Powermeter *wrapped, float kp, float ki, float kd, float output_max,
                PidMode mode);

  // Time source. Defaults to millis() but a test harness can inject a fake
  // clock for deterministic dt sequencing.
  void set_clock(std::function<float()> clock_seconds) {
    this->clock_seconds_ = std::move(clock_seconds);
  }

  std::vector<float> get_powermeter_watts() override;

 protected:
  float kp_;
  float ki_;
  float kd_;
  float output_max_;
  PidMode mode_;

  // PID state. Integral kept in double — long-running accumulator that
  // would otherwise drift on steady signals.
  double integral_{0.0};
  std::optional<double> prev_error_;
  std::optional<float> prev_time_seconds_;

  std::function<float()> clock_seconds_;
};

}  // namespace ct002
}  // namespace esphome
