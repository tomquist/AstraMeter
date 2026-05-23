#include "pid.h"

#include <algorithm>
#include <cmath>

#ifndef CT002_HOST_TEST
#include "esphome/core/hal.h"
#endif

namespace esphome {
namespace ct002 {

PidPowermeter::PidPowermeter(Powermeter *wrapped, float kp, float ki, float kd,
                             float output_max, PidMode mode)
    : PowermeterWrapper(wrapped),
      kp_(kp),
      ki_(ki),
      kd_(kd),
      output_max_(output_max),
      mode_(mode) {
#ifndef CT002_HOST_TEST
  this->clock_seconds_ = []() { return static_cast<float>(::esphome::millis()) / 1000.0f; };
#endif
}

std::vector<float> PidPowermeter::get_powermeter_watts() {
  auto raw_values = this->wrapped_->get_powermeter_watts();
  const float now = this->clock_seconds_ ? this->clock_seconds_() : 0.0f;

  double total_power = 0.0;
  for (float v : raw_values)
    total_power += v;
  const double error = -total_power;

  float dt = 0.0f;
  if (!this->prev_time_seconds_.has_value()) {
    this->prev_error_ = error;
    this->prev_time_seconds_ = now;
  } else {
    dt = now - *this->prev_time_seconds_;
    if (dt <= 0.0f)
      dt = 0.0f;
  }

  const double p_term = static_cast<double>(this->kp_) * error;

  double d_term = 0.0;
  if (dt > 0.0f && this->prev_error_.has_value()) {
    d_term = static_cast<double>(this->kd_) * (error - *this->prev_error_) / dt;
  }

  if (dt > 0.0f) {
    const double tentative_integral = this->integral_ + error * dt;
    const double tentative_output =
        p_term + static_cast<double>(this->ki_) * tentative_integral + d_term;
    const bool not_saturated = std::fabs(tentative_output) <= this->output_max_;
    const bool unwinding = (this->integral_ != 0.0) && (this->integral_ * error < 0.0);
    if (not_saturated || unwinding) {
      this->integral_ = tentative_integral;
    }
  }
  const double i_term = static_cast<double>(this->ki_) * this->integral_;

  this->prev_error_ = error;
  this->prev_time_seconds_ = now;

  double pid_output = p_term + i_term + d_term;
  pid_output = std::max<double>(-this->output_max_, std::min<double>(this->output_max_, pid_output));

  const size_t n = raw_values.size();
  const float per_phase = (n > 0) ? static_cast<float>(pid_output / n) : 0.0f;

  std::vector<float> out;
  out.reserve(n);
  if (this->mode_ == PidMode::BIAS) {
    for (float v : raw_values)
      out.push_back(v + per_phase);
  } else {
    out.assign(n, per_phase);
  }
  return out;
}

}  // namespace ct002
}  // namespace esphome
