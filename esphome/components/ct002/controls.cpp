#include "controls.h"

#include <cmath>
#include <cstdio>

namespace esphome {
namespace ct002 {
namespace controls {

namespace {

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
