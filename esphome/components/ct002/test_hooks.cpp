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
//   sensor_stale          back-date the sensor stamps past max_sensor_age so
//                         the next read reports the grid sensor unavailable
//                         (SensorBackedPowermeter returns {}), which the
//                         handler maps to [0,0,0] — the meter-outage path.
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

bool CT002Component::apply_cfg_(const std::string &key, double v) {
  const auto f = static_cast<float>(v);
  // Balancer config fields.
  if (key == "fair_distribution") this->balancer_cfg_.fair_distribution = (v != 0.0);
  else if (key == "balance_gain") this->balancer_cfg_.balance_gain = f;
  else if (key == "balance_deadband") this->balancer_cfg_.balance_deadband = f;
  else if (key == "error_boost_threshold") this->balancer_cfg_.error_boost_threshold = f;
  else if (key == "error_boost_max") this->balancer_cfg_.error_boost_max = f;
  else if (key == "error_reduce_threshold") this->balancer_cfg_.error_reduce_threshold = f;
  else if (key == "max_correction_per_step") this->balancer_cfg_.max_correction_per_step = f;
  else if (key == "max_target_step") this->balancer_cfg_.max_target_step = f;
  else if (key == "min_efficient_power") this->balancer_cfg_.min_efficient_power = f;
  else if (key == "probe_min_power") this->balancer_cfg_.probe_min_power = f;
  else if (key == "efficiency_rotation_interval") this->balancer_cfg_.efficiency_rotation_interval = f;
  else if (key == "efficiency_fade_alpha") this->balancer_cfg_.efficiency_fade_alpha = f;
  else if (key == "efficiency_saturation_threshold")
    this->balancer_cfg_.efficiency_saturation_threshold = f;
  else if (key == "min_dc_output") this->balancer_cfg_.min_dc_output = f;
  // Saturation tracker fields.
  else if (key == "saturation_enabled") this->saturation_enabled_ = (v != 0.0);
  else if (key == "saturation_alpha") this->saturation_alpha_ = f;
  else if (key == "saturation_min_target") this->saturation_min_target_ = f;
  else if (key == "saturation_decay_factor") this->saturation_decay_factor_ = f;
  else if (key == "saturation_grace_seconds") this->saturation_grace_seconds_ = f;
  else if (key == "saturation_stall_timeout_seconds") this->saturation_stall_timeout_seconds_ = f;
  else return false;
  // Rebuild so the change takes effect (resets balancer state — fine before
  // a scenario begins).
  this->build_balancer_();
  return true;
}

void CT002Component::handle_control_command_(const std::string &cmd,
                                             const struct sockaddr_storage &from,
                                             socklen_t from_len) {
  std::string reply;

  // `cfg <key> <value>` carries a string key, so parse it separately from
  // the numeric commands below.
  if (cmd.rfind("cfg ", 0) == 0) {
    char key[48] = {0};
    double value = 0.0;
    if (std::sscanf(cmd.c_str() + 4, "%47s %lf", key, &value) == 2 &&
        this->apply_cfg_(std::string(key), value)) {
      reply = std::string("ok cfg ") + key;
    } else {
      reply = "err cfg (bad key or parse)";
    }
    this->control_socket_->sendto(reply.data(), reply.size(), 0,
                                  reinterpret_cast<const struct sockaddr *>(&from), from_len);
    return;
  }

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
  } else if (matched >= 2 && std::strcmp(verb, "dedupe") == 0) {
    // Set the dedup window (ms) at runtime — sims run with 0 (off), the
    // dedup scenario sets it to its window. Mirrors the Python harness
    // setting ct002._dedup._window.
    this->dedupe_window_ms_ = static_cast<uint32_t>(a);
    char tmp[48];
    std::snprintf(tmp, sizeof(tmp), "ok dedupe %u", this->dedupe_window_ms_);
    reply = tmp;
  } else if (matched >= 1 && std::strcmp(verb, "sensor_stale") == 0) {
    // Force the SensorBackedPowermeter freshness check to fail: back-date the
    // per-phase stamps past max_sensor_age_ms_ so the next read reports the
    // sensor unavailable (returns {}), which handle_request_ maps to [0,0,0].
    // Deterministic — no real-time sleep needed. A subsequent `grid` command
    // re-stamps "now" and restores freshness.
    const uint32_t now = ::esphome::millis();
    const uint32_t past = now - (this->max_sensor_age_ms_ + 1000);  // unsigned wrap is fine
    // Iterate over the active phases the freshness check reads (mirrors
    // SensorBackedPowermeter::get_powermeter_watts in sensor_backed.cpp).
    for (uint8_t p = 0; p < this->num_phases_; ++p) this->raw_stamp_ms_[p] = past;
    reply = "ok sensor_stale";
  } else if (matched >= 1 && std::strcmp(verb, "force_rotation") == 0) {
    // Mirror ct002.force_efficiency_rotation() for the rotation tests.
    this->force_balancer_rotation();
    reply = "ok force_rotation";
  } else if (matched >= 1 && std::strcmp(verb, "dump") == 0) {
    // Serialize the internal state the Python e2e suites read directly, so
    // the black-box binary can be asserted on the same way. Pipe-delimited:
    //   ok|smooth_target=<f>|<cid>,<phase>,<last_instructed>,<last_target>,<sat>,<active>,<manual>,<reported>|...
    std::string s = "ok|smooth_target=";
    char num[48];
    std::snprintf(num, sizeof(num), "%.3f",
                  static_cast<double>(this->last_smooth_target_.value_or(0.0f)));
    s += num;
    for (const auto &kv : this->consumers_) {
      const auto &consumer = kv.second;
      if (consumer.timestamp <= 0.0) continue;
      const double sat = this->balancer_ ? this->balancer_->get_saturation(kv.first) : 0.0;
      double last_target = 0.0;
      if (this->balancer_) {
        auto lt = this->balancer_->get_last_target(kv.first);
        if (lt.has_value()) last_target = *lt;
      }
      s += "|";
      s += kv.first;
      std::snprintf(num, sizeof(num), ",%s,%.1f,%.1f,%.4f,%d,%d,%.1f", consumer.phase.c_str(),
                    static_cast<double>(consumer.last_instructed_power), last_target, sat,
                    consumer.active ? 1 : 0, consumer.manual_enabled ? 1 : 0,
                    static_cast<double>(consumer.power));
      s += num;
    }
    reply = s;
  } else {
    reply = "err unknown command";
  }

  this->control_socket_->sendto(reply.data(), reply.size(), 0,
                                reinterpret_cast<const struct sockaddr *>(&from), from_len);
}

}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_TEST_HOOKS
