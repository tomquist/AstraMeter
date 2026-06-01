#include "marstek_responder.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>

namespace esphome {
namespace ct002 {
namespace mqtt_insights {

namespace {

// Replace "{}" placeholders in a 2-slot template (ct_type, mac) — keep
// substitution local and dependency-free so this file can be compiled
// into host-gcc tests without ESPHome's helper headers.
std::string fmt2(const char *tmpl, const std::string &a, const std::string &b) {
  std::string out;
  out.reserve(std::strlen(tmpl) + a.size() + b.size());
  const char *p = tmpl;
  bool used_a = false;
  while (*p) {
    if (p[0] == '{' && p[1] == '}') {
      out.append(used_a ? b : a);
      used_a = true;
      p += 2;
    } else {
      out.push_back(*p++);
    }
  }
  return out;
}

bool is_hex12(const std::string &s) {
  if (s.size() != 12) return false;
  for (char c : s) {
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

std::string to_lower(std::string s) {
  for (auto &c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

std::string strip(const std::string &s) {
  size_t b = 0, e = s.size();
  while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
  while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
  return s.substr(b, e - b);
}

// Mirrors Python's _parse_ctrl_kv: split on ',', then partition on '='.
// Returns false on malformed UTF-8-ish input (here just empty body).
bool parse_ctrl_kv(const std::string &body, std::vector<std::pair<std::string, std::string>> *out) {
  if (body.empty()) return false;
  size_t i = 0;
  while (i <= body.size()) {
    size_t comma = body.find(',', i);
    std::string chunk = body.substr(i, comma == std::string::npos ? std::string::npos : comma - i);
    auto eq = chunk.find('=');
    if (eq != std::string::npos) {
      std::string k = to_lower(strip(chunk.substr(0, eq)));
      std::string v = strip(chunk.substr(eq + 1));
      if (!k.empty()) out->emplace_back(std::move(k), std::move(v));
    }
    if (comma == std::string::npos) break;
    i = comma + 1;
  }
  return true;
}

const std::string *find(const std::vector<std::pair<std::string, std::string>> &kv,
                        const std::string &k) {
  for (const auto &p : kv) {
    if (p.first == k) return &p.second;
  }
  return nullptr;
}

std::string sanitize_cd4_field(const std::string &v) {
  std::string out;
  out.reserve(v.size());
  for (char c : v) {
    out.push_back((c == ',' || c == ';' || c == '=') ? '_' : c);
  }
  return out;
}

}  // namespace

std::string normalize_mac(const std::string &raw) {
  std::string cleaned;
  cleaned.reserve(raw.size());
  for (char c : raw) {
    if (c == ':' || c == '-' || std::isspace(static_cast<unsigned char>(c))) continue;
    cleaned.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
  }
  return is_hex12(cleaned) ? cleaned : std::string();
}

std::optional<PollContext> parse_poll_payload(const std::string &body) {
  std::vector<std::pair<std::string, std::string>> kv;
  if (!parse_ctrl_kv(body, &kv)) return std::nullopt;
  const std::string *cd = find(kv, "cd");
  if (cd == nullptr) return std::nullopt;
  char *end = nullptr;
  long cd_val = std::strtol(cd->c_str(), &end, 10);
  if (end == cd->c_str()) return std::nullopt;
  PollContext ctx;
  if (cd_val == 1) {
    ctx.echo_cd = 1;
    return ctx;
  }
  if (cd_val == 4) {
    const std::string *p1 = find(kv, "p1");
    if (p1 == nullptr) return std::nullopt;
    char *e2 = nullptr;
    long sid = std::strtol(p1->c_str(), &e2, 10);
    if (e2 == p1->c_str()) return std::nullopt;
    ctx.echo_cd = 4;
    ctx.slave_id = static_cast<int>(sid);
    return ctx;
  }
  return std::nullopt;
}

bool is_poll_payload(const std::string &body) { return parse_poll_payload(body).has_value(); }

std::optional<AppTopic> parse_app_topic(const std::string &topic) {
  // Match ^(hame|marstek)_energy/([^/]+)/App/([^/]+)/ctrl$
  const char *hame = "hame_energy/";
  const char *mst = "marstek_energy/";
  size_t prefix_len = 0;
  if (topic.compare(0, std::strlen(hame), hame) == 0) prefix_len = std::strlen(hame);
  else if (topic.compare(0, std::strlen(mst), mst) == 0) prefix_len = std::strlen(mst);
  else return std::nullopt;
  size_t s1 = topic.find('/', prefix_len);
  if (s1 == std::string::npos) return std::nullopt;
  if (topic.compare(s1, 5, "/App/") != 0) return std::nullopt;
  size_t s2 = topic.find('/', s1 + 5);
  if (s2 == std::string::npos) return std::nullopt;
  // Trailing path must be exactly "/ctrl" — no extra segments allowed.
  if (topic.size() != s2 + 5 || topic.compare(s2, 5, "/ctrl") != 0) return std::nullopt;
  AppTopic out;
  out.ct_type = topic.substr(prefix_len, s1 - prefix_len);
  out.mac = to_lower(topic.substr(s1 + 5, s2 - (s1 + 5)));
  // Python's regex captures are `[^/]+` — both segments must be non-empty.
  // Reject e.g. `hame_energy//App/.../ctrl` or `.../App//ctrl`.
  if (out.ct_type.empty() || out.mac.empty()) return std::nullopt;
  return out;
}

std::vector<std::string> app_topics_for(const std::string &ct_type, const std::string &mac) {
  return {fmt2(APP_TOPIC_HAME, ct_type, mac), fmt2(APP_TOPIC_MARSTEK, ct_type, mac)};
}

std::vector<std::string> device_topics_for(const std::string &ct_type, const std::string &mac) {
  return {fmt2(DEVICE_TOPIC_HAME, ct_type, mac), fmt2(DEVICE_TOPIC_MARSTEK, ct_type, mac)};
}

std::string format_cd4_slave_csv(const std::vector<ResponderRow> &rows) {
  if (rows.empty()) return {};
  std::string out;
  bool first = true;
  for (const auto &row : rows) {
    if (!first) out.push_back(',');
    first = false;
    std::string host = row.last_ip;
    // Strip whitespace; default to 0.0.0.0 when blank (matches Python).
    host = strip(host);
    if (host.empty()) host = "0.0.0.0";
    out.append("slv_t=").append(sanitize_cd4_field(row.device_type));
    out.append(",slv_id=").append(sanitize_cd4_field(row.consumer_id));
    out.append(",slv_ip=").append(sanitize_cd4_field(host));
    out.append(",slv_p=").append(row.phase);
  }
  return out;
}

std::string build_aggregate_response(const std::vector<float> &watts, int wifi_rssi, int ver_v,
                                     int connected_slave_count, bool echo_cd1, int ble_s,
                                     const std::string &fc4_v) {
  float a = watts.size() > 0 ? watts[0] : 0.0f;
  float b = watts.size() > 1 ? watts[1] : 0.0f;
  float c = watts.size() > 2 ? watts[2] : 0.0f;
  int ia = static_cast<int>(std::lround(a));
  int ib = static_cast<int>(std::lround(b));
  int ic = static_cast<int>(std::lround(c));
  int it = ia + ib + ic;
  char buf[512];
  if (echo_cd1) {
    // Extended cd=1 runtime frame. Order matches marstek_mqtt.py exactly:
    // power, wif_s, RSSI/version, slv_n, cur_d, ble_s, fc4_v, kWh tail.
    std::snprintf(buf, sizeof(buf),
                  "pwr_a=%d,pwr_b=%d,pwr_c=%d,pwr_t=%d,wif_s=2,"
                  "wif_r=%d,ver_v=%d,slv_n=%d,cur_d=0,"
                  "ble_s=%d,fc4_v=%s,"
                  "kwh=0.00,n_kwh=0.00,used_kwh=0.00,fed_kwh=0.00",
                  ia, ib, ic, it, wifi_rssi, ver_v, connected_slave_count, ble_s, fc4_v.c_str());
  } else {
    // Legacy "core" frame for any non-cd1 path (and for periodic broadcasts
    // when no poll has happened yet).
    std::snprintf(buf, sizeof(buf),
                  "pwr_a=%d,pwr_b=%d,pwr_c=%d,pwr_t=%d,"
                  "wif_r=%d,ver_v=%d,wif_s=2",
                  ia, ib, ic, it, wifi_rssi, ver_v);
  }
  return std::string(buf);
}

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome
