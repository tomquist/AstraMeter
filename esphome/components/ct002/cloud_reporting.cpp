#include "cloud_reporting.h"

#ifdef USE_CT002_CLOUD_REPORTING
// Runtime component — only compiled when the cloud_reporting sub-block is
// present in YAML (it needs http_request). The pure URL builders it calls live
// in cloud_reporting_url.cpp and are always compiled (covered by the host test).

#include <algorithm>
#include <cmath>
#include <ctime>
#include <vector>

#include "esphome/components/http_request/http_request.h"
#include "esphome/components/network/util.h"
#ifdef USE_ESP32
#include <esp_http_client.h>
#endif
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace ct002 {
namespace cloud_reporting {

static const char *const TAG = "cloud_reporting";

void CloudReportingComponent::setup() {
  if (this->ct002_ == nullptr) {
    ESP_LOGE(TAG, "ct002 not bound — refusing to start");
    this->mark_failed();
    return;
  }
  if (this->http_ == nullptr) {
    ESP_LOGE(TAG, "http_request not bound — add a `http_request:` block to your YAML");
    this->mark_failed();
    return;
  }
  // The reported id is the CT MAC. It's resolved lazily in loop() rather than
  // here: marstek_registration may set ct002.ct_mac after our setup() completes,
  // so we wait for a MAC instead of failing if it isn't ready yet.
  this->state_ = State::WAIT_FOR_NETWORK;
  this->timeout_ms_ = this->http_->get_timeout();
}

void CloudReportingComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "CT002 cloud reporting:");
  ESP_LOGCONFIG(TAG, "  host: %s", this->host_.c_str());
  ESP_LOGCONFIG(TAG, "  id: %s",
                this->device_id_.empty() ? "(pending — ct_mac, or the Bluetooth ID)" : this->device_id_.c_str());
  // uint32_t is `long unsigned` on xtensa, so %u needs the cast to match
  // (lossless — both are 32 bits here and on the host build).
  ESP_LOGCONFIG(TAG, "  interval: %u ms", static_cast<unsigned>(this->interval_ms_));
}

bool CloudReportingComponent::network_ready_() const { return network::is_connected(); }

ServerTime CloudReportingComponent::local_time_() const {
  const std::time_t t = std::time(nullptr);
  std::tm tm_buf{};
  gmtime_r(&t, &tm_buf);
  return ServerTime{tm_buf.tm_year + 1900, tm_buf.tm_mon + 1, tm_buf.tm_mday,
                    tm_buf.tm_hour,        tm_buf.tm_min,      tm_buf.tm_sec};
}

int CloudReportingComponent::sv_or_default_() const {
  return this->sv_ >= 0 ? this->sv_ : reporting_firmware_version(this->ct002_->ct_type());
}

CtMeasurement CloudReportingComponent::gather_() const {
  CtMeasurement m;
  const std::vector<float> phases = this->ct002_->latest_grid_power();
  m.ap = phases.size() > 0 ? static_cast<int>(lroundf(phases[0])) : 0;
  m.bp = phases.size() > 1 ? static_cast<int>(lroundf(phases[1])) : 0;
  m.cp = phases.size() > 2 ? static_cast<int>(lroundf(phases[2])) : 0;
  m.dp = m.ap + m.bp + m.cp;
  m.rssi = this->ct002_->wifi_rssi();
  m.slv = static_cast<int>(this->ct002_->connected_slave_count());
  m.udp = 1;
  m.mqtt = 0;
  // HME-4 only (the URL builder leaves them out for HME-3): the optional
  // voltage sensors, truncated to whole volts as the meter does, and 0 for a
  // phase without a fresh reading. Current stays 0; AstraMeter has none.
  int *const volts[3] = {&m.va, &m.vb, &m.vc};
  for (uint8_t i = 0; i < 3; ++i) {
    const optional<float> v = this->ct002_->grid_voltage(i);
    *volts[i] = v.has_value() ? static_cast<int>(*v) : 0;
  }
  // A real CT fills c* from the buckets it sends as the UDP response's
  // *_dchrg_power and d* from its *_chrg_power.
  const CT002Component::PhaseBucketPowers b = this->ct002_->reporting_phase_buckets();
  m.cz = static_cast<int>(lroundf(b.dchrg_power[BUCKET_X]));
  m.ca = static_cast<int>(lroundf(b.dchrg_power[BUCKET_A]));
  m.cb = static_cast<int>(lroundf(b.dchrg_power[BUCKET_B]));
  m.cc = static_cast<int>(lroundf(b.dchrg_power[BUCKET_C]));
  m.cd = static_cast<int>(lroundf(b.dchrg_power[BUCKET_ABC]));
  m.dz = static_cast<int>(lroundf(b.chrg_power[BUCKET_X]));
  m.da = static_cast<int>(lroundf(b.chrg_power[BUCKET_A]));
  m.db = static_cast<int>(lroundf(b.chrg_power[BUCKET_B]));
  m.dc = static_cast<int>(lroundf(b.chrg_power[BUCKET_C]));
  m.dd = static_cast<int>(lroundf(b.chrg_power[BUCKET_ABC]));
  return m;
}

