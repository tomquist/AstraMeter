#include "status_json.h"

#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>

namespace esphome {
namespace ct002 {
namespace status {

namespace {

// Minimal streaming JSON writer. Hand-rolled rather than ArduinoJson because
// this file must stay free of ESPHome/Arduino headers to keep the wire format
// host-testable, and because the omit-absent rule below is easier to enforce
// when nothing is written for a value that does not exist.
class JsonWriter {
 public:
  std::string take() { return std::move(this->out_); }

  void begin_object() {
    this->prefix_();
    this->push_('{');
  }
  void begin_object(const char *key) {
    this->key_(key);
    this->push_('{');
  }
  void end_object() { this->pop_('}'); }

  void begin_array(const char *key) {
    this->key_(key);
    this->push_('[');
  }
  void end_array() { this->pop_(']'); }

  void set(const char *key, const std::string &value) {
    this->key_(key);
    this->quote_(value);
  }
  /// Explicit, so a `char[]` argument cannot decay to `char *` and pick the
  /// bool overload instead — which is exactly what a raw buffer would do.
  void set(const char *key, const char *value) {
    this->key_(key);
    this->quote_(value);
  }
  /// Mirrors Python's `value or None`: an empty string is absence, not "".
  void set_if(const char *key, const std::string &value) {
    if (!value.empty()) this->set(key, value);
  }
  void set(const char *key, bool value) {
    this->key_(key);
    this->out_ += value ? "true" : "false";
  }
  /// Emitted only when true, for flags whose absence already means false.
  void set_if(const char *key, bool value) {
    if (value) this->set(key, true);
  }
  void set(const char *key, long long value) {
    this->key_(key);
    this->out_ += std::to_string(value);
  }
  void set(const char *key, double value, int digits) {
    const std::string text = format_number(value, digits);
    if (text.empty()) return;  // NaN/inf: absent, never a fake 0
    this->key_(key);
    this->out_ += text;
  }
  void set(const char *key, const std::optional<float> &value, int digits) {
    if (value.has_value()) this->set(key, static_cast<double>(*value), digits);
  }
  void set(const char *key, const std::optional<bool> &value) {
    if (value.has_value()) this->set(key, *value);
  }
  /// A wall-clock epoch as an ISO timestamp, omitted while the clock is unsynced.
  void set_time(const char *key, const std::optional<double> &epoch) {
    if (!epoch.has_value()) return;
    char stamp[ISO_UTC_BUF_SIZE];
    if (!iso_utc_to(*epoch, stamp)) return;
    this->set(key, stamp);
  }
  void value(const std::string &item) {
    this->prefix_();
    this->quote_(item);
  }
  void value(double item, int digits) {
    const std::string text = format_number(item, digits);
    this->prefix_();
    this->out_ += text.empty() ? "0" : text;
  }

 private:
  void prefix_() {
    if (!this->first_) this->out_ += ',';
    this->first_ = false;
  }
  void key_(const char *key) {
    this->prefix_();
    this->out_ += '"';
    this->out_ += key;
    this->out_ += "\":";
  }
  void push_(char open) {
    this->out_ += open;
    // The parent level has just had a member written (prefix_ cleared its
    // flag), so what is stacked is always "not first" — restored on pop.
    if (this->depth_ < 32) {
      if (this->first_) {
        this->stack_ |= (1u << this->depth_);
      } else {
        this->stack_ &= ~(1u << this->depth_);
      }
      this->depth_++;
    }
    this->first_ = true;
  }
  void pop_(char close) {
    this->out_ += close;
    if (this->depth_ == 0) {
      this->first_ = false;
      return;
    }
    this->depth_--;
    this->first_ = (this->stack_ & (1u << this->depth_)) != 0;
  }
  void quote_(const std::string &text) { this->quote_(text.c_str(), text.size()); }
  void quote_(const char *text) { this->quote_(text, std::strlen(text)); }
  void quote_(const char *text, size_t length) {
    this->out_ += '"';
    for (size_t i = 0; i < length; i++) {
      const char c = text[i];
      switch (c) {
        case '"':
          this->out_ += "\\\"";
          break;
        case '\\':
          this->out_ += "\\\\";
          break;
        case '\n':
          this->out_ += "\\n";
          break;
        case '\r':
          this->out_ += "\\r";
          break;
        case '\t':
          this->out_ += "\\t";
          break;
        default:
          if (static_cast<unsigned char>(c) < 0x20) {
            char esc[7];
            std::snprintf(esc, sizeof(esc), "\\u%04x", static_cast<unsigned char>(c));
            this->out_ += esc;
          } else {
            this->out_ += c;
          }
      }
    }
    this->out_ += '"';
  }

