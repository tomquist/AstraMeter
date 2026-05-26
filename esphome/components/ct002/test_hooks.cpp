// Test-only control channel for the ct002 component. Entirely gated behind
// USE_CT002_TEST_HOOKS, which is defined only when the YAML sets
// `test_control_port:` (see __init__.py). Production firmware never compiles
// any of this.
//
// It lets a host-platform end-to-end test drive the emulator deterministically
// over a second UDP socket, mirroring what the in-process Python e2e harness
// can do directly:
//
//   grid <l1> [l2] [l3]   inject grid power (W) straight into the sensor
//                         cache, synchronously, bypassing sensor update
//                         intervals. Stamps the values "now" so the
//                         freshness check passes.
//   clock_set <seconds>   engage the mock clock at an absolute value
//   clock_advance <secs>  engage the mock clock and advance it (deltas are
//                         what the balancer/saturation/eviction/dedup care
//                         about, so this is the usual driver)
//   clock_real            disengage the mock clock (back to millis())
//
// Every command replies with "ok ..." (or "err ...") to the sender so the
// test can synchronise (send command, await ack, then poll the CT002 port).

#include "ct002.h"

#ifdef USE_CT002_TEST_HOOKS

#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace ct002 {

static const char *const TAG_CTRL = "ct002.test";

void CT002Component::start_control_server_() {
  if (this->control_port_ == 0) return;
  this->control_socket_ = socket::socket_ip(SOCK_DGRAM, IPPROTO_UDP);
  if (this->control_socket_ == nullptr) {
    ESP_LOGW(TAG_CTRL, "Could not allocate control socket");
    return;
  }
  int enable = 1;
  this->control_socket_->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
  this->control_socket_->setblocking(false);
  struct sockaddr_storage server;
  socklen_t server_len = socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&server),
                                                  sizeof(server), this->control_port_);
  if (server_len == 0 ||
      this->control_socket_->bind(reinterpret_cast<struct sockaddr *>(&server), server_len) < 0) {
    ESP_LOGW(TAG_CTRL, "Could not bind control socket to port %u", this->control_port_);
    this->control_socket_ = nullptr;
    return;
  }
  ESP_LOGCONFIG(TAG_CTRL, "ct002 TEST control server on port %u (test hooks enabled)",
                this->control_port_);
}

void CT002Component::pump_control_() {
  for (int i = 0; i < 8; ++i) {
    char buf[128];
    struct sockaddr_storage from{};
    socklen_t from_len = sizeof(from);
    ssize_t n = this->control_socket_->recvfrom(buf, sizeof(buf) - 1,
                                                reinterpret_cast<struct sockaddr *>(&from),
                                                &from_len);
    if (n <= 0) break;
    buf[n] = '\0';
    // Trim trailing whitespace/newline.
    while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\r' || buf[n - 1] == ' ')) {
      buf[--n] = '\0';
    }
    this->handle_control_command_(std::string(buf), from, from_len);
  }
}

void CT002Component::handle_control_command_(const std::string &cmd,
                                             const struct sockaddr_storage &from,
                                             socklen_t from_len) {
  std::string reply;
  char verb[24] = {0};
  double a = 0.0, b = 0.0, c = 0.0;
  const int matched = std::sscanf(cmd.c_str(), "%23s %lf %lf %lf", verb, &a, &b, &c);

  if (matched >= 1 && std::strcmp(verb, "grid") == 0) {
    // Inject grid power straight into the sensor cache, stamped now so the
    // SensorBackedPowermeter freshness check passes. matched-1 values given;
    // missing phases default to 0.
    const uint32_t now_ms = ::esphome::millis();
    const double vals[3] = {a, b, c};
    for (uint8_t p = 0; p < 3; ++p) {
      this->raw_values_[p] = (matched - 1 > p) ? static_cast<float>(vals[p]) : 0.0f;
      this->raw_stamp_ms_[p] = now_ms;
    }
    char tmp[64];
    std::snprintf(tmp, sizeof(tmp), "ok grid %.1f %.1f %.1f", this->raw_values_[0],
                  this->raw_values_[1], this->raw_values_[2]);
    reply = tmp;
  } else if (matched >= 2 && std::strcmp(verb, "clock_set") == 0) {
    this->mock_clock_enabled_ = true;
    this->mock_clock_seconds_ = a;
    char tmp[48];
    std::snprintf(tmp, sizeof(tmp), "ok clock_set %.3f", this->mock_clock_seconds_);
    reply = tmp;
  } else if (matched >= 2 && std::strcmp(verb, "clock_advance") == 0) {
    this->mock_clock_enabled_ = true;
    this->mock_clock_seconds_ += a;
    char tmp[48];
    std::snprintf(tmp, sizeof(tmp), "ok clock_advance %.3f", this->mock_clock_seconds_);
    reply = tmp;
  } else if (matched >= 1 && std::strcmp(verb, "clock_real") == 0) {
    this->mock_clock_enabled_ = false;
    reply = "ok clock_real";
  } else {
    reply = "err unknown command";
  }

  this->control_socket_->sendto(reply.data(), reply.size(), 0,
                                reinterpret_cast<const struct sockaddr *>(&from), from_len);
}

}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_TEST_HOOKS
