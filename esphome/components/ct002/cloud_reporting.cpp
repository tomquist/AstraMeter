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

int CloudReportingComponent::http_get_(const std::string &url, std::string *body) {
  // Log only the endpoint (path before '?') — the query string carries the
  // device id and telemetry, which shouldn't land in device logs.
  const std::string endpoint = url.substr(0, url.find('?'));
  std::vector<http_request::Header> headers = {{"User-Agent", "Dart/2.19 (dart:io)"}};
  auto container = this->http_->get(url, headers);
  if (container == nullptr) {
    ESP_LOGW(TAG, "HTTP GET failed: %s", endpoint.c_str());
    return -1;
  }
  int status = container->status_code;
  ESP_LOGD(TAG, "HTTP %d from %s", status, endpoint.c_str());
  if (body != nullptr) {
    body->clear();
    // Both images read the response into a 512-byte buffer.
    constexpr size_t MAX_BODY = 512;
    std::vector<uint8_t> buf(
        std::min<size_t>(container->content_length ? container->content_length : MAX_BODY, MAX_BODY));
    if (!buf.empty()) {
      auto result = http_request::http_read_fully(container.get(), buf.data(), buf.size(),
                                                  /*chunk_size=*/buf.size(), this->http_->get_timeout());
      // Capture bytes_read BEFORE end() — some backends invalidate the
      // container's counters once end() runs.
      const size_t bytes_read = container->get_bytes_read();
      if (result.status == http_request::HttpReadStatus::OK) {
        body->assign(reinterpret_cast<const char *>(buf.data()), bytes_read);
      } else {
        // A response we could not read never completed, as on the Python side.
        ESP_LOGW(TAG, "HTTP read failed: %s", endpoint.c_str());
        status = -1;
      }
    }
  }
  container->end();
  return status;
}

bool CloudReportingComponent::handshake_() {
  this->sample_energy_(this->gather_());
  // getDateInfo is a device upsert: the server writes `aid`->the record's
  // `type` and `sv`->its `version`. Send the CT model and the firmware version
  // a reporting CT runs, as a real one does.
  std::string body;
  const int status = this->http_get_(build_get_date_info_url(this->host_, this->device_id_, this->fcv_,
                                                             this->ct002_->ct_type(), this->sv_or_default_()),
                                     &body);
  // Both images wait for the module's `+QHTTPGET: 0,200`: only 200 counts.
  if (status != 200) return false;
  if (!parse_server_time(body, &this->server_time_)) {
    ESP_LOGD(TAG, "Handshake carried no server time; using local time");
    this->server_time_ = this->local_time_();
  }
  this->counters_.update(this->energy_.total(), this->server_time_);
  return true;
}

bool CloudReportingComponent::report_() {
  CtMeasurement m = this->gather_();
  this->sample_energy_(m);
  m.eled = this->counters_.eled();
  m.elet = this->counters_.elet();
  const ServerTime &t = this->server_time_;
  const int status =
      this->http_get_(build_set_ct_reporting_url(this->host_, this->ct002_->ct_type(), this->device_id_,
                                                 t.time_no(), t.year, t.month, t.day, m));
  return status == 200;
}

void CloudReportingComponent::retry_later_() { this->next_attempt_ms_ = millis() + this->retry_ms_; }

void CloudReportingComponent::loop() {
  if (this->state_ == State::FATAL) return;
  // The reported id is the CT the cloud knows: ct_mac, or the ID the app added
  // over Bluetooth while ct_mac is unset. marstek_registration may set ct_mac
  // after our setup(), and a new id is another device, so it gets its own
  // handshake. Wait until there is one.
  const std::string &id = this->ct002_->app_mac();
  if (id.empty()) return;
  if (id != this->device_id_) {
    this->device_id_ = id;
    this->state_ = State::WAIT_FOR_NETWORK;
  }
  const uint32_t now = millis();
  if (static_cast<int32_t>(now - this->next_sample_ms_) >= 0) {
    this->next_sample_ms_ = now + this->sample_ms_;
    this->sample_energy_(this->gather_());
  }
  // A real CT runs its reporting step only while Wi-Fi is up, keeping its state
  // across a drop.
  if (!this->network_ready_()) return;
  if (this->state_ == State::WAIT_FOR_NETWORK) {
    this->state_ = State::HANDSHAKE;
    this->next_attempt_ms_ = now;
  }
  if (static_cast<int32_t>(now - this->next_attempt_ms_) < 0) return;
  switch (this->state_) {
    case State::HANDSHAKE:
      if (this->handshake_()) {
        this->state_ = State::REPORT;  // report right away.
      } else {
        this->retry_later_();
      }
      break;
    case State::REPORT:
      if (this->report_()) {
        this->state_ = State::WAIT;
        this->next_attempt_ms_ = millis() + this->interval_ms_;
      } else {
        this->retry_later_();
      }
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
