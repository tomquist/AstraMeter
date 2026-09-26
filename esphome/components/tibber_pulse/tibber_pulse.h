#pragma once

// Tibber Pulse, read locally through the Pulse Bridge: polls the bridge's
// telegram endpoint over HTTP Basic auth, decodes the meter's SML telegram and
// publishes grid power in watts. The ESPHome counterpart of
// src/astrameter/powermeter/tibber_pulse.py, minus its push stream — stock
// ESPHome has no WebSocket client, so this only polls.
//
// The bridge's webserver is slow (answers regularly take over a second, #551)
// and http_request blocks for the whole exchange, so the request runs on a
// worker task of its own. update() hands it one request at a time; the main
// loop picks up the answer, decodes it and publishes. Nothing here ever waits
// on the network from the main loop.

#include <atomic>
#include <string>

#include "esphome/components/http_request/http_request.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

#include "bridge_endpoint.h"
#include "sml_power.h"

#ifdef USE_ESP32
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#else
#include <condition_variable>
#include <mutex>
#include <thread>
#endif

namespace esphome {
namespace tibber_pulse {

class TibberPulseComponent : public PollingComponent {
 public:
  void set_http(http_request::HttpRequestComponent *http) { this->http_ = http; }
  void set_host(const std::string &host) { this->host_ = host; }
  void set_node_id(const std::string &node_id) { this->node_id_ = node_id; }
  void set_user(const std::string &user) { this->user_ = user; }
  /// The full Authorization header value ("Basic ..."), built at codegen.
  void set_authorization(const std::string &authorization) { this->authorization_ = authorization; }
  void set_obis_total(const ObisCode &code) { this->obis_.total = code; }
  void set_obis_l1(const ObisCode &code) { this->obis_.l1 = code; }
  void set_obis_l2(const ObisCode &code) { this->obis_.l2 = code; }
  void set_obis_l3(const ObisCode &code) { this->obis_.l3 = code; }
  void set_power_sensor(sensor::Sensor *s) { this->power_sensor_ = s; }
  void set_power_l1_sensor(sensor::Sensor *s) { this->phase_sensors_[0] = s; }
  void set_power_l2_sensor(sensor::Sensor *s) { this->phase_sensors_[1] = s; }
  void set_power_l3_sensor(sensor::Sensor *s) { this->phase_sensors_[2] = s; }

  void setup() override;
  void update() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

 protected:
  /// Worker task: wait for a request, run it, hand the answer back.
  void worker_loop_();
  /// Worker task: one GET with the bridge's credentials, body read in full.
  HttpResponse http_get_(const std::string &url);
  void start_worker_();
  void wake_worker_();
  /// Main loop: log, decode and publish the worker's answer.
  void handle_result_(FetchResult &result);
  void publish_(const PowerReading &reading);
  void note_miss_(const char *what);

  http_request::HttpRequestComponent *http_{nullptr};
  std::string host_;
  std::string node_id_{"1"};
  std::string user_{"admin"};
  std::string authorization_;
  ObisSelection obis_;
  sensor::Sensor *power_sensor_{nullptr};
  sensor::Sensor *phase_sensors_[3]{nullptr, nullptr, nullptr};

  // Touched by the worker only.
  BridgeEndpoint endpoint_;

  // The handover. The main loop sets in_flight_ and wakes the worker; the
  // worker fills result_ and then sets result_ready_ (release), which is what
  // makes result_ safe for the main loop to read after an acquire load. Only
  // the main loop clears either, so a request is never started while the last
  // answer is still unread.
  bool in_flight_{false};
  std::atomic<bool> result_ready_{false};
  FetchResult result_;

  // Main loop bookkeeping.
  uint32_t last_good_ms_{0};
  bool had_good_{false};
  bool stale_reported_{false};
  bool warned_no_phases_{false};
  uint32_t skipped_updates_{0};

#ifdef USE_ESP32
  TaskHandle_t task_{nullptr};
#else
  std::mutex wake_lock_;
  std::condition_variable wake_;
  bool wake_pending_{false};
#endif
};

}  // namespace tibber_pulse
}  // namespace esphome
