// Host-gcc tests for the dashboard's status document. The wire schema is
// shared with the Python stack (src/astrameter/status/serialize.py and
// web/ts/dashboard/types.ts), so these pin the two properties the page
// depends on: field names/units match the schema, and a value the firmware
// cannot produce is *missing* rather than rendered as a real-looking zero.
// Compiles only status_json.cpp (no ESPHome deps).

#include "esphome/components/ct002/status_json.h"

#include <cmath>

#include <gtest/gtest.h>

namespace esphome {
namespace ct002 {
namespace status {
namespace {

/// Index of the "A" bucket in PhaseBucket order (x, A, B, C, ABC).
constexpr size_t BUCKET_A_INDEX = 1;

bool contains(const std::string &haystack, const std::string &needle) {
  return haystack.find(needle) != std::string::npos;
}

/// A document with one device and one battery, everything populated.
StatusDocument sample() {
  StatusDocument doc;
  doc.generated_at = 1717236000.0;  // 2024-06-01T10:00:00+00:00
  doc.seq = 7;
  doc.uptime_s = 3661.25f;
  doc.service.version = "2.2.4";
  doc.service.git_commit = "0123456789abcdef0123456789abcdef01234567";
  doc.service.log_level = "DEBUG";
  doc.service.web_port = 80;
  doc.service.started_at = 1717232339.0;

  PowermeterStatus meter;
  meter.name = "Grid L1 Power";
  meter.kind = "SensorBackedPowermeter";
  meter.pipeline = {"HampelPowermeter", "SmoothedPowermeter"};
  meter.online = true;
  meter.last_read_age_s = 0.85f;
  meter.last_read_ok = true;
  meter.last_values_w = std::vector<float>{123.45f};
  meter.last_total_w = 123.45f;
  doc.powermeters.push_back(meter);

  DeviceStatus device;
  device.device_id = "astrameter-ct002";
  device.ct_type = "HME-4";
  device.ct_mac = "02b25012abcd";
  device.udp_port = 12345;
  device.wifi_rssi_dbm = -57;
  device.running = true;
  device.started_at = 1717232339.0;
  device.active_control = true;
  device.dedupe_window_s = 0.25f;
  device.grid = std::array<float, 3>{12.0f, 0.0f, -3.5f};
  device.grid_total_w = 8.5f;
  device.grid_sample_at = 1717235999.0;
  device.buckets[BUCKET_A_INDEX].dchrg_w = 300.0f;
  device.buckets[BUCKET_A_INDEX].count = 1;
  device.buckets[BUCKET_A_INDEX].active = true;

  BalancerSnapshot balancer;
  balancer.efficiency_rotation_enabled = true;
  balancer.predictor.grid_estimate = -12.25f;
  balancer.predictor.trust = 0.4375f;
  balancer.predictor.innovation_sign = -1;
  balancer.predictor.pool_output = 300.0f;
  balancer.import_trim.dwell = 6;
  balancer.import_trim.engaged = true;
  balancer.efficiency.demand_ema = 420.0f;
  balancer.control_quality.verdict = "stable";
  balancer.control_quality.score = 92.5;
  balancer.control_quality.has_score = true;
  balancer.control_quality.error_ema = 18.25;
  balancer.control_quality.in_band_fraction = 0.875;
  balancer.control_quality.crossings_per_second = 0.05;
  balancer.control_quality.band = 25.0f;
  balancer.control_quality.samples = 640;
  balancer.efficiency.priority_order = {"aabbccddeeff"};
  balancer.efficiency.last_rotation_age = 61.5;
  device.balancer = balancer;

  ConsumerStatus consumer;
  consumer.consumer_id = "aabbccddeeff";
  consumer.device_type = "HMG-50";
  consumer.last_ip = "192.168.1.42";
  consumer.phase = "A";
  consumer.bucket = "A";
  consumer.mode = "auto";
  consumer.builtin_inverter = true;
  consumer.reported_power_w = 300.0f;
  consumer.last_instructed_power_w = 295.0f;
  consumer.last_seen_at = 1717235998.0;
  consumer.last_seen_age_s = 2.0f;
  consumer.poll_interval_s = 1.0f;
  consumer.ttl_s = 5.0f;
  BalancerConsumerSnapshot state;
  state.last_target = 295.0f;
  state.saturation = 0.125;
  state.fade_weight = 1.0;
  state.pace_cap = 30.0f;
  state.pace_sign = 1;
  consumer.balancer = state;
  device.consumers.push_back(consumer);

  doc.devices.push_back(device);

  MqttInsightsStatus insights;
  insights.connected = true;
  insights.broker = "192.168.1.10";
  insights.port = 1883;
  insights.base_topic = "astrameter";
  insights.ha_discovery = true;
  insights.ha_discovery_prefix = "homeassistant";
  doc.mqtt_insights = insights;
  return doc;
}

TEST(FormatNumber, RoundsAndTrims) {
  EXPECT_EQ(format_number(12.34567, 1), "12.3");
  EXPECT_EQ(format_number(12.0, 1), "12");
  EXPECT_EQ(format_number(0.0, 3), "0");
  EXPECT_EQ(format_number(0.1234, 3), "0.123");
  EXPECT_EQ(format_number(-3.55, 1), "-3.5");
}

TEST(FormatNumber, NegativeZeroReadsAsZero) {
  EXPECT_EQ(format_number(-0.004, 1), "0");
}

TEST(FormatNumber, NonFiniteHasNoRepresentation) {
  // JSON cannot carry these, and the caller omits the field rather than
  // inventing one — a NaN grid reading must not surface as 0 W.
  EXPECT_EQ(format_number(NAN, 1), "");
  EXPECT_EQ(format_number(INFINITY, 1), "");
}

TEST(IsoUtc, MatchesPythonIsoformat) {
  EXPECT_EQ(iso_utc(1717236000.0), "2024-06-01T10:00:00+00:00");
}

TEST(IsoUtc, UnsyncedClockHasNoTimestamp) {
  // Seconds-since-boot from an unsynced ESP32 would render as 1970.
  EXPECT_EQ(iso_utc(1234.0), "");
  EXPECT_EQ(iso_utc(0.0), "");
}

TEST(StatusJson, IdentifiesItselfAsTheEsphomeBackend) {
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"schema_version\":1"));
  EXPECT_TRUE(contains(json, "\"backend\":\"esphome\""));
  EXPECT_TRUE(contains(json, "\"runtime\":\"esphome\""));
  EXPECT_TRUE(contains(json, "\"generated_at\":\"2024-06-01T10:00:00+00:00\""));
}

TEST(StatusJson, NamesTheBuildItWasCompiledFrom) {
  // Same field and same full-SHA form as the Python backend's, so the page's
  // Diagnostics tab reads identically whichever stack is answering.
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"version\":\"2.2.4\""));
  EXPECT_TRUE(
      contains(json, "\"git_commit\":\"0123456789abcdef0123456789abcdef01234567\""));
}

