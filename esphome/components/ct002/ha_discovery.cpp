#include "ha_discovery.h"

#include <cctype>
#include <functional>

#include "esphome/components/json/json_util.h"

namespace esphome {
namespace ct002 {
namespace mqtt_insights {

namespace {

// Serialize a JSON object built by `fill` WITHOUT the 5120-byte cap that
// esphome::json::build_json imposes. The CT002 consumer discovery payload
// is ~7 KB (20 components inlined in one device-based config); build_json
// would silently truncate it to invalid JSON, so HA would never create
// the per-battery device. ArduinoJson itself has no such cap and ESP-IDF's
// MQTT client fragments large publishes, so building into a JsonDocument
// and serializing straight to a std::string publishes the full payload.
std::string serialize_unbounded(const std::function<void(JsonObject)> &fill) {
  JsonDocument doc;
  JsonObject root = doc.to<JsonObject>();
  fill(root);
  std::string out;
  serializeJson(doc, out);
  return out;
}

}  // namespace

std::string sanitize_id(const std::string &value) {
  std::string out;
  out.reserve(value.size());
  for (char c : value) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' ||
        c == '-') {
      out.push_back(c);
    } else {
      out.push_back('_');
    }
  }
  return out;
}

namespace {

// Build the standard `origin` block — we lose Python's git-sha resolution
// since the firmware build doesn't have access to it at runtime; emit a
// stable string so HA's "added by" metadata still groups our entities.
void add_origin(JsonObject obj) {
  JsonObject origin = obj["origin"].to<JsonObject>();
  origin["name"] = "astrameter";
  origin["sw_version"] = "esphome";
  origin["support_url"] = "https://github.com/tomquist/astrameter";
}

void add_system_availability(JsonObject avail, const std::string &base_topic) {
  avail["topic"] = base_topic + "/status";
  avail["payload_available"] = "online";
  avail["payload_not_available"] = "offline";
}

// Helper to add a power-sensor component entry. Mirrors the (key, label,
// tmpl) tuple loop in discovery.py for consumer/battery payloads.
void add_power_sensor(JsonObject components, const std::string &key, const std::string &label,
                      const std::string &uid_prefix, const std::string &tmpl,
                      const std::string &state_topic, bool primary) {
  JsonObject comp = components[key].to<JsonObject>();
  comp["platform"] = "sensor";
  comp["unique_id"] = uid_prefix + "_" + key;
  comp["device_class"] = "power";
  comp["unit_of_measurement"] = "W";
  comp["state_topic"] = state_topic;
  comp["value_template"] = tmpl;
  if (primary) {
    // ArduinoJson treats null and absent differently; HA wants "name: null"
    // for the primary entity (entity gets the device name).
    comp["name"] = nullptr;
  } else {
    comp["name"] = label;
  }
}

}  // namespace

std::pair<std::string, std::string> build_ct002_consumer_discovery(
    const std::string &base_topic, const std::string &device_id,
    const std::string &consumer_id, const std::string &ha_prefix,
    const std::string &device_type, const std::string &network_mac,
    const std::string &battery_ip) {
  const std::string safe_dev = sanitize_id(device_id);
  const std::string safe_cid = sanitize_id(consumer_id);
  const std::string node_id = "astrameter_ct002_" + safe_dev + "_" + safe_cid;
  const std::string state_topic =
      base_topic + "/ct002/" + device_id + "/consumer/" + consumer_id;
  const std::string avail_topic = state_topic + "/availability";
  const std::string uid_prefix = "astrameter_ct002_" + safe_dev + "_" + safe_cid;
  const std::string meter_identifier = "astrameter_ct002_" + safe_dev;
  // mac_slug: lowercase, strip "-" / "_" — used for both the device identifier
  // and the optional bluetooth connection. Mirrors discovery.py:190.
  std::string mac_slug = safe_cid;
  for (auto &c : mac_slug) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  std::string mac_slug_clean;
  mac_slug_clean.reserve(mac_slug.size());
  for (char c : mac_slug) {
    if (c != '-' && c != '_') mac_slug_clean.push_back(c);
  }
  mac_slug = mac_slug_clean;

  auto buf = serialize_unbounded([&](JsonObject root) {
    JsonObject components = root["components"].to<JsonObject>();

    // Power sensors. Same labels / templates as Python.
    add_power_sensor(components, "grid_power_total", "Grid Power", uid_prefix,
                     "{{ value_json.grid_power.total }}", state_topic, true);
    add_power_sensor(components, "grid_power_l1", "Grid Power L1", uid_prefix,
                     "{{ value_json.grid_power.l1 }}", state_topic, false);
    add_power_sensor(components, "grid_power_l2", "Grid Power L2", uid_prefix,
                     "{{ value_json.grid_power.l2 }}", state_topic, false);
    add_power_sensor(components, "grid_power_l3", "Grid Power L3", uid_prefix,
                     "{{ value_json.grid_power.l3 }}", state_topic, false);
    add_power_sensor(components, "target_l1", "Target L1", uid_prefix,
                     "{{ value_json.target.l1 }}", state_topic, false);
    add_power_sensor(components, "target_l2", "Target L2", uid_prefix,
                     "{{ value_json.target.l2 }}", state_topic, false);
    add_power_sensor(components, "target_l3", "Target L3", uid_prefix,
                     "{{ value_json.target.l3 }}", state_topic, false);
    add_power_sensor(components, "reported_power", "Reported Power", uid_prefix,
                     "{{ value_json.reported_power }}", state_topic, false);
    add_power_sensor(components, "last_target", "Last Target", uid_prefix,
                     "{{ value_json.last_target }}", state_topic, false);

    // Saturation
    JsonObject sat = components["saturation"].to<JsonObject>();
    sat["platform"] = "sensor";
    sat["unique_id"] = uid_prefix + "_saturation";
    sat["name"] = "Saturation";
    sat["unit_of_measurement"] = "%";
    sat["state_topic"] = state_topic;
    sat["value_template"] = "{{ (value_json.saturation * 100) | round(1) }}";

    // Phase enum
    JsonObject phase = components["phase"].to<JsonObject>();
    phase["platform"] = "sensor";
    phase["unique_id"] = uid_prefix + "_phase";
    phase["name"] = "Phase";
    phase["device_class"] = "enum";
    JsonArray opts = phase["options"].to<JsonArray>();
    opts.add("A");
    opts.add("B");
    opts.add("C");
    phase["state_topic"] = state_topic;
    phase["value_template"] = "{{ value_json.phase }}";
    phase["entity_category"] = "diagnostic";

    // Diagnostic string sensors.
    struct DiagSpec {
      const char *key;
      const char *label;
      const char *tmpl;
    };
    static const DiagSpec diag[] = {
        {"device_type", "Device Type", "{{ value_json.device_type }}"},
        {"battery_ip", "Battery IP", "{{ value_json.battery_ip }}"},
        {"ct_type", "CT Type", "{{ value_json.ct_type }}"},
        {"ct_mac", "CT MAC", "{{ value_json.ct_mac }}"},
    };
    for (const auto &d : diag) {
      JsonObject c = components[d.key].to<JsonObject>();
      c["platform"] = "sensor";
      c["unique_id"] = std::string(uid_prefix) + "_" + d.key;
      c["name"] = d.label;
      c["state_topic"] = state_topic;
      c["value_template"] = d.tmpl;
      c["entity_category"] = "diagnostic";
    }

    // Last seen timestamp
    JsonObject ls = components["last_seen"].to<JsonObject>();
    ls["platform"] = "sensor";
    ls["unique_id"] = uid_prefix + "_last_seen";
    ls["name"] = "Last Seen";
    ls["device_class"] = "timestamp";
    ls["state_topic"] = state_topic;
    ls["value_template"] = "{{ value_json.last_seen }}";
    ls["entity_category"] = "diagnostic";

    // Poll interval
    JsonObject pi = components["poll_interval"].to<JsonObject>();
    pi["platform"] = "sensor";
    pi["unique_id"] = uid_prefix + "_poll_interval";
    pi["name"] = "Poll Interval";
    pi["device_class"] = "duration";
    pi["unit_of_measurement"] = "s";
    pi["state_topic"] = state_topic;
    pi["value_template"] = "{{ value_json.poll_interval }}";
    pi["entity_category"] = "diagnostic";

    // Per-consumer controllable entities each use their own command topic with
    // retain=true, so Home Assistant persists the value across restarts (the
    // broker redelivers the retained command on re-subscribe). A dedicated
    // topic per setting is required — a broker keeps only one retained message
    // per topic. Mirrors discovery.py.

    // Manual target number
    JsonObject mt = components["manual_target"].to<JsonObject>();
    mt["platform"] = "number";
    mt["unique_id"] = uid_prefix + "_manual_target";
    mt["name"] = "Manual Target";
    mt["unit_of_measurement"] = "W";
    mt["device_class"] = "power";
    mt["min"] = -10000;
    mt["max"] = 10000;
    mt["mode"] = "box";
    mt["state_topic"] = state_topic;
    mt["value_template"] = "{{ value_json.manual_target | default(0) }}";
    mt["command_topic"] = state_topic + "/manual_target/set";
    mt["retain"] = true;
    mt["entity_category"] = "config";

    // Auto target switch
    JsonObject at = components["auto_target"].to<JsonObject>();
    at["platform"] = "switch";
    at["unique_id"] = uid_prefix + "_auto_target";
    at["name"] = "Auto Target";
    at["state_topic"] = state_topic;
    at["command_topic"] = state_topic + "/auto_target/set";
    at["value_template"] = "{{ value_json.auto_target }}";
    at["payload_on"] = "true";
    at["payload_off"] = "false";
    at["state_on"] = "True";
    at["state_off"] = "False";
    at["retain"] = true;
    at["entity_category"] = "config";

    // Active switch
    JsonObject act = components["active"].to<JsonObject>();
    act["platform"] = "switch";
    act["unique_id"] = uid_prefix + "_active";
    act["name"] = "Active";
    act["state_topic"] = state_topic;
    act["command_topic"] = state_topic + "/active/set";
    act["value_template"] = "{{ value_json.active }}";
    act["payload_on"] = "true";
    act["payload_off"] = "false";
    act["state_on"] = "True";
    act["state_off"] = "False";
    act["retain"] = true;

    // Distribution weight number — relative fair-share weight across batteries
    // (1.0 neutral). Raise on a larger battery / lower on a smaller one to bias
    // the split, e.g. 1.5 vs 1.0 for a ~60:40 distribution.
    JsonObject dw = components["distribution_weight"].to<JsonObject>();
    dw["platform"] = "number";
    dw["unique_id"] = uid_prefix + "_distribution_weight";
    dw["name"] = "Distribution Weight";
    dw["min"] = 0;
    dw["max"] = 10;
    dw["step"] = 0.1;
    dw["mode"] = "slider";
    dw["state_topic"] = state_topic;
    dw["value_template"] = "{{ value_json.distribution_weight | default(1.0) }}";
    dw["command_topic"] = state_topic + "/distribution_weight/set";
    dw["retain"] = true;
    dw["entity_category"] = "config";

    // Device info
    JsonObject device = root["device"].to<JsonObject>();
    JsonArray identifiers = device["identifiers"].to<JsonArray>();
    identifiers.add("astrameter_consumer_" + mac_slug);
    if (device_type.empty()) {
      device["name"] = "AstraMeter Consumer " + mac_slug;
    } else {
      device["name"] = "AstraMeter Consumer " + device_type + " " + mac_slug;
    }
    device["manufacturer"] = "Marstek";
    device["via_device"] = meter_identifier;
    // Connections array
    bool is_hex12 = mac_slug.size() == 12;
    if (is_hex12) {
      for (char c : mac_slug) {
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
          is_hex12 = false;
          break;
        }
      }
    }
    JsonArray conns = device["connections"].to<JsonArray>();
    bool any_conn = false;
    if (is_hex12) {
      std::string bt;
      bt.reserve(17);
      for (size_t i = 0; i < 12; i += 2) {
        if (i) bt.push_back(':');
        bt.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(mac_slug[i]))));
        bt.push_back(static_cast<char>(std::toupper(static_cast<unsigned char>(mac_slug[i + 1]))));
      }
      JsonArray e = conns.add<JsonArray>();
      e.add("bluetooth");
      e.add(bt);
      any_conn = true;
    }
    if (!network_mac.empty()) {
      JsonArray e = conns.add<JsonArray>();
      e.add("mac");
      e.add(network_mac);
      any_conn = true;
    }
    if (!battery_ip.empty()) {
      JsonArray e = conns.add<JsonArray>();
      e.add("ip");
      e.add(battery_ip);
      any_conn = true;
    }
    if (!any_conn) device.remove("connections");
    if (!device_type.empty()) device["model_id"] = device_type;

    add_origin(root);

    root["availability_mode"] = "all";
    JsonArray avail = root["availability"].to<JsonArray>();
    add_system_availability(avail.add<JsonObject>(), base_topic);
    JsonObject self_avail = avail.add<JsonObject>();
    self_avail["topic"] = avail_topic;
    self_avail["payload_available"] = "online";
    self_avail["payload_not_available"] = "offline";

    root["state_topic"] = state_topic;
  });

  return {ha_prefix + "/device/" + node_id + "/config", std::string(buf)};
}

