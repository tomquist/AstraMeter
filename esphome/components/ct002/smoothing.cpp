#include "smoothing.h"

#include <algorithm>
#include <cmath>

namespace esphome {
namespace ct002 {

SmoothedPowermeter::SmoothedPowermeter(Powermeter *wrapped, float alpha, float max_step)
    : PowermeterWrapper(wrapped), alpha_(alpha), max_step_(max_step) {}

void SmoothedPowermeter::reset() {
  PowermeterWrapper::reset();
  this->value_.reset();
  this->last_sample_.clear();
  this->last_raw_total_.reset();
}

std::vector<float> SmoothedPowermeter::get_powermeter_watts() {
  auto raw_values = this->wrapped_->get_powermeter_watts();
  double raw_total = 0.0;
  for (float v : raw_values)
    raw_total += v;

  if (!this->value_.has_value()) {
    this->value_ = raw_total;
    this->last_sample_ = raw_values;
    this->last_raw_total_ = raw_total;
    return this->distribute_(raw_values, raw_total);
  }

  const bool sample_id_eq = raw_values == this->last_sample_;
  const bool total_eq =
      this->last_raw_total_.has_value() && *this->last_raw_total_ == raw_total;
  if (sample_id_eq && total_eq) {
    // Dedup: multiple polls within the same meter cycle should not compound
    // the EMA update.
    return this->distribute_(raw_values, raw_total);
  }
  this->last_sample_ = raw_values;
  this->last_raw_total_ = raw_total;

  double catchup_alpha = this->alpha_;
  const bool raw_positive = raw_total > 0.0;
  const bool value_positive = *this->value_ > 0.0;
  if (raw_positive != value_positive) {
    catchup_alpha =
        std::max<double>(this->alpha_, std::min<double>(0.5, this->alpha_ * 4.0));
  }
  double delta = catchup_alpha * (raw_total - *this->value_);
  if (this->max_step_ > 0.0f) {
    delta = std::max<double>(-this->max_step_, std::min<double>(this->max_step_, delta));
  }
  this->value_ = *this->value_ + delta;
  return this->distribute_(raw_values, raw_total);
}

std::vector<float> SmoothedPowermeter::distribute_(const std::vector<float> &raw_values,
                                                   double raw_total) {
  if (raw_total == 0.0 || !this->value_.has_value())
    return raw_values;
  const double ratio = *this->value_ / raw_total;
  std::vector<float> out;
  out.reserve(raw_values.size());
  for (float v : raw_values)
    out.push_back(static_cast<float>(v * ratio));
  return out;
}

std::vector<float> DeadbandPowermeter::get_powermeter_watts() {
  auto values = this->wrapped_->get_powermeter_watts();
  if (this->deadband_ > 0.0f) {
    double total = 0.0;
    for (float v : values)
      total += v;
    if (std::fabs(total) < this->deadband_) {
      return std::vector<float>(values.size(), 0.0f);
    }
  }
  return values;
}

}  // namespace ct002
}  // namespace esphome
