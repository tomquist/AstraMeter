#include "mqtt_insights.h"

#ifdef USE_MQTT
// Body only compiles when the mqtt component is configured. Forward-
// declarations in the header keep the class signature parseable on
// other platforms; the methods themselves only exist on builds that
// link the mqtt client.

#include <cmath>
#include <cstdio>
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

  // Capture Marstek topic identity from ct002. We allow these to be empty
  // (matching Python's behaviour where MAC may be derived later from
  // registration); subscribe_marstek_ handles that case.
  this->marstek_ct_type_ = this->ct002_->ct_type();
  this->marstek_mac_ = normalize_mac(this->ct002_->ct_mac());

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
    auto [topic, payload] =
        build_ct002_device_discovery(this->base_topic_, this->device_id_,
                                     this->ha_discovery_prefix_, this->addon_slug_);
    this->mqtt_->publish(topic, payload, 0, true);
    this->device_discovered_ = true;
  }

  this->subscribe_commands_();
  this->subscribe_marstek_();
}

void MqttInsightsComponent::on_mqtt_disconnected_() {
  ESP_LOGD(TAG, "MQTT disconnected");
  this->device_discovered_ = false;
  this->discovered_consumers_.clear();
  this->discovered_consumers_with_ip_.clear();
}

void MqttInsightsComponent::subscribe_commands_() {
  // Consumer command topic — single wildcard subscription, dispatch on
  // received message. ESPHome's mqtt client doesn't support arbitrary
  // wildcards in its subscribe_json convenience method (well, it does,
  // but we want to handle both consumer and device topics with one
  // handler), so we use the raw subscribe.
  const std::string consumer_wild =
      this->base_topic_ + "/ct002/" + this->device_id_ + "/consumer/+/set";
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

void MqttInsightsComponent::subscribe_marstek_() {
  if (!this->marstek_mqtt_enabled_) return;
  if (this->marstek_mac_.empty()) {
    ESP_LOGW(TAG,
             "Marstek MQTT enabled but ct_mac is unset — skipping App-topic subscribe. "
             "Set ct002.ct_mac (or wait for marstek_registration to derive one).");
    return;
  }
  for (const auto &topic : app_topics_for(this->marstek_ct_type_, this->marstek_mac_)) {
    this->mqtt_->subscribe(
        topic,
        [this](const std::string &t, const std::string &p) { this->handle_marstek_message_(t, p); },
        0);
  }
}

void MqttInsightsComponent::publish_consumer_event_(const std::string &consumer_id) {
  if (!this->mqtt_->is_connected()) return;
  auto snap = this->ct002_->snapshot_consumer(consumer_id);
  const std::string state_topic = this->base_topic_ + "/ct002/" + this->device_id_ +
                                  "/consumer/" + consumer_id;

  // Build per-consumer state JSON. Field set mirrors service.py's
  // consumer_state dict exactly so HA's value_templates resolve.
  auto state_buf = json::build_json([&](JsonObject root) {
    JsonObject gp = root["grid_power"].to<JsonObject>();
    const float total = snap.grid_power[0] + snap.grid_power[1] + snap.grid_power[2];
    gp["total"] = std::lround(total);
    gp["l1"] = std::lround(snap.grid_power[0]);
    gp["l2"] = std::lround(snap.grid_power[1]);
    gp["l3"] = std::lround(snap.grid_power[2]);
    JsonObject tg = root["target"].to<JsonObject>();
    tg["l1"] = std::lround(snap.target[0]);
    tg["l2"] = std::lround(snap.target[1]);
    tg["l3"] = std::lround(snap.target[2]);
    root["phase"] = snap.phase;
    root["reported_power"] = std::lround(snap.reported_power);
    root["device_type"] = snap.device_type;
    root["battery_ip"] = snap.last_ip;
    root["ct_type"] = this->ct002_->ct_type();
    root["ct_mac"] = this->ct002_->ct_mac();
    root["saturation"] = snap.saturation;
    if (snap.last_target.has_value()) {
      root["last_target"] = std::lround(*snap.last_target);
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
  });
  this->mqtt_->publish(state_topic, state_buf, 0, true);
  this->mqtt_->publish(state_topic + "/availability", "online", 6, 0, true);

  // Device-level status — published on every consumer update so HA sees
  // fresh smooth_target / consumer_count. Mirrors service.py:425.
  auto device_buf = json::build_json([&](JsonObject root) {
    float smooth = 0.0f;
    for (size_t i = 0; i < 3; ++i) smooth += snap.target[i];
    root["smooth_target"] = std::lround(smooth);
    // Reflect the configured active_control setting — HA's binary_sensor
    // should show "off" when the user disabled active control in YAML
    // rather than always reading "running".
    root["active_control"] = this->ct002_->active_control();
    root["consumer_count"] = this->ct002_->reporting_consumer_count();
  });
  this->mqtt_->publish(this->base_topic_ + "/ct002/" + this->device_id_ + "/status", device_buf, 0,
                       true);

  // Consumer-level discovery on first sight — re-published when
  // battery_ip first becomes known so HA's device.connections array
  // picks up the ["ip", ...] entry (Python: service.py:447-476 re-runs
  // discovery whenever ARP lookup succeeds for a previously-unknown
  // consumer). We track "discovered with IP" separately from
  // "discovered" so a later IP arrival triggers exactly one
  // re-discovery, not one per subsequent event.
  if (this->ha_discovery_) {
    const bool first_sight =
        this->discovered_consumers_.find(consumer_id) == this->discovered_consumers_.end();
    const bool ip_just_arrived =
        !snap.last_ip.empty() &&
        this->discovered_consumers_with_ip_.find(consumer_id) ==
            this->discovered_consumers_with_ip_.end();
    if (first_sight || ip_just_arrived) {
      this->discovered_consumers_.insert(consumer_id);
      if (!snap.last_ip.empty()) this->discovered_consumers_with_ip_.insert(consumer_id);
      auto [topic, payload] = build_ct002_consumer_discovery(
          this->base_topic_, this->device_id_, consumer_id, this->ha_discovery_prefix_,
          snap.device_type, /*network_mac=*/"", snap.last_ip);
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
  this->discovered_consumers_with_ip_.erase(consumer_id);
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
    const std::string consumer_id = middle.substr(consumer_marker.size());
    this->handle_consumer_command_(consumer_id, payload);
  } else if (middle.empty()) {
    this->handle_device_command_(payload);
  }
}

void MqttInsightsComponent::handle_consumer_command_(const std::string &consumer_id,
                                                     const std::string &payload) {
  bool parsed = json::parse_json(payload, [&](JsonObject root) -> bool {
    if (root["active"].is<bool>()) {
      this->ct002_->set_consumer_active(consumer_id, root["active"].as<bool>());
    }
    if (root["auto_target"].is<bool>()) {
      this->ct002_->set_consumer_auto_target(consumer_id, root["auto_target"].as<bool>());
    }
    if (root["manual_target"].is<float>() || root["manual_target"].is<int>()) {
      float t = root["manual_target"].as<float>();
      if (std::isfinite(t) && t >= -10000.0f && t <= 10000.0f) {
        this->ct002_->set_consumer_manual_target(consumer_id, t);
      } else {
        ESP_LOGW(TAG, "Out-of-range manual_target for %s: %.1f", consumer_id.c_str(), t);
      }
    }
    return true;
  });
  if (!parsed) ESP_LOGW(TAG, "Invalid consumer command payload for %s", consumer_id.c_str());
}

void MqttInsightsComponent::handle_device_command_(const std::string &payload) {
  bool parsed = json::parse_json(payload, [&](JsonObject root) -> bool {
    if (root["force_rotation"].is<bool>() && root["force_rotation"].as<bool>()) {
      this->ct002_->force_balancer_rotation();
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
  ESP_LOGCONFIG(TAG, "  Marstek MAC: %s",
                this->marstek_mac_.empty() ? "(unset — App-topic subscribe skipped)"
                                           : this->marstek_mac_.c_str());
}

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome

#endif  // USE_MQTT