void CloudReportingComponent::sample_energy_(const CtMeasurement &m) {
  // HME-4 nets its per-phase import and export registers; HME-3 reads the
  // meter's import register (OBIS 1.8.0), which only counts import.
  const int power = this->ct002_->ct_type() == "HME-3" ? std::max(m.dp, 0) : m.dp;
  this->energy_.sample(millis(), static_cast<float>(power));
}

// Log only the endpoint (path before '?') — the query string carries the
// device id and telemetry, which shouldn't land in device logs.
static std::string endpoint_of(const std::string &url) { return url.substr(0, url.find('?')); }

// Both images read the response into a 512-byte buffer.
static constexpr size_t MAX_BODY = 512;
static const char *const USER_AGENT = "Dart/2.19 (dart:io)";

#ifdef USE_ESP32
// The ESP-IDF client rather than http_request: the worker is not the main loop,
// and http_request's Arduino backend feeds the main loop's watchdog bookkeeping
// from whichever task it runs on. esp_http_client touches neither.
static HttpResult run_get(const std::string &url, bool want_body, uint32_t timeout_ms) {
  HttpResult r;
  esp_http_client_config_t config{};
  config.url = url.c_str();
  config.method = HTTP_METHOD_GET;
  config.timeout_ms = static_cast<int>(timeout_ms);
  config.user_agent = USER_AGENT;
  config.disable_auto_redirect = true;
  esp_http_client_handle_t client = esp_http_client_init(&config);
  if (client == nullptr) return r;
  if (esp_http_client_open(client, 0) == ESP_OK && esp_http_client_fetch_headers(client) >= 0) {
    const int status = esp_http_client_get_status_code(client);
    bool read_ok = true;
    if (want_body) {
      char buf[MAX_BODY];
      size_t total = 0;
      while (total < sizeof(buf)) {
        const int n = esp_http_client_read(client, buf + total, static_cast<int>(sizeof(buf) - total));
        if (n < 0) {
          read_ok = false;
          break;
        }
        if (n == 0) break;
        total += static_cast<size_t>(n);
      }
      if (read_ok) r.body.assign(buf, total);
    }
    // A response we could not read never completed, as on the Python side.
    if (read_ok) r.status = status;
  }
  esp_http_client_close(client);
  esp_http_client_cleanup(client);
  return r;
}

void CloudReportingComponent::worker_main_(void *arg) {
  auto *self = static_cast<CloudReportingComponent *>(arg);
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    std::string url;
    bool want_body = false;
    {
      LockGuard lock(self->slot_mutex_);
      if (!self->slot_.take(&url, &want_body)) continue;
    }
    HttpResult r = run_get(url, want_body, self->timeout_ms_);
    LockGuard lock(self->slot_mutex_);
    self->slot_.finish(std::move(r));
  }
}
#else
// No worker off ESP32: the GET runs on the main loop through http_request.
HttpResult CloudReportingComponent::http_get_(const std::string &url, bool want_body) {
  HttpResult r;
  std::vector<http_request::Header> headers = {{"User-Agent", USER_AGENT}};
  auto container = this->http_->get(url, headers);
  if (container == nullptr) return r;
  int status = container->status_code;
  if (want_body) {
    std::vector<uint8_t> buf(
        std::min<size_t>(container->content_length ? container->content_length : MAX_BODY, MAX_BODY));
    if (!buf.empty()) {
      auto result = http_request::http_read_fully(container.get(), buf.data(), buf.size(),
                                                  /*chunk_size=*/buf.size(), this->timeout_ms_);
      // Capture bytes_read BEFORE end() — some backends invalidate the
      // container's counters once end() runs.
      const size_t bytes_read = container->get_bytes_read();
      if (result.status == http_request::HttpReadStatus::OK) {
        r.body.assign(reinterpret_cast<const char *>(buf.data()), bytes_read);
      } else {
        // A response we could not read never completed, as on the Python side.
        status = -1;
      }
    }
  }
  container->end();
  r.status = status;
  return r;
}
#endif

void CloudReportingComponent::start_get_(const std::string &url, bool want_body) {
  this->endpoint_ = endpoint_of(url);
  this->in_flight_ = true;
  LockGuard lock(this->slot_mutex_);
  if (!this->slot_.submit(url, want_body)) return;  // unreachable: one GET at a time
#ifdef USE_ESP32
  // Started on first use, so a board that never reports spends no memory on it.
  if (this->worker_ == nullptr &&
      xTaskCreate(worker_main_, "cloud_http", 6144, this, tskIDLE_PRIORITY + 1, &this->worker_) != pdPASS) {
    this->worker_ = nullptr;
    ESP_LOGW(TAG, "Could not start the HTTP task; retrying later");
    std::string taken;
    bool body = false;
    this->slot_.take(&taken, &body);
    this->slot_.finish(HttpResult{});
    return;
  }
  xTaskNotifyGive(this->worker_);
#else
  std::string taken;
  bool body = false;
  this->slot_.take(&taken, &body);
  this->slot_.finish(this->http_get_(taken, body));
#endif
}