TEST(StatusJson, OffersNoConfigurationSurface) {
  // An ESPHome device's settings are compiled into its firmware, so the page
  // must not offer to change them: no config_mode at all, and writes off.
  const std::string json = build_status_json(sample());
  EXPECT_FALSE(contains(json, "config_mode"));
  EXPECT_TRUE(contains(json, "\"config_writable\":false"));
  EXPECT_TRUE(contains(json, "\"controls\":false"));
  EXPECT_TRUE(contains(json, "\"balancer_internals\":true"));
}

TEST(StatusJson, CarriesTheDeviceAndItsGrid) {
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"kind\":\"ct002\""));
  EXPECT_TRUE(contains(json, "\"ct_mac\":\"02b25012abcd\""));
  EXPECT_TRUE(contains(json, "\"udp_port\":12345"));
  EXPECT_TRUE(contains(json, "\"wifi_rssi_dbm\":-57"));
  EXPECT_TRUE(contains(json, "\"l1_w\":12"));
  EXPECT_TRUE(contains(json, "\"l3_w\":-3.5"));
  EXPECT_TRUE(contains(json, "\"grid_total_w\":8.5"));
  EXPECT_TRUE(contains(json, "\"sample_at\":\"2024-06-01T09:59:59+00:00\""));
}

TEST(StatusJson, NamesEveryPhaseBucket) {
  const std::string json = build_status_json(sample());
  for (const char *name : BUCKET_NAMES) {
    EXPECT_TRUE(contains(json, std::string("\"") + name + "\":{"));
  }
  EXPECT_TRUE(contains(json, "\"dchrg_w\":300,\"count\":1,\"active\":true"));
}

