// Validation for the dashboard's write path.
//
// This is the C++ side of `_CONTROL_RANGES` / `_CONTROL_SCALE` /
// `_coerce_control_value` in `src/astrameter/web_server.py`, and the bounds
// MUST stay identical on both. The CT002 setters do not bound their inputs —
// the ranges live in the MQTT command handlers — so a value one stack accepts
// and the other rejects would be settable here and then silently reverted the
// next time the broker replays its retained command.
//
// Dependency-free (only <string>) so the host gtest can hold the two tables
// against each other.
#pragma once

#include <string>

namespace esphome {
namespace ct002 {
namespace controls {

/// One control value as it arrived on the wire, after JSON typing.
struct ControlValue {
  bool is_bool{false};
  bool flag{false};
  float number{0.0f};
};

/// True when *field* is a per-battery control this firmware knows.
bool is_consumer_field(const std::string &field);

/// True when *field* is a device-wide control this firmware knows.
bool is_device_field(const std::string &field);

/// Whether *field* is a button rather than a setting. A button write carries
/// no value, and nothing about it is worth mirroring onto a retained topic.
bool is_device_button(const std::string &field);

/// Validate and scale *value* for *field*, mirroring _coerce_control_value.
///
/// Returns an empty string on success (with *value* scaled to the unit the
/// setter expects), or the message to report back — worded like the Python
/// one, since the page shows whichever backend answered it verbatim.
std::string coerce_consumer_control(const std::string &field, ControlValue &value);

}  // namespace controls
}  // namespace ct002
}  // namespace esphome
