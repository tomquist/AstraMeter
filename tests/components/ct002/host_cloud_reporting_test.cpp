// Host-gcc parity tests for the cloud-reporting URL builders. Mirrors
// src/astrameter/cloud_reporting_test.py — both sides must emit byte-identical
// query strings, including the model differences and the HME-3 missing-`&`
// quirk. Compiles only cloud_reporting_url.cpp (no ESPHome deps).

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
  m.cz = -5;
  m.ca = -1;
  m.cb = -2;
  m.cc = -3;
  m.cd = -4;
  m.dz = 5;
  m.da = 1;
  m.db = 2;
  m.dc = 3;
  m.dd = 4;
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
                                                     1700000000LL, 2026, 6, 18, sample());
  EXPECT_NE(url.find("&slv=2&udp=1&mqtt=1"), std::string::npos);
  EXPECT_NE(url.find("&va=230&vb=231&vc=232&ia=1.50&ib=2.25&ic=0.00"), std::string::npos);
  EXPECT_NE(url.find("date=2026-06-18"), std::string::npos);
  EXPECT_NE(url.find("eled=111&elet=222"), std::string::npos);
  // Ends with the charge/discharge buckets.
  const std::string tail = "&cz=-5&ca=-1&cb=-2&cc=-3&cd=-4&dz=5&da=1&db=2&dc=3&dd=4";
  EXPECT_EQ(url.compare(url.size() - tail.size(), tail.size(), tail), 0);
}

TEST(CloudReportingUrl, Hme3OmitsVoltageCurrentAndKeepsQuirk) {
  const std::string url = build_set_ct_reporting_url("eu.hamedata.com", "HME-3", "aabbccddeeff",
                                                     1700000000LL, 2026, 6, 18, sample());
  // Firmware quirk: no '&' between slv and udp.
  EXPECT_NE(url.find("&slv=2udp=1&mqtt=1"), std::string::npos);
  // HME-3 sends no instantaneous voltage/current.
  EXPECT_EQ(url.find("va="), std::string::npos);
  EXPECT_EQ(url.find("ia="), std::string::npos);
  const std::string tail = "&cz=-5&ca=-1&cb=-2&cc=-3&cd=-4&dz=5&da=1&db=2&dc=3&dd=4";
  EXPECT_EQ(url.compare(url.size() - tail.size(), tail.size(), tail), 0);
}

TEST(CloudReportingUrl, HostIsConfigurable) {
  const std::string url = build_get_date_info_url("cn.hamedata.com", "m", "f", "a", 0);
  EXPECT_EQ(url.rfind("http://cn.hamedata.com/", 0), 0u);
}

}  // namespace
}  // namespace cloud_reporting
}  // namespace ct002
}  // namespace esphome