TEST(StatusJson, CarriesTheBalancerInternals) {
  const std::string json = build_status_json(sample());
  // Half-to-even, the same rounding Python's round() applies on its side of
  // the schema: -12.25 → -12.2, not -12.3.
  EXPECT_TRUE(contains(json, "\"grid_estimate_w\":-12.2"));
  EXPECT_TRUE(contains(json, "\"trust\":0.438"));
  EXPECT_TRUE(contains(json, "\"import_trim\":{\"dwell\":6,\"dwell_target\":6"));
  EXPECT_TRUE(contains(json, "\"engaged\":true"));
  EXPECT_TRUE(contains(json, "\"demand_ema_w\":420"));
  EXPECT_TRUE(contains(json, "\"priority_order\":[\"aabbccddeeff\"]"));
  // No probe is running, so the object is absent rather than empty.
  EXPECT_FALSE(contains(json, "\"probe\""));
}

TEST(StatusJson, CarriesTheControlQualityVerdict) {
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"verdict\":\"stable\""));
  EXPECT_TRUE(contains(json, "\"score_pct\":92.5"));
  // Half-to-even, like Python's round(): 18.25 → 18.2.
  EXPECT_TRUE(contains(json, "\"error_w\":18.2"));
  EXPECT_TRUE(contains(json, "\"in_band_fraction\":0.875"));
  // Per minute on the wire, like the Python side: 0.05/s reads as 3/min.
  EXPECT_TRUE(contains(json, "\"crossings_per_min\":3"));
  EXPECT_TRUE(contains(json, "\"band_w\":25"));
  EXPECT_TRUE(contains(json, "\"samples\":640"));
}

TEST(StatusJson, WithholdsControlQualityEvidenceUntilItHasSome) {
  // An unmeasured window must not report "0 W mean error, 0% in band": that
  // describes a perfectly held grid and a permanently failing one identically.
  StatusDocument doc = sample();
  ControlQualitySnapshot &quality = doc.devices[0].balancer->control_quality;
  quality.verdict = "idle";
  quality.has_score = false;
  quality.samples = 0;
  const std::string json = build_status_json(doc);
  EXPECT_TRUE(contains(json, "\"verdict\":\"idle\""));
  EXPECT_FALSE(contains(json, "score_pct"));
  EXPECT_FALSE(contains(json, "error_w"));
  EXPECT_FALSE(contains(json, "in_band_fraction"));
  EXPECT_FALSE(contains(json, "crossings_per_min"));
  // Configuration, not a measurement — still there.
  EXPECT_TRUE(contains(json, "\"band_w\":25"));
}

TEST(StatusJson, CarriesTheBattery) {
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"consumer_id\":\"aabbccddeeff\""));
  EXPECT_TRUE(contains(json, "\"device_type\":\"HMG-50\""));
  EXPECT_TRUE(contains(json, "\"builtin_inverter\":true"));
  EXPECT_TRUE(contains(json, "\"reported_power_w\":300"));
  EXPECT_TRUE(contains(json, "\"last_seen_age_s\":2"));
  EXPECT_TRUE(contains(json, "\"mode\":\"auto\""));
  EXPECT_TRUE(contains(json, "\"last_target_w\":295"));
  EXPECT_TRUE(contains(json, "\"saturation\":0.125"));
  // The page reads this as a percentage, like the MQTT entity of the same name.
  EXPECT_TRUE(contains(json, "\"efficiency_window_weight_pct\":100"));
}

TEST(StatusJson, CarriesTheMqttInsightsIntegration) {
  const std::string json = build_status_json(sample());
  EXPECT_TRUE(contains(json, "\"integrations\":{\"mqtt_insights\":{"));
  EXPECT_TRUE(contains(json, "\"connected\":true"));
  EXPECT_TRUE(contains(json, "\"broker\":\"192.168.1.10\""));
  EXPECT_TRUE(contains(json, "\"port\":1883"));
  EXPECT_TRUE(contains(json, "\"base_topic\":\"astrameter\""));
  // No asyncio queue exists in this port, so the depth is absent rather than
  // reported as a permanently empty one.
  EXPECT_FALSE(contains(json, "queue_depth"));
}

