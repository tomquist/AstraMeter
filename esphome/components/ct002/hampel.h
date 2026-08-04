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
// redistributed proportionally (equal split when |raw_total| is too small to
// scale from -- see MAX_SCALE_RATIO).
//
// The window always holds the raw totals, including rejected ones -- what the
// canonical Hampel identifier does, and what lets the filter follow a real
// change. Writing the median back over a rejected sample instead, as this
// once did, makes the window converge to a constant, which drives MAD to
// zero, which pins the threshold at min_threshold, after which every sample
// more than that from the old median is rejected: a sustained change froze
// the reading at its pre-change value indefinitely.
class HampelPowermeter : public PowermeterWrapper {
 public:
  static constexpr double MAD_SCALE = 1.4826;

  // How far the per-phase values may be scaled up to make their sum equal the
  // median. Beyond this the split is not worth preserving: a rejected sample
  // whose total is near zero would otherwise be multiplied by
  // median / raw_total, turning a phase reading a couple of watts into tens
  // of kilowatts. Only ever reached on the dropout side -- a spike scales
  // down -- so the cap costs nothing in the normal case.
  static constexpr double MAX_SCALE_RATIO = 4.0;

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