std::pair<std::string, std::string> build_ct002_device_discovery(
    const std::string &base_topic, const std::string &device_id,
    const std::string &ha_prefix, const std::string &addon_slug) {
  const std::string safe_dev = sanitize_id(device_id);
  const std::string node_id = "astrameter_ct002_" + safe_dev;
  const std::string state_topic = base_topic + "/ct002/" + device_id + "/status";
  const std::string uid_prefix = "astrameter_ct002_" + safe_dev;

  auto buf = serialize_unbounded([&](JsonObject root) {
    JsonObject components = root["components"].to<JsonObject>();

    JsonObject st = components["smooth_target"].to<JsonObject>();
    st["platform"] = "sensor";
    st["unique_id"] = uid_prefix + "_smooth_target";
    st["name"] = nullptr;
    st["device_class"] = "power";
    st["unit_of_measurement"] = "W";
    st["state_topic"] = state_topic;
    st["value_template"] = "{{ value_json.smooth_target }}";

    JsonObject ac = components["active_control"].to<JsonObject>();
    ac["platform"] = "binary_sensor";
    ac["unique_id"] = uid_prefix + "_active_control";
    ac["name"] = "Active Control";
    ac["device_class"] = "running";
    ac["state_topic"] = state_topic;
    ac["value_template"] = "{{ value_json.active_control }}";
    ac["payload_on"] = "True";
    ac["payload_off"] = "False";

    JsonObject cc = components["consumer_count"].to<JsonObject>();
    cc["platform"] = "sensor";
    cc["unique_id"] = uid_prefix + "_consumer_count";
    cc["name"] = "Consumer Count";
    cc["state_topic"] = state_topic;
    cc["value_template"] = "{{ value_json.consumer_count }}";
    cc["entity_category"] = "diagnostic";

    JsonObject fr = components["force_rotation"].to<JsonObject>();
    fr["platform"] = "button";
    fr["unique_id"] = uid_prefix + "_force_rotation";
    fr["name"] = "Force Rotation";
    fr["command_topic"] = base_topic + "/ct002/" + device_id + "/set";
    fr["payload_press"] = "{\"force_rotation\": true}";
    fr["entity_category"] = "config";

    JsonObject device = root["device"].to<JsonObject>();
    device["identifiers"] = node_id;
    device["name"] = "AstraMeter CT002 " + device_id;
    device["manufacturer"] = "astrameter";
    if (!addon_slug.empty()) device["via_device"] = addon_slug;

    add_origin(root);
    JsonArray avail = root["availability"].to<JsonArray>();
    add_system_availability(avail.add<JsonObject>(), base_topic);
    root["state_topic"] = state_topic;
  });

  return {ha_prefix + "/device/" + node_id + "/config", std::string(buf)};
}

}  // namespace mqtt_insights
}  // namespace ct002
}  // namespace esphome
