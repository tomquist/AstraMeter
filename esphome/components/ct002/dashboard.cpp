#include "dashboard.h"

#ifdef USE_CT002_DASHBOARD

#include <ctime>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "dashboard_asset.h"

namespace esphome {
namespace ct002 {
namespace dashboard {

static const char *const TAG = "astrameter.dashboard";

// How long a request waits for the main loop to build it a fresh document.
// The loop runs at tens of hertz, so this is normally over in a few
// milliseconds; the ceiling only matters when the loop is busy (a Wi-Fi
// reconnect, a long UDP burst) and keeps the httpd task from parking there.
static constexpr uint32_t STATUS_WAIT_MS = 500;
static constexpr uint32_t STATUS_POLL_MS = 5;

namespace {

std::string request_url(AsyncWebServerRequest *request) {
#ifdef USE_ESP32
  char buffer[AsyncWebServerRequest::URL_BUF_SIZE];
  const auto url = request->url_to(buffer);
  return std::string(url.begin(), url.end());
#else
  return std::string(request->url().c_str());
#endif
}

/// The current wall-clock epoch, or 0 when the clock has not synced. Without
/// SNTP an ESP32 counts from 1970, and a 1970 timestamp on the page is worse
/// than no timestamp at all — everything downstream treats 0 as "unknown".
double wall_clock_epoch() {
  const double epoch = static_cast<double>(::time(nullptr));
  return epoch >= status::WALL_CLOCK_SANE_THRESHOLD ? epoch : 0.0;
}

}  // namespace

void DashboardComponent::setup() {
  if (this->ct002_ == nullptr || this->base_ == nullptr) {
    ESP_LOGE(TAG, "not bound to ct002 / web server — refusing to start");
    this->mark_failed();
    return;
  }
  this->base_->init();
  this->base_->add_handler(this);
}

void DashboardComponent::loop() {
  // Built only when a request is waiting for one: with nobody watching, an
  // idle device should not be spending heap and CPU on a document that will
  // never be read.
  if (!this->refresh_requested_) return;
  this->refresh_requested_ = false;
  this->rebuild_();
}

void DashboardComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "AstraMeter dashboard:");
  // Still called after a failed setup(), where there is no server to ask.
  if (this->base_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  URL: http://<device>:%u%s/", this->base_->get_port(),
                  this->path_.c_str());
  }
  ESP_LOGCONFIG(TAG, "  Page: %zu bytes (gzipped)", sizeof(DASHBOARD_HTML_GZ));
}

bool DashboardComponent::canHandle(AsyncWebServerRequest *request) const {
  if (request->method() != HTTP_GET) return false;
  const std::string url = request_url(request);
  if (url.compare(0, this->path_.size(), this->path_) != 0) return false;
  const std::string rest = url.substr(this->path_.size());
  return rest.empty() || rest == "/" || rest == "/index.html" || rest == "/api/status";
}

void DashboardComponent::handleRequest(AsyncWebServerRequest *request) {
  const std::string url = request_url(request);
  const std::string rest = url.substr(this->path_.size());
  if (rest == "/api/status") {
    this->handle_status_(request);
    return;
  }
  if (rest.empty()) {
    // Every URL the page asks for is document-relative, so without the
    // trailing slash "api/status" would resolve one level too high — the same
    // reason Home Assistant's ingress serves its panel from a directory URL.
    request->redirect(this->path_ + "/");
    return;
  }
  this->handle_page_(request);
}

void DashboardComponent::handle_page_(AsyncWebServerRequest *request) {
  auto *response =
      request->beginResponse(200, "text/html", DASHBOARD_HTML_GZ, sizeof(DASHBOARD_HTML_GZ));
  response->addHeader("Content-Encoding", "gzip");
  // The page ships inside the firmware, so a cached copy outlives the update
  // that replaced it unless the browser revalidates.
  response->addHeader("Cache-Control", "no-cache");
  request->send(response);
}

void DashboardComponent::handle_status_(AsyncWebServerRequest *request) {
  // The live state belongs to the main loop; ask it for a document and wait
  // for one built after this request rather than reading the maps from here.
  const uint32_t start = this->generation_;
  this->refresh_requested_ = true;
  const uint32_t began_at = millis();
  while (this->generation_ == start && millis() - began_at < STATUS_WAIT_MS) {
    delay(STATUS_POLL_MS);
  }

  std::string body;
  {
    LockGuard guard(this->lock_);
    body = this->document_;
  }
  if (body.empty()) {
    // The loop never got round to us. Saying so beats serving a stale or
    // half-built document that the page would render as current.
    request->send(503, "application/json", "{\"error\":\"status not ready\"}");
    return;
  }
  request->send(200, "application/json", body.c_str());
}

void DashboardComponent::rebuild_() {
  const double wall = wall_clock_epoch();
  const double uptime = static_cast<double>(millis()) / 1000.0;

  status::StatusDocument doc;
  if (wall > 0.0) {
    doc.generated_at = wall;
    doc.service.started_at = wall - uptime;
  }
  doc.seq = ++this->seq_;
  doc.uptime_s = static_cast<float>(uptime);
  doc.service.version = this->version_;
  doc.service.log_level = this->log_level_;
  doc.service.web_port = this->base_->get_port();
  doc.powermeters.push_back(this->ct002_->powermeter_status());
  doc.devices.push_back(this->ct002_->status_snapshot(wall));

  // Serialize outside the lock: only the handover is contended, and the
  // httpd task should never wait on the encoder.
  std::string body = status::build_status_json(doc);
  {
    LockGuard guard(this->lock_);
    this->document_ = std::move(body);
  }
  this->generation_++;
}

}  // namespace dashboard
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_DASHBOARD
