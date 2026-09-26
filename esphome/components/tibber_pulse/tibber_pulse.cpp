#include "tibber_pulse.h"

#include <algorithm>
#include <cstdint>
#include <utility>

#include "esphome/components/network/util.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace tibber_pulse {

static const char *const TAG = "tibber_pulse";

// The Pulse Bridge mirrors a push source (the meter emits ~1/s, with jitter):
// a poll occasionally returns an incomplete or CRC-bad telegram. Such misses
// heal on their own, so they only count as a problem once no telegram has
// decoded for this long (#518) — the same window the Python source uses.
static constexpr uint32_t STALE_AFTER_MS = 15000;

// SML telegrams are a few hundred bytes; an eHZ with every register enabled
// stays well under 2 KB. Anything larger is not a telegram.
static constexpr size_t MAX_BODY = 4096;
static constexpr size_t READ_CHUNK = 512;

#ifdef USE_ESP32
// esp_http_client plus ESP_LOG formatting; the body lives on the heap.
static constexpr uint32_t WORKER_STACK = 6144;
#endif

void TibberPulseComponent::setup() {
  if (this->http_ == nullptr) {
    ESP_LOGE(TAG, "http_request not bound");
    this->mark_failed();
    return;
  }
  this->start_worker_();
  // The loop only has work once the worker has an answer; it re-enables it.
  this->disable_loop();
}

void TibberPulseComponent::start_worker_() {
#ifdef USE_ESP32
  const BaseType_t created = xTaskCreate(
      [](void *arg) { static_cast<TibberPulseComponent *>(arg)->worker_loop_(); }, "tibber_pulse", WORKER_STACK,
      this, 1, &this->task_);
  if (created != pdPASS) {
    ESP_LOGE(TAG, "Could not start the request task");
    this->mark_failed();
  }
#else
  std::thread([this]() { this->worker_loop_(); }).detach();
#endif
}

void TibberPulseComponent::wake_worker_() {
#ifdef USE_ESP32
  xTaskNotifyGive(this->task_);
#else
  {
    std::lock_guard<std::mutex> guard(this->wake_lock_);
    this->wake_pending_ = true;
  }
  this->wake_.notify_one();
#endif
}

void TibberPulseComponent::update() {
  if (this->is_failed()) return;
  if (this->in_flight_) {
    // The bridge is slower than update_interval. Skip rather than queue: the
    // answer on its way is as fresh as a queued one would be.
    if (++this->skipped_updates_ == 1) ESP_LOGD(TAG, "Previous request still running; skipping this poll");
    return;
  }
  if (!network::is_connected()) {
    ESP_LOGV(TAG, "Network not connected; not polling");
    return;
  }
  this->skipped_updates_ = 0;
  this->in_flight_ = true;
  this->wake_worker_();
}

void TibberPulseComponent::worker_loop_() {
  for (;;) {
#ifdef USE_ESP32
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
#else
    {
      std::unique_lock<std::mutex> guard(this->wake_lock_);
      this->wake_.wait(guard, [this]() { return this->wake_pending_; });
      this->wake_pending_ = false;
    }
#endif
    this->result_ = this->endpoint_.fetch(this->host_, this->node_id_,
                                          [this](const std::string &url) { return this->http_get_(url); });
    this->result_ready_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
  }
}

HttpResponse TibberPulseComponent::http_get_(const std::string &url) {
  HttpResponse response;
  const std::vector<http_request::Header> headers = {{"Authorization", this->authorization_}};
  auto container = this->http_->get(url, headers);
  if (container == nullptr) return response;
  response.status = container->status_code;
  if (response.status < 200 || response.status >= 300) {
    container->end();
    return response;
  }
  // ESP-IDF reports a body of unknown length as (size_t) -1; only a length
  // the bridge actually declared can be too large.
  const size_t declared = container->content_length;
  if (declared > MAX_BODY && declared < SIZE_MAX / 2) {
    ESP_LOGW(TAG, "Bridge sent %u bytes, more than a telegram; ignoring it", static_cast<unsigned>(declared));
    container->end();
    response.status = -1;
    return response;
  }
  // The body is binary SML, NULs and all: read it as bytes, never as a
  // C string. http_read_fully() is not used because it feeds the main loop's
  // watchdog, which this task does not own; container->read() feeds a
  // watchdog only when the calling task has one.
  response.body.resize(MAX_BODY);
  size_t read = 0;
  uint32_t last_data = millis();
  const uint32_t timeout_ms = this->http_->get_timeout();
  auto step = http_request::HttpReadLoopResult::RETRY;
  while (read < MAX_BODY) {
    const int got = container->read(response.body.data() + read, std::min(READ_CHUNK, MAX_BODY - read));
    step = http_request::http_read_loop_result(got, last_data, timeout_ms, container->is_read_complete());
    if (step == http_request::HttpReadLoopResult::RETRY) continue;
    if (step != http_request::HttpReadLoopResult::DATA) break;
    read += static_cast<size_t>(got);
  }
  const bool complete = container->is_read_complete();
  container->end();
  if (read == MAX_BODY && !complete) {
    ESP_LOGW(TAG, "Telegram larger than %u bytes; ignoring it", static_cast<unsigned>(MAX_BODY));
    response.status = -1;
    response.body.clear();
    return response;
  }
  if (read == 0 && step != http_request::HttpReadLoopResult::COMPLETE) {
    ESP_LOGW(TAG, "Reading the telegram failed (%s)",
             step == http_request::HttpReadLoopResult::TIMEOUT ? "timeout" : "connection error");
    response.status = -1;
    response.body.clear();
    return response;
  }
  // A body that stopped short is kept: the telegram's own CRC is what decides
  // whether it is whole, and a bridge that sends no length at all ends its
  // body exactly this way.
  if (step != http_request::HttpReadLoopResult::COMPLETE && read < MAX_BODY)
    ESP_LOGV(TAG, "Body ended without completing (%u bytes)", static_cast<unsigned>(read));
  response.body.resize(read);
  return response;
}

