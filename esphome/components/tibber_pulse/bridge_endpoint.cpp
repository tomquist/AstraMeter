#include "bridge_endpoint.h"

#include <utility>

namespace esphome {
namespace tibber_pulse {

namespace {

constexpr int HTTP_NOT_FOUND = 404;

bool is_success(int status) { return status >= 200 && status < 300; }

}  // namespace

const char *endpoint_path(Endpoint endpoint) {
  return endpoint == Endpoint::NODE_DATA ? "node_data.json" : "data.json";
}

Endpoint other_endpoint(Endpoint endpoint) {
  return endpoint == Endpoint::NODE_DATA ? Endpoint::DATA : Endpoint::NODE_DATA;
}

std::string build_url(const std::string &host, Endpoint endpoint, const std::string &node_id) {
  return "http://" + host + "/" + endpoint_path(endpoint) + "?node_id=" + node_id;
}

FetchResult BridgeEndpoint::fetch(const std::string &host, const std::string &node_id, const HttpGet &get) {
  // Pick the fallback from the path this fetch tried, not from current_, which
  // an overlapping fetch may already have moved.
  const Endpoint tried = this->current_;
  HttpResponse first = get(build_url(host, tried, node_id));
  FetchResult result;
  if (first.status != HTTP_NOT_FOUND) {
    result.ok = is_success(first.status);
    result.status = first.status;
    result.endpoint = tried;
    result.body = std::move(first.body);
    return result;
  }
  // The bridge doesn't serve this path, so it runs the other firmware
  // generation: at startup, or after an OTA update while running.
  const Endpoint other = other_endpoint(tried);
  HttpResponse second = get(build_url(host, other, node_id));
  result.status = second.status;
  result.endpoint = other;
  if (!is_success(second.status)) return result;
  result.ok = true;
  result.switched = true;
  result.body = std::move(second.body);
  this->current_ = other;
  return result;
}

}  // namespace tibber_pulse
}  // namespace esphome
