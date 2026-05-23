#include "hampel.h"

#include <algorithm>
#include <cmath>

namespace esphome {
namespace ct002 {

HampelPowermeter::HampelPowermeter(Powermeter *wrapped, size_t window, float n_sigma,
                                   float min_threshold)
    : PowermeterWrapper(wrapped),
      window_size_(window),
      n_sigma_(n_sigma),
      min_threshold_(min_threshold) {}

void HampelPowermeter::reset() {
  PowermeterWrapper::reset();
  this->window_.clear();
}

// Median over a deque<double>. Sorts a copy — matches Python's
// statistics.median() (which sorts the full sequence). Window sizes are
// small (single-digit), so the O(n log n) cost is negligible.
static double median_of(const std::deque<double> &values) {
  std::vector<double> sorted(values.begin(), values.end());
  std::sort(sorted.begin(), sorted.end());
  const size_t n = sorted.size();
  if (n == 0)
    return 0.0;
  if (n % 2 == 1)
    return sorted[n / 2];
  return 0.5 * (sorted[n / 2 - 1] + sorted[n / 2]);
}

std::vector<float> HampelPowermeter::get_powermeter_watts() {
  auto raw_values = this->wrapped_->get_powermeter_watts();
  if (raw_values.empty())
    return {};

  double raw_total = 0.0;
  for (float v : raw_values)
    raw_total += v;

  if (this->window_.size() >= this->window_size_) {
    this->window_.pop_front();
  }
  this->window_.push_back(raw_total);

  if (this->window_.size() < this->window_size_) {
    return raw_values;
  }

  const double median = median_of(this->window_);
  std::deque<double> abs_dev;
  for (double x : this->window_)
    abs_dev.push_back(std::fabs(x - median));
  const double mad = median_of(abs_dev);
  const double threshold =
      std::max(static_cast<double>(this->n_sigma_) * MAD_SCALE * mad,
               static_cast<double>(this->min_threshold_));

  if (threshold <= 0.0 || std::fabs(raw_total - median) <= threshold) {
    return raw_values;
  }

  // Outlier: write the median back into the window so a single spike does
  // not poison future detections (canonical Hampel identifier behavior).
  this->window_.back() = median;

  if (std::fabs(raw_total) < 1e-9) {
    const float share = static_cast<float>(median / raw_values.size());
    return std::vector<float>(raw_values.size(), share);
  }
  const double ratio = median / raw_total;
  std::vector<float> out;
  out.reserve(raw_values.size());
  for (float v : raw_values)
    out.push_back(static_cast<float>(v * ratio));
  return out;
}

}  // namespace ct002
}  // namespace esphome
