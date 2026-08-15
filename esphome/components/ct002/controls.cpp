#include "controls.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>

namespace esphome {
namespace ct002 {
namespace controls {

bool is_json_content_type(const std::string &header) {
  // Essence only: everything from the first ';' is a parameter, and a browser
  // ignores it when deciding whether the request needs a preflight.
  std::string essence = header.substr(0, header.find(';'));
  const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
  size_t begin = 0;
  while (begin < essence.size() && is_space(essence[begin])) {
    begin++;
  }
  size_t end = essence.size();
  while (end > begin && is_space(essence[end - 1])) {
    end--;
  }
  essence = essence.substr(begin, end - begin);
  for (char &c : essence) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return essence == "application/json";
}

namespace {

/// Lowercase, and drop the root label a resolver ignores.
std::string normalise_host(const std::string &name) {
  size_t begin = 0;
  size_t end = name.size();
  const auto is_space = [](unsigned char c) { return std::isspace(c) != 0; };
  while (begin < end && is_space(name[begin])) begin++;
  while (end > begin && is_space(name[end - 1])) end--;
  while (end > begin && name[end - 1] == '.') end--;
  std::string out = name.substr(begin, end - begin);
  for (char &c : out) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return out;
}

/// The name part of a `Host` header value, without its port.
///
/// An IPv6 literal is bracketed there (RFC 3986), which is also what keeps its
/// colons apart from the port separator.
std::string host_name(const std::string &host) {
  std::string trimmed = host;
  const size_t first = trimmed.find_first_not_of(" \t");
  if (first == std::string::npos) return "";
  trimmed = trimmed.substr(first);
  if (trimmed[0] == '[') {
    const size_t close = trimmed.find(']');
    return close == std::string::npos ? trimmed.substr(1) : trimmed.substr(1, close - 1);
  }
  // One colon separates a port; several mean an unbracketed IPv6 literal,
  // which is malformed but still an address.
  if (std::count(trimmed.begin(), trimmed.end(), ':') == 1) {
    trimmed = trimmed.substr(0, trimmed.find(':'));
  }
  return trimmed;
}

/// True for a syntactically valid IPv6 literal, with an optional trailing
/// IPv4 part (`::ffff:192.168.1.5`) and at most one `::` run.
///
/// Presence of a colon is NOT enough: `evil::example` and `1:2:3:4:5:6:7:8:9`
/// both carry one and neither is an address. Python reaches the same verdict
/// through `ipaddress.ip_address`, and the two sides have to agree.
bool is_ipv4_literal(const std::string &name);

bool is_ipv6_literal(const std::string &name) {
  if (name.find(':') == std::string::npos) return false;
  // A zone id (`fe80::1%eth0`) names a local interface. Python's `ip_address`
  // would take one, but a browser cannot put it in a URL's host, so
  // `is_allowed_host` there refuses it explicitly to stay level with this.
  if (name.find('%') != std::string::npos) return false;
  const size_t size = name.size();
  if (size < 2) return false;  // ":" alone
  // At most one "::" run may stand in for a stretch of zero groups.
  const size_t run = name.find("::");
  if (run != std::string::npos && name.find("::", run + 1) != std::string::npos) return false;
  // A lone ':' at either end is malformed — only "::" may sit there.
  if (name[0] == ':' && name[1] != ':') return false;
  if (name[size - 1] == ':' && name[size - 2] != ':') return false;

  int groups = 0;
  size_t start = 0;
  while (start < size) {
    size_t colon = name.find(':', start);
    if (colon == std::string::npos) colon = size;
    const std::string part = name.substr(start, colon - start);
    if (part.empty()) {
      // The empty halves of the "::" run, already accounted for above.
    } else if (part.find('.') != std::string::npos) {
      // Only the last group may be a dotted-quad tail (`::ffff:192.168.1.5`).
      if (colon != size) return false;
      if (!is_ipv4_literal(part)) return false;
      groups += 2;  // an embedded IPv4 fills two 16-bit groups
    } else {
      if (part.size() > 4) return false;
      for (const char c : part) {
        if (std::isxdigit(static_cast<unsigned char>(c)) == 0) return false;
      }
      groups++;
    }
    start = colon + 1;
  }
  // Exactly 8 groups, or fewer with a "::" run standing in for the rest.
  return run != std::string::npos ? groups < 8 : groups == 8;
}

/// True for an address that needed no name lookup, so nothing could rebind it.
bool is_ipv4_literal(const std::string &name) {
  if (name.empty()) return false;
  int groups = 0;
  size_t start = 0;
  while (true) {
    const size_t dot = name.find('.', start);
    const std::string part =
        name.substr(start, dot == std::string::npos ? std::string::npos : dot - start);
    // Reject a leading zero, matching Python's ipaddress, which refuses the
    // ambiguous octal-looking form rather than guessing at it.
    if (part.empty() || part.size() > 3 || (part.size() > 1 && part[0] == '0')) return false;
    int value = 0;
    for (const char c : part) {
      if (std::isdigit(static_cast<unsigned char>(c)) == 0) return false;
      value = value * 10 + (c - '0');
    }
    if (value > 255) return false;
    groups++;
    if (dot == std::string::npos) break;
    start = dot + 1;
  }
  return groups == 4;
}

/// True when *name* ends in *suffix*.
bool ends_with(const std::string &name, const std::string &suffix) {
  return name.size() >= suffix.size() &&
         name.compare(name.size() - suffix.size(), suffix.size(), suffix) == 0;
}

struct Range {
  const char *field;
  float low;
  float high;
  /// Wire unit → setter unit. The efficiency window arrives as a percentage,
  /// matching the MQTT entity of the same name, and the setter wants 0..1.
  float scale;
};

// Mirrors _CONTROL_RANGES + _CONTROL_SCALE in src/astrameter/web_server.py.
constexpr Range NUMERIC_FIELDS[] = {
    {"manual_target", -10000.0f, 10000.0f, 1.0f},
    {"distribution_weight", 0.0f, 10.0f, 1.0f},
    {"efficiency_window_weight", 0.0f, 100.0f, 0.01f},
    {"min_dc_output", 0.0f, 1000.0f, 1.0f},
};

// Mirrors _CONTROL_BOOLS.
constexpr const char *BOOL_FIELDS[] = {"active", "auto_target"};

// Mirrors the device-wide branch of _handle_control_device.
constexpr const char *DEVICE_FIELDS[] = {"active_control", "force_rotation"};
// Buttons, not settings: they carry no value, so a write may arrive bare, and
// there is no retained state for a dashboard write to mirror onto MQTT — a
// retained press would re-fire on every reconnect.
constexpr const char *DEVICE_BUTTONS[] = {"force_rotation"};

const Range *find_numeric(const std::string &field) {
  for (const Range &range : NUMERIC_FIELDS) {
    if (field == range.field) return &range;
  }
  return nullptr;
}

bool is_bool_field(const std::string &field) {
  for (const char *name : BOOL_FIELDS) {
    if (field == name) return true;
  }
  return false;
}

/// "%g"-style, so a bound reads as "10" and "-10000" like Python's :g format.
std::string compact(float value) {
  char buffer[32];
  std::snprintf(buffer, sizeof(buffer), "%g", static_cast<double>(value));
  return std::string(buffer);
}

}  // namespace

bool is_allowed_host(const std::string &host, const std::vector<std::string> &allowed) {
  const std::string name = normalise_host(host_name(host));
  if (name.empty()) {
    // HTTP/1.1 requires a Host header and every browser sends one, so its
    // absence is not a request this surface needs to answer.
    return false;
  }
  for (const auto &entry : allowed) {
    if (normalise_host(entry) == name) return true;
  }
  if (is_ipv4_literal(name) || is_ipv6_literal(name)) return true;
  // `localhost` resolves to the loopback address and nowhere else, and
  // `.local` is mDNS (RFC 6762) — resolved by multicast on the link, not
  // through a nameserver an outsider can answer for.
  return name == "localhost" || ends_with(name, ".localhost") || ends_with(name, ".local");
}

bool is_consumer_field(const std::string &field) {
  return is_bool_field(field) || find_numeric(field) != nullptr;
}

bool is_device_button(const std::string &field) {
  for (const char *name : DEVICE_BUTTONS) {
    if (field == name) return true;
  }
  return false;
}

bool is_device_field(const std::string &field) {
  for (const char *name : DEVICE_FIELDS) {
    if (field == name) return true;
  }
  return false;
}

std::string coerce_consumer_control(const std::string &field, ControlValue &value) {
  if (is_bool_field(field)) {
    if (!value.is_bool) return field + " must be true or false";
    return {};
  }
  const Range *range = find_numeric(field);
  if (range == nullptr) return "Unknown device or field";
  if (value.is_bool) return field + " must be a number";
  if (!std::isfinite(value.number) || value.number < range->low || value.number > range->high) {
    return field + " must be between " + compact(range->low) + " and " + compact(range->high);
  }
  value.number *= range->scale;
  return {};
}

}  // namespace controls
}  // namespace ct002
}  // namespace esphome
