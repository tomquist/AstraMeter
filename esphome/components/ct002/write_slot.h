#pragma once

// The dashboard's write handover, kept out of dashboard.cpp so it can be
// tested for real.
//
// On an ESP32 one side of this runs on the httpd task and the other on the
// main loop. dashboard.cpp cannot be built for the host platform (ESPHome has
// no web server there), so anything living in it gets compile coverage and
// nothing more — and this is the part where the two tasks actually meet.
//
// The caller holds its own mutex across every method except completed(), which
// is the one a waiting request spins on: esphome::Mutex on the device,
// std::mutex in the host test. This type owns only the bookkeeping — which
// write is where, and which one finished.

#include <atomic>
#include <cstdint>

namespace esphome {
namespace ct002 {
namespace dashboard {

/// One write in flight at a time, each identified by a ticket so a waiter can
/// never mistake somebody else's completion for its own.
template<typename Value> class WriteSlot {
 public:
  /// Submit *value*. Returns its ticket, or 0 when another write is still
  /// going — either waiting to be taken or being applied right now.
  ///
  /// Refusing while one is being applied is the point: the slot is empty
  /// during that window, so a second write could otherwise reserve it and
  /// then read the first one's result as its own.
  uint32_t reserve(const Value &value) {
    if (this->pending_ || this->in_flight_) return 0;
    this->value_ = value;
    this->ticket_ = this->next_ticket_;
    if (++this->next_ticket_ == 0) this->next_ticket_ = 1;  // 0 means "none"
    this->pending_ = true;
    return this->ticket_;
  }

  /// Main loop: take the waiting write, if there is one. It counts as in
  /// flight until complete().
  bool take(Value *value, uint32_t *ticket) {
    if (!this->pending_) return false;
    *value = this->value_;
    *ticket = this->ticket_;
    this->pending_ = false;
    this->in_flight_ = true;
    return true;
  }

  /// Main loop: *ticket* has been applied, or was not applicable.
  void complete(uint32_t ticket, bool applied) {
    this->applied_ = applied;
    this->in_flight_ = false;
    this->completed_.store(ticket);
  }

  /// Give up on *ticket*. True when it was still waiting and is now cancelled;
  /// false once the loop has taken it, since a write being applied cannot be
  /// recalled — and false for a ticket that is not the one in the slot, so a
  /// request that timed out can never cancel somebody else's.
  bool withdraw(uint32_t ticket) {
    if (!this->pending_ || this->ticket_ != ticket) return false;
    this->pending_ = false;
    return true;
  }

  /// The last completed ticket. The one method safe to call without the
  /// caller's mutex, because it is what the waiting httpd task spins on.
  uint32_t completed() const { return this->completed_.load(); }

  /// Whether *ticket* is the write that finished, and what it did.
  bool result(uint32_t ticket, bool *applied) const {
    if (this->completed_.load() != ticket) return false;
    *applied = this->applied_;
    return true;
  }

 private:
  Value value_{};
  uint32_t ticket_{0};
  uint32_t next_ticket_{1};
  bool pending_{false};
  bool in_flight_{false};
  bool applied_{false};
  std::atomic<uint32_t> completed_{0};
};

}  // namespace dashboard
}  // namespace ct002
}  // namespace esphome
