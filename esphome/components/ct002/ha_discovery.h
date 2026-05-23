// Build Home Assistant MQTT Device Discovery payloads (HA 2024.11+).
// One-for-one port of src/astrameter/mqtt_insights/discovery.py:
//   - same topic, node_id, unique_id structure
//   - same `components` map keys
//   - same value_templates (HA Jinja, not C++ — copied as-is from Python)
//
// All builders return a (topic, payload) pair. The payload is a serialized
// JSON string built via ArduinoJson; we serialize once at build time so the
// service layer can cache/retain it cheaply.
#pragma once

#include <string>
#include <utility>

namespace esphome {
namespace ct002 {
namespace mqtt_insights {

// Replace any character that's not [A-Za-z0-9_-] with "_". Mirrors
// discovery.py::_sanitize_id (regex r"[^a-zA-Z0-9_-]").
std::string sanitize_id(const std::string &value);

// CT002 per-consumer (per-battery) HA Discovery payload.
// device_type / network_mac / battery_ip drive the `device.connections`
// list and the human-readable name; pass "" for anything you don't have.
std::pair<std::string, std::string> build_ct002_consumer_discovery(
    const std::string &base_topic, const std::string &device_id,
    const std::string &consumer_id, const std::string &ha_prefix,
    const std::string &device_type = "", const std::string &network_mac = "",
    const std::string &battery_ip = "");

// CT002 device-level HA Discovery payload (parent device, smooth_target
// sensor, active_control binary_sensor, consumer_count diagnostic,
// force_rotation button). addon_slug → via_device when non-empty.
std::pair<std::string, std::string> build_ct002_device_discovery(
    const std::string &base_topic, const std::string &device_id,
    const std::string &ha_prefix, const std::string &addon_slug = "");

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome
