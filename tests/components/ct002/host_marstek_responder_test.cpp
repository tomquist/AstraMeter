// Host-gcc tests for the Marstek MQTT responder helpers in
// esphome/components/astrameter_mqtt_insights/marstek_responder.{h,cpp}.
// These mirror src/astrameter/mqtt_insights/marstek_mqtt_test.py — the same
// inputs must produce the same wire bytes so the Marstek app (and
// hm2mqtt-style parsers) see identical frames whether the Python or
// ESPHome stack served them.

#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "esphome/components/astrameter_mqtt_insights/marstek_responder.h"

using esphome::astrameter_mqtt_insights::app_topics_for;
using esphome::astrameter_mqtt_insights::build_aggregate_response;
using esphome::astrameter_mqtt_insights::device_topics_for;
using esphome::astrameter_mqtt_insights::format_cd4_slave_csv;
using esphome::astrameter_mqtt_insights::is_poll_payload;
using esphome::astrameter_mqtt_insights::normalize_mac;
using esphome::astrameter_mqtt_insights::parse_app_topic;
using esphome::astrameter_mqtt_insights::parse_poll_payload;
using esphome::astrameter_mqtt_insights::ResponderRow;

TEST(MarstekResponder, NormalizeMac) {
  EXPECT_EQ("aabbccddeeff", normalize_mac("AA:BB:CC:DD:EE:FF"));
  EXPECT_EQ("aabbccddeeff", normalize_mac("aa-bb-cc-dd-ee-ff"));
  EXPECT_EQ("aabbccddeeff", normalize_mac("AABBCCDDEEFF"));
  EXPECT_EQ("aabbccddeeff", normalize_mac("  aa:bb:cc:dd:ee:ff  "));
  EXPECT_EQ("", normalize_mac(""));
  EXPECT_EQ("", normalize_mac("not-hex"));
  EXPECT_EQ("", normalize_mac("aabbccddee"));   // too short
  EXPECT_EQ("", normalize_mac("aabbccddeeffaa"));  // too long
}

TEST(MarstekResponder, ParsePollCd1) {
  auto p = parse_poll_payload("cd=1");
  ASSERT_TRUE(p.has_value());
  EXPECT_EQ(1, p->echo_cd);
  EXPECT_FALSE(p->slave_id.has_value());
}

TEST(MarstekResponder, ParsePollCd4WithP1) {
  auto p = parse_poll_payload("cd=4,p1=2");
  ASSERT_TRUE(p.has_value());
  EXPECT_EQ(4, p->echo_cd);
  ASSERT_TRUE(p->slave_id.has_value());
  EXPECT_EQ(2, *p->slave_id);
}

TEST(MarstekResponder, ParsePollCd4WithoutP1Rejected) {
  // Mirrors Python: cd=4 without p1 returns None — never invent a selector
  // the app did not send.
  EXPECT_FALSE(parse_poll_payload("cd=4").has_value());
}

TEST(MarstekResponder, ParsePollUnknownCdRejected) {
  EXPECT_FALSE(parse_poll_payload("cd=2").has_value());
  EXPECT_FALSE(parse_poll_payload("cd=99,p1=1").has_value());
}

TEST(MarstekResponder, ParsePollMissingCdRejected) {
  EXPECT_FALSE(parse_poll_payload("p1=1").has_value());
  EXPECT_FALSE(parse_poll_payload("").has_value());
  EXPECT_FALSE(parse_poll_payload("garbage").has_value());
}

TEST(MarstekResponder, ParsePollKeysCaseInsensitive) {
  // Python's _parse_ctrl_kv lowercases keys.
  auto p = parse_poll_payload("CD=1");
  ASSERT_TRUE(p.has_value());
  EXPECT_EQ(1, p->echo_cd);
}

TEST(MarstekResponder, IsPollPayloadConvenience) {
  EXPECT_TRUE(is_poll_payload("cd=1"));
  EXPECT_TRUE(is_poll_payload("cd=4,p1=0"));
  EXPECT_FALSE(is_poll_payload("cd=4"));
  EXPECT_FALSE(is_poll_payload(""));
}

TEST(MarstekResponder, ParseAppTopicHame) {
  auto t = parse_app_topic("hame_energy/HMG-50/App/AABBCCDDEEFF/ctrl");
  ASSERT_TRUE(t.has_value());
  EXPECT_EQ("HMG-50", t->ct_type);
  EXPECT_EQ("aabbccddeeff", t->mac);
}

TEST(MarstekResponder, ParseAppTopicMarstek) {
  auto t = parse_app_topic("marstek_energy/HME-4/App/112233445566/ctrl");
  ASSERT_TRUE(t.has_value());
  EXPECT_EQ("HME-4", t->ct_type);
  EXPECT_EQ("112233445566", t->mac);
}

