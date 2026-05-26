#pragma once

#include <deque>

#include "wrapper_base.h"

namespace esphome {
namespace ct002 {

// Rolling-median outlier filter for sum-of-phases power readings. Mirrors
// src/astrameter/powermeter/wrappers/hampel.py.
//
// Maintains a rolling window of the most recent `window` totals. When the
// next total lies more than `n_sigma * 1.4826 * MAD` away from the window
// median (with a floor of `min_threshold` watts to handle the constant-
// signal MAD=0 degenerate case), the sample is treated as an outlier: the
// reported total is replaced by the median and per-phase values are
// redistributed proportionally (equal split when |raw_total| is near zero).
// The window entry itself is mutated to the median so a single spike does
// not poison future detections.
class HampelPowermeter : public PowermeterWrapper {
 public:
  static constexpr double MAD_SCALE = 1.4826;

  HampelPowermeter(Powermeter *wrapped, size_t window, float n_sigma, float min_threshold);

  std::vector<float> get_powermeter_watts() override;
  void reset() override;

 protected:
  std::deque<double> window_;
  size_t window_size_;
  float n_sigma_;
  float min_threshold_;
};

}  // namespace ct002
}  // namespace esphome
