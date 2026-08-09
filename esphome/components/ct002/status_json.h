// The status document the ESPHome dashboard serves from `GET api/status`.
//
// This is the C++ side of `src/astrameter/status/serialize.py`: it takes the
// runtime snapshot structs and renames/rounds them into the documented wire
// schema (`web/ts/dashboard/types.ts`). The two stacks serve the SAME schema,
// so a field only ever exists here under the name Python already gives it.
//
// An ESPHome build serves a *reduced* document — no powermeter list of its own
// beyond the sensor feed, no configuration surface, no controls — which the
// schema explicitly allows: every field is optional at every level and the UI
// renders what it receives.
//
// Two rules carried over from serialize.py, and the reason this layer exists
// at all:
//
//   * **Absence is not zero.** A value the firmware cannot produce is left
//     out of the JSON entirely, so the page omits the field instead of
//     drawing a real-looking `0`.
//   * **Monotonic marks never cross the wire.** millis()-based marks are
//     emitted as an age in seconds; only genuine wall-clock epochs (i.e. once
//     SNTP has synced) become `*_at` timestamps, and they are omitted until
//     then rather than reported as 1970.
//
// Dependency-free apart from balancer.h (itself std-only), so the host gtest
// (tests/components/ct002/host_status_json_test.cpp) can cover the wire format
// without any ESPHome headers.
#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

#include "balancer.h"

