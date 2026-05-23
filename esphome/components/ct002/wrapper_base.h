#pragma once

#include <cstddef>
#include <vector>

namespace esphome {
namespace ct002 {

// Mirrors src/astrameter/powermeter/base.py. Sync (not async) — the Python
// async story exists only for polling-source I/O; wrapper bodies contain no
// `await`, so a sync C++ port is faithful.
class Powermeter {
 public:
  virtual ~Powermeter() = default;

  // Per-phase power readings in watts. Returns 1-element (single-phase) or
  // 3-element (three-phase) vectors; an empty vector signals "unavailable".
  virtual std::vector<float> get_powermeter_watts() = 0;

  // Raw (unfiltered) per-phase readings — used by mqtt_insights to publish
  // the unsmoothed signal alongside the filtered one. The base wrapper
  // delegates to upstream's raw, so the call chain unwinds to the source.
  virtual std::vector<float> get_powermeter_watts_raw() {
    return this->get_powermeter_watts();
  }

  // Clears any time-windowed state. Called by CT002 after long gaps where
  // accumulated state would be stale (e.g. Wi-Fi reconnect, sensor outage).
  virtual void reset() {}
};

// Decorator base. Holds a non-owning pointer to the upstream Powermeter;
// CT002 owns the actual wrapper instances via std::unique_ptr<Powermeter>
// so lifetimes match the pipeline construction order.
class PowermeterWrapper : public Powermeter {
 public:
  explicit PowermeterWrapper(Powermeter *wrapped) : wrapped_(wrapped) {}

  std::vector<float> get_powermeter_watts_raw() override {
    return this->wrapped_->get_powermeter_watts_raw();
  }
  void reset() override { this->wrapped_->reset(); }

 protected:
  Powermeter *wrapped_;
};

}  // namespace ct002
}  // namespace esphome
