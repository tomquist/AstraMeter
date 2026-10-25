// Pure helpers for the Marstek HTTP cloud reporting (hamedata.com): the URL
// builders, the handshake's server-time parser and the energy registers.
//
// Dependency-free (only <string>/<cstdint>) so the host gtest
// (tests/components/ct002/host_cloud_reporting_test.cpp) can cover the wire
// format without any ESPHome headers. Mirrors src/astrameter/cloud_reporting.py —
// the two sides must emit byte-identical query strings (incl. the model
// differences and the HME-3 missing-`&` quirk).
#pragma once

#include <cstdint>
#include <string>

namespace esphome {
namespace ct002 {
namespace cloud_reporting {

// The live values a CT puts in a `setCtReporting` GET. Powers are watts, signed
// (import positive); the URL builder applies the model's wire cast. `c*` and
// `d*` are the bucket fields in x/A/B/C/ABC order (z<->x unassigned, d<->ABC
// combined): `c*` carries the UDP response's `*_dchrg_power` and `d*` its
// `*_chrg_power`, which is where both images read them from. `va/vb/vc` (volts)
// and `ia/ib/ic` (amps) are HME-4 only; `eled`/`elet` are the day/hour energy
// registers in 0.1 Wh (HME-3 carries them as 64-bit).
// Mirrors src/astrameter/cloud_reporting.py::CtMeasurement.
struct CtMeasurement {
  int ap{0}, bp{0}, cp{0}, dp{0};
  int rssi{0}, slv{0}, udp{0}, mqtt{0};
  int64_t eled{0}, elet{0};
  int cz{0}, ca{0}, cb{0}, cc{0}, cd{0};
  int dz{0}, da{0}, db{0}, dc{0}, dd{0};
  int va{0}, vb{0}, vc{0};
  float ia{0.0f}, ib{0.0f}, ic{0.0f};
};

// Wall-clock time as the handshake response gives it (the server's zone).
struct ServerTime {
  int year{0}, month{0}, day{0}, hour{0}, minute{0}, second{0};
  // The `timeNo` a CT reports: the 10-minute slot of the day (0-143).
  int time_no() const { return (this->hour * 60 + this->minute) / 10; }
};

// The firmware version a cloud-reporting CT of *ct_type* sends as `sv`: only
// HME-4 v124 and HME-3 v122 report to the cloud at all.
int reporting_firmware_version(const std::string &ct_type);

// Parse the handshake response body (`_YYYY_MM_DD_HH_MM_SS_`). Returns false
// when it carries no valid time.
bool parse_server_time(const std::string &body, ServerTime *out);

// Integrates sampled grid power (W) into an energy register (0.1 Wh). Each
// sample's power is held until the next one (a left Riemann sum).
class EnergyIntegrator {
 public:
  void sample(uint32_t now_ms, float power_w);
  int64_t total() const { return static_cast<int64_t>(this->total_); }

 protected:
  double total_{0.0};
  bool has_last_{false};
  uint32_t last_ms_{0};
  float last_w_{0.0f};
};

// The `eled`/`elet` registers: energy since the start of the day/hour. Mirrors
// the handshake step of both images: add what the energy register gained since
// the last handshake to both, then clear `elet` when the server's hour changed
// and `eled` when its day did.
class EnergyCounters {
 public:
  void update(int64_t total, const ServerTime &now);
  int64_t eled() const { return this->eled_; }
  int64_t elet() const { return this->elet_; }

 protected:
  int64_t eled_{0};
  int64_t elet_{0};
  int64_t last_total_{0};
  int hour_{-1};
  int day_{-1};
};

// Build the handshake/time-sync GET. Matches
// `http://<host>/app/neng/getDateInfoeu.php?uid=%s&fcv=%s&aid=%s&sv=%d`.
std::string build_get_date_info_url(const std::string &host, const std::string &uid,
                                    const std::string &fcv, const std::string &aid, int sv);

// Build the `setCtReporting` GET for *ct_type* ("HME-4" or "HME-3"), reproducing
// the firmware templates field-for-field, including the model differences
// (HME-4 sends power magnitudes) and the HME-3 missing-`&` slv/udp quirk.
std::string build_set_ct_reporting_url(const std::string &host, const std::string &ct_type,
                                       const std::string &device_id, int64_t time_no, int year,
                                       int month, int day, const CtMeasurement &m);

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