void TibberPulseComponent::loop() {
  if (!this->result_ready_.load(std::memory_order_acquire)) {
    this->disable_loop();
    return;
  }
  FetchResult result = std::move(this->result_);
  this->result_ = FetchResult{};
  this->result_ready_.store(false, std::memory_order_relaxed);
  this->in_flight_ = false;
  this->disable_loop();
  this->handle_result_(result);
}

void TibberPulseComponent::handle_result_(FetchResult &result) {
  const char *path = endpoint_path(result.endpoint);
  if (!result.ok) {
    this->status_set_warning();
    if (result.status == 401) {
      ESP_LOGW(TAG, "Bridge refused the credentials (HTTP 401) — check the password printed on the bridge");
    } else if (result.status == 404) {
      ESP_LOGW(TAG,
               "Bridge serves neither /node_data.json nor /data.json for node_id %s (HTTP 404) — is its local "
               "webserver enabled, and is the node id right?",
               this->node_id_.c_str());
    } else if (result.status < 0) {
      ESP_LOGW(TAG, "No answer from the bridge at %s on /%s", this->host_.c_str(), path);
    } else {
      ESP_LOGW(TAG, "Bridge answered HTTP %d on /%s", result.status, path);
    }
    return;
  }
  if (result.switched) ESP_LOGI(TAG, "Bridge serves /%s, using it from now on", path);

  PowerReading reading;
  const DecodeStatus status = decode_sml_power(result.body.data(), result.body.size(), this->obis_, &reading);
  if (status != DecodeStatus::OK) {
    ESP_LOGV(TAG, "Undecodable telegram (%s, %u bytes)", decode_status_str(status),
             static_cast<unsigned>(result.body.size()));
    this->note_miss_(decode_status_str(status));
    return;
  }
  this->had_good_ = true;
  this->last_good_ms_ = millis();
  this->stale_reported_ = false;
  this->status_clear_warning();
  this->publish_(reading);
}

void TibberPulseComponent::note_miss_(const char *what) {
  // An occasional bad telegram is normal for this bridge; only a run of them
  // is worth a warning.
  if (this->had_good_ && millis() - this->last_good_ms_ <= STALE_AFTER_MS) {
    ESP_LOGD(TAG, "Could not decode the telegram (%s); keeping the last reading", what);
    return;
  }
  this->status_set_warning();
  if (!this->stale_reported_) {
    ESP_LOGW(TAG, "Could not decode the SML telegram from the bridge (%s)", what);
    this->stale_reported_ = true;
  } else {
    ESP_LOGD(TAG, "Could not decode the SML telegram from the bridge (%s)", what);
  }
}

void TibberPulseComponent::publish_(const PowerReading &reading) {
  if (reading.has_phases) {
    ESP_LOGD(TAG, "Grid power %.0f W (L1 %.0f W, L2 %.0f W, L3 %.0f W)", reading.total, reading.phases[0],
             reading.phases[1], reading.phases[2]);
  } else {
    ESP_LOGD(TAG, "Grid power %.0f W", reading.total);
  }
  if (this->power_sensor_ != nullptr && reading.has_total)
    this->power_sensor_->publish_state(static_cast<float>(reading.total));
  bool want_phases = false;
  for (int p = 0; p < 3; p++) {
    if (this->phase_sensors_[p] == nullptr) continue;
    want_phases = true;
    if (reading.has_phases) this->phase_sensors_[p]->publish_state(static_cast<float>(reading.phases[p]));
  }
  if (want_phases && !reading.has_phases && !this->warned_no_phases_) {
    ESP_LOGW(TAG,
             "The meter reports no per-phase power (all three of the L1-L3 registers); only `power` can be "
             "published. Use `power` instead, or set obis_power_l1..l3 to the meter's registers.");
    this->warned_no_phases_ = true;
  }
}

void TibberPulseComponent::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Tibber Pulse Bridge:\n"
                "  Host: %s\n"
                "  Node ID: %s\n"
                "  User: %s",
                this->host_.c_str(), this->node_id_.c_str(), this->user_.c_str());
  LOG_UPDATE_INTERVAL(this);
  LOG_SENSOR("  ", "Power", this->power_sensor_);
  LOG_SENSOR("  ", "Power L1", this->phase_sensors_[0]);
  LOG_SENSOR("  ", "Power L2", this->phase_sensors_[1]);
  LOG_SENSOR("  ", "Power L3", this->phase_sensors_[2]);
}

}  // namespace tibber_pulse
}  // namespace esphome
