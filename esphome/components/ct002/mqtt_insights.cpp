#include "mqtt_insights.h"

#ifdef USE_MQTT
// Body only compiles when the mqtt component is configured. Forward-
// declarations in the header keep the class signature parseable on
// other platforms; the methods themselves only exist on builds that
// link the mqtt client.

#include <cctype>
#include <cerrno>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <ctime>

#include "esphome/components/json/json_util.h"
#include "esphome/core/application.h"
#include "esphome/core/log.h"

#include "ha_discovery.h"

// Floor we treat as "real wall-clock time available" — anything before
// 2020-01-01 means SNTP hasn't synced yet and time(nullptr) is just
// returning seconds-since-boot. HA renders sub-1970 timestamps as the
// epoch start, which is worse than publishing null.
static constexpr time_t WALL_CLOCK_SANE_THRESHOLD = 1577836800;  // 2020-01-01 UTC

namespace esphome {
namespace ct002 {
namespace mqtt_insights {

static const char *const TAG = "astrameter.mqtt_insights";

void MqttInsightsComponent::setup() {
  if (this->ct002_ == nullptr) {
    ESP_LOGE(TAG, "ct002 not bound — refusing to start");
    this->mark_failed();
    return;
  }
  if (this->mqtt_ == nullptr) {
    // Fall back to the global mqtt client if codegen didn't set one (the
    // typical case — there's exactly one mqtt: block).
    this->mqtt_ = mqtt::global_mqtt_client;
  }
  if (this->mqtt_ == nullptr) {
    ESP_LOGE(TAG, "no mqtt component available — add an `mqtt:` block to your YAML");
    this->mark_failed();
    return;
  }

  // Marstek topic identity (ct_type + MAC) is resolved LAZILY from ct002
  // at connect time and re-checked every loop — NOT captured here. The MAC
  // often isn't known at setup(): marstek_registration runs at
  // setup_priority::AFTER_CONNECTION (after this component's AFTER_WIFI
  // setup) and applies the cloud/persisted MAC via ct002->set_ct_mac()
  // only then. Capturing it here would permanently miss it. See
  // ensure_marstek_subscription_.

  // Wire up the listeners — ct002 calls these synchronously after a
  // successful poll-reply cycle, mirroring the Python service's
  // on_ct002_response.
  this->ct002_->add_consumer_event_listener(
      [this](const std::string &cid) { this->publish_consumer_event_(cid); });
  this->ct002_->add_consumer_removed_listener(
      [this](const std::string &cid) { this->publish_consumer_removed_(cid); });

  // Marstek periodic broadcast — ESPHome's set_interval handles cleanup
  // on component teardown. We arm it once, regardless of connect state;
  // the tick checks is_connected() and skips when offline.
  if (this->marstek_mqtt_enabled_ && this->marstek_mqtt_interval_ms_ > 0) {
    this->set_interval("marstek_broadcast", this->marstek_mqtt_interval_ms_,
                       [this]() { this->marstek_broadcast_tick_(); });
    this->marstek_timer_armed_ = true;
  }

  ESP_LOGCONFIG(TAG, "MQTT Insights bound: device_id=%s, base_topic=%s",
                this->device_id_.c_str(), this->base_topic_.c_str());
}

void MqttInsightsComponent::loop() {
  // Detect connect/disconnect transitions ourselves — ESPHome's mqtt
  // component exposes set_on_connect but that's a setter (not multicast),
  // and we don't want to take that slot from the user's own callbacks.
  const bool connected = this->mqtt_->is_connected();
  if (connected && !this->was_connected_) {
    this->on_mqtt_connected_();
  } else if (!connected && this->was_connected_) {
    this->on_mqtt_disconnected_();
  }
  this->was_connected_ = connected;

  // Lazily subscribe to Marstek App topics once the MAC is known. Only
  // poll while not yet subscribed (marstek_mac_ empty) so there's no
  // steady-state per-loop cost; the MAC is applied once at boot by
  // marstek_registration and cleared here only on disconnect, so a
  // re-subscribe is driven by the reconnect path, not by polling.
  if (connected && this->marstek_mqtt_enabled_ && this->marstek_mac_.empty()) {
    this->ensure_marstek_subscription_();
  }
}

void MqttInsightsComponent::on_mqtt_connected_() {
  ESP_LOGD(TAG, "MQTT connected — publishing status and discovery");
  // Birth message — retained "online".
  this->mqtt_->publish(this->base_topic_ + "/status", "online", 6, 1, true);

  // Republish device-level discovery on every reconnect (in case the
  // broker dropped retained messages).
  this->device_discovered_ = false;
  this->discovered_consumers_.clear();

  if (this->ha_discovery_) {
    auto [topic, payload] = build_ct002_device_discovery(
        this->base_topic_, this->device_id_, this->ha_discovery_prefix_,
        this->ct002_ != nullptr && this->ct002_->efficiency_rotation_enabled());
    this->mqtt_->publish(topic, payload, 0, true);
    this->device_discovered_ = true;
  }

  this->subscribe_commands_();
  this->ensure_marstek_subscription_();
}

void MqttInsightsComponent::on_mqtt_disconnected_() {
  ESP_LOGD(TAG, "MQTT disconnected");
  this->device_discovered_ = false;
  this->discovered_consumers_.clear();
  // Drop the subscription record so we re-subscribe on reconnect (the
  // broker forgets non-persistent subscriptions across a disconnect).
  this->marstek_mac_.clear();
  this->marstek_ct_type_.clear();
}

void MqttInsightsComponent::subscribe_commands_() {
  // Consumer command topics — each per-consumer setting has its own retained
  // sub-topic ({base}/ct002/{dev}/consumer/{cid}/{field}/set), so we match the
  // extra wildcard level and dispatch on the trailing field. The device-level
  // button keeps the plain {base}/ct002/{dev}/set topic.
  const std::string consumer_wild =
      this->base_topic_ + "/ct002/" + this->device_id_ + "/consumer/+/+/set";
  const std::string device_topic =
      this->base_topic_ + "/ct002/" + this->device_id_ + "/set";
  this->mqtt_->subscribe(
      consumer_wild,
      [this](const std::string &topic, const std::string &payload) {
        this->handle_command_message_(topic, payload);
      },
      1);
  this->mqtt_->subscribe(
      device_topic,
      [this](const std::string &topic, const std::string &payload) {
        this->handle_command_message_(topic, payload);
      },
      1);
}

void MqttInsightsComponent::ensure_marstek_subscription_() {
  if (!this->marstek_mqtt_enabled_) return;
  if (!this->mqtt_->is_connected()) return;
  // Resolve the current identity from ct002 each call — the MAC may be set
  // by marstek_registration after we first connected.
  const std::string mac = normalize_mac(this->ct002_->ct_mac());
  const std::string ct = this->ct002_->ct_type();
  if (mac.empty()) return;  // not known yet — try again next loop
  // marstek_mac_/marstek_ct_type_ hold the CURRENTLY-subscribed identity.
  if (mac == this->marstek_mac_ && ct == this->marstek_ct_type_) return;  // already current
  // Identity changed (or first subscribe): drop the stale subscription.
  if (!this->marstek_mac_.empty()) {
    for (const auto &t : app_topics_for(this->marstek_ct_type_, this->marstek_mac_))
      this->mqtt_->unsubscribe(t);
  }
  for (const auto &t : app_topics_for(ct, mac)) {
    this->mqtt_->subscribe(
        t,
        [this](const std::string &tp, const std::string &p) { this->handle_marstek_message_(tp, p); },
        0);
  }
  this->marstek_mac_ = mac;
  this->marstek_ct_type_ = ct;
  ESP_LOGI(TAG, "Marstek MQTT: subscribed App topics for %s/%s", ct.c_str(), mac.c_str());
}

void MqttInsightsComponent::publish_consumer_event_(const std::string &consumer_id) {
  if (!this->mqtt_->is_connected()) return;
  auto snap = this->ct002_->snapshot_consumer(consumer_id);
  const std::string state_topic = this->base_topic_ + "/ct002/" + this->device_id_ +
                                  "/consumer/" + consumer_id;

  // Build per-consumer state JSON. Field set + value TYPES mirror
  // service.py's consumer_state dict (ct002.py:719-744): grid_power.* and
  // target.* are floats; reported_power is an int (parse_int upstream);
  // last_target is the balancer's raw float. Emitting floats here matters
  // because the HA value_templates pass the value through unrounded, so
  // lrounding would silently truncate fractional watts vs the Python stack.
  auto state_buf = json::build_json([&](JsonObject root) {
    JsonObject gp = root["grid_power"].to<JsonObject>();
    const float total = snap.grid_power[0] + snap.grid_power[1] + snap.grid_power[2];
    gp["total"] = total;
    gp["l1"] = snap.grid_power[0];
    gp["l2"] = snap.grid_power[1];
    gp["l3"] = snap.grid_power[2];
    JsonObject tg = root["target"].to<JsonObject>();
    tg["l1"] = snap.target[0];
    tg["l2"] = snap.target[1];
    tg["l3"] = snap.target[2];
    root["phase"] = snap.phase;
    root["reported_power"] = std::lround(snap.reported_power);
    root["device_type"] = snap.device_type;
    root["battery_ip"] = snap.last_ip;
    root["ct_type"] = this->ct002_->ct_type();
    root["ct_mac"] = this->ct002_->ct_mac();
    root["saturation"] = snap.saturation;
    if (snap.last_target.has_value()) {
      root["last_target"] = *snap.last_target;
    } else {
      root["last_target"] = nullptr;
    }
    root["active"] = snap.active;
    if (snap.poll_interval.has_value()) {
      root["poll_interval"] = *snap.poll_interval;
    } else {
      root["poll_interval"] = nullptr;
    }
    // Last seen timestamp — HA's `device_class: timestamp` wants Unix
    // epoch seconds (or ISO 8601). snap.timestamp is millis()-derived
    // (monotonic seconds since boot), which HA would render as ~1970+uptime
    // — clearly wrong. Use the system wall clock if SNTP has synced
    // (or any other time source has set it), otherwise publish null so
    // HA shows "unavailable" instead of a wildly-wrong date.
    const time_t now_wall = std::time(nullptr);
    if (now_wall >= WALL_CLOCK_SANE_THRESHOLD) {
      root["last_seen"] = static_cast<long>(now_wall);
    } else {
      root["last_seen"] = nullptr;
    }
    if (snap.manual_target.has_value()) {
      root["manual_target"] = *snap.manual_target;
    } else {
      root["manual_target"] = nullptr;
    }
    root["auto_target"] = snap.auto_target;
    root["distribution_weight"] = snap.distribution_weight;
    // On-wire value is the 0-1 fraction; HA renders it as a percentage.
    root["efficiency_window_weight"] = snap.efficiency_window_weight;
    if (snap.min_dc_output.has_value()) {
      root["min_dc_output"] = *snap.min_dc_output;
    } else {
      root["min_dc_output"] = nullptr;
    }
  });
  this->mqtt_->publish(state_topic, state_buf, 0, true);
  this->mqtt_->publish(state_topic + "/availability", "online", 6, 0, true);

  // Device-level status — published on every consumer update so HA sees
  // fresh smooth_target / consumer_count. Mirrors service.py:425.
  auto device_buf = json::build_json([&](JsonObject root) {
    // smooth_target is the total input grid power (post-filter,
    // pre-balancer), mirroring Python's _last_smooth_target — NOT the sum
    // of the balancer's per-phase output targets.
    root["smooth_target"] = std::lround(snap.smooth_target);
    // Reflect the live active_control setting — HA's "Active Control" switch
    // reads its state here, so it shows "off" when active control is disabled
    // (via YAML or the switch itself) rather than always reading "on".
    root["active_control"] = this->ct002_->active_control();
    root["consumer_count"] = this->ct002_->reporting_consumer_count();
  });
  this->mqtt_->publish(this->base_topic_ + "/ct002/" + this->device_id_ + "/status", device_buf, 0,
                       true);

  // Consumer-level discovery on first sight. The payload no longer depends on
  // battery_ip (no `connections` are emitted; see ha_discovery.cpp / #438), so
  // a single publish per consumer suffices — matching Python service.py.
  if (this->ha_discovery_) {
    const bool first_sight =
        this->discovered_consumers_.find(consumer_id) == this->discovered_consumers_.end();
    if (first_sight) {
      this->discovered_consumers_.insert(consumer_id);
      auto [topic, payload] = build_ct002_consumer_discovery(
          this->base_topic_, this->device_id_, consumer_id, this->ha_discovery_prefix_,
          snap.device_type,
          this->ct002_ != nullptr && this->ct002_->efficiency_rotation_enabled());
      this->mqtt_->publish(topic, payload, 0, true);
    }
  }
}

void MqttInsightsComponent::publish_consumer_removed_(const std::string &consumer_id) {
  if (!this->mqtt_->is_connected()) return;
  const std::string avail_topic = this->base_topic_ + "/ct002/" + this->device_id_ +
                                  "/consumer/" + consumer_id + "/availability";
  this->mqtt_->publish(avail_topic, "offline", 7, 0, true);
  this->discovered_consumers_.erase(consumer_id);
}

void MqttInsightsComponent::handle_command_message_(const std::string &topic,
                                                    const std::string &payload) {
  // Strip "{base}/ct002/{device}/" prefix and "/set" suffix.
  const std::string prefix = this->base_topic_ + "/ct002/" + this->device_id_ + "/";
  const std::string suffix = "/set";
  if (topic.size() <= prefix.size() + suffix.size()) return;
  if (topic.compare(0, prefix.size(), prefix) != 0) return;
  if (topic.compare(topic.size() - suffix.size(), suffix.size(), suffix) != 0) return;
  const std::string middle =
      topic.substr(prefix.size(), topic.size() - prefix.size() - suffix.size());

  const std::string consumer_marker = "consumer/";
  if (middle.compare(0, consumer_marker.size(), consumer_marker) == 0) {
    // rest = "{consumer_id}/{field}" — field is the trailing segment.
    const std::string rest = middle.substr(consumer_marker.size());
    const std::string::size_type pos = rest.rfind('/');
    if (pos == std::string::npos) return;  // malformed
    const std::string consumer_id = rest.substr(0, pos);
    const std::string field = rest.substr(pos + 1);
    this->handle_consumer_field_command_(consumer_id, field, payload);
  } else if (middle.empty()) {
    this->handle_device_command_(payload);
  }
}

// Parse a scalar boolean payload ("true"/"on"/"1" vs "false"/"off"/"0").
static bool parse_bool_payload(const std::string &payload, bool &out) {
  std::string s;
  for (char c : payload) {
    if (!std::isspace(static_cast<unsigned char>(c)))
      s.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
  }
  if (s == "true" || s == "on" || s == "1") {
    out = true;
    return true;
  }
  if (s == "false" || s == "off" || s == "0") {
    out = false;
    return true;
  }
  return false;
}

// Parse a scalar float payload. Returns false on empty/garbage input. Rejects
// trailing non-whitespace (e.g. "1.5abc") to match Python's float(), keeping
// the two command paths behaviourally identical.
static bool parse_float_payload(const std::string &payload, float &out) {
  const char *begin = payload.c_str();
  char *end = nullptr;
  errno = 0;
  const float v = std::strtof(begin, &end);
  if (end == begin || errno != 0)
    return false;
  while (*end != '\0') {
    if (!std::isspace(static_cast<unsigned char>(*end)))
      return false;
    ++end;
  }
  out = v;
  return true;
}

void MqttInsightsComponent::handle_consumer_field_command_(const std::string &consumer_id,
                                                           const std::string &field,
                                                           const std::string &payload) {
  // An empty payload clears a retained command — ignore it silently.
  if (payload.find_first_not_of(" \t\r\n") == std::string::npos) return;

  if (field == "active") {
    bool v;
    if (parse_bool_payload(payload, v))
      this->ct002_->set_consumer_active(consumer_id, v);
    else
      ESP_LOGW(TAG, "Invalid active value for %s: %s", consumer_id.c_str(), payload.c_str());
  } else if (field == "auto_target") {
    bool v;
    if (parse_bool_payload(payload, v))
      this->ct002_->set_consumer_auto_target(consumer_id, v);
    else
      ESP_LOGW(TAG, "Invalid auto_target value for %s: %s", consumer_id.c_str(), payload.c_str());
  } else if (field == "manual_target") {
    float t;
    if (!parse_float_payload(payload, t)) {
      ESP_LOGW(TAG, "Invalid manual_target value for %s: %s", consumer_id.c_str(), payload.c_str());
    } else if (std::isfinite(t) && t >= -10000.0f && t <= 10000.0f) {
      this->ct002_->set_consumer_manual_target(consumer_id, t);
    } else {
      ESP_LOGW(TAG, "Out-of-range manual_target for %s: %.1f", consumer_id.c_str(), t);
    }
  } else if (field == "distribution_weight") {
    float w;
    if (!parse_float_payload(payload, w)) {
      ESP_LOGW(TAG, "Invalid distribution_weight for %s: %s", consumer_id.c_str(), payload.c_str());
    } else if (std::isfinite(w) && w >= 0.0f && w <= 10.0f) {
      this->ct002_->set_consumer_distribution_weight(consumer_id, w);
    } else {
      ESP_LOGW(TAG, "Out-of-range distribution_weight for %s: %.2f", consumer_id.c_str(), w);
    }
  } else if (field == "efficiency_window_weight") {
    // HA sends a percentage (0-100 %); convert to the internal 0-1 fraction.
    float pct;
    if (!parse_float_payload(payload, pct)) {
      ESP_LOGW(TAG, "Invalid efficiency_window_weight for %s: %s", consumer_id.c_str(),
               payload.c_str());
    } else if (std::isfinite(pct) && pct >= 0.0f && pct <= 100.0f) {
      this->ct002_->set_consumer_efficiency_window_weight(consumer_id, pct / 100.0f);
    } else {
      ESP_LOGW(TAG, "Out-of-range efficiency_window_weight for %s: %.2f",
               consumer_id.c_str(), pct);
    }
  } else if (field == "min_dc_output") {
    float v;
    if (!parse_float_payload(payload, v)) {
      ESP_LOGW(TAG, "Invalid min_dc_output for %s: %s", consumer_id.c_str(), payload.c_str());
    } else if (std::isfinite(v) && v >= 0.0f && v <= 1000.0f) {
      this->ct002_->set_consumer_min_dc_output(consumer_id, v);
    } else {
      ESP_LOGW(TAG, "Out-of-range min_dc_output for %s: %.1f", consumer_id.c_str(), v);
    }
  }
}

void MqttInsightsComponent::handle_device_command_(const std::string &payload) {
  bool parsed = json::parse_json(payload, [&](JsonObject root) -> bool {
    if (root["force_rotation"].is<bool>() && root["force_rotation"].as<bool>()) {
      this->ct002_->force_balancer_rotation();
    }
    // Active Control switch — toggles the emulator between computing targets
    // (on, default) and relay mode (off). Published retained by HA so the
    // choice restores on restart.
    if (root["active_control"].is<bool>()) {
      this->ct002_->set_active_control(root["active_control"].as<bool>());
    } else if (!root["active_control"].isNull()) {
      ESP_LOGW(TAG, "Invalid active_control value in device command");
    }
    return true;
  });
  if (!parsed) ESP_LOGW(TAG, "Invalid device command payload");
}

void MqttInsightsComponent::handle_marstek_message_(const std::string &topic,
                                                    const std::string &payload) {
  auto parsed_topic = parse_app_topic(topic);
  if (!parsed_topic.has_value()) return;
  // Validate ct_type + mac match our binding (defensive — broker can
  // deliver a message that matches our subscription filter but with
  // unexpected content).
  if (parsed_topic->ct_type != this->marstek_ct_type_) return;
  if (parsed_topic->mac != this->marstek_mac_) return;

  auto poll = parse_poll_payload(payload);
  if (!poll.has_value()) {
    ESP_LOGD(TAG, "Marstek MQTT: non-poll payload on %s", topic.c_str());
    return;
  }
  this->publish_marstek_reply_(*poll);
}

void MqttInsightsComponent::marstek_broadcast_tick_() {
  if (!this->mqtt_->is_connected()) return;
  if (this->marstek_mac_.empty()) return;
  // Periodic broadcast uses the cd=1 frame shape so the app sees the
  // full runtime info (matches Python's _marstek_broadcast_loop's
  // MarstekPollContext(echo_cd=1, slave_id=None)).
  PollContext poll;
  poll.echo_cd = 1;
  this->publish_marstek_reply_(poll);
}

void MqttInsightsComponent::publish_marstek_reply_(const PollContext &poll) {
  std::string body;
  if (poll.echo_cd == 4) {
    // Convert ct002's row shape to the responder's standalone shape (the
    // responder header stays free of ESPHome deps so it's host-gcc testable).
    std::vector<ResponderRow> rows;
    for (const auto &r : this->ct002_->reporting_consumer_rows()) {
      rows.push_back({r.consumer_id, r.device_type, r.last_ip, r.phase});
    }
    body = format_cd4_slave_csv(rows);
  } else {
    // ver_v is hardcoded to DEFAULT_VER_V here. Python derives it per-binding
    // from the Marstek cloud device list's `version` field
    // (ver_v_from_marstek_api_version), but on the ESPHome path that value
    // isn't plumbed from marstek_registration into the responder — and when
    // the cloud `version` is absent Python falls back to this same default,
    // so the wire frame matches the common case. Wiring the real firmware
    // version through would be a registration↔insights data flow we don't
    // have yet.
    body = build_aggregate_response(this->ct002_->latest_grid_power(), this->ct002_->wifi_rssi(),
                                    DEFAULT_VER_V,
                                    static_cast<int>(this->ct002_->connected_slave_count()),
                                    /*echo_cd1=*/true);
  }
  for (const auto &reply : device_topics_for(this->marstek_ct_type_, this->marstek_mac_)) {
    this->mqtt_->publish(reply, body, 0, false);
  }
}

void MqttInsightsComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "AstraMeter MQTT Insights:");
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Base topic: %s", this->base_topic_.c_str());
  ESP_LOGCONFIG(TAG, "  HA Discovery: %s (prefix=%s)", YESNO(this->ha_discovery_),
                this->ha_discovery_prefix_.c_str());
  ESP_LOGCONFIG(TAG, "  Marstek MQTT: %s (interval=%us)", YESNO(this->marstek_mqtt_enabled_),
                this->marstek_mqtt_interval_ms_ / 1000U);
  // ct_mac is resolved lazily at connect time; at dump_config (boot) it
  // may legitimately still be empty if marstek_registration hasn't applied
  // it yet — the App-topic subscribe happens once it's known.
  const std::string mac_now = normalize_mac(this->ct002_->ct_mac());
  ESP_LOGCONFIG(TAG, "  Marstek MAC: %s",
                mac_now.empty() ? "(pending — subscribe deferred until MAC known)"
                                : mac_now.c_str());
}

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome

#endif  // USE_MQTT