bool CloudReportingComponent::poll_get_(HttpResult *out) {
  LockGuard lock(this->slot_mutex_);
  return this->slot_.collect(out);
}

void CloudReportingComponent::start_handshake_() {
  this->sample_energy_(this->gather_());
  ESP_LOGD(TAG, "Handshake as %s", this->device_id_.c_str());
  // Send the CT model and the firmware version a reporting CT runs, as a real
  // one does.
  this->start_get_(build_get_date_info_url(this->host_, this->device_id_, this->fcv_, this->ct002_->ct_type(),
                                           this->sv_or_default_()),
                   true);
}

bool CloudReportingComponent::finish_handshake_(const HttpResult &r) {
  // Both images wait for the module's `+QHTTPGET: 0,200`: only 200 counts.
  if (r.status != 200) return false;
  if (!parse_server_time(r.body, &this->server_time_)) {
    ESP_LOGD(TAG, "Handshake carried no server time; using local time");
    this->server_time_ = this->local_time_();
  }
  this->counters_.update(this->energy_.total(), this->server_time_);
  return true;
}

void CloudReportingComponent::start_report_() {
  CtMeasurement m = this->gather_();
  this->sample_energy_(m);
  m.eled = this->counters_.eled();
  m.elet = this->counters_.elet();
  const ServerTime &t = this->server_time_;
  this->start_get_(build_set_ct_reporting_url(this->host_, this->ct002_->ct_type(), this->device_id_, t.time_no(),
                                              t.year, t.month, t.day, m),
                   false);
}

void CloudReportingComponent::finish_step_(const HttpResult &r) {
  if (r.status < 0) {
    ESP_LOGW(TAG, "HTTP GET failed: %s", this->endpoint_.c_str());
  } else {
    ESP_LOGD(TAG, "HTTP %d from %s", r.status, this->endpoint_.c_str());
  }
  if (this->state_ == State::HANDSHAKE) {
    if (this->finish_handshake_(r)) {
      this->state_ = State::REPORT;  // report right away.
      this->next_attempt_ms_ = millis();
      return;
    }
  } else if (this->state_ == State::REPORT) {
    if (r.status == 200) {
      this->state_ = State::WAIT;
      this->next_attempt_ms_ = millis() + this->interval_ms_;
      ESP_LOGD(TAG, "Report accepted; next in %u s", static_cast<unsigned>(this->interval_ms_ / 1000));
      return;
    }
  } else {
    return;
  }
  ESP_LOGD(TAG, "%s failed; retrying in %u s", this->state_ == State::HANDSHAKE ? "Handshake" : "Report",
           static_cast<unsigned>(this->retry_ms_ / 1000));
  this->retry_later_();
}

void CloudReportingComponent::retry_later_() { this->next_attempt_ms_ = millis() + this->retry_ms_; }

void CloudReportingComponent::loop() {
  if (this->state_ == State::FATAL) return;
  const uint32_t now = millis();
  if (this->in_flight_) {
    // Keep sampling while a GET runs: it can take seconds.
    if (static_cast<int32_t>(now - this->next_sample_ms_) >= 0) {
      this->next_sample_ms_ = now + this->sample_ms_;
      this->sample_energy_(this->gather_());
    }
    HttpResult r;
    if (!this->poll_get_(&r)) return;
    this->in_flight_ = false;
    this->finish_step_(r);
    return;
  }
  // The reported id is the CT the cloud knows: ct_mac, or the ID the app added
  // over Bluetooth while ct_mac is unset. marstek_registration may set ct_mac
  // after our setup(), and a new id is another device, so it gets its own
  // handshake. Wait until there is one.
  const std::string &id = this->ct002_->app_mac();
  if (id.empty()) {
    if (!this->logged_id_wait_) {
      ESP_LOGD(TAG, "Waiting for the CT's id: ct_mac, the Marstek registration, or the Bluetooth ID");
      this->logged_id_wait_ = true;
    }
    return;
  }
  if (id != this->device_id_) {
    this->device_id_ = id;
    this->state_ = State::WAIT_FOR_NETWORK;
  }
  if (static_cast<int32_t>(now - this->next_sample_ms_) >= 0) {
    this->next_sample_ms_ = now + this->sample_ms_;
    this->sample_energy_(this->gather_());
  }
  // A real CT runs its reporting step only while Wi-Fi is up, keeping its state
  // across a drop.
  if (!this->network_ready_()) {
    if (!this->logged_network_wait_) {
      ESP_LOGD(TAG, "Waiting for the network");
      this->logged_network_wait_ = true;
    }
    return;
  }
  this->logged_network_wait_ = false;
  if (this->state_ == State::WAIT_FOR_NETWORK) {
    this->state_ = State::HANDSHAKE;
    this->next_attempt_ms_ = now;
  }
  if (static_cast<int32_t>(now - this->next_attempt_ms_) < 0) return;
  switch (this->state_) {
    case State::HANDSHAKE:
      this->start_handshake_();
      break;
    case State::REPORT:
      this->start_report_();
      break;
    case State::WAIT:
      this->state_ = State::HANDSHAKE;
      break;
    default:
      break;
  }
}

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_CLOUD_REPORTING
