// AstraMeter MQTT Insights component. Mirrors the Python service in
// src/astrameter/mqtt_insights/service.py adapted for ESPHome's single-
// threaded loop:
//   * No asyncio queue — events fire synchronously when ct002 calls back.
//   * No reconnect loop — ESPHome's mqtt component owns reconnect; we
//     detect connect/disconnect transitions by polling is_connected() and
//     re-publish discovery on rising edges.
//   * No ARP lookup — lwIP doesn't expose /proc/net/arp; we surface
//     bluetooth + ip connections only (network_mac stays empty).
//
// Wiring: the component takes a CT002Component* (required) and a
// MQTTClientComponent* (defaults to global_mqtt_client). It subscribes to
// command + Marstek topics on first connect and re-subscribes on each
// reconnect. Discovery state is cleared on disconnect so retained-but-
// stale entries get republished correctly when the broker comes back.
#pragma once

#include <string>
#include <unordered_set>

#include "esphome/core/component.h"
#include "esphome/core/defines.h"

#include "ct002.h"
#include "marstek_responder.h"

// `mqtt:` is only supported on esp32 / esp8266 / bk72xx / rtl87xx — on
// the host platform there is no mqtt_client.h and the class declaration
// here would fail to parse. Forward-declare the mqtt client pointer and
// gate the include + class body on USE_MQTT so this header compiles
// cleanly on every target (including host, where the sub-block is never
// instantiated anyway).
#ifdef USE_MQTT
#include "esphome/components/mqtt/mqtt_client.h"
#endif

namespace esphome {
#ifndef USE_MQTT
// Minimal stub so the pointer member type below resolves on platforms
// without MQTT. The real declaration lives in mqtt/mqtt_client.h.
namespace mqtt {
class MQTTClientComponent;
}
#endif
namespace ct002 {
namespace mqtt_insights {

class MqttInsightsComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // Configuration (set from codegen).
  void set_ct002(ct002::CT002Component *ct002) { this->ct002_ = ct002; }
  void set_mqtt(mqtt::MQTTClientComponent *mqtt) { this->mqtt_ = mqtt; }
  void set_device_id(const std::string &v) { this->device_id_ = v; }
  void set_base_topic(const std::string &v) { this->base_topic_ = v; }
  void set_ha_discovery(bool v) { this->ha_discovery_ = v; }
  void set_ha_discovery_prefix(const std::string &v) { this->ha_discovery_prefix_ = v; }
  void set_addon_slug(const std::string &v) { this->addon_slug_ = v; }
  void set_marstek_mqtt_enabled(bool v) { this->marstek_mqtt_enabled_ = v; }
  void set_marstek_mqtt_interval_ms(uint32_t v) { this->marstek_mqtt_interval_ms_ = v; }

 protected:
  // Reaction to a fresh consumer event from ct002. Mirrors
  // service.py::_handle_ct002_event.
  void publish_consumer_event_(const std::string &consumer_id);
  void publish_consumer_removed_(const std::string &consumer_id);

  // Discovery republish — called on every connect rising edge.
  void on_mqtt_connected_();
  void on_mqtt_disconnected_();

  // Command path — invoked from the mqtt subscribe callback.
  void handle_command_message_(const std::string &topic, const std::string &payload);
  void handle_marstek_message_(const std::string &topic, const std::string &payload);
  void handle_consumer_field_command_(const std::string &consumer_id, const std::string &field,
                                      const std::string &payload);
  void handle_device_command_(const std::string &payload);

  // Marstek periodic broadcast (runs on a set_interval timer).
  void marstek_broadcast_tick_();
  // Send a Marstek reply for a single poll (poll == nullopt → use core frame).
  void publish_marstek_reply_(const PollContext &poll);

  // Subscribe helpers.
  void subscribe_commands_();
  // (Re)subscribe to Marstek App topics once ct002's ct_mac is known.
  // Idempotent: no-op while the MAC is empty or unchanged; re-subscribes
  // if the MAC changes (e.g. marstek_registration applies it after we
  // connected). Called on connect and every loop while connected.
  void ensure_marstek_subscription_();

  // Configuration.
  ct002::CT002Component *ct002_{nullptr};
  mqtt::MQTTClientComponent *mqtt_{nullptr};
  std::string device_id_{"ct002_main"};
  std::string base_topic_{"astrameter"};
  bool ha_discovery_{true};
  std::string ha_discovery_prefix_{"homeassistant"};
  std::string addon_slug_;
  bool marstek_mqtt_enabled_{true};
  uint32_t marstek_mqtt_interval_ms_{300000};

  // Connection state tracking.
  bool was_connected_{false};

  // Discovery dedupe — keys cleared on disconnect.
  bool device_discovered_{false};
  std::unordered_set<std::string> discovered_consumers_;
  // Subset of discovered_consumers_ that had a non-empty battery_ip when
  // discovery was published. Used to trigger exactly one re-publish when
  // an IP first becomes known (mirrors Python's ARP-success re-discovery).
  std::unordered_set<std::string> discovered_consumers_with_ip_;

  // Marstek broadcast scheduling — uses set_interval, captured here so we
  // can cancel if reconfigured at runtime. Single timer because there's
  // only one ct002 per insights component.
  bool marstek_timer_armed_{false};

  // Currently-subscribed Marstek identity (normalised MAC + ct_type).
  // Empty when not subscribed. Set by ensure_marstek_subscription_ once
  // ct002's ct_mac is known; cleared on disconnect so we re-subscribe on
  // reconnect. handle_marstek_message_ / publish_marstek_reply_ key off
  // these, so they're always in sync with the live subscription.
  std::string marstek_mac_;
  std::string marstek_ct_type_;
};

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome
