#pragma once

// The cloud reporter's HTTP handover, kept out of cloud_reporting.cpp so it can
// be tested for real.
//
// On an ESP32 the main loop submits a GET and a worker task runs it, so a slow
// cloud never holds up the batteries' UDP replies: a request can take seconds,
// and the main loop is what answers the batteries. cloud_reporting.cpp is only
// built when the YAML asks for it, so the part where the two tasks meet lives
// here, free of ESPHome deps.
//
// The caller holds its own mutex across every method: esphome::Mutex on the
// device, std::mutex in the host test. This type owns only the bookkeeping.

#include <string>
#include <utility>

namespace esphome {
namespace ct002 {
namespace cloud_reporting {

struct HttpResult {
  // The HTTP status, or -1 when the request never completed.
  int status{-1};
  // The start of the response body, when it was asked for.
  std::string body;
};

/// One GET at a time: queued by the main loop, run by the worker, collected by
/// the main loop again.
class HttpSlot {
 public:
  /// Main loop: queue a GET of *url*. False while the last one is still queued,
  /// running or uncollected, so a result can never belong to another request.
  bool submit(const std::string &url, bool want_body) {
    if (this->state_ != State::IDLE) return false;
    this->url_ = url;
    this->want_body_ = want_body;
    this->result_ = HttpResult{};
    this->state_ = State::QUEUED;
    return true;
  }

  /// Worker: take the queued GET. It counts as running until finish().
  bool take(std::string *url, bool *want_body) {
    if (this->state_ != State::QUEUED) return false;
    *url = this->url_;
    *want_body = this->want_body_;
    this->state_ = State::RUNNING;
    return true;
  }

  /// Worker: the running GET is done.
  void finish(HttpResult result) {
    if (this->state_ != State::RUNNING) return;
    this->result_ = std::move(result);
    this->state_ = State::DONE;
  }

  /// Main loop: the finished GET's result, which frees the slot.
  bool collect(HttpResult *out) {
    if (this->state_ != State::DONE) return false;
    *out = std::move(this->result_);
    this->state_ = State::IDLE;
    return true;
  }

  /// Whether a GET is queued, running or waiting to be collected.
  bool busy() const { return this->state_ != State::IDLE; }

 protected:
  enum class State { IDLE, QUEUED, RUNNING, DONE };
  State state_{State::IDLE};
  std::string url_;
  bool want_body_{false};
  HttpResult result_;
};

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
