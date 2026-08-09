#include "dashboard.h"

#ifdef USE_CT002_DASHBOARD

#include <ctime>
#include <string_view>

#include "esphome/components/json/json_util.h"
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
// Assumes a 1 kHz FreeRTOS tick, which ESPHome pins (CONFIG_FREERTOS_HZ=1000).
// At IDF's own 100 Hz default this would round down to vTaskDelay(0) — no
// yield — and the httpd task would spin out the whole wait without ever
// letting the lower-priority main loop produce what it is waiting for.
static constexpr uint32_t STATUS_POLL_MS = 5;

// A control write is four short fields. Anything larger is not one of ours,
// and reading it would only tie up the httpd task's buffer.
static constexpr size_t MAX_CONTROL_BODY = 512;

// How old the cached document may be before a request is answered 503
// instead. Comfortably past the page's poll interval plus the wait above, so
// only a genuinely stuck main loop reaches it.
static constexpr uint32_t STATUS_STALE_MS = 10000;

namespace {

/// The request path, copied into *buffer* (which must outlive the result).
///
/// No non-ESP32 branch: `dashboard:` is ESP32-only, and everything below reads
/// the raw `httpd_req_t` anyway. Returning a view rather than a string keeps
/// this allocation-free — it runs twice per request, on the httpd task.
std::string_view request_url(AsyncWebServerRequest *request,
                             char (&buffer)[AsyncWebServerRequest::URL_BUF_SIZE]) {
  const auto url = request->url_to(buffer);
  return std::string_view(url.begin(), url.size());
}

/// The part of *url* after the mount prefix, or nothing when it is elsewhere.
optional<std::string_view> strip_prefix(std::string_view url, const std::string &prefix) {
  if (url.compare(0, prefix.size(), prefix) != 0) return {};
  return url.substr(prefix.size());
}

/// Answer with a JSON body under the status code the API actually promises.
///
/// ESPHome's IDF web server maps only 200, 404 and 409 to a status line and
/// turns every other code into 500 (AsyncWebServerRequest::init_response_), so
/// `request->send(503, ...)` would reach the page as a server error and the
/// page's "is this retryable / is this my fault" split would be wrong. The
/// status goes onto the request handle directly instead — the same handle the
/// control body is read from a few lines below.
void send_json(AsyncWebServerRequest *request, int code, const char *body) {
  httpd_req_t *raw = *request;
  const char *status;
  switch (code) {
    case 400:
      status = "400 Bad Request";
      break;
    case 403:
      status = "403 Forbidden";
      break;
    case 404:
      status = "404 Not Found";
      break;
    case 415:
      status = "415 Unsupported Media Type";
      break;
    case 503:
      status = "503 Service Unavailable";
      break;
    default:
      status = "200 OK";
      break;
  }
  // httpd keeps the pointer rather than a copy, so these must be literals.
  httpd_resp_set_status(raw, status);
  httpd_resp_set_type(raw, "application/json");
  httpd_resp_send(raw, body, HTTPD_RESP_USE_STRLEN);
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
  // A write waiting on the httpd task. Applied here, on the loop that owns
  // the consumer map, and only then reported back as done.
  PendingWrite write;
  uint32_t ticket = 0;
  bool have_write = false;
  {
    LockGuard guard(this->lock_);
    // Taken, not just read: it counts as in flight from here until it is
    // applied, so a handler that gives up waiting can neither cancel it nor
    // let the next request reserve the slot and read this write's answer.
    have_write = this->writes_.take(&write, &ticket);
  }
  if (have_write) {
    const bool applied =
        write.consumer_id.empty()
            ? apply_device_control(this->ct002_, write.field, write.value)
            : apply_consumer_control(this->ct002_, write.consumer_id, write.field, write.value);
    // Mirror the setting onto its retained command topic, or the broker
    // replays the old value on the next reconnect and undoes this.
    // force_rotation is a button, not a setting: it has no retained state to
    // protect, and a retained press would re-fire on every reconnect.
    if (applied && this->mqtt_insights_ != nullptr && !controls::is_device_button(write.field)) {
      if (write.consumer_id.empty()) {
        this->mqtt_insights_->mirror_device_command(write.field, write.wire_value);
      } else {
        this->mqtt_insights_->mirror_consumer_command(write.consumer_id, write.field,
                                                      write.wire_value);
      }
    }
    {
      LockGuard guard(this->lock_);
      this->writes_.complete(ticket, applied);
    }
    // No refresh needed here: a poll always asks for one itself and waits for
    // a generation newer than its request, so it can never be served the frame
    // built before this write landed.
  }

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
  char buffer[AsyncWebServerRequest::URL_BUF_SIZE];
  const auto stripped = strip_prefix(request_url(request, buffer), this->path_);
  if (!stripped.has_value()) return false;
  const std::string_view rest = *stripped;
  if (request->method() == HTTP_POST) {
    // Claimed even with controls off, so the page gets a 403 that says why
    // rather than a 404 that reads like a broken build.
    return rest == "/api/control/consumer" || rest == "/api/control/device";
  }
  if (request->method() != HTTP_GET) return false;
  return rest.empty() || rest == "/" || rest == "/index.html" || rest == "/api/status";
}

void DashboardComponent::handleRequest(AsyncWebServerRequest *request) {
  char buffer[AsyncWebServerRequest::URL_BUF_SIZE];
  const std::string_view rest = request_url(request, buffer).substr(this->path_.size());
  if (rest == "/api/control/consumer" || rest == "/api/control/device") {
    this->handle_control_(request, rest == "/api/control/device");
    return;
  }
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
  uint32_t age = 0;
  {
    LockGuard guard(this->lock_);
    body = this->document_;
    age = millis() - this->built_at_;
  }
  // Nothing built yet, or the main loop has been stuck long enough that what
  // we hold is no longer a description of now. Saying so beats serving a
  // frozen document the page would render as current: without a wall clock
  // its ages are computed here, so a stale frame is indistinguishable from a
  // live one at the browser.
  if (body.empty() || age > STATUS_STALE_MS) {
    send_json(request, 503, "{\"error\":\"status not ready\"}");
    return;
  }
  request->send(200, "application/json", body.c_str());
}

void DashboardComponent::handle_control_(AsyncWebServerRequest *request, bool device_wide) {
  if (!this->controls_) {
    send_json(request, 403,
              "{\"error\":\"Controls are off. Set `controls: true` on this device's "
              "dashboard block to allow writes.\"}");
    return;
  }

  // Insist on the page's own content type, and not only to be strict about
  // the wire format.
  //
  // A browser may send text/plain, application/x-www-form-urlencoded or
  // multipart/form-data cross-origin with no preflight, so without this check
  // any web page the owner happens to visit could POST here — the body of a
  // no-cors fetch() is text/plain by default, and the ESP-IDF shim's fallback
  // takes every content type that is not a form. `application/json` is not on
  // that safelist: asking for it forces a preflight, and the preflight has no
  // handler and 404s. Same-origin callers (the page) are unaffected.
  const auto content_type = request->get_header("Content-Type");
  if (!content_type.has_value() ||
      content_type.value().find("application/json") == std::string::npos) {
    send_json(request, 415,
              "{\"error\":\"Content-Type must be application/json\"}");
    return;
  }

  // Read the body ourselves. The ESP-IDF shim only parses form-encoded POSTs
  // into request params, and logs an "unsupported content type" warning for
  // anything else — but it leaves the body unread on the socket, so the JSON
  // the page sends is still here for us. Reading it keeps ONE wire format
  // across both backends instead of a second encoding just for the firmware.
  // It also means the check above is what keeps this recv honest: for a form
  // body the shim has already drained the socket, and we would find nothing.
  httpd_req_t *raw = *request;
  const size_t length = raw->content_len;
  if (length == 0 || length > MAX_CONTROL_BODY) {
    send_json(request, 400, "{\"error\":\"Invalid request: bad body length\"}");
    return;
  }
  std::string body(length, '\0');
  size_t got = 0;
  while (got < length) {
    const int chunk = httpd_req_recv(raw, &body[got], length - got);
    if (chunk <= 0) {
      send_json(request, 400, "{\"error\":\"Invalid request: body not received\"}");
      return;
    }
    got += static_cast<size_t>(chunk);
  }

  DashboardComponent::PendingWrite write;
  std::string error;
  const bool parsed = json::parse_json(body, [&](JsonObject root) -> bool {
    if (!device_wide) {
      if (!root["consumer_id"].is<const char *>()) {
        error = "Invalid request: consumer_id";
        return false;
      }
      write.consumer_id = root["consumer_id"].as<std::string>();
    }
    if (!root["field"].is<const char *>()) {
      error = "Invalid request: field";
      return false;
    }
    write.field = root["field"].as<std::string>();
    JsonVariant value = root["value"];
    if (value.is<bool>()) {
      write.value.is_bool = true;
      write.value.flag = value.as<bool>();
      write.wire_value = write.value.flag ? "true" : "false";
    } else if (value.is<float>()) {
      write.value.number = value.as<float>();
      // Captured before coercion scales it — the retained mirror carries the
      // unit the wire uses, which is the unit the reader scales from.
      write.wire_value = status::format_number(static_cast<double>(write.value.number), 3);
    } else if (device_wide && controls::is_device_button(write.field) && value.isNull()) {
      // `force_rotation` is a button and carries no value; the page posts it
      // bare. Every other device field is a setting, and inventing `true` for
      // one would switch on something nobody asked for — `active_control`
      // most of all.
      write.value.is_bool = true;
      write.value.flag = true;
      write.wire_value = "true";
    } else {
      error = "Invalid request: value";
      return false;
    }
    return true;
  });
  if (!parsed) {
    const std::string payload =
        "{\"error\":\"" + (error.empty() ? std::string("Invalid request") : error) + "\"}";
    send_json(request, 400, payload.c_str());
    return;
  }

  if (device_wide) {
    if (!controls::is_device_field(write.field)) {
      send_json(request, 404, "{\"error\":\"Unknown field\"}");
      return;
    }
  } else {
    // Same bounds as the MQTT command handlers and the Python dashboard: a
    // value one of them would refuse must not be settable here, or the next
    // retained-command replay would silently undo it.
    const std::string message = controls::coerce_consumer_control(write.field, write.value);
    if (!message.empty()) {
      const bool unknown = !controls::is_consumer_field(write.field);
      const std::string payload = "{\"error\":\"" + message + "\"}";
      send_json(request, unknown ? 404 : 400, payload.c_str());
      return;
    }
  }

  bool applied = false;
  if (!this->submit_write_(write, &applied)) {
    send_json(request, 503, "{\"error\":\"Device busy, try again\"}");
    return;
  }
  if (!applied) {
    send_json(request, 404, "{\"error\":\"Unknown device or field\"}");
    return;
  }
  ESP_LOGI(TAG, "control: %s%s%s = %s", write.consumer_id.c_str(),
           write.consumer_id.empty() ? "" : ".", write.field.c_str(),
           write.value.is_bool ? (write.value.flag ? "true" : "false")
                               : status::format_number(write.value.number, 3).c_str());
  request->send(200, "application/json", "{\"applied\":true}");
}

bool DashboardComponent::submit_write_(const PendingWrite &write, bool *applied) {
  uint32_t ticket;
  {
    LockGuard guard(this->lock_);
    // One write at a time: these come from a user's tap, so a queue would
    // only ever hold a duplicate of what is already going.
    ticket = this->writes_.reserve(write);
  }
  if (ticket == 0) return false;
  const uint32_t began_at = millis();
  while (this->writes_.completed() != ticket && millis() - began_at < STATUS_WAIT_MS) {
    delay(STATUS_POLL_MS);
  }
  LockGuard guard(this->lock_);
  if (this->writes_.result(ticket, applied)) return true;
  // The loop never got to *this* write. Withdraw it so it cannot land minutes
  // later, after the request was answered, and so the slot does not stay
  // occupied and 503 every write after it. Withdrawing is refused once the
  // loop has taken it: it is being applied right now and cannot be recalled,
  // and 503 is the honest end of a request we waited out — the next poll
  // shows what actually landed.
  this->writes_.withdraw(ticket);
  return false;
}

void DashboardComponent::rebuild_() {
  const double wall = wall_clock_epoch();
  const double uptime = static_cast<double>(millis()) / 1000.0;

  status::StatusDocument doc;
  if (wall > 0.0) {
    doc.generated_at = wall;
    doc.service.started_at = wall - uptime;
  }
  doc.capabilities.controls = this->controls_;
  doc.seq = this->generation_.load(std::memory_order_relaxed) + 1;
  doc.uptime_s = static_cast<float>(uptime);
  doc.service.version = this->version_;
  doc.service.log_level = this->log_level_;
  doc.service.web_port = this->base_->get_port();
  doc.powermeters.push_back(this->ct002_->powermeter_status());
  doc.devices.push_back(this->ct002_->status_snapshot(wall));
  if (this->mqtt_insights_ != nullptr) {
    doc.mqtt_insights = this->mqtt_insights_->status_snapshot();
  }

  // Serialize outside the lock: only the handover is contended, and the
  // httpd task should never wait on the encoder.
  std::string body = status::build_status_json(doc);
  {
    LockGuard guard(this->lock_);
    this->document_ = std::move(body);
    this->built_at_ = millis();
  }
  this->generation_++;
}

}  // namespace dashboard
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_DASHBOARD