  std::string out_;
  // One bit per open container, array or not. A mask rather than a
  // vector<bool>: nesting here is five deep at most, and the vector both
  // allocates and pulls in the bit-iterator machinery to say so.
  uint32_t stack_{0};
  uint8_t depth_{0};
  bool first_{true};
};

void write_ids(JsonWriter &json, const char *key, const std::vector<std::string> &ids) {
  if (ids.empty()) return;  // `list(...) or None` in serialize.py
  json.begin_array(key);
  for (const auto &id : ids) json.value(id);
  json.end_array();
}

// Mirrors serialize.py::_balancer_consumer_to_wire.
void write_consumer_balancer(JsonWriter &json, const BalancerConsumerSnapshot &state) {
  json.begin_object("balancer");
  json.set("last_target_w", state.last_target, 1);
  json.set("last_intent_w", state.last_intent, 1);
  json.set("last_intent_reading_w", state.last_intent_reading, 1);
  json.set("saturation", state.saturation, 3);
  json.set("saturation_grace_remaining_s", state.saturation_grace_remaining, 1);
  json.set("fade_weight", state.fade_weight, 3);
  json.set("deprioritized", state.deprioritized);
  json.begin_object("pace");
  json.set("cap_w", static_cast<double>(state.pace_cap), 1);
  json.set("sign", static_cast<long long>(state.pace_sign));
  json.end_object();
  json.begin_object("oscillation");
  json.set("score", static_cast<double>(state.osc_score), 3);
  json.set("last_sign", static_cast<long long>(state.osc_last_sign));
  json.end_object();
  json.end_object();
}

// Mirrors serialize.py::consumer_to_wire.
void write_consumer(JsonWriter &json, const ConsumerStatus &consumer) {
  json.begin_object();
  json.set_if("consumer_id", consumer.consumer_id);
  json.set_if("device_type", consumer.device_type);
  json.begin_object("capabilities");
  json.set("builtin_inverter", consumer.builtin_inverter);
  json.set("ac_input", consumer.ac_input);
  json.set("dc_input", consumer.dc_input);
  json.end_object();
  json.set_if("last_ip", consumer.last_ip);
  json.set_if("phase", consumer.phase);
  json.set_if("bucket", consumer.bucket);
  json.set("participates", consumer.participates);
  json.set("reported_power_w", static_cast<double>(consumer.reported_power_w), 1);
  json.set("last_instructed_power_w", static_cast<double>(consumer.last_instructed_power_w), 1);
  json.set_time("last_seen_at", consumer.last_seen_at);
  json.set("last_seen_age_s", consumer.last_seen_age_s, 1);
  json.set_if("never_reported", consumer.never_reported);
  json.set("poll_interval_s", consumer.poll_interval_s, 1);
  json.set("answer_interval_s", consumer.answer_interval_s, 1);
  json.set("ttl_s", static_cast<double>(consumer.ttl_s), 1);
  json.set("expired", consumer.expired);
  json.set_if("mode", consumer.mode);
  json.set("active", consumer.active);
  json.set("manual_enabled", consumer.manual_enabled);
  json.set("manual_target_w", static_cast<double>(consumer.manual_target_w), 1);
  json.set("distribution_weight", static_cast<double>(consumer.distribution_weight), 3);
  json.set("efficiency_window_weight_pct",
           static_cast<double>(consumer.efficiency_window_weight_pct), 1);
  json.set("min_dc_output_w", consumer.min_dc_output_w, 1);
  json.set("min_dc_output_applicable", consumer.min_dc_output_applicable);
  if (consumer.balancer.has_value()) write_consumer_balancer(json, *consumer.balancer);
  json.end_object();
}

// Mirrors serialize.py::_control_quality_to_wire.
void write_control_quality(JsonWriter &json, const ControlQualitySnapshot &quality) {
  json.begin_object("control_quality");
  json.set_if("verdict", quality.verdict);
  if (quality.has_score) json.set("score_pct", quality.score, 1);
  // The three measurements are omitted until the window holds a sample: the
  // EMAs start at zero, and "mean grid error 0 W, time inside band 0%" beside
  // an `idle` verdict describes a perfectly held grid and a permanently
  // failing one identically — neither of which was measured.
  if (quality.samples > 0) {
    json.set("error_w", quality.error_ema, 1);
    json.set("in_band_fraction", quality.in_band_fraction, 3);
    // Per minute on the wire: per second reads as 0.002 in the UI, and this
    // is a number a human is meant to interpret.
    json.set("crossings_per_min", quality.crossings_per_second * 60.0, 2);
  }
  // Configuration, not a measurement: always meaningful.
  json.set("band_w", static_cast<double>(quality.band), 1);
  json.set("samples", static_cast<long long>(quality.samples));
  json.end_object();
}

// Mirrors serialize.py::_balancer_to_wire, minus its `config` block: the page
// never renders the balancer's configuration (it is compiled into this
// firmware and visible in the YAML), so ~600 bytes of every poll would be
// paid for nothing.
void write_balancer(JsonWriter &json, const BalancerSnapshot &balancer) {
  json.begin_object("balancer");
  json.set("efficiency_rotation_enabled", balancer.efficiency_rotation_enabled);
  json.begin_object("predictor");
  json.set("grid_estimate_w", balancer.predictor.grid_estimate, 1);
  json.set("trust", static_cast<double>(balancer.predictor.trust), 3);
  json.set("innovation_sign", static_cast<long long>(balancer.predictor.innovation_sign));
  json.set("pool_output_w", static_cast<double>(balancer.predictor.pool_output), 1);
  json.end_object();
  json.begin_object("import_trim");
  json.set("dwell", static_cast<long long>(balancer.import_trim.dwell));
  json.set("dwell_target", static_cast<long long>(balancer.import_trim.dwell_target));
  json.set("gate_w", static_cast<double>(balancer.import_trim.gate), 1);
  json.set("engaged", balancer.import_trim.engaged);
  json.end_object();
  json.begin_object("efficiency");
  json.set("demand_ema_w", balancer.efficiency.demand_ema, 1);
  write_ids(json, "priority_order", balancer.efficiency.priority_order);
  write_ids(json, "deprioritized", balancer.efficiency.deprioritized);
  json.set("last_rotation_age_s", balancer.efficiency.last_rotation_age, 1);
  json.set("all_dc_under_surplus", balancer.efficiency.all_dc_under_surplus);
  json.end_object();
  write_control_quality(json, balancer.control_quality);
  if (balancer.probe.has_value()) {
    const ProbeSnapshot &probe = *balancer.probe;
    json.begin_object("probe");
    json.set_if("candidate_id", probe.candidate_id);
    write_ids(json, "active_ids", probe.active_ids);
    write_ids(json, "backup_ids", probe.backup_ids);
    json.set("proof_samples", static_cast<long long>(probe.proof_samples));
    json.set("requested_power_w", static_cast<double>(probe.requested_power_abs), 1);
    json.set("started_age_s", probe.started_age, 1);
    json.set("deadline_in_s", probe.deadline_in, 1);
    json.end_object();
  }
  json.end_object();
}

// Mirrors serialize.py::ct002_to_wire.
void write_device(JsonWriter &json, const DeviceStatus &device) {
  json.begin_object();
  json.set("kind", std::string("ct002"));
  json.set_if("device_id", device.device_id);
  json.set_if("ct_type", device.ct_type);
  json.set_if("ct_mac", device.ct_mac);
  json.set("udp_port", static_cast<long long>(device.udp_port));
  json.set("wifi_rssi_dbm", static_cast<long long>(device.wifi_rssi_dbm));
  json.set("running", device.running);
  json.set_time("started_at", device.started_at);
  json.begin_object("control");
  json.set("active_control", device.active_control);
  if (device.consumer_ttl_s.has_value())
    json.set("consumer_ttl_s", static_cast<long long>(*device.consumer_ttl_s));
  json.set("dedupe_window_s", static_cast<double>(device.dedupe_window_s), 3);
  json.end_object();
  if (device.grid.has_value()) {
    json.begin_object("grid");
    static constexpr const char *PHASE_KEYS[3] = {"l1_w", "l2_w", "l3_w"};
    for (size_t i = 0; i < 3; i++) {
      json.set(PHASE_KEYS[i], static_cast<double>((*device.grid)[i]), 1);
    }
    json.set("grid_total_w", static_cast<double>(device.grid_total_w), 1);
    json.set_time("sample_at", device.grid_sample_at);
    json.set("meter_failed", device.meter_failed);
    json.end_object();
  }
  json.begin_object("buckets");
  for (size_t i = 0; i < device.buckets.size(); i++) {
    json.begin_object(BUCKET_NAMES[i]);
    json.set("chrg_w", static_cast<double>(device.buckets[i].chrg_w), 1);
    json.set("dchrg_w", static_cast<double>(device.buckets[i].dchrg_w), 1);
    json.set("count", static_cast<long long>(device.buckets[i].count));
    json.set("active", device.buckets[i].active);
    json.end_object();
  }
  json.end_object();
  if (device.balancer.has_value()) write_balancer(json, *device.balancer);
  json.begin_array("consumers");
  for (const auto &consumer : device.consumers) write_consumer(json, consumer);
  json.end_array();
  json.end_object();
}

// Mirrors serialize.py::powermeter_to_wire.
void write_powermeter(JsonWriter &json, const PowermeterStatus &meter) {
  json.begin_object();
  json.set_if("name", meter.name);
  json.set_if("kind", meter.kind);
  write_ids(json, "pipeline", meter.pipeline);
  json.set("online", meter.online);
  json.set("last_read_age_s", meter.last_read_age_s, 1);
  json.set("last_read_ok", meter.last_read_ok);
  if (meter.last_values_w.has_value()) {
    json.begin_array("last_values_w");
    for (const float value : *meter.last_values_w) json.value(static_cast<double>(value), 1);
    json.end_array();
  }
  json.set("last_total_w", meter.last_total_w, 1);
  json.end_object();
}

// Mirrors the `integrations` block registry.py builds. Only MQTT Insights is
// carried: the page renders no card for the others, so cloud reporting and
// Marstek registration would be bytes on every poll that nothing reads.
void write_integrations(JsonWriter &json, const MqttInsightsStatus &insights) {
  json.begin_object("integrations");
  json.begin_object("mqtt_insights");
  json.set("connected", insights.connected);
  json.set_if("broker", insights.broker);
  if (insights.port != 0) json.set("port", static_cast<long long>(insights.port));
  json.set_if("base_topic", insights.base_topic);
  json.set("ha_discovery", insights.ha_discovery);
  json.set_if("ha_discovery_prefix", insights.ha_discovery_prefix);
  json.end_object();
  json.end_object();
}

}  // namespace

std::string format_number(double value, int digits) {
  if (!std::isfinite(value)) return {};
  char buffer[48];
  const int written = std::snprintf(buffer, sizeof(buffer), "%.*f", digits, value);
  // A magnitude that does not fit is not a reading, it is a bug upstream —
  // and half a number would be invalid JSON. Drop the field instead.
  if (written < 0 || static_cast<size_t>(written) >= sizeof(buffer)) return {};
  std::string text(buffer);
  // Trim the trailing zeros the fixed format leaves behind: "0.000" is three
  // wasted bytes on every field of every poll, and JSON reads "0" the same.
  if (text.find('.') != std::string::npos) {
    text.erase(text.find_last_not_of('0') + 1);
    if (!text.empty() && text.back() == '.') text.pop_back();
  }
  // "-0" is the same number as "0" and only ever confuses a reader.
  if (text == "-0" || text.empty()) text = "0";
  return text;
}

bool iso_utc_to(double epoch, char (&out)[ISO_UTC_BUF_SIZE]) {
  out[0] = '\0';
  if (epoch < WALL_CLOCK_SANE_THRESHOLD) return false;
  const std::time_t seconds = static_cast<std::time_t>(epoch);
  std::tm parts{};
#ifdef _WIN32
  if (gmtime_s(&parts, &seconds) != 0) return false;
#else
  if (gmtime_r(&seconds, &parts) == nullptr) return false;
#endif
  // "+00:00" rather than "Z", matching datetime.isoformat() on the Python side.
  const int written =
      std::snprintf(out, ISO_UTC_BUF_SIZE, "%04d-%02d-%02dT%02d:%02d:%02d+00:00",
                    parts.tm_year + 1900, parts.tm_mon + 1, parts.tm_mday, parts.tm_hour,
                    parts.tm_min, parts.tm_sec);
  if (written < 0 || static_cast<size_t>(written) >= ISO_UTC_BUF_SIZE) {
    out[0] = '\0';
    return false;
  }
  return true;
}

std::string iso_utc(double epoch) {
  char buffer[ISO_UTC_BUF_SIZE];
  if (!iso_utc_to(epoch, buffer)) return {};
  return std::string(buffer);
}

std::string build_status_json(const StatusDocument &doc) {
  JsonWriter json;
  json.begin_object();
  json.set("schema_version", static_cast<long long>(SCHEMA_VERSION));
  json.set_time("generated_at", doc.generated_at);
  json.begin_object("capabilities");
  json.set("backend", std::string("esphome"));
  json.set("stream", doc.capabilities.stream);
  json.set("poll_interval_ms", static_cast<long long>(doc.capabilities.poll_interval_ms));
  json.set("config_writable", doc.capabilities.config_writable);
  json.set("controls", doc.capabilities.controls);
  json.set("balancer_internals", doc.capabilities.balancer_internals);
  json.end_object();
  json.set("seq", static_cast<long long>(doc.seq));
  json.set("uptime_s", doc.uptime_s, 1);
  json.begin_object("service");
  json.set_if("version", doc.service.version);
  json.set_if("log_level", doc.service.log_level);
  json.set("runtime", std::string("esphome"));
  json.set_time("started_at", doc.service.started_at);
  json.begin_object("web");
  json.set("port", static_cast<long long>(doc.service.web_port));
  json.end_object();
  json.end_object();
  if (!doc.powermeters.empty()) {
    json.begin_array("powermeters");
    for (const auto &meter : doc.powermeters) write_powermeter(json, meter);
    json.end_array();
  }
  if (!doc.devices.empty()) {
    json.begin_array("devices");
    for (const auto &device : doc.devices) write_device(json, device);
    json.end_array();
  }
  if (doc.mqtt_insights.has_value()) write_integrations(json, *doc.mqtt_insights);
  json.end_object();
  return json.take();
}

}  // namespace status
}  // namespace ct002
}  // namespace esphome
