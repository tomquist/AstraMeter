#include "cloud_reporting_url.h"

#include <cstdio>
#include <cstdlib>
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

// HME-4 converts its float power with an unsigned 16-bit cast whose float
// conversion ignores the sign, so the cloud gets the magnitude.
int hme4_power(int watts) { return std::abs(watts) & 0xFFFF; }

bool is_leap(int year) { return (year % 4 == 0 && year % 100 != 0) || year % 400 == 0; }

int days_in_month(int year, int month) {
  static const int DAYS[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  return month == 2 && is_leap(year) ? 29 : DAYS[month - 1];
}

// One `_<digits>` group; advances *pos past it.
bool read_group(const std::string &s, size_t *pos, int *out) {
  if (*pos >= s.size() || s[*pos] != '_') return false;
  size_t i = *pos + 1;
  long value = 0;
  const size_t start = i;
  while (i < s.size() && s[i] >= '0' && s[i] <= '9') {
    // Saturate rather than overflow: an absurd value still fails validation.
    if (value <= 1000000) value = value * 10 + (s[i] - '0');
    ++i;
  }
  if (i == start) return false;
  *out = static_cast<int>(value);
  *pos = i;
  return true;
}

}  // namespace

// `sv` a reporting CT sends — mirrors REPORTING_FIRMWARE_VERSIONS.
int reporting_firmware_version(const std::string &ct_type) { return ct_type == "HME-3" ? 122 : 124; }

// Mirrors parse_server_time: the first `_Y_M_D_h_m_s_` run anywhere in *body*.
bool parse_server_time(const std::string &body, ServerTime *out) {
  for (size_t start = body.find('_'); start != std::string::npos; start = body.find('_', start + 1)) {
    size_t pos = start;
    int v[6];
    bool ok = true;
    for (int &field : v) {
      if (!read_group(body, &pos, &field)) {
        ok = false;
        break;
      }
    }
    if (!ok || pos >= body.size() || body[pos] != '_') continue;
    const int year = v[0], month = v[1], day = v[2];
    if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > days_in_month(year, month) ||
        v[3] > 23 || v[4] > 59 || v[5] > 59)
      return false;
    *out = ServerTime{year, month, day, v[3], v[4], v[5]};
    return true;
  }
  return false;
}

// 0.1 Wh is 360 watt-seconds.
void EnergyIntegrator::sample(uint32_t now_ms, float power_w) {
  if (this->has_last_) {
    const uint32_t dt_ms = now_ms - this->last_ms_;
    this->total_ += static_cast<double>(this->last_w_) * (static_cast<double>(dt_ms) / 1000.0) / 360.0;
  }
  this->has_last_ = true;
  this->last_ms_ = now_ms;
  this->last_w_ = power_w;
}

void EnergyCounters::update(int64_t total, const ServerTime &now) {
  const int64_t delta = total - this->last_total_;
  this->eled_ += delta;
  this->elet_ += delta;
  this->last_total_ = total;
  if (now.hour != this->hour_) this->elet_ = 0;
  if (now.day != this->day_) this->eled_ = 0;
  this->hour_ = now.hour;
  this->day_ = now.day;
}

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
  append_int(&url, hme3 ? m.ap : hme4_power(m.ap));
  url += "&bp=";
  append_int(&url, hme3 ? m.bp : hme4_power(m.bp));
  url += "&cp=";
  append_int(&url, hme3 ? m.cp : hme4_power(m.cp));
  url += "&dp=";
  append_int(&url, hme3 ? m.dp : hme4_power(m.dp));
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
