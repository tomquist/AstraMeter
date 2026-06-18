#include "cloud_reporting.h"

#ifdef USE_CT002_CLOUD_REPORTING
// Runtime component — only compiled when the cloud_reporting sub-block is
// present in YAML (it needs http_request). The pure URL builders it calls live
// in cloud_reporting_url.cpp and are always compiled (covered by the host test).

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
  // Fall back to the configured CT MAC when no explicit id was given.
  if (this->device_id_.empty()) this->device_id_ = this->ct002_->ct_mac();
  if (this->device_id_.empty()) {
    ESP_LOGE(TAG, "no device id — set cloud_reporting.device_id or ct002.ct_mac");
    this->mark_failed();
    return;
  }
  this->state_ = State::WAIT_FOR_NETWORK;
}

void CloudReportingComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "CT002 cloud reporting:");
  ESP_LOGCONFIG(TAG, "  host: %s", this->host_.c_str());
  ESP_LOGCONFIG(TAG, "  id: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  interval: %u ms", this->interval_ms_);
}

bool CloudReportingComponent::network_ready_() const { return network::is_connected(); }

void CloudReportingComponent::now_(int64_t *epoch, int *year, int *month, int *day) const {
  const std::time_t t = std::time(nullptr);
  *epoch = static_cast<int64_t>(t);
  std::tm tm_buf{};
  gmtime_r(&t, &tm_buf);
  *year = tm_buf.tm_year + 1900;
  *month = tm_buf.tm_mon + 1;
  *day = tm_buf.tm_mday;
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
  const CT002Component::PhaseBucketPowers b = this->ct002_->reporting_phase_buckets();
  m.cz = static_cast<int>(lroundf(b.chrg_power[BUCKET_X]));
  m.ca = static_cast<int>(lroundf(b.chrg_power[BUCKET_A]));
  m.cb = static_cast<int>(lroundf(b.chrg_power[BUCKET_B]));
  m.cc = static_cast<int>(lroundf(b.chrg_power[BUCKET_C]));
  m.cd = static_cast<int>(lroundf(b.chrg_power[BUCKET_ABC]));
  m.dz = static_cast<int>(lroundf(b.dchrg_power[BUCKET_X]));
  m.da = static_cast<int>(lroundf(b.dchrg_power[BUCKET_A]));
  m.db = static_cast<int>(lroundf(b.dchrg_power[BUCKET_B]));
  m.dc = static_cast<int>(lroundf(b.dchrg_power[BUCKET_C]));
  m.dd = static_cast<int>(lroundf(b.dchrg_power[BUCKET_ABC]));
  return m;
}

void CloudReportingComponent::http_get_(const std::string &url) {
  std::vector<http_request::Header> headers = {{"User-Agent", "Dart/2.19 (dart:io)"}};
  auto container = this->http_->get(url, headers);
  if (container == nullptr) {
    ESP_LOGW(TAG, "HTTP GET failed: %s", url.c_str());
    return;
  }
  ESP_LOGD(TAG, "HTTP %d from %s", container->status_code, url.c_str());
  container->end();
}

void CloudReportingComponent::loop() {
  if (this->state_ == State::FATAL) return;
  if (this->state_ == State::WAIT_FOR_NETWORK) {
    if (!this->network_ready_()) return;
    this->state_ = State::HANDSHAKE;
  }
  if (this->state_ == State::HANDSHAKE) {
    this->http_get_(build_get_date_info_url(this->host_, this->device_id_, this->fcv_,
                                            this->account_id_, this->sv_));
    this->state_ = State::REPORT;
    this->next_report_ms_ = millis();  // first report right away.
  }
  if (this->state_ == State::REPORT) {
    if (!this->network_ready_()) {
      this->state_ = State::WAIT_FOR_NETWORK;
      return;
    }
    if (static_cast<int32_t>(millis() - this->next_report_ms_) < 0) return;
    this->next_report_ms_ = millis() + this->interval_ms_;
    int64_t epoch;
    int y, mo, d;
    this->now_(&epoch, &y, &mo, &d);
    const CtMeasurement m = this->gather_();
    this->http_get_(build_set_ct_reporting_url(this->host_, this->ct002_->ct_type(),
                                               this->device_id_, epoch, y, mo, d, m));
  }
}

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_CLOUD_REPORTING
