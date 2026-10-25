// AstraMeter Marstek HTTP cloud-reporting component.
//
// Mirrors src/astrameter/cloud_reporting.py adapted for ESPHome. A real CT002
// (`HME-4`) / CT003 (`HME-3`) on its newest firmware (v124 / v122) reports to
// the Marstek cloud (hamedata.com) over plain HTTP GET — no TLS, no
// token/signature; the device is identified only by the cleartext `id`/`uid`
// query params. Every cycle:
//
//   1. a handshake `getDateInfoeu.php` (uid/fcv/aid/sv), whose body is the
//      server's wall-clock time; on success the day/hour energy registers roll
//      forward against it, then
//   2. a `setCtReporting` GET, retried until the cloud answers, with `timeNo` =
//      the 10-minute slot of the server's day and `date` = the server's date,
//   3. a 300 s wait, then back to the handshake.
//
// The pure helpers live in cloud_reporting_url.{h,cpp} (no ESPHome deps) so
// the wire format is host-testable. This component runs the cycle as a
// loop()-driven state machine (like marstek_registration) so the watchdog stays
// fed between HTTP calls. It is gated by USE_CT002_CLOUD_REPORTING (set from
// _to_code_cloud_reporting in ct002/__init__.py) because it needs http_request.
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
    WAIT_FOR_NETWORK,  // hold until the network is first up.
    HANDSHAKE,         // getDateInfo, retried until it answers.
    REPORT,            // setCtReporting, retried until it answers.
    WAIT,              // the interval after an accepted report.
    FATAL,             // misconfigured (no ct002/http). Stop.
  };

  bool network_ready_() const;
  // Gather the current measurement from the bound CT002 component.
  CtMeasurement gather_() const;
  // Feed the current grid power into the energy register.
  void sample_energy_(const CtMeasurement &m);
  // Wall-clock fallback for a handshake that carried no server time; the epoch
  // (1970-01-01) when no time source has synced.
  ServerTime local_time_() const;
  // Perform one plain-HTTP GET. Returns the status (-1 when the request never
  // completed) and, when *body* is set, up to 512 bytes of the response.
  int http_get_(const std::string &url, std::string *body = nullptr);
  bool handshake_();
  bool report_();
  // Stay in the current state until `retry_ms` from now.
  void retry_later_();
  int sv_or_default_() const;

  // Configuration.
  ct002::CT002Component *ct002_{nullptr};
  http_request::HttpRequestComponent *http_{nullptr};
  std::string host_{"eu.hamedata.com"};
  // The reported id is the CT MAC, followed in loop() from ct002_->app_mac()
  // (marstek_registration may set it after our setup()).
  std::string device_id_;
  std::string fcv_{"202409090159"};
  // Sent as `sv` in the handshake, which the cloud writes into the device's
  // `version` field. Negative = the firmware version a reporting CT of the
  // configured model runs (reporting_firmware_version).
  int sv_{-1};
  uint32_t interval_ms_{300000};
  // A real CT retries on its next main-loop pass, bounded by the Wi-Fi
  // module's 60 s HTTP timeout. Mirrors RETRY_SECONDS.
  uint32_t retry_ms_{60000};
  // How often the grid power is sampled into the energy register.
  uint32_t sample_ms_{10000};

  // State.
  State state_{State::WAIT_FOR_NETWORK};
  uint32_t next_attempt_ms_{0};
  uint32_t next_sample_ms_{0};
  EnergyIntegrator energy_;
  EnergyCounters counters_;
  // The server time from the last handshake; every report until the next
  // handshake (retries included) carries it, as on a real CT.
  ServerTime server_time_;
};

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
