// Host-gcc tests for the dashboard's write-path validation. Mirrors
// _CONTROL_RANGES / _CONTROL_SCALE / _coerce_control_value in
// src/astrameter/web_server.py — the bounds must be identical on both stacks,
// or a value one accepts and the other refuses would be settable from one
// dashboard and then silently reverted by the next retained MQTT replay.
// Compiles only controls.cpp (no ESPHome deps).

#include "esphome/components/ct002/controls.h"

#include <cmath>

#include <gtest/gtest.h>

namespace esphome {
namespace ct002 {
namespace controls {
namespace {

ControlValue number(float value) {
  ControlValue out;
  out.number = value;
  return out;
}

ControlValue boolean(bool value) {
  ControlValue out;
  out.is_bool = true;
  out.flag = value;
  return out;
}

TEST(Controls, KnowsTheConsumerFields) {
  for (const char *field : {"manual_target", "auto_target", "active", "distribution_weight",
                            "efficiency_window_weight", "min_dc_output"}) {
    EXPECT_TRUE(is_consumer_field(field)) << field;
  }
  EXPECT_FALSE(is_consumer_field("nonsense"));
  // Device-wide fields are not per-battery ones, and vice versa.
  EXPECT_FALSE(is_consumer_field("active_control"));
  EXPECT_TRUE(is_device_field("active_control"));
  EXPECT_TRUE(is_device_field("force_rotation"));
  EXPECT_FALSE(is_device_field("manual_target"));
}

TEST(Controls, AcceptsValuesInsideThePythonRanges) {
  for (const auto &pair : {std::make_pair("manual_target", -10000.0f),
                           std::make_pair("manual_target", 10000.0f),
                           std::make_pair("distribution_weight", 0.0f),
                           std::make_pair("distribution_weight", 10.0f),
                           std::make_pair("min_dc_output", 0.0f),
                           std::make_pair("min_dc_output", 1000.0f)}) {
    ControlValue value = number(pair.second);
    EXPECT_EQ(coerce_consumer_control(pair.first, value), "") << pair.first << " " << pair.second;
    EXPECT_FLOAT_EQ(value.number, pair.second);
  }
}

TEST(Controls, RefusesValuesOutsideThem) {
  ControlValue value = number(10000.5f);
  EXPECT_EQ(coerce_consumer_control("manual_target", value),
            "manual_target must be between -10000 and 10000");
  value = number(10.5f);
  EXPECT_EQ(coerce_consumer_control("distribution_weight", value),
            "distribution_weight must be between 0 and 10");
  value = number(101.0f);
  EXPECT_EQ(coerce_consumer_control("efficiency_window_weight", value),
            "efficiency_window_weight must be between 0 and 100");
  value = number(-1.0f);
  EXPECT_EQ(coerce_consumer_control("min_dc_output", value),
            "min_dc_output must be between 0 and 1000");
}

TEST(Controls, RefusesANonFiniteNumber) {
  ControlValue value = number(NAN);
  EXPECT_FALSE(coerce_consumer_control("manual_target", value).empty());
}

TEST(Controls, ScalesTheEfficiencyWindowFromPercent) {
  // The wire carries a percentage, matching the MQTT entity of the same name;
  // the setter wants a fraction.
  ControlValue value = number(50.0f);
  EXPECT_EQ(coerce_consumer_control("efficiency_window_weight", value), "");
  EXPECT_FLOAT_EQ(value.number, 0.5f);
}

TEST(Controls, KeepsBooleanFieldsBoolean) {
  ControlValue value = boolean(true);
  EXPECT_EQ(coerce_consumer_control("active", value), "");
  EXPECT_TRUE(value.flag);

  value = number(1.0f);
  EXPECT_EQ(coerce_consumer_control("active", value), "active must be true or false");
  EXPECT_EQ(coerce_consumer_control("auto_target", value), "auto_target must be true or false");
}

TEST(Controls, RefusesABooleanForANumericField) {
  ControlValue value = boolean(true);
  EXPECT_EQ(coerce_consumer_control("manual_target", value), "manual_target must be a number");
}

TEST(Controls, TellsButtonsApartFromSettings) {
  // Two consumers rely on this: a button may be written with no value, and a
  // button is never mirrored onto a retained MQTT topic.
  EXPECT_TRUE(is_device_button("force_rotation"));
  EXPECT_FALSE(is_device_button("active_control"));
  EXPECT_FALSE(is_device_button("manual_target"));
  EXPECT_FALSE(is_device_button("nonsense"));
  // Every button is still a device field.
  EXPECT_TRUE(is_device_field("force_rotation"));
}

TEST(Controls, ReportsAnUnknownFieldAsUnknown) {
  ControlValue value = number(1.0f);
  EXPECT_EQ(coerce_consumer_control("nonsense", value), "Unknown device or field");
}

TEST(Controls, AcceptsJsonContentTypeWithParameters) {
  EXPECT_TRUE(is_json_content_type("application/json"));
  // The page's own fetch() sends a charset; casing and padding are the
  // header's business, not ours.
  EXPECT_TRUE(is_json_content_type("application/json; charset=utf-8"));
  EXPECT_TRUE(is_json_content_type("Application/JSON"));
  EXPECT_TRUE(is_json_content_type("  application/json  "));
}

TEST(Controls, RefusesAContentTypeThatOnlyMentionsJson) {
  // What decides whether a browser preflights is the essence — the part
  // before the first ';'. Each of these is one of the three encodings a
  // browser sends cross-origin with no preflight, dressed up to contain
  // "application/json" so that a find() on the raw header would match. A
  // write taking no body (the restart routes) would then go straight
  // through, since nothing later re-checks the format.
  EXPECT_FALSE(is_json_content_type("text/plain; x=application/json"));
  EXPECT_FALSE(is_json_content_type("text/plain; application/json"));
  EXPECT_FALSE(is_json_content_type("text/plain;charset=application/json"));
  EXPECT_FALSE(
      is_json_content_type("multipart/form-data; boundary=application/json"));
  EXPECT_FALSE(
      is_json_content_type("application/x-www-form-urlencoded; a=application/json"));
}

TEST(Controls, RefusesTheOrdinaryNonJsonContentTypes) {
  EXPECT_FALSE(is_json_content_type(""));
  EXPECT_FALSE(is_json_content_type("text/plain"));
  EXPECT_FALSE(is_json_content_type("application/x-www-form-urlencoded"));
  EXPECT_FALSE(is_json_content_type("multipart/form-data; boundary=x"));
  // A prefix match must not count either.
  EXPECT_FALSE(is_json_content_type("application/json-patch+json"));
}

// -- the host guard (DNS rebinding) ------------------------------------
//
// Mirrors `test_is_allowed_host_reads_the_header_the_way_a_browser_writes_it`
// and the route-level host tests in `src/astrameter/web_server_test.py`. Both
// stacks must accept the same addresses: this page has no login either, and a
// name one of them answers under is a name that can be rebound onto it.

TEST(Controls, AcceptsAnAddressThatCannotBeRebound) {
  const std::vector<std::string> none;
  // An IP literal needs no lookup, so there is no answer to poison.
  EXPECT_TRUE(is_allowed_host("192.168.1.50", none));
  EXPECT_TRUE(is_allowed_host("192.168.1.50:80", none));
  EXPECT_TRUE(is_allowed_host("10.0.0.5:6052", none));
  // IPv6, bracketed as a Host header carries it — and unbracketed, which is
  // malformed but still an address.
  EXPECT_TRUE(is_allowed_host("[fd00::1]:80", none));
  EXPECT_TRUE(is_allowed_host("[::1]", none));
  EXPECT_TRUE(is_allowed_host("fd00::1", none));
  // Resolved without a nameserver an outsider can answer for.
  EXPECT_TRUE(is_allowed_host("localhost", none));
  EXPECT_TRUE(is_allowed_host("localhost:6052", none));
  EXPECT_TRUE(is_allowed_host("astrameter.local", none));
  // The root label is the resolver's to ignore, and case is not ours to keep.
  EXPECT_TRUE(is_allowed_host("AstraMeter.LOCAL.:80", none));
}

TEST(Controls, RefusesANameAnotherSiteCouldPointHere) {
  const std::vector<std::string> none;
  EXPECT_FALSE(is_allowed_host("evil.example", none));
  EXPECT_FALSE(is_allowed_host("evil.example:6052", none));
  EXPECT_FALSE(is_allowed_host("astrameter.evil.example", none));
  // Ending in an allowed label is not the same as being one.
  EXPECT_FALSE(is_allowed_host("notlocalhost", none));
  EXPECT_FALSE(is_allowed_host("localhost.evil.example", none));
  EXPECT_FALSE(is_allowed_host("local", none));
  // A dotted name that is not a valid address is still a name.
  EXPECT_FALSE(is_allowed_host("1.2.3.4.evil.example", none));
  EXPECT_FALSE(is_allowed_host("999.1.1.1", none));
  EXPECT_FALSE(is_allowed_host("1.2.3", none));
  // Rejected for the same reason Python's ipaddress rejects it, rather than
  // guessing at an octal-looking group.
  EXPECT_FALSE(is_allowed_host("010.1.1.1", none));
  // No Host header is not something a browser produces.
  EXPECT_FALSE(is_allowed_host("", none));
  EXPECT_FALSE(is_allowed_host("   ", none));
}

TEST(Controls, RefusesAColonBearingNameThatIsNotAnAddress) {
  // A colon alone must not stand in for "this is IPv6": Python reaches its
  // verdict through `ipaddress.ip_address`, and a name this side waved
  // through would be settable here and refused there.
  const std::vector<std::string> none;
  EXPECT_FALSE(is_allowed_host("evil::example", none));
  EXPECT_FALSE(is_allowed_host("1:2:3:4:5:6:7:8:9", none));  // too many groups
  EXPECT_FALSE(is_allowed_host("1:2:3:4:5:6:7", none));      // too few, no "::"
  EXPECT_FALSE(is_allowed_host("fd00::1::2", none));         // two "::" runs
  EXPECT_FALSE(is_allowed_host("fd00:::1", none));
  EXPECT_FALSE(is_allowed_host("fd00::12345", none));        // group too long
  EXPECT_FALSE(is_allowed_host("fd00::zz", none));           // not hex
  EXPECT_FALSE(is_allowed_host(":1", none));                 // lone leading ':'
  EXPECT_FALSE(is_allowed_host("fd00::1%eth0", none));       // zone id
  EXPECT_FALSE(is_allowed_host("192.168.1.5::1", none));     // quad not at end
}

TEST(Controls, AcceptsTheIPv6FormsPythonAccepts) {
  const std::vector<std::string> none;
  EXPECT_TRUE(is_allowed_host("1:2:3:4:5:6:7:8", none));
  EXPECT_TRUE(is_allowed_host("fd00::", none));
  EXPECT_TRUE(is_allowed_host("::", none));
  EXPECT_TRUE(is_allowed_host("::1", none));
  EXPECT_TRUE(is_allowed_host("fd00::1", none));
  // An IPv4-mapped tail fills the last two groups.
  EXPECT_TRUE(is_allowed_host("::ffff:192.168.1.5", none));
  EXPECT_TRUE(is_allowed_host("[::ffff:192.168.1.5]:80", none));
}

TEST(Controls, AcceptsANameTheOperatorListed) {
  const std::vector<std::string> allowed{"astra.example.lan", " PROXY.example.lan. "};
  EXPECT_TRUE(is_allowed_host("astra.example.lan", allowed));
  EXPECT_TRUE(is_allowed_host("astra.example.lan:80", allowed));
  EXPECT_TRUE(is_allowed_host("ASTRA.Example.LAN.", allowed));
  EXPECT_TRUE(is_allowed_host("proxy.example.lan", allowed));
  // Listing one name does not open the port to every other.
  EXPECT_FALSE(is_allowed_host("other.example.lan", allowed));
  EXPECT_FALSE(is_allowed_host("evil.example", allowed));
}

}  // namespace
}  // namespace controls
}  // namespace ct002
}  // namespace esphome
