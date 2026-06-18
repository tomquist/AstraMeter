// Pure URL builders for the Marstek HTTP cloud reporting (hamedata.com).
//
// Dependency-free (only <string>/<cstdint>) so the host gtest
// (tests/components/ct002/host_cloud_reporting_test.cpp) can cover the wire
// format without any ESPHome headers. Mirrors the builders in
// src/astrameter/cloud_reporting.py — the two sides must emit byte-identical
// query strings (incl. the model differences and the HME-3 missing-`&` quirk).
#pragma once

#include <cstdint>
#include <string>

namespace esphome {
namespace ct002 {
namespace cloud_reporting {

// The live values a CT puts in a `setCtReporting` GET. Powers are watts. `c*`
// are the charge buckets, `d*` the discharge buckets, in x/A/B/C/ABC order
// (z<->x unassigned, d<->ABC combined). `va/vb/vc` (volts) and `ia/ib/ic` (amps)
// are HME-4 only; `eled`/`elet` are cumulative energy (HME-3 carries 64-bit).
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

// Build the handshake/time-sync GET. Matches
// `http://<host>/app/neng/getDateInfoeu.php?uid=%s&fcv=%s&aid=%s&sv=%d`.
std::string build_get_date_info_url(const std::string &host, const std::string &uid,
                                    const std::string &fcv, const std::string &aid, int sv);

// Build the `setCtReporting` GET for *ct_type* ("HME-4" or "HME-3"), reproducing
// the firmware templates field-for-field, including the model differences and
// the HME-3 missing-`&` slv/udp quirk.
std::string build_set_ct_reporting_url(const std::string &host, const std::string &ct_type,
                                       const std::string &device_id, int64_t time_no, int year,
                                       int month, int day, const CtMeasurement &m);

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