namespace esphome {
namespace ct002 {
namespace status {

// Bumped when a field changes meaning; matches SCHEMA_VERSION in
// src/astrameter/status/registry.py.
inline constexpr int SCHEMA_VERSION = 1;

// Wall-clock epochs below this are seconds-since-boot from an unsynced clock,
// not a real date (the same floor mqtt_insights uses). Timestamps are omitted
// rather than emitted as 1970.
inline constexpr double WALL_CLOCK_SANE_THRESHOLD = 1577836800.0;  // 2020-01-01 UTC

// Cross-talk bucket names in PhaseBucket order (see ct002.h), mirroring
// Python's PHASE_BUCKETS.
inline constexpr const char *BUCKET_NAMES[5] = {"x", "A", "B", "C", "ABC"};

/// What this deployment can do, so the UI never branches on backend identity.
struct Capabilities {
  // A push transport is reserved, not implemented — the field exists so a
  // future one is a capability flip rather than a redesign.
  bool stream{false};
  uint32_t poll_interval_ms{2000};
  // Per-battery write endpoints. Off unless the YAML's `controls:` asks for
  // them — the page carries no login of its own.
  bool controls{false};
  // An ESPHome device's configuration is compiled into its firmware; there is
  // nothing a dashboard could write, so the Configuration tab stays away.
  bool config_writable{false};
  bool balancer_internals{true};
};

struct ServiceStatus {
  std::string version;     ///< AstraMeter version, empty when unknown
  std::string git_commit;  ///< full SHA of the checkout built from, empty when unknown
  std::string log_level;   ///< compiled-in log level, empty when unknown
  uint16_t web_port{80};
  std::optional<double> started_at;  ///< wall-clock epoch, if the clock synced
};

/// Identity + health of the grid-power feed, mirroring PowermeterHealth.
struct PowermeterStatus {
  std::string name;
  std::string kind;
  /// Filter wrappers applied, innermost first — the same class names the
  /// Python stack reports, so the card reads identically on both.
  std::vector<std::string> pipeline;
  std::optional<bool> online;
  std::optional<float> last_read_age_s;
  bool last_read_ok{false};
  std::optional<std::vector<float>> last_values_w;
  std::optional<float> last_total_w;
};

struct BucketStatus {
  float chrg_w{0.0f};
  float dchrg_w{0.0f};
  int count{0};
  bool active{false};
};

struct ConsumerStatus {
  std::string consumer_id;
  std::string device_type;
  std::string last_ip;
  std::string phase;
  std::string bucket;
  std::string mode;  ///< "auto" | "manual" | "inactive"
  bool builtin_inverter{false};
  bool ac_input{false};
  bool dc_input{false};
  bool participates{true};
  float reported_power_w{0.0f};
  float last_instructed_power_w{0.0f};
  // The schema's per-phase `target_w` is deliberately not carried: this
  // firmware keeps one device-wide reply vector rather than one per battery,
  // so filling it would repeat the same triple on every card. The per-battery
  // target the page actually renders is `balancer.last_target_w` below.
  std::optional<double> last_seen_at;  ///< wall-clock epoch, if the clock synced
  std::optional<float> last_seen_age_s;
  /// True only for a battery that has never polled — stated, not inferred
  /// from the absence of a timestamp (a reduced document has none either way).
  bool never_reported{false};
  std::optional<float> poll_interval_s;
  std::optional<float> answer_interval_s;
  float ttl_s{0.0f};
  bool expired{false};
  bool active{true};
  bool manual_enabled{false};
  float manual_target_w{0.0f};
  float distribution_weight{1.0f};
  /// As a percentage, matching the "Efficiency Window Weight" MQTT entity —
  /// the dashboard and the user's HA entity list must agree on the unit.
  float efficiency_window_weight_pct{100.0f};
  std::optional<float> min_dc_output_w;
  bool min_dc_output_applicable{false};
  std::optional<BalancerConsumerSnapshot> balancer;
};

struct DeviceStatus {
  std::string device_id;
  std::string ct_type;
  std::string ct_mac;
  uint16_t udp_port{0};
  int wifi_rssi_dbm{0};
  bool running{false};
  std::optional<double> started_at;  ///< wall-clock epoch, if the clock synced
  bool active_control{true};
  std::optional<uint32_t> consumer_ttl_s;
  float dedupe_window_s{0.0f};
  std::optional<std::array<float, 3>> grid;
  float grid_total_w{0.0f};
  std::optional<double> grid_sample_at;  ///< wall-clock epoch, if the clock synced
  bool meter_failed{false};
  std::array<BucketStatus, 5> buckets;
  std::optional<BalancerSnapshot> balancer;
  std::vector<ConsumerStatus> consumers;
};

/// MQTT Insights, for the page's Diagnostics card.
///
/// A subset of the Python stack's MqttInsightsSnapshot: the fields the card
/// renders, and only the ones this firmware can answer. The queue depth has
/// no counterpart — the ESPHome port publishes synchronously and owns no
/// queue (see mqtt_insights.h) — so it is left out rather than reported as a
/// permanently empty one. Like Python's, this carries the broker locator only:
/// the username and password never enter the document.
struct MqttInsightsStatus {
  bool connected{false};
  std::string broker;
  uint16_t port{0};
  std::string base_topic;
  bool ha_discovery{false};
  std::string ha_discovery_prefix;
};

struct StatusDocument {
  Capabilities capabilities;
  std::optional<double> generated_at;  ///< wall-clock epoch, if the clock synced
  uint32_t seq{0};
  std::optional<float> uptime_s;
  ServiceStatus service;
  std::vector<PowermeterStatus> powermeters;
  std::vector<DeviceStatus> devices;
  /// Absent unless the `mqtt_insights:` sub-block is configured.
  std::optional<MqttInsightsStatus> mqtt_insights;
};

/// Serialize *doc* to the wire JSON. Absent values are omitted, never zeroed.
std::string build_status_json(const StatusDocument &doc);

// ── helpers, exposed for the host test ──────────────────────────────────

/// A number rounded to *digits* decimals with trailing zeros trimmed, or an
/// empty string for a value JSON cannot carry (NaN/inf) — callers omit those.
std::string format_number(double value, int digits);

/// An epoch as ISO-8601 UTC ("2024-05-06T07:08:09+00:00", matching Python's
/// datetime.isoformat()), or empty when the clock has not synced.
/// Room for "YYYY-MM-DDTHH:MM:SS+00:00" and its terminator.
inline constexpr size_t ISO_UTC_BUF_SIZE = 32;

/// Format *epoch* into *out*, or return false (and empty *out*) when the clock
/// has not synced. The buffer form is what the serializer uses: the string one
/// would heap-allocate for every timestamp, of which there is one per battery.
bool iso_utc_to(double epoch, char (&out)[ISO_UTC_BUF_SIZE]);

std::string iso_utc(double epoch);

}  // namespace status
}  // namespace ct002
}  // namespace esphome
