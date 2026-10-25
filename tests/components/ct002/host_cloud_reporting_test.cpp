// Host-gcc parity tests for the cloud-reporting helpers. Mirrors
// src/astrameter/cloud_reporting_test.py — both sides must emit byte-identical
// query strings, including the model differences and the HME-3 missing-`&`
// quirk, and agree on the server time, timeNo and energy registers. Compiles
// only cloud_reporting_url.cpp (no ESPHome deps).

#include "esphome/components/ct002/cloud_reporting_url.h"

#include <gtest/gtest.h>

namespace esphome {
namespace ct002 {
namespace cloud_reporting {
namespace {

CtMeasurement sample() {
  CtMeasurement m;
  m.ap = 10;
  m.bp = 20;
  m.cp = 30;
  m.dp = 60;
  m.rssi = -55;
  m.slv = 2;
  m.udp = 1;
  m.mqtt = 1;
  m.eled = 111;
  m.elet = 222;
  m.cz = 5;
  m.ca = 1;
  m.cb = 2;
  m.cc = 3;
  m.cd = 4;
  m.dz = -5;
  m.da = -1;
  m.db = -2;
  m.dc = -3;
  m.dd = -4;
  m.va = 230;
  m.vb = 231;
  m.vc = 232;
  m.ia = 1.5f;
  m.ib = 2.25f;
  m.ic = 0.0f;
  return m;
}

TEST(CloudReportingUrl, GetDateInfoMatchesTemplate) {
  const std::string url =
      build_get_date_info_url("eu.hamedata.com", "aabbccddeeff", "202409090159", "acct1", 3);
  EXPECT_EQ(url,
            "http://eu.hamedata.com/app/neng/getDateInfoeu.php"
            "?uid=aabbccddeeff&fcv=202409090159&aid=acct1&sv=3");
}

TEST(CloudReportingUrl, Hme4IncludesVoltageCurrentAndAmpersand) {
  const std::string url = build_set_ct_reporting_url("eu.hamedata.com", "HME-4", "aabbccddeeff",
                                                     82, 2026, 6, 18, sample());
  EXPECT_NE(url.find("&slv=2&udp=1&mqtt=1&timeNo=82&date=2026-06-18"), std::string::npos);
  EXPECT_NE(url.find("&va=230&vb=231&vc=232&ia=1.50&ib=2.25&ic=0.00"), std::string::npos);
  EXPECT_NE(url.find("date=2026-06-18"), std::string::npos);
  EXPECT_NE(url.find("eled=111&elet=222"), std::string::npos);
  // Ends with the charge/discharge buckets.
  const std::string tail = "&cz=5&ca=1&cb=2&cc=3&cd=4&dz=-5&da=-1&db=-2&dc=-3&dd=-4";
  EXPECT_EQ(url.compare(url.size() - tail.size(), tail.size(), tail), 0);
}

TEST(CloudReportingUrl, Hme3OmitsVoltageCurrentAndKeepsQuirk) {
  CtMeasurement s = sample();
  s.ap = -150;
  s.dp = -90;
  const std::string url = build_set_ct_reporting_url("eu.hamedata.com", "HME-3", "aabbccddeeff",
                                                     82, 2026, 6, 18, s);
  // Firmware quirk: no '&' between slv and udp.
  EXPECT_NE(url.find("&slv=2udp=1&mqtt=1"), std::string::npos);
  // HME-3 sends no instantaneous voltage/current.
  EXPECT_EQ(url.find("va="), std::string::npos);
  EXPECT_EQ(url.find("ia="), std::string::npos);
  // ... and signed powers.
  EXPECT_NE(url.find("&ap=-150&bp=20&cp=30&dp=-90&"), std::string::npos);
  const std::string tail = "&cz=5&ca=1&cb=2&cc=3&cd=4&dz=-5&da=-1&db=-2&dc=-3&dd=-4";
  EXPECT_EQ(url.compare(url.size() - tail.size(), tail.size(), tail), 0);
}

TEST(CloudReportingUrl, Hme4SendsPowerMagnitudes) {
  CtMeasurement m = sample();
  m.ap = -150;
  m.bp = 20;
  m.cp = -70000;
  m.dp = -200;
  const std::string url = build_set_ct_reporting_url("eu.hamedata.com", "HME-4", "m", 0, 2026, 6, 18, m);
  // The unsigned 16-bit cast drops the sign and wraps past 65535.
  EXPECT_NE(url.find("&ap=150&bp=20&cp=4464&dp=200&"), std::string::npos);
}

TEST(CloudReportingUrl, ReportingFirmwareVersion) {
  EXPECT_EQ(reporting_firmware_version("HME-4"), 124);
  EXPECT_EQ(reporting_firmware_version("HME-3"), 122);
}

TEST(CloudReportingTime, ParsesServerTime) {
  ServerTime t;
  ASSERT_TRUE(parse_server_time("_2026_06_18_13_47_05_", &t));
  EXPECT_EQ(t.year, 2026);
  EXPECT_EQ(t.month, 6);
  EXPECT_EQ(t.day, 18);
  EXPECT_EQ(t.hour, 13);
  EXPECT_EQ(t.minute, 47);
  EXPECT_EQ(t.second, 5);
  EXPECT_FALSE(parse_server_time("", &t));
  EXPECT_FALSE(parse_server_time("<html>error</html>", &t));
  EXPECT_FALSE(parse_server_time("_2026_13_40_25_61_00_", &t));
  EXPECT_FALSE(parse_server_time("_99999999999999999999_1_1_0_0_0_", &t));
}

TEST(CloudReportingTime, TimeNoIsTenMinuteSlotOfDay) {
  const int cases[][3] = {{0, 0, 0}, {0, 9, 0}, {0, 10, 1}, {13, 47, 82}, {23, 59, 143}};
  for (const auto &c : cases) {
    EXPECT_EQ((ServerTime{2026, 6, 18, c[0], c[1], 0}).time_no(), c[2]);
  }
}

TEST(CloudReportingEnergy, IntegratorHoldsEachSampleUntilTheNext) {
  EnergyIntegrator e;
  e.sample(0, 3600.0f);
  EXPECT_EQ(e.total(), 0);
  // 3600 W for 10 s = 10 Wh = 100 units of 0.1 Wh.
  e.sample(10000, -1800.0f);
  EXPECT_EQ(e.total(), 100);
  // -1800 W for 10 s = -5 Wh.
  e.sample(20000, 0.0f);
  EXPECT_EQ(e.total(), 50);
}

TEST(CloudReportingEnergy, CountersResetOnNewHourAndDay) {
  EnergyCounters c;
  c.update(1000, ServerTime{2026, 6, 18, 13, 0, 0});
  // The first handshake has no previous hour or day, so both start at 0.
  EXPECT_EQ(c.eled(), 0);
  EXPECT_EQ(c.elet(), 0);
  c.update(1500, ServerTime{2026, 6, 18, 13, 5, 0});
  EXPECT_EQ(c.eled(), 500);
  EXPECT_EQ(c.elet(), 500);
  // A new hour clears elet after adding the gain, as on the device.
  c.update(1700, ServerTime{2026, 6, 18, 14, 0, 0});
  EXPECT_EQ(c.eled(), 700);
  EXPECT_EQ(c.elet(), 0);
  c.update(1800, ServerTime{2026, 6, 18, 14, 5, 0});
  EXPECT_EQ(c.eled(), 800);
  EXPECT_EQ(c.elet(), 100);
  c.update(1900, ServerTime{2026, 6, 19, 0, 0, 0});
  EXPECT_EQ(c.eled(), 0);
  EXPECT_EQ(c.elet(), 0);
}

TEST(CloudReportingUrl, HostIsConfigurable) {
  const std::string url = build_get_date_info_url("cn.hamedata.com", "m", "f", "a", 0);
  EXPECT_EQ(url.rfind("http://cn.hamedata.com/", 0), 0u);
}

}  // namespace
}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