TEST(MarstekResponder, ParseAppTopicRejectsBadShapes) {
  EXPECT_FALSE(parse_app_topic("hame_energy/HMG-50/Dev/AABBCC/ctrl").has_value());
  EXPECT_FALSE(parse_app_topic("foo/HMG-50/App/AABBCC/ctrl").has_value());
  EXPECT_FALSE(parse_app_topic("hame_energy/HMG-50/App/AABBCC").has_value());
  EXPECT_FALSE(parse_app_topic("hame_energy/HMG-50/App/AABBCC/ctrl/extra").has_value());
}

TEST(MarstekResponder, TopicTemplatesEmitBothFlavors) {
  auto apps = app_topics_for("HME-4", "aabbccddeeff");
  ASSERT_EQ(2u, apps.size());
  EXPECT_EQ("hame_energy/HME-4/App/aabbccddeeff/ctrl", apps[0]);
  EXPECT_EQ("marstek_energy/HME-4/App/aabbccddeeff/ctrl", apps[1]);

  auto devs = device_topics_for("HME-4", "aabbccddeeff");
  ASSERT_EQ(2u, devs.size());
  EXPECT_EQ("hame_energy/HME-4/device/aabbccddeeff/ctrl", devs[0]);
  EXPECT_EQ("marstek_energy/HME-4/device/aabbccddeeff/ctrl", devs[1]);
}

TEST(MarstekResponder, AggregateResponseLegacyCore) {
  // echo_cd1=false → legacy core frame (no slv_n/cur_d/kWh tail).
  std::string body = build_aggregate_response({100.0f, 200.0f, 300.0f}, -50, 148, 3, false);
  EXPECT_EQ("pwr_a=100,pwr_b=200,pwr_c=300,pwr_t=600,wif_r=-50,ver_v=148,wif_s=2", body);
}

TEST(MarstekResponder, AggregateResponseExtendedCd1) {
  std::string body = build_aggregate_response({100.0f, 0.0f, -50.0f}, -67, 148, 2, true);
  // Field order matches marstek_mqtt.py::build_response exactly.
  EXPECT_EQ(
      "pwr_a=100,pwr_b=0,pwr_c=-50,pwr_t=50,wif_s=2,"
      "wif_r=-67,ver_v=148,slv_n=2,cur_d=0,"
      "ble_s=0,fc4_v=202409090159,"
      "kwh=0.00,n_kwh=0.00,used_kwh=0.00,fed_kwh=0.00",
      body);
}

TEST(MarstekResponder, AggregateResponseRounding) {
  // round-half-to-even (Python) vs round-half-away-from-zero (lround).
  // Python's round(0.5) == 0, lround(0.5) == 1 — known divergence on .5
  // boundaries. Use a value that's unambiguous to keep this test stable
  // across rounding mode differences.
  std::string body = build_aggregate_response({100.4f}, -50, 148, 0, false);
  EXPECT_NE(std::string::npos, body.find("pwr_a=100"));
  EXPECT_NE(std::string::npos, body.find("pwr_t=100"));
}

TEST(MarstekResponder, AggregateResponseShortWattsZeroPads) {
  // Python: vs = list(watts) + [0.0] * (3 - len(watts))
  std::string body = build_aggregate_response({}, -50, 148, 0, false);
  EXPECT_EQ("pwr_a=0,pwr_b=0,pwr_c=0,pwr_t=0,wif_r=-50,ver_v=148,wif_s=2", body);
}

TEST(MarstekResponder, Cd4CsvEmptyWhenNoRows) {
  EXPECT_EQ("", format_cd4_slave_csv({}));
}

TEST(MarstekResponder, Cd4CsvSingleRow) {
  std::vector<ResponderRow> rows{
      {/*consumer_id=*/"aabbccddeeff", /*device_type=*/"HMG-50",
       /*last_ip=*/"192.168.1.10", /*phase=*/"A"},
  };
  EXPECT_EQ("slv_t=HMG-50,slv_id=aabbccddeeff,slv_ip=192.168.1.10,slv_p=A",
            format_cd4_slave_csv(rows));
}

TEST(MarstekResponder, Cd4CsvMultiRowDefaultIp) {
  std::vector<ResponderRow> rows{
      {"aabbccddeeff", "HMG-50", "", "A"},
      {"112233445566", "HMG-50", "192.168.1.11", "B"},
  };
  EXPECT_EQ(
      "slv_t=HMG-50,slv_id=aabbccddeeff,slv_ip=0.0.0.0,slv_p=A,"
      "slv_t=HMG-50,slv_id=112233445566,slv_ip=192.168.1.11,slv_p=B",
      format_cd4_slave_csv(rows));
}

TEST(MarstekResponder, Cd4CsvSanitizesEqAndComma) {
  // Marstek's outer split-on-comma parser requires exactly one '=' per
  // token; payload values must not contain '=' or ','. Mirrors
  // _cd4_escape_field in marstek_mqtt.py.
  std::vector<ResponderRow> rows{
      {"id,with=both", "type;weird", "1.2.3.4", "C"},
  };
  EXPECT_EQ("slv_t=type_weird,slv_id=id_with_both,slv_ip=1.2.3.4,slv_p=C",
            format_cd4_slave_csv(rows));
}
