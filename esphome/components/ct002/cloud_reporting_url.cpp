#include "cloud_reporting_url.h"

#include <cstdio>
#include <string>

namespace esphome {
namespace ct002 {
namespace cloud_reporting {

namespace {

// Percent-encode for query-string usage (matches Python's urllib quote()).
// Not named with a short ALL-CAPS identifier — Arduino's Print.h #defines HEX.
std::string url_encode(const std::string &v) {
  static const char *HEX_DIGITS = "0123456789ABCDEF";
  std::string out;
  out.reserve(v.size() * 3);
  for (unsigned char c : v) {
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '-' ||
        c == '_' || c == '.' || c == '~') {
      out.push_back(static_cast<char>(c));
    } else {
      out.push_back('%');
      out.push_back(HEX_DIGITS[c >> 4]);
      out.push_back(HEX_DIGITS[c & 0x0F]);
    }
  }
  return out;
}

void append_int(std::string *s, long long v) { s->append(std::to_string(v)); }

void append_fixed2(std::string *s, float v) {
  char buf[32];
  std::snprintf(buf, sizeof(buf), "%.2f", static_cast<double>(v));
  s->append(buf);
}

void append_date(std::string *s, int year, int month, int day) {
  char buf[16];
  std::snprintf(buf, sizeof(buf), "%d-%02d-%02d", year, month, day);
  s->append(buf);
}

}  // namespace

std::string build_get_date_info_url(const std::string &host, const std::string &uid,
                                    const std::string &fcv, const std::string &aid, int sv) {
  std::string url = "http://";
  url += host;
  url += "/app/neng/getDateInfoeu.php?uid=";
  url += url_encode(uid);
  url += "&fcv=";
  url += url_encode(fcv);
  url += "&aid=";
  url += url_encode(aid);
  url += "&sv=";
  append_int(&url, sv);
  return url;
}

std::string build_set_ct_reporting_url(const std::string &host, const std::string &ct_type,
                                       const std::string &device_id, int64_t time_no, int year,
                                       int month, int day, const CtMeasurement &m) {
  const bool hme3 = ct_type == "HME-3";
  std::string url = "http://";
  url += host;
  url += "/prod/api/v1/setCtReporting?id=";
  url += url_encode(device_id);
  url += "&eled=";
  append_int(&url, static_cast<long long>(m.eled));
  url += "&elet=";
  append_int(&url, static_cast<long long>(m.elet));
  url += "&ap=";
  append_int(&url, m.ap);
  url += "&bp=";
  append_int(&url, m.bp);
  url += "&cp=";
  append_int(&url, m.cp);
  url += "&dp=";
  append_int(&url, m.dp);
  url += "&rssi=";
  append_int(&url, m.rssi);
  // Firmware quirk: HME-3 omits the '&' between slv and udp; HME-4 includes it.
  url += "&slv=";
  append_int(&url, m.slv);
  url += hme3 ? "udp=" : "&udp=";
  append_int(&url, m.udp);
  url += "&mqtt=";
  append_int(&url, m.mqtt);
  url += "&timeNo=";
  append_int(&url, static_cast<long long>(time_no));
  url += "&date=";
  append_date(&url, year, month, day);
  // HME-4 (a clamp) reports instantaneous voltage/current; HME-3 does not.
  if (!hme3) {
    url += "&va=";
    append_int(&url, m.va);
    url += "&vb=";
    append_int(&url, m.vb);
    url += "&vc=";
    append_int(&url, m.vc);
    url += "&ia=";
    append_fixed2(&url, m.ia);
    url += "&ib=";
    append_fixed2(&url, m.ib);
    url += "&ic=";
    append_fixed2(&url, m.ic);
  }
  url += "&cz=";
  append_int(&url, m.cz);
  url += "&ca=";
  append_int(&url, m.ca);
  url += "&cb=";
  append_int(&url, m.cb);
  url += "&cc=";
  append_int(&url, m.cc);
  url += "&cd=";
  append_int(&url, m.cd);
  url += "&dz=";
  append_int(&url, m.dz);
  url += "&da=";
  append_int(&url, m.da);
  url += "&db=";
  append_int(&url, m.db);
  url += "&dc=";
  append_int(&url, m.dc);
  url += "&dd=";
  append_int(&url, m.dd);
  return url;
}

}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
