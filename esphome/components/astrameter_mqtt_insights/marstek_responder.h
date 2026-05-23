// Pure helpers for the Marstek MQTT responder. Mirrors
// src/astrameter/mqtt_insights/marstek_mqtt.py — keep tokens, default
// constants, and parse rules byte-for-byte identical so the Marstek app
// (and hm2mqtt-style parsers) see exactly the frame they expect.
//
// No ESPHome dependencies — unit-testable via host-gcc gtest. The row
// struct here is the same shape as ct002::CT002Component::ReportingConsumerRow;
// callers convert at the boundary so this header stays standalone.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace esphome {
namespace astrameter_mqtt_insights {

// Standalone mirror of CT002Component::ReportingConsumerRow. Same field
// names so a brace-init list works from either side.
struct ResponderRow {
  std::string consumer_id;
  std::string device_type;
  std::string last_ip;
  std::string phase;
};

// MQTT topic templates the Marstek app speaks. Old hame_energy/* and new
// marstek_energy/* are subscribed in parallel — devices in the wild use
// either depending on firmware vintage.
inline constexpr const char *APP_TOPIC_HAME = "hame_energy/{}/App/{}/ctrl";
inline constexpr const char *APP_TOPIC_MARSTEK = "marstek_energy/{}/App/{}/ctrl";
inline constexpr const char *DEVICE_TOPIC_HAME = "hame_energy/{}/device/{}/ctrl";
inline constexpr const char *DEVICE_TOPIC_MARSTEK = "marstek_energy/{}/device/{}/ctrl";

// Defaults that mirror real-device observed values — keep aligned with
// Python's marstek_mqtt.py so hm2mqtt-style parsers recognise frames.
inline constexpr int DEFAULT_VER_V = 148;
inline constexpr const char *DEFAULT_FC4_V = "202409090159";

// Convert "AA:BB:CC:DD:EE:FF" / "AA-BB-..." / "aabbcc..." into 12 lowercase
// hex chars; returns "" if not exactly 12 hex chars after stripping. Mirrors
// Python's normalize_mac.
std::string normalize_mac(const std::string &raw);

// Description of an incoming poll request. Python: MarstekPollContext.
struct PollContext {
  int echo_cd{1};                  // 1 = aggregate runtime info, 4 = slave list
  std::optional<int> slave_id;     // set iff echo_cd == 4
};

// Parse the body of an App/ctrl message. Returns nullopt for non-poll
// payloads (we never invent a selector — cd=4 without p1 is ignored,
// matching marstek_mqtt.py::parse_marstek_poll_payload).
std::optional<PollContext> parse_poll_payload(const std::string &body);

// Convenience: true iff parse_poll_payload would return a value.
bool is_poll_payload(const std::string &body);

// Pull (ct_type, mac_lowercase) out of an App/ctrl topic. Accepts both
// "hame_energy/.../App/.../ctrl" and "marstek_energy/.../App/.../ctrl".
struct AppTopic {
  std::string ct_type;
  std::string mac;
};
std::optional<AppTopic> parse_app_topic(const std::string &topic);

// Build the bytes for both flavors of the device/ctrl reply topic — caller
// publishes on every entry so old and new app builds both see the response.
std::vector<std::string> device_topics_for(const std::string &ct_type, const std::string &mac);
std::vector<std::string> app_topics_for(const std::string &ct_type, const std::string &mac);

// CSV body of a cd=4 reply: repeated slv_t/slv_id/slv_ip/slv_p tokens, no
// cd= echo. Mirrors marstek_mqtt.py::format_cd4_slave_csv exactly — the
// app's outer split-on-comma parser requires single-= tokens, so commas
// and equals signs in payload values are sanitized to underscores.
std::string format_cd4_slave_csv(const std::vector<ResponderRow> &rows);

// Build the aggregate runtime-info reply body for a cd=1 poll. The order
// is significant — Marstek-style parsers rely on positional ordering for
// pwr_*, wif_s before RSSI/version, slv_n before cur_d, then ble_s/fc4_v
// and the kWh tail. See marstek_mqtt.py::build_response.
std::string build_aggregate_response(const std::vector<float> &watts, int wifi_rssi,
                                     int ver_v, int connected_slave_count,
                                     bool echo_cd1, int ble_s = 0,
                                     const std::string &fc4_v = DEFAULT_FC4_V);

}  // namespace astrameter_mqtt_insights
}  // namespace esphome
