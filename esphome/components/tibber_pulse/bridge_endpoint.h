// Which path the Pulse Bridge serves its telegram on, and the fallback between
// the two.
//
// Bridge firmware ~1794 (September 2026) renamed the telegram endpoint from
// /data.json to /node_data.json and 404s the old path (#685); older firmware
// only serves /data.json. No path works on both, so ask for the new one first
// (bridges update over the air), fall back to the other on 404, and remember
// whichever answered — switching back again should a later update make the
// remembered one 404.
//
// Dependency-free (std only) so the host gtest
// (tests/components/tibber_pulse/host_bridge_endpoint_test.cpp) can drive it.
// Mirrors TibberPulse._fetch_telegram in
// src/astrameter/powermeter/tibber_pulse.py, including its rule that a fetch
// picks its fallback from the path *it* tried, not from whatever an
// overlapping fetch has since remembered.
#pragma once

#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace esphome {
namespace tibber_pulse {

enum class Endpoint : uint8_t { NODE_DATA, DATA };

/// "node_data.json" or "data.json".
const char *endpoint_path(Endpoint endpoint);
Endpoint other_endpoint(Endpoint endpoint);

/// http://<host>/<endpoint>?node_id=<node_id>
std::string build_url(const std::string &host, Endpoint endpoint, const std::string &node_id);

struct HttpResponse {
  /// HTTP status, or negative when no response arrived at all.
  int status{-1};
  std::vector<uint8_t> body;
};

using HttpGet = std::function<HttpResponse(const std::string &url)>;

struct FetchResult {
  /// A 2xx from whichever path answered last.
  bool ok{false};
  /// The status of the last request made: the fallback's, if there was one.
  int status{-1};
  /// The path that request went to.
  Endpoint endpoint{Endpoint::NODE_DATA};
  /// The bridge answered on the fallback path, which is now remembered.
  bool switched{false};
  std::vector<uint8_t> body;
};

class BridgeEndpoint {
 public:
  Endpoint current() const { return this->current_; }

  /// Fetch one telegram through *get*, falling back to the other path on 404.
  FetchResult fetch(const std::string &host, const std::string &node_id, const HttpGet &get);

 protected:
  Endpoint current_{Endpoint::NODE_DATA};
};

}  // namespace tibber_pulse
}  // namespace esphome
