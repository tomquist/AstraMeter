#include "ct002.h"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdlib>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "hampel.h"
#include "pid.h"
#include "smoothing.h"
#include "protocol.h"

namespace esphome {
namespace ct002 {

static const char *const TAG = "ct002";

namespace {

// Round-half-to-even, matching Python's built-in round() (which the
// Python emulator uses for every wire value). C++ std::lround is
// round-half-AWAY-from-zero, so the two diverge on exact .5 ties
// (round(2.5)==2 in Python, lround(2.5)==3). Reachable on the wire when a
// filtered sensor value lands on x.5. Implemented explicitly rather than
// via std::lrint so we don't depend on the global FP rounding mode.
long round_half_even(double v) {
  const double fl = std::floor(v);
  const double frac = v - fl;
  if (frac < 0.5) return static_cast<long>(fl);
  if (frac > 0.5) return static_cast<long>(fl) + 1;
  const long fll = static_cast<long>(fl);
  return (fll % 2 == 0) ? fll : fll + 1;  // tie → nearest even
}

// Mirror of src/astrameter/ct002/protocol.py::parse_int → Python int():
// strips surrounding whitespace and requires the ENTIRE remaining string
// to be a valid base-10 integer, else returns the default. strtol alone
// accepts trailing garbage ("5abc"→5, "5.7"→5), which Python's int()
// rejects.
int parse_int_strict(const std::string &s, int default_value) {
  const char *begin = s.c_str();
  char *end = nullptr;
  errno = 0;
  const long parsed = std::strtol(begin, &end, 10);
  if (end == begin || errno != 0) return default_value;
  while (*end != '\0') {
    if (!std::isspace(static_cast<unsigned char>(*end))) return default_value;
    ++end;
  }
  return static_cast<int>(parsed);
}

// Mirror of Python's _bucket_for_phase: A/B/C → their buckets, "D" → the
// combined ABC bucket, anything else (the normalized "0") → x.
size_t bucket_index_for_phase(const std::string &phase) {
  if (phase == "A") return BUCKET_A;
  if (phase == "B") return BUCKET_B;
  if (phase == "C") return BUCKET_C;
  if (phase == "D") return BUCKET_ABC;
  return BUCKET_X;
}

}  // namespace

// Wall-clock seconds for balancer/saturation accounting. Uses ESPHome's
// monotonic millis() because absolute wall time isn't available on bare
// metal at boot; the balancer only cares about deltas, so a monotonic
// reference is fine. Under the test-hooks build the e2e harness can engage
// a mock clock for deterministic time-gated behaviour.
double CT002Component::now_seconds_() const {
#ifdef USE_CT002_TEST_HOOKS
  if (this->mock_clock_enabled_) return this->mock_clock_seconds_;
#endif
  return static_cast<double>(::esphome::millis()) / 1000.0;
}

// (Re)build the load balancer from the current balancer_cfg_ + saturation_*
// members. Called once from setup(); the test-hooks `cfg` control command
// calls it again after mutating a config field so an e2e test can run
// scenarios under different balancer/saturation settings without a reflash.
// Rebuilding resets balancer state — fine before a scenario starts.
void CT002Component::build_balancer_() {
  this->balancer_ = std::make_unique<LoadBalancer>(
      this->balancer_cfg_, this->saturation_alpha_, this->saturation_min_target_,
      this->saturation_decay_factor_, this->saturation_grace_seconds_,
      this->saturation_stall_timeout_seconds_, this->saturation_enabled_,
      [this]() { return this->now_seconds_(); },
      [this]() {
        for (auto &p : this->pipeline_) p->reset();
      });
}

void CT002Component::enable_hampel(size_t window, float n_sigma, float min_threshold) {
  this->hampel_cfg_ = HampelCfg{window, n_sigma, min_threshold};
}
void CT002Component::enable_smoothing(float alpha, float max_step) {
  this->smoothing_cfg_ = SmoothingCfg{alpha, max_step};
}
void CT002Component::enable_deadband(float deadband) { this->deadband_threshold_ = deadband; }
void CT002Component::enable_pid(float kp, float ki, float kd, float output_max, PidMode mode) {
  this->pid_cfg_ = PidCfg{kp, ki, kd, output_max, mode};
}

void CT002Component::setup() {
  this->num_phases_ = (this->power_sensor_l2_ != nullptr) ? 3 : 1;

  auto cache = [this](size_t i, float v) {
    this->raw_values_[i] = v;
    this->raw_stamp_ms_[i] = ::esphome::millis();
  };
  this->power_sensor_l1_->add_on_state_callback([cache](float v) { cache(0, v); });
  if (this->power_sensor_l2_ != nullptr)
    this->power_sensor_l2_->add_on_state_callback([cache](float v) { cache(1, v); });
  if (this->power_sensor_l3_ != nullptr)
    this->power_sensor_l3_->add_on_state_callback([cache](float v) { cache(2, v); });

  // Build the filter pipeline. Order matches Python config_loader.py:
  // SensorBacked → Hampel → Smoothed → Deadband → PID.
  auto head = std::make_unique<SensorBackedPowermeter>(this->num_phases_, &this->raw_values_,
                                                      &this->raw_stamp_ms_,
                                                      this->max_sensor_age_ms_);
  Powermeter *current = head.get();
  this->pipeline_.push_back(std::move(head));
  if (this->hampel_cfg_.has_value()) {
    auto w = std::make_unique<HampelPowermeter>(current, this->hampel_cfg_->window,
                                               this->hampel_cfg_->n_sigma,
                                               this->hampel_cfg_->min_threshold);
    current = w.get();
    this->pipeline_.push_back(std::move(w));
  }
  if (this->smoothing_cfg_.has_value()) {
    auto w = std::make_unique<SmoothedPowermeter>(current, this->smoothing_cfg_->alpha,
                                                 this->smoothing_cfg_->max_step);
    current = w.get();
    this->pipeline_.push_back(std::move(w));
  }
  if (this->deadband_threshold_.has_value()) {
    auto w = std::make_unique<DeadbandPowermeter>(current, *this->deadband_threshold_);
    current = w.get();
    this->pipeline_.push_back(std::move(w));
  }
  if (this->pid_cfg_.has_value()) {
    auto w = std::make_unique<PidPowermeter>(current, this->pid_cfg_->kp, this->pid_cfg_->ki,
                                            this->pid_cfg_->kd, this->pid_cfg_->output_max,
                                            this->pid_cfg_->mode);
    current = w.get();
    this->pipeline_.push_back(std::move(w));
  }
  this->pipeline_head_ = current;

  this->build_balancer_();

  this->start_udp_server_();
#ifdef USE_CT002_TEST_HOOKS
  this->start_control_server_();
#endif

  // Consumer eviction — fires every 5 s and evicts anything older than its
  // TTL (the configured consumer_ttl, or by default ~2 missed poll cycles —
  // see consumer_ttl_for_). Mirrors Python's _cleanup_consumers loop.
  // Without this the consumers_ map grows unbounded across battery turnover
  // and the mqtt_insights "offline" availability is never published.
  this->set_interval("ct002_evict", 5000, [this]() { this->evict_stale_consumers_(); });

  ESP_LOGCONFIG(TAG, "CT002 setup: %u phase(s), ct_type=%s, udp_port=%u",
                this->num_phases_, this->ct_type_.c_str(), this->udp_port_);
}

void CT002Component::start_udp_server_() {
  this->socket_ = socket::socket_ip(SOCK_DGRAM, IPPROTO_UDP);
  if (this->socket_ == nullptr) {
    ESP_LOGW(TAG, "Could not allocate UDP socket");
    this->mark_failed();
    return;
  }
  int enable = 1;
  this->socket_->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
  // SO_BROADCAST is best-effort — some LWIP variants don't expose it; if
  // the call fails the unicast path still works.
  this->socket_->setsockopt(SOL_SOCKET, SO_BROADCAST, &enable, sizeof(enable));
  if (this->socket_->setblocking(false) < 0) {
    ESP_LOGW(TAG, "Could not put UDP socket into non-blocking mode");
  }
  struct sockaddr_storage server;
  socklen_t server_len = socket::set_sockaddr_any(reinterpret_cast<struct sockaddr *>(&server),
                                                  sizeof(server), this->udp_port_);
  if (server_len == 0) {
    ESP_LOGW(TAG, "Could not set bind address for UDP socket");
    this->mark_failed();
    return;
  }
  if (this->socket_->bind(reinterpret_cast<struct sockaddr *>(&server), server_len) < 0) {
    ESP_LOGW(TAG, "Could not bind UDP socket to port %u: errno=%d", this->udp_port_, errno);
    this->mark_failed();
    return;
  }
  ESP_LOGCONFIG(TAG, "CT002 UDP server listening on port %u", this->udp_port_);
}

void CT002Component::loop() {
  if (this->socket_ != nullptr) this->pump_udp_();
#ifdef USE_CT002_TEST_HOOKS
  if (this->control_socket_ != nullptr) this->pump_control_();
#endif
}

void CT002Component::pump_udp_() {
  // Cap iterations to avoid starving other components if a burst arrives.
  for (int i = 0; i < 32; ++i) {
    uint8_t buf[256];
    struct sockaddr_storage from{};
    socklen_t from_len = sizeof(from);
    ssize_t n = this->socket_->recvfrom(buf, sizeof(buf),
                                        reinterpret_cast<struct sockaddr *>(&from), &from_len);
    if (n <= 0) break;
    char ip_str[24]{};
    uint16_t port = 0;
    if (from.ss_family == AF_INET) {
      auto *sin = reinterpret_cast<struct sockaddr_in *>(&from);
      const uint8_t *octets = reinterpret_cast<const uint8_t *>(&sin->sin_addr);
      std::snprintf(ip_str, sizeof(ip_str), "%u.%u.%u.%u", octets[0], octets[1], octets[2],
                    octets[3]);
      port = static_cast<uint16_t>((sin->sin_port >> 8) | (sin->sin_port << 8));
    }
    this->handle_request_(buf, static_cast<size_t>(n), std::string(ip_str), port);
  }
}

void CT002Component::handle_request_(const uint8_t *data, size_t len,
                                     const std::string &addr_ip, uint16_t addr_port) {
  std::string error;
  auto parsed = parse_request(data, len, &error);
  if (!parsed) {
    ESP_LOGD(TAG, "Invalid CT002 request from %s: %s", addr_ip.c_str(), error.c_str());
    return;
  }
  const auto &fields = *parsed;
  if (fields.size() < 4) return;
  if (!this->validate_ct_mac_(fields)) return;

  const std::string meter_mac = fields[1];
  const std::string consumer_id = this->consumer_key_(meter_mac, addr_ip, addr_port);
  std::string reported_phase = fields.size() > 4 ? fields[4] : "";
  for (auto &c : reported_phase) c = static_cast<char>(std::toupper(c));
  // Trim whitespace (Python uses str.strip().upper()).
  while (!reported_phase.empty() && std::isspace(static_cast<unsigned char>(reported_phase.front())))
    reported_phase.erase(reported_phase.begin());
  while (!reported_phase.empty() && std::isspace(static_cast<unsigned char>(reported_phase.back())))
    reported_phase.pop_back();
  const bool in_inspection_mode = reported_phase != "A" && reported_phase != "B" && reported_phase != "C";
  int reported_power = 0;
  if (fields.size() > 5) {
    // Match Python's parse_int (int()): reject trailing garbage / float
    // syntax instead of strtol's lenient prefix parse.
    reported_power = parse_int_strict(fields[5], 0);
  }
  // Optional 7th field: "participate" flag (newer senders, e.g. B2500).
  // Absent/empty defaults to participating; an explicit 0 opts out.
  bool participates = true;
  if (fields.size() > 6) {
    std::string p = fields[6];
    while (!p.empty() && std::isspace(static_cast<unsigned char>(p.front()))) p.erase(p.begin());
    while (!p.empty() && std::isspace(static_cast<unsigned char>(p.back()))) p.pop_back();
    participates = p.empty() || parse_int_strict(p, 1) != 0;
  }

  // Deduplication — drop repeat polls from the same consumer inside the
  // configured window (keyed by consumer_id so retransmits are suppressed
  // regardless of source UDP port). Mirrors ct002.py:636-644. Disabled
  // (default window 0) means every datagram is processed.
  if (!this->dedup_should_process_(consumer_id)) {
    ESP_LOGD(TAG, "Ignoring duplicate request from %s (consumer=%s) — dedupe window",
             addr_ip.c_str(), consumer_id.c_str());
    return;
  }

  const std::string meter_dev_type = fields[0];
  // Store the phase exactly as reported: "D" selects the combined ABC bucket
  // and any inspection marker is normalized to "0" (the x bucket) inside
  // update_consumer_report_ — forcing "A" here would mis-count inspection
  // and combined reporters into phase A (issue #460).
  this->update_consumer_report_(consumer_id, reported_phase,
                                static_cast<float>(reported_power), meter_dev_type, addr_ip,
                                participates);

  // Read the filter pipeline → balancer.
  std::vector<float> values;
  if (this->pipeline_head_) values = this->pipeline_head_->get_powermeter_watts();
  // An empty reading means the powermeter is unavailable (sensor aged out /
  // outage). The {0,0,0} below is then a *sentinel*, not a real reading: skip
  // active control so the stateful controller (grid-state predictor, saturation
  // EMA, ...) never treats a fabricated zero grid as a fresh sample and emits a
  // non-zero delta from its internal state — the wind-up issue #403 guards
  // against. The battery holds on the literal zero adjustment instead. Mirrors
  // ct002.py _handle_request.
  const bool meter_ok = !values.empty();
  if (values.empty()) values = {0.0f, 0.0f, 0.0f};
  while (values.size() < 3) values.push_back(0.0f);
  values.resize(3);

  if (this->active_control_ && !in_inspection_mode && meter_ok) {
    values = this->compute_smooth_target_(values, consumer_id);
  }
  while (values.size() < 3) values.push_back(0.0f);
  values.resize(3);

  if (!in_inspection_mode) {
    auto &consumer = this->get_consumer_(consumer_id);
    size_t phase_idx = 0;
    if (consumer.phase == "B") phase_idx = 1;
    else if (consumer.phase == "C") phase_idx = 2;
    consumer.last_instructed_power = reported_power + values[phase_idx];
  }

  auto response_fields = this->build_response_fields_(fields, values);
  auto payload = build_payload(response_fields);

  if (this->socket_ != nullptr && !payload.empty()) {
    struct sockaddr_storage to{};
    socklen_t to_len = socket::set_sockaddr(reinterpret_cast<struct sockaddr *>(&to), sizeof(to),
                                            addr_ip, addr_port);
    if (to_len > 0) {
      this->socket_->sendto(payload.data(), payload.size(), 0,
                            reinterpret_cast<struct sockaddr *>(&to), to_len);
    }
  }

  // Fire listeners after a successful reply so mqtt_insights can publish
  // fresh state. Skipped during inspection mode since per-phase grid_power
  // is not yet meaningful (consumer is still discovering its phase).
  if (!in_inspection_mode) {
    for (auto &cb : this->consumer_event_listeners_) cb(consumer_id);
  }
}

std::string CT002Component::consumer_key_(const std::string &meter_mac,
                                          const std::string &addr_ip, uint16_t addr_port) const {
  if (!meter_mac.empty()) {
    std::string lower(meter_mac);
    for (auto &c : lower) c = static_cast<char>(std::tolower(c));
    return lower;
  }
  return addr_ip + ":" + std::to_string(addr_port);
}

Consumer &CT002Component::get_consumer_(const std::string &consumer_id) {
  auto it = this->consumers_.find(consumer_id);
  if (it == this->consumers_.end()) {
    Consumer c;
    c.consumer_id = consumer_id;
    return this->consumers_.emplace(consumer_id, std::move(c)).first->second;
  }
  return it->second;
}

void CT002Component::update_consumer_report_(const std::string &consumer_id,
                                            const std::string &phase, float power,
                                            const std::string &device_type,
                                            const std::string &source_ip, bool participates) {
  std::string normalized_phase = phase;
  for (auto &c : normalized_phase) c = static_cast<char>(std::toupper(c));
  if (normalized_phase != "A" && normalized_phase != "B" && normalized_phase != "C" &&
      normalized_phase != "D") {
    // Anything else ("0", empty, future markers) is the unassigned/
    // inspection state; store the wire's canonical "0" so aggregation
    // routes it to the x bucket instead of inventing a phase (issue #460).
    normalized_phase = "0";
  }
  auto &consumer = this->get_consumer_(consumer_id);
  const double now = now_seconds_();
  // Capture the prior phase BEFORE the update. Python keys "is there a
  // previous phase?" off timestamp>0, not the phase string (a fresh
  // Consumer defaults to "A"), so a never-seen consumer reports None.
  const bool had_prior = consumer.timestamp > 0.0;
  const std::string previous_phase = had_prior ? consumer.phase : std::string();
  // EMA-smoothed poll interval — mirrors Python's _update_consumer_report
  // (ct002.py:298-307). Seeded on the second poll; round-trip to 0.1s
  // resolution to match what the Python service publishes to MQTT.
  if (consumer.timestamp > 0.0) {
    const float raw_interval = static_cast<float>(now - consumer.timestamp);
    // Python rounds poll_interval to 1 decimal with round(x, 1) — banker's.
    auto round_tenth = [](float v) {
      return static_cast<float>(round_half_even(static_cast<double>(v) * 10.0)) / 10.0f;
    };
    if (!consumer.poll_interval.has_value()) {
      consumer.poll_interval = round_tenth(raw_interval);
    } else {
      consumer.poll_interval = round_tenth(POLL_INTERVAL_EMA_ALPHA * raw_interval +
                                           (1.0f - POLL_INTERVAL_EMA_ALPHA) *
                                               *consumer.poll_interval);
    }
  }
  consumer.phase = normalized_phase;
  consumer.power = power;
  consumer.timestamp = now;
  consumer.device_type = device_type;
  consumer.participates = participates;
  if (!source_ip.empty()) consumer.last_ip = source_ip;

  // Phase detected (new battery) / phase changed (re-detected on a
  // different leg) — mirrors Python's _update_consumer_report. Only fires
  // for a declared A/B/C/D phase that differs from the prior one;
  // inspection-mode polls (normalized to "0") never trigger it.
  const auto is_declared = [](const std::string &p) {
    return p == "A" || p == "B" || p == "C" || p == "D";
  };
  if (is_declared(normalized_phase) && previous_phase != normalized_phase) {
    const bool prior_valid = is_declared(previous_phase);
    if (prior_valid) {
      ESP_LOGI(TAG, "CT002 consumer %s phase changed: %s -> %s", consumer_id.c_str(),
               previous_phase.c_str(), normalized_phase.c_str());
    } else {
      ESP_LOGI(TAG, "CT002 consumer %s phase detected: %s", consumer_id.c_str(),
               normalized_phase.c_str());
    }
  }
}

bool CT002Component::validate_ct_mac_(const std::vector<std::string> &fields) const {
  if (this->ct_mac_.empty()) return true;
  if (fields.size() < 4) return false;
  std::string req(fields[3]);
  for (auto &c : req) c = static_cast<char>(std::tolower(c));
  std::string cfg(this->ct_mac_);
  for (auto &c : cfg) c = static_cast<char>(std::tolower(c));
  return req == cfg;
}

ReportMap CT002Component::collect_reports_for_balancer_() const {
  ReportMap out;
  for (const auto &kv : this->consumers_) {
    if (kv.second.timestamp > 0.0) {
      ConsumerReport r;
      r.device_type = kv.second.device_type;
      r.phase = kv.second.phase;
      r.power = kv.second.power;
      r.weight = kv.second.distribution_weight;
      r.efficiency_window_weight = kv.second.efficiency_window_weight;
      r.min_dc_output = kv.second.min_dc_output;
      out[kv.first] = std::move(r);
    }
  }
  return out;
}

double CT002Component::consumer_ttl_for_(const Consumer &c) const {
  if (this->consumer_ttl_seconds_.has_value())
    return static_cast<double>(*this->consumer_ttl_seconds_);
  if (!c.poll_interval.has_value()) return ADAPTIVE_TTL_FALLBACK_SECONDS;
  return std::max(ADAPTIVE_TTL_MIN_SECONDS,
                  ADAPTIVE_TTL_POLL_MULTIPLIER * static_cast<double>(*c.poll_interval));
}

bool CT002Component::consumer_expired_(const Consumer &c, double now) const {
  return c.timestamp > 0.0 && now - c.timestamp > this->consumer_ttl_for_(c);
}

CT002Component::PhaseReports CT002Component::collect_reports_by_phase_() const {
  PhaseReports out;
  const double now = this->now_seconds_();
  for (const auto &kv : this->consumers_) {
    const auto &c = kv.second;
    if (c.timestamp <= 0.0) continue;
    // Respect the request's "participate" flag: a battery that opted out (7th
    // field == 0) is not aggregated into the per-phase buckets or the count.
    if (!c.participates) continue;
    // The real CT clears a slot that missed ~1-2 poll cycles before
    // aggregating, so a battery that drops off the network stops being
    // counted almost immediately. Mirror that per response here; the cleanup
    // interval removes the entry shortly after (issue #462).
    if (this->consumer_expired_(c, now)) continue;
    const size_t idx = bucket_index_for_phase(c.phase);
    // Count every battery reporting into the bucket (regardless of power) so
    // relay mode can forward the real per-phase battery count (each battery
    // divides the forwarded aggregate by it to take its 1/N share).
    out.count[idx] += 1;
    float power;
    if (this->active_control_ && idx >= BUCKET_A && idx <= BUCKET_C) {
      // Active control: aggregate the net power we *instructed* this
      // consumer to be at, so PV passthrough doesn't masquerade as
      // discharge (issue #376).
      power = static_cast<float>(round_half_even(c.last_instructed_power));
      // With ramp pacing the per-poll delta is capped, so the instructed
      // net power can keep the sign of the battery's involuntary output
      // while the control *intent* points the other way (the issue #376
      // scenario). Filter by the balancer's recorded unpaced intent.
      const auto intent = this->balancer_ ? this->balancer_->get_last_intent(kv.first)
                                          : std::optional<float>{};
      if (intent.has_value() &&
          ((*intent <= 0.0f && power > 0.0f) || (*intent >= 0.0f && power < 0.0f))) {
        power = 0.0f;
      }
    } else {
      // Relay mode forwards each battery's *reported* power, exactly like
      // the real CT (issue #457). x/ABC consumers are never actively
      // instructed, so their reported power is the only truthful signal in
      // either mode.
      power = static_cast<float>(round_half_even(static_cast<double>(c.power)));
    }
    if (power == 0.0f) continue;
    out.active[idx] = true;
    if (power < 0.0f) out.chrg_power[idx] += power;
    else out.dchrg_power[idx] += power;
  }
  return out;
}

std::vector<float> CT002Component::compute_smooth_target_(const std::vector<float> &values,
                                                          const std::string &consumer_id) {
  // Cache the pre-balancer grid power for snapshot_consumer / Marstek
  // MQTT broadcasts. We do this even when active_control is off so the
  // insights component still gets fresh L1/L2/L3 readings.
  for (size_t i = 0; i < 3 && i < values.size(); ++i) this->last_grid_power_[i] = values[i];
  for (size_t i = values.size(); i < 3; ++i) this->last_grid_power_[i] = 0.0f;

  if (!this->active_control_ || values.empty() || !this->balancer_) {
    for (size_t i = 0; i < 3; ++i) this->last_target_[i] = this->last_grid_power_[i];
    return values;
  }

  auto reports = this->collect_reports_for_balancer_();
  std::unordered_set<std::string> inactive;
  std::unordered_set<std::string> manual;
  for (const auto &kv : this->consumers_) {
    // A consumer that opted out via the "participate" flag is treated as
    // inactive: active control excludes it from the distribution pool.
    if (!kv.second.active || !kv.second.participates) inactive.insert(kv.first);
    if (kv.second.manual_enabled) manual.insert(kv.first);
  }
  ConsumerMode mode{ConsumerModeKind::AUTO};
  auto it = this->consumers_.find(consumer_id);
  if (it != this->consumers_.end()) {
    if (!it->second.active || !it->second.participates)
      mode = ConsumerMode{ConsumerModeKind::INACTIVE};
    else if (it->second.manual_enabled)
      mode = ConsumerMode{ConsumerModeKind::MANUAL, it->second.manual_target};
  }
  float grid_total = 0.0f;
  for (float v : values) grid_total += v;
  // smooth_target mirrors Python's _last_smooth_target (ct002.py:361-362):
  // the total INPUT grid power (post-filter, pre-balancer), NOT the sum of
  // the balancer's per-phase output targets. mqtt_insights publishes this
  // as the device-level smooth_target sensor.
  this->last_smooth_target_ = grid_total;
  auto out_arr = this->balancer_->compute_target(consumer_id, mode, reports, grid_total,
                                                 inactive, manual, values);
  for (size_t i = 0; i < 3; ++i) this->last_target_[i] = out_arr[i];
  return {out_arr[0], out_arr[1], out_arr[2]};
}

std::vector<std::string> CT002Component::build_response_fields_(
    const std::vector<std::string> &request_fields, const std::vector<float> &values) {
  std::vector<float> v = values;
  if (v.size() != 3) v = {0.0f, 0.0f, 0.0f};
  const float phase_a = v[0];
  const float phase_b = v[1];
  const float phase_c = v[2];
  const float total = phase_a + phase_b + phase_c;
  const std::string meter_dev_type = request_fields.size() > 0 ? request_fields[0] : "HMG-50";
  const std::string meter_mac = request_fields.size() > 1 ? request_fields[1] : "";
  const std::string ct_mac_out =
      !this->ct_mac_.empty() ? this->ct_mac_
                             : (request_fields.size() > 3 ? request_fields[3] : "");

  auto to_int_str = [](float f) {
    char buf[16];
    // round_half_even matches Python's round() on the wire values.
    std::snprintf(buf, sizeof(buf), "%ld", round_half_even(static_cast<double>(f)));
    return std::string(buf);
  };

  std::vector<std::string> fields;
  fields.reserve(RESPONSE_LABEL_COUNT);
  fields.push_back(this->ct_type_);
  fields.push_back(ct_mac_out);
  fields.push_back(meter_dev_type);
  fields.push_back(meter_mac);
  fields.push_back(to_int_str(phase_a));
  fields.push_back(to_int_str(phase_b));
  fields.push_back(to_int_str(phase_c));
  fields.push_back(to_int_str(total));
  fields.push_back("0");  // A_chrg_nb
  fields.push_back("0");  // B_chrg_nb
  fields.push_back("0");  // C_chrg_nb
  fields.push_back("0");  // ABC_chrg_nb
  fields.push_back(std::to_string(this->wifi_rssi_));
  fields.push_back(std::to_string(this->info_idx_counter_));
  fields.push_back("0");  // x_chrg_power
  fields.push_back("0");  // A_chrg_power
  fields.push_back("0");  // B_chrg_power
  fields.push_back("0");  // C_chrg_power
  fields.push_back("0");  // ABC_chrg_power
  fields.push_back("0");  // x_dchrg_power
  fields.push_back("0");  // A_dchrg_power
  fields.push_back("0");  // B_dchrg_power
  fields.push_back("0");  // C_dchrg_power
  fields.push_back("0");  // ABC_dchrg_power

  const auto phase_reports = this->collect_reports_by_phase_();
  const float phase_power[3] = {phase_a, phase_b, phase_c};
  for (size_t i = 0; i < 3; ++i) {
    const size_t bucket = BUCKET_A + i;
    if (this->active_control_) {
      // Active control distributes a per-consumer target, so each battery
      // applies it as-is: report a count of 1 when the phase is active.
      // Deliberately NOT the real per-phase count (issue #459): the battery
      // divides the grid value by this count (relay share-split, g / nb);
      // our value is already this battery's individual target, so a real
      // count N would make every battery under-respond by a factor of N.
      // The issue #455 relay-count fix applies to the relay branch only.
      if (phase_reports.active[bucket] || phase_power[i] != 0.0f) {
        fields[8 + i] = "1";
      }
    } else {
      // Relay mode forwards the per-phase aggregate; report the real battery
      // count so each battery takes its 1/N share.
      fields[8 + i] = std::to_string(phase_reports.count[bucket]);
    }
    fields[15 + i] = to_int_str(phase_reports.chrg_power[bucket]);
    fields[20 + i] = to_int_str(phase_reports.dchrg_power[bucket]);
  }
  // x (unassigned/inspection) bucket — chrg/dchrg only; the response carries
  // no x count field.
  fields[14] = to_int_str(phase_reports.chrg_power[BUCKET_X]);
  fields[19] = to_int_str(phase_reports.dchrg_power[BUCKET_X]);
  // ABC (combined, phase "D") bucket. Combined-mode consumers are never
  // actively instructed (the emulator has no combined control mode), so they
  // are effectively relayed in both modes: forward the real count.
  fields[11] = std::to_string(phase_reports.count[BUCKET_ABC]);
  fields[18] = to_int_str(phase_reports.chrg_power[BUCKET_ABC]);
  fields[23] = to_int_str(phase_reports.dchrg_power[BUCKET_ABC]);

  while (fields.size() < RESPONSE_LABEL_COUNT) fields.push_back("0");
  this->info_idx_counter_ = (this->info_idx_counter_ + 1) % 256;
  return fields;
}

size_t CT002Component::reporting_consumer_count() const {
  size_t n = 0;
  for (const auto &kv : this->consumers_) if (kv.second.timestamp > 0.0) ++n;
  return n;
}

void CT002Component::dump_config() {
  ESP_LOGCONFIG(TAG, "CT002 Component:");
  ESP_LOGCONFIG(TAG, "  Phases: %u", this->num_phases_);
  ESP_LOGCONFIG(TAG, "  CT Type: %s", this->ct_type_.c_str());
  ESP_LOGCONFIG(TAG, "  CT MAC: %s", this->ct_mac_.empty() ? "(mirror)" : this->ct_mac_.c_str());
  ESP_LOGCONFIG(TAG, "  UDP Port: %u", this->udp_port_);
  ESP_LOGCONFIG(TAG, "  Active Control: %s", YESNO(this->active_control_));
  ESP_LOGCONFIG(TAG, "  Max Sensor Age: %u ms", this->max_sensor_age_ms_);
  ESP_LOGCONFIG(TAG, "  Reporting Consumers: %u", static_cast<unsigned>(this->reporting_consumer_count()));
}

// ── MQTT-insights integration ─────────────────────────────────────────

CT002Component::ConsumerSnapshot CT002Component::snapshot_consumer(
    const std::string &consumer_id) const {
  ConsumerSnapshot snap;
  snap.consumer_id = consumer_id;
  auto it = this->consumers_.find(consumer_id);
  if (it == this->consumers_.end()) return snap;
  const auto &c = it->second;
  snap.phase = c.phase;
  snap.device_type = c.device_type;
  snap.last_ip = c.last_ip;
  snap.reported_power = c.power;
  snap.active = c.active;
  snap.auto_target = !c.manual_enabled;
  if (c.manual_enabled) snap.manual_target = c.manual_target;
  snap.distribution_weight = c.distribution_weight;
  snap.efficiency_window_weight = c.efficiency_window_weight;
  snap.min_dc_output = c.min_dc_output;
  snap.poll_interval = c.poll_interval;
  snap.timestamp = c.timestamp;
  snap.grid_power = this->last_grid_power_;
  snap.target = this->last_target_;
  if (this->last_smooth_target_.has_value()) snap.smooth_target = *this->last_smooth_target_;
  if (this->balancer_) {
    // Per-consumer saturation, same source the Python ct002 reads at
    // ct002.py:737 ("saturation": self._balancer.get_saturation(...)).
    snap.saturation = static_cast<float>(this->balancer_->get_saturation(consumer_id));
    auto last = this->balancer_->get_last_target(consumer_id);
    if (last.has_value()) snap.last_target = *last;
  }
  return snap;
}

std::vector<std::string> CT002Component::reporting_consumer_ids() const {
  std::vector<std::string> out;
  out.reserve(this->consumers_.size());
  for (const auto &kv : this->consumers_) {
    if (kv.second.timestamp > 0.0) out.push_back(kv.first);
  }
  return out;
}

std::vector<float> CT002Component::latest_grid_power() const {
  const uint32_t now_ms = ::esphome::millis();
  std::vector<float> out;
  out.reserve(3);
  for (size_t i = 0; i < 3; ++i) {
    const uint32_t age = (this->raw_stamp_ms_[i] == 0) ? UINT32_MAX
                                                       : (now_ms - this->raw_stamp_ms_[i]);
    if (age > this->max_sensor_age_ms_) {
      out.push_back(0.0f);
    } else {
      out.push_back(this->raw_values_[i]);
    }
  }
  return out;
}

size_t CT002Component::connected_slave_count() const {
  return this->reporting_consumer_count();
}

std::vector<CT002Component::ReportingConsumerRow> CT002Component::reporting_consumer_rows() const {
  std::vector<ReportingConsumerRow> out;
  out.reserve(this->consumers_.size());
  for (const auto &kv : this->consumers_) {
    if (kv.second.timestamp <= 0.0) continue;
    ReportingConsumerRow row;
    row.consumer_id = kv.first;
    row.device_type = kv.second.device_type;
    row.last_ip = kv.second.last_ip;
    row.phase = kv.second.phase;
    out.push_back(std::move(row));
  }
  return out;
}

void CT002Component::set_consumer_active(const std::string &consumer_id, bool active) {
  // Auto-create the consumer entry — Python's _get_consumer does the same,
  // so HA commands that arrive before the first UDP poll still work.
  auto &consumer = this->get_consumer_(consumer_id);
  if (active && !consumer.active) {
    // Returning to active: drop stale saturation/last_target so the
    // balancer doesn't act on pre-pause readings (Python: ct002.py:263-269).
    if (this->balancer_) this->balancer_->reset_consumer(consumer_id);
  }
  consumer.active = active;
}

void CT002Component::set_consumer_manual_target(const std::string &consumer_id, float target) {
  auto &consumer = this->get_consumer_(consumer_id);
  consumer.manual_enabled = true;
  consumer.manual_target = target;
}

void CT002Component::set_consumer_distribution_weight(const std::string &consumer_id,
                                                      float weight) {
  // Same [0, 10] range the Python setter enforces (0 = take no share); ignore
  // non-finite or out-of-range values rather than corrupting the split.
  if (!std::isfinite(weight) || weight < 0.0f || weight > 10.0f) return;
  this->get_consumer_(consumer_id).distribution_weight = weight;
}

void CT002Component::set_consumer_efficiency_window_weight(const std::string &consumer_id,
                                                           float weight) {
  // Same [0, 1] range the Python setter enforces (1 = full participation,
  // 0 = skipped while limiting); ignore non-finite or out-of-range values.
  if (!std::isfinite(weight) || weight < 0.0f || weight > 1.0f) return;
  this->get_consumer_(consumer_id).efficiency_window_weight = weight;
}

void CT002Component::set_consumer_min_dc_output(const std::string &consumer_id,
                                                float value) {
  // Per-device MIN_DC_OUTPUT override (W); validation aligned with the Python
  // setter (finite, >= 0, no upper bound). The MQTT command handler still
  // enforces the 0..1000 entry range on both stacks.
  if (!std::isfinite(value) || value < 0.0f) return;
  this->get_consumer_(consumer_id).min_dc_output = value;
}

void CT002Component::set_consumer_auto_target(const std::string &consumer_id, bool auto_target) {
  auto &consumer = this->get_consumer_(consumer_id);
  const bool was_manual = consumer.manual_enabled;
  if (auto_target) {
    consumer.manual_enabled = false;
    // Returning to auto control: same reset_consumer semantics as
    // re-activating, so the balancer rebuilds priority/efficiency state
    // from a clean slate (Python: ct002.py:246-250).
    if (was_manual && this->balancer_) this->balancer_->reset_consumer(consumer_id);
  } else {
    consumer.manual_enabled = true;
    // Entering manual mode: pull this consumer out of the efficiency
    // rotation immediately, otherwise it stays in the cached pool with
    // a stale weight until the next sample_id change
    // (Python: ct002.py:251-253).
    if (this->balancer_) this->balancer_->detach_from_auto_pool(consumer_id);
  }
}

void CT002Component::evict_stale_consumers_() {
  const double now = now_seconds_();
  std::vector<std::string> stale;
  for (const auto &kv : this->consumers_) {
    if (this->consumer_expired_(kv.second, now)) {
      stale.push_back(kv.first);
    }
  }
  for (const auto &id : stale) {
    // Fire listeners FIRST so they can read the consumer's last snapshot
    // before the entry disappears (matches Python's _call_event_listener
    // with {"_removed": True} payload before the dict pop).
    for (auto &cb : this->consumer_removed_listeners_) cb(id);
    this->consumers_.erase(id);
    if (this->balancer_) this->balancer_->remove_consumer(id);
  }
  if (!stale.empty()) {
    ESP_LOGD(TAG, "Evicted %u stale consumer(s)", static_cast<unsigned>(stale.size()));
  }
  // Purge dedup timestamps — entries only matter within the dedupe window;
  // with an adaptive TTL there is no single number, so purge on a horizon
  // safely past any per-consumer TTL and the dedupe window itself (mirrors
  // Python's _cleanup_consumers purge).
  if (!this->dedup_last_.empty()) {
    const double horizon =
        this->consumer_ttl_seconds_.has_value()
            ? static_cast<double>(*this->consumer_ttl_seconds_)
            : std::max(ADAPTIVE_TTL_FALLBACK_SECONDS,
                       static_cast<double>(this->dedupe_window_ms_) / 1000.0);
    const double cutoff = now - horizon;
    for (auto it = this->dedup_last_.begin(); it != this->dedup_last_.end();) {
      if (it->second < cutoff) it = this->dedup_last_.erase(it);
      else ++it;
    }
  }
}

bool CT002Component::dedup_should_process_(const std::string &consumer_id) {
  if (this->dedupe_window_ms_ == 0) return true;
  const double now = now_seconds_();
  const double window = static_cast<double>(this->dedupe_window_ms_) / 1000.0;
  auto it = this->dedup_last_.find(consumer_id);
  if (it != this->dedup_last_.end() && (now - it->second) < window) {
    return false;  // within window — drop (does NOT refresh the timestamp)
  }
  this->dedup_last_[consumer_id] = now;
  return true;
}

void CT002Component::force_balancer_rotation() {
  if (!this->balancer_) return;
  // Mirror Python's force_efficiency_rotation pool filtering: only include
  // consumers that are reporting AND active AND not under manual override.
  // The earlier "all reporting consumers" pool let inactive/manual
  // batteries into the rotation, which the balancer then had to filter
  // out internally — easier to get the pool right at the source.
  std::unordered_set<std::string> pool;
  for (const auto &kv : this->consumers_) {
    if (kv.second.timestamp > 0.0 && kv.second.active && !kv.second.manual_enabled) {
      pool.insert(kv.first);
    }
  }
  this->balancer_->force_rotation(pool);
}

}  // namespace ct002
}  // namespace esphome
