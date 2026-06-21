// AstraMeter Marstek HTTP cloud-reporting component.
//
// Mirrors src/astrameter/cloud_reporting.py adapted for ESPHome. A real CT002
// (`HME-4`) / CT003 (`HME-3`) periodically reports to the Marstek cloud
// (hamedata.com) over plain HTTP GET — no TLS, no token/signature; the device is
// identified only by the cleartext `id`/`aid` query params. The flow:
//
//   1. a one-shot handshake `getDateInfoeu.php` (uid/fcv/aid/sv), then
//   2. a timer-driven `setCtReporting` GET with live grid power, the per-bucket
//      charge/discharge split, link state and an incrementing `timeNo`.
//
// The pure URL builders live in cloud_reporting_url.{h,cpp} (no ESPHome deps) so
// the wire format is host-testable. This component runs the
// handshake-then-report flow as a loop()-driven state machine (like
// marstek_registration) so the watchdog stays fed between HTTP calls. It is
// gated by USE_CT002_CLOUD_REPORTING (set from _to_code_cloud_reporting in
// ct002/__init__.py) because it needs http_request.
#pragma once

#include <cstdint>
#include <string>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"

#ifdef USE_CT002_CLOUD_REPORTING
#include "esphome/components/http_request/http_request.h"
#endif

#include "cloud_reporting_url.h"
#include "ct002.h"

namespace esphome {
#ifndef USE_CT002_CLOUD_REPORTING
namespace http_request {
class HttpRequestComponent;
}
#endif
namespace ct002 {
namespace cloud_reporting {

class CloudReportingComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

  // Configuration setters — wired by the codegen.
  void set_ct002(ct002::CT002Component *c) { this->ct002_ = c; }
  void set_http(http_request::HttpRequestComponent *h) { this->http_ = h; }
  void set_host(const std::string &v) { this->host_ = v; }
  void set_fcv(const std::string &v) { this->fcv_ = v; }
  void set_sv(int v) { this->sv_ = v; }
  void set_interval_ms(uint32_t v) { this->interval_ms_ = v; }

 protected:
  enum class State {
    WAIT_FOR_NETWORK,  // hold until the network is up.
    HANDSHAKE,         // one-shot getDateInfo.
    REPORT,            // periodic setCtReporting.
    FATAL,             // misconfigured (no ct002/http). Stop.
  };

  bool network_ready_() const;
  // Gather the current measurement from the bound CT002 component.
  CtMeasurement gather_() const;
  // Best-effort wall-clock (epoch + Y/M/D) for `timeNo`/`date`; falls back to
  // the epoch (1970-01-01) when no time source has synced.
  void now_(int64_t *epoch, int *year, int *month, int *day) const;
  // Perform one plain-HTTP GET, ignoring the body (logs the status).
  void http_get_(const std::string &url);

  // Configuration.
  ct002::CT002Component *ct002_{nullptr};
  http_request::HttpRequestComponent *http_{nullptr};
  std::string host_{"eu.hamedata.com"};
  // The reported id is the CT MAC, resolved lazily in loop() from
  // ct002_->ct_mac() (marstek_registration may set it after our setup()).
  std::string device_id_;
  std::string fcv_{"202409090159"};
  // Sent as `sv` in the handshake, which the cloud writes into the device's
  // `version` field; defaults to the managed registration version (121).
  int sv_{121};
  uint32_t interval_ms_{60000};

  // State.
  State state_{State::WAIT_FOR_NETWORK};
  uint32_t next_report_ms_{0};
};

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