TEST(StatusJson, OmitsTheIntegrationsBlockWhenThereAreNone) {
  StatusDocument doc = sample();
  doc.mqtt_insights.reset();
  EXPECT_FALSE(contains(build_status_json(doc), "integrations"));
}

TEST(StatusJson, OmitsWhatItCannotProduce) {
  StatusDocument doc = sample();
  // An unsynced clock: ages stay, timestamps go.
  doc.generated_at.reset();
  doc.service.started_at.reset();
  doc.devices[0].started_at.reset();
  doc.devices[0].grid_sample_at.reset();
  doc.devices[0].consumers[0].last_seen_at.reset();
  doc.devices[0].consumers[0].min_dc_output_w.reset();
  doc.service.version.clear();
  // Not built from a git checkout — the page must show no commit at all
  // rather than an empty one.
  doc.service.git_commit.clear();
  const std::string json = build_status_json(doc);
  EXPECT_FALSE(contains(json, "generated_at"));
  EXPECT_FALSE(contains(json, "started_at"));
  EXPECT_FALSE(contains(json, "sample_at"));
  EXPECT_FALSE(contains(json, "last_seen_at"));
  EXPECT_FALSE(contains(json, "min_dc_output_w"));
  EXPECT_FALSE(contains(json, "\"version\""));
  EXPECT_FALSE(contains(json, "git_commit"));
  // ...but the ages the page falls back to are still there.
  EXPECT_TRUE(contains(json, "\"uptime_s\":3661.2"));
  EXPECT_TRUE(contains(json, "\"last_seen_age_s\":2"));
}

TEST(StatusJson, SaysWhenABatteryHasNeverReported) {
  StatusDocument doc = sample();
  ConsumerStatus &consumer = doc.devices[0].consumers[0];
  consumer.last_seen_at.reset();
  consumer.last_seen_age_s.reset();
  consumer.never_reported = true;
  const std::string json = build_status_json(doc);
  // Stated, not inferred from the missing timestamp — this backend has no
  // timestamps at all when the clock is unsynced.
  EXPECT_TRUE(contains(json, "\"never_reported\":true"));
}

TEST(StatusJson, DropsSectionsWithNothingInThem) {
  StatusDocument doc;
  const std::string json = build_status_json(doc);
  EXPECT_FALSE(contains(json, "powermeters"));
  EXPECT_FALSE(contains(json, "devices"));
  // The three fields the schema guarantees survive an empty document.
  EXPECT_TRUE(contains(json, "\"schema_version\":1"));
  EXPECT_TRUE(contains(json, "\"capabilities\""));
}

TEST(StatusJson, EscapesTextThatWouldBreakTheDocument) {
  StatusDocument doc = sample();
  doc.powermeters[0].name = "Grid \"L1\"\\meter\n";
  const std::string json = build_status_json(doc);
  EXPECT_TRUE(contains(json, "\"name\":\"Grid \\\"L1\\\"\\\\meter\\n\""));
}

TEST(StatusJson, BracketsAndBracesBalance) {
  const std::string json = build_status_json(sample());
  int depth = 0;
  bool in_string = false;
  bool escaped = false;
  for (const char c : json) {
    if (in_string) {
      if (escaped) {
        escaped = false;
      } else if (c == '\\') {
        escaped = true;
      } else if (c == '"') {
        in_string = false;
      }
      continue;
    }
    if (c == '"') in_string = true;
    if (c == '{' || c == '[') depth++;
    if (c == '}' || c == ']') depth--;
    ASSERT_GE(depth, 0);
  }
  EXPECT_EQ(depth, 0);
  EXPECT_FALSE(in_string);
  // A stray comma before a closing brace is the classic hand-rolled-writer
  // bug and the one thing a substring assertion would never catch.
  EXPECT_FALSE(contains(json, ",}"));
  EXPECT_FALSE(contains(json, ",]"));
  EXPECT_FALSE(contains(json, "{,"));
  EXPECT_FALSE(contains(json, ",,"));
}

}  // namespace
}  // namespace status
}  // namespace ct002
}  // namespace esphome
