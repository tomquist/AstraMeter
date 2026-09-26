// The /node_data.json <-> /data.json fallback, held to the behaviour of
// TibberPulse._fetch_telegram in src/astrameter/powermeter/tibber_pulse.py
// (see tibber_pulse_test.py there for the same cases on the Python side).
#include <gtest/gtest.h>

#include <map>
#include <string>
#include <vector>

#include "esphome/components/tibber_pulse/bridge_endpoint.h"

using esphome::tibber_pulse::BridgeEndpoint;
using esphome::tibber_pulse::build_url;
using esphome::tibber_pulse::Endpoint;
using esphome::tibber_pulse::FetchResult;
using esphome::tibber_pulse::HttpGet;
using esphome::tibber_pulse::HttpResponse;

namespace {

const std::string NEW_URL = "http://bridge/node_data.json?node_id=1";
const std::string OLD_URL = "http://bridge/data.json?node_id=1";

/// A bridge serving fixed statuses per URL, recording every request.
struct FakeBridge {
  std::map<std::string, int> status;
  std::vector<std::string> requests;

  HttpGet getter() {
    return [this](const std::string &url) {
      this->requests.push_back(url);
      HttpResponse r;
      auto it = this->status.find(url);
      r.status = it == this->status.end() ? 404 : it->second;
      if (r.status == 200) r.body = {0x1b, 0x00, 0x1b};  // binary, NUL included
      return r;
    };
  }
};

TEST(BridgeEndpoint, BuildsUrls) {
  EXPECT_EQ(build_url("bridge", Endpoint::NODE_DATA, "1"), NEW_URL);
  EXPECT_EQ(build_url("192.168.1.5:8080", Endpoint::DATA, "7"), "http://192.168.1.5:8080/data.json?node_id=7");
}

TEST(BridgeEndpoint, NewFirmwareAnswersFirstPath) {
  FakeBridge bridge{{{NEW_URL, 200}}, {}};
  BridgeEndpoint ep;
  FetchResult r = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_TRUE(r.ok);
  EXPECT_FALSE(r.switched);
  EXPECT_EQ(r.endpoint, Endpoint::NODE_DATA);
  EXPECT_EQ(r.body, (std::vector<uint8_t>{0x1b, 0x00, 0x1b}));
  EXPECT_EQ(bridge.requests, std::vector<std::string>{NEW_URL});
}

TEST(BridgeEndpoint, OldFirmwareFallsBackOnceAndRemembers) {
  FakeBridge bridge{{{OLD_URL, 200}}, {}};
  BridgeEndpoint ep;
  FetchResult first = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_TRUE(first.ok);
  EXPECT_TRUE(first.switched);
  EXPECT_EQ(first.endpoint, Endpoint::DATA);
  EXPECT_EQ(ep.current(), Endpoint::DATA);
  // The fallback costs one extra request per switch, not one per poll.
  FetchResult second = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_TRUE(second.ok);
  EXPECT_FALSE(second.switched);
  EXPECT_EQ(bridge.requests, (std::vector<std::string>{NEW_URL, OLD_URL, OLD_URL}));
}

TEST(BridgeEndpoint, SwitchesBackAfterAnUpdate) {
  // Remembered /data.json, then the bridge updates over the air.
  FakeBridge bridge{{{OLD_URL, 200}}, {}};
  BridgeEndpoint ep;
  ep.fetch("bridge", "1", bridge.getter());
  bridge.status = {{NEW_URL, 200}};
  bridge.requests.clear();
  FetchResult r = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_TRUE(r.ok);
  EXPECT_TRUE(r.switched);
  EXPECT_EQ(ep.current(), Endpoint::NODE_DATA);
  EXPECT_EQ(bridge.requests, (std::vector<std::string>{OLD_URL, NEW_URL}));
}

TEST(BridgeEndpoint, OnlyA404FallsBack) {
  // 401 (wrong password), 500, or no answer at all say nothing about which
  // firmware the bridge runs: report them and stay put.
  for (int status : {401, 500, 503, -1}) {
    SCOPED_TRACE(status);
    FakeBridge bridge{{{NEW_URL, status}, {OLD_URL, 200}}, {}};
    BridgeEndpoint ep;
    FetchResult r = ep.fetch("bridge", "1", bridge.getter());
    EXPECT_FALSE(r.ok);
    EXPECT_EQ(r.status, status);
    EXPECT_EQ(ep.current(), Endpoint::NODE_DATA);
    EXPECT_EQ(bridge.requests, std::vector<std::string>{NEW_URL});
  }
}

TEST(BridgeEndpoint, BothPathsMissingKeepsTheRememberedOne) {
  FakeBridge bridge{{}, {}};
  BridgeEndpoint ep;
  FetchResult r = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_FALSE(r.ok);
  EXPECT_EQ(r.status, 404);
  EXPECT_EQ(r.endpoint, Endpoint::DATA);
  EXPECT_EQ(ep.current(), Endpoint::NODE_DATA);
}

TEST(BridgeEndpoint, FailedFallbackDoesNotSwitch) {
  FakeBridge bridge{{{OLD_URL, 500}}, {}};
  BridgeEndpoint ep;
  FetchResult r = ep.fetch("bridge", "1", bridge.getter());
  EXPECT_FALSE(r.ok);
  EXPECT_EQ(r.status, 500);
  EXPECT_FALSE(r.switched);
  EXPECT_EQ(ep.current(), Endpoint::NODE_DATA);
}

TEST(BridgeEndpoint, OverlappingFetchPicksFallbackFromThePathItTried) {
  // Fetch A tries /node_data.json; while it waits, fetch B runs to completion
  // on the same selector and switches it to /data.json. When A's request then
  // 404s, its fallback must be the *other* of what A tried — /data.json — and
  // not the other of what B left behind, which would send A back to the path
  // that just 404'd.
  FakeBridge bridge{{{OLD_URL, 200}}, {}};
  BridgeEndpoint ep;
  bool overlapped = false;
  HttpGet get_a = [&](const std::string &url) {
    if (!overlapped) {
      overlapped = true;
      FetchResult b = ep.fetch("bridge", "1", bridge.getter());
      EXPECT_TRUE(b.switched);
      EXPECT_EQ(ep.current(), Endpoint::DATA);
    }
    return bridge.getter()(url);
  };
  FetchResult a = ep.fetch("bridge", "1", get_a);
  EXPECT_TRUE(a.ok);
  EXPECT_EQ(a.endpoint, Endpoint::DATA);
  EXPECT_EQ(ep.current(), Endpoint::DATA);
  // B: new then old. A: new (tried before B moved it), then old.
  EXPECT_EQ(bridge.requests, (std::vector<std::string>{NEW_URL, OLD_URL, NEW_URL, OLD_URL}));
}

}  // namespace
