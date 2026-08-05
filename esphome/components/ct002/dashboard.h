// AstraMeter live status dashboard, served from the ESP32 itself.
//
// The optional `dashboard:` sub-block of `ct002:` mounts two routes on
// ESPHome's shared HTTP server (web_server_base, the same one `web_server:`
// and `captive_portal:` use):
//
//   GET  <path>/                    the dashboard page — the exact same bundle
//                                   the Python add-on serves, gzipped in flash
//   GET  <path>/api/status          the status document (status_json.h)
//   POST <path>/api/control/consumer  per-battery writes, when `controls:` is on
//   POST <path>/api/control/device    device-wide writes, likewise
//
// The page is the one shipped in src/astrameter/static/dashboard.html, so the
// firmware and the add-on are never two different UIs. What differs is the
// document behind it: an ESPHome build serves a reduced one — no configuration
// surface (a device's config is compiled into its firmware) and no controls
// today — and the page renders whatever it receives.
//
// Threading: on ESP32 the HTTP handler runs on the httpd task, NOT the main
// loop, so it must never walk the live consumer map. The document is built in
// loop() and handed over as a finished string under a mutex; a request asks
// for a refresh and waits briefly for the main loop to produce one. See
// handle_status_ / rebuild_.
#pragma once

#include <cstdint>
#include <string>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"

#ifdef USE_CT002_DASHBOARD
#include "esphome/core/helpers.h"
#include "esphome/components/web_server_base/web_server_base.h"
#endif

#include "ct002.h"

namespace esphome {
namespace ct002 {
namespace dashboard {

#ifdef USE_CT002_DASHBOARD

class DashboardComponent : public Component, public AsyncWebHandler {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::WIFI - 1.0f; }

  void set_ct002(CT002Component *ct002) { this->ct002_ = ct002; }
  void set_base(web_server_base::WebServerBase *base) { this->base_ = base; }
  /// Mount prefix without a trailing slash; empty for the server root.
  void set_path(const std::string &path) { this->path_ = path; }
  void set_version(const std::string &version) { this->version_ = version; }
  void set_log_level(const std::string &level) { this->log_level_ = level; }
  /// Whether the write endpoints exist at all. Off unless the YAML asks:
  /// the page has no login of its own, so an unauthenticated LAN visitor
  /// should not be able to re-target someone's batteries.
  void set_controls(bool controls) { this->controls_ = controls; }

  // NOLINTNEXTLINE(readability-identifier-naming)
  bool canHandle(AsyncWebServerRequest *request) const override;
  // NOLINTNEXTLINE(readability-identifier-naming)
  void handleRequest(AsyncWebServerRequest *request) override;

 protected:
  /// One validated control waiting for the main loop to apply it.
  struct PendingWrite {
    /// Empty for a device-wide control.
    std::string consumer_id;
    std::string field;
    controls::ControlValue value;
  };

  void handle_page_(AsyncWebServerRequest *request);
  void handle_status_(AsyncWebServerRequest *request);
  void handle_control_(AsyncWebServerRequest *request, bool device_wide);
  /// Rebuild the cached document. Main loop only — it walks live state.
  void rebuild_();
  /// Hand *write* to the main loop and wait for it. False = the loop never
  /// got to it (the caller answers 503) — `*applied` says whether it landed.
  bool submit_write_(const PendingWrite &write, bool *applied);

  CT002Component *ct002_{nullptr};
  web_server_base::WebServerBase *base_{nullptr};
  std::string path_;
  std::string version_;
  std::string log_level_;
  bool controls_{false};

  // Handover between the httpd task and the main loop. `document_` and
  // `pending_` are only ever touched under `lock_`; the flags are one-way
  // requests set by a handler and cleared by the loop, and the generation
  // counters let a waiting handler tell "the loop has been round since I
  // asked" from "still stale".
  Mutex lock_;
  std::string document_;
  volatile bool refresh_requested_{false};
  volatile uint32_t generation_{0};
  uint32_t seq_{0};

  bool pending_valid_{false};
  PendingWrite pending_;
  bool pending_applied_{false};
  volatile uint32_t write_generation_{0};
};

#endif  // USE_CT002_DASHBOARD

}  // namespace dashboard
}  // namespace ct002
}  // namespace esphome
