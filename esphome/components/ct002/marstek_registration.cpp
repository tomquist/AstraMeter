#include "marstek_registration.h"

#ifdef USE_CT002_MARSTEK_REGISTRATION
// Body only compiles when the marstek_registration sub-block is present in
// YAML. The header forward-declares HttpRequestComponent in that case so
// the class signature still parses on builds that omit http_request.h.

#include <algorithm>
#include <cstdio>
#include <cstring>

#include "esphome/components/http_request/http_request.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/md5/md5.h"
#include "esphome/components/network/util.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

namespace esphome {
namespace ct002 {
namespace marstek_registration {

static const char *const TAG = "marstek_registration";

namespace {

// Percent-encode for query-string usage — matches urllib.parse.urlencode
// for our payload values (which are all hex/email-style ASCII), conservatively
// escaping anything outside RFC 3986 unreserved chars.
std::string url_encode(const std::string &v) {
  // Don't name this `HEX` — Arduino's Print.h has `#define HEX 16` (used
  // by `Serial.print(x, HEX)`), and the preprocessor mangles our variable
  // into `static const char *16 = ...` on esp32-arduino. Same macro
  // collision lurks for any short ALL-CAPS Arduino-flavored name; prefer
  // descriptive identifiers in this file.
  static const char *HEX_DIGITS = "0123456789ABCDEF";
  std::string out;
  out.reserve(v.size() * 3);
  for (unsigned char c : v) {
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '-' ||
        c == '_' || c == '.' || c == '~') {
      out.push_back(static_cast<char>(c));
    } else {
      out.push_back('%');
      out.push_back(HEX_DIGITS[c >> 4]);
      out.push_back(HEX_DIGITS[c & 0x0F]);
    }
  }
  return out;
}

std::string to_lower_str(std::string s) {
  for (auto &c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

bool starts_with_lower(const std::string &s, const std::string &prefix) {
  if (s.size() < prefix.size()) return false;
  for (size_t i = 0; i < prefix.size(); ++i) {
    if (std::tolower(static_cast<unsigned char>(s[i])) !=
        std::tolower(static_cast<unsigned char>(prefix[i]))) {
      return false;
    }
  }
  return true;
}

std::string strip_trailing_slash(const std::string &s) {
  if (!s.empty() && s.back() == '/') return s.substr(0, s.size() - 1);
  return s;
}

// "ct002" → "HME-4", "ct003" → "HME-3". Mirrors marstek_api.py::_desired_type.
std::string desired_type(const std::string &device_type) {
  return device_type == "ct002" ? "HME-4" : "HME-3";
}

// Read the `code` field from a Marstek response, accepting either a JSON
// string ("2") or a JSON integer (2). Python casts via str() so it
// tolerates both shapes (marstek_api.py:98); the cloud has been observed
// emitting both depending on endpoint. Returns "" if the field is
// missing or some other type. Always lowercased.
std::string read_code(JsonObject root) {
  auto v = root["code"];
  if (v.is<const char *>()) {
    const char *s = v.as<const char *>();
    return s == nullptr ? std::string() : std::string(s);
  }
  // ArduinoJson's typed `is<>` is conservative — `is<long long>` is not a
  // stable specialization across builds (esp32-arduino's bundled
  // ArduinoJson can refuse the template), so probe with `is<int>` /
  // `is<long>` only and read as `long`. Marstek codes are single-digit
  // integers, so no overflow risk.
  if (v.is<int>() || v.is<long>()) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "%ld", static_cast<long>(v.as<long>()));
    return std::string(buf);
  }
  return {};
}

// User-facing device name in the Marstek cloud — keep in sync with
// marstek_api.py::_desired_name so the device list looks identical in
// the app whether the Python or ESPHome stack created it.
std::string desired_name(const std::string &device_type) {
  return device_type == "ct002" ? "AstraMeter CT002" : "AstraMeter CT003";
}

}  // namespace

void MarstekRegistrationComponent::setup() {
  if (this->ct002_ == nullptr) {
    ESP_LOGE(TAG, "ct002 not bound — refusing to start");
    this->mark_failed();
    return;
  }
  if (this->http_ == nullptr) {
    ESP_LOGE(TAG, "http_request not bound — add a `http_request:` block to your YAML");
    this->mark_failed();
    return;
  }
  if (this->mailbox_.empty() || this->password_.empty()) {
    ESP_LOGE(TAG, "mailbox/password required — refusing to start");
    this->mark_failed();
    return;
  }

  // Acquire a stable preferences slot. The hash combines the prefs type id
  // with the configured ct_type so changing device_type forces a fresh
  // registration (HME-3 and HME-4 records aren't interchangeable).
  uint32_t hash = PREFS_TYPE_ID;
  for (char c : this->device_type_) hash = hash * 33 + static_cast<uint8_t>(c);
  this->pref_ = global_preferences->make_preference<StoredMac>(hash, /*in_flash=*/true);

  std::string persisted = this->load_persisted_mac_();
  if (!persisted.empty() && !this->force_reregister_) {
    ESP_LOGI(TAG, "Marstek MAC loaded from prefs: %s", persisted.c_str());
    this->apply_mac_(persisted);
    this->enter_state_(State::IDLE_PERSISTED);
    return;
  }

  this->enter_state_(State::WAIT_FOR_NETWORK);
}

void MarstekRegistrationComponent::loop() {
  // Cheap fast path — short-circuit once registration is done so loop()
  // is essentially free for the rest of the device's life.
  if (this->state_ == State::IDLE_PERSISTED || this->state_ == State::DONE ||
      this->state_ == State::FATAL) {
    return;
  }
  // Backoff sleep — wait until the deadline before reattempting.
  if (this->state_ == State::ERROR_BACKOFF) {
    if (millis() < this->backoff_deadline_ms_) return;
    ESP_LOGI(TAG, "Backoff elapsed, retrying registration");
    this->enter_state_(State::WAIT_FOR_NETWORK);
  }
  this->tick_state_();
}

bool MarstekRegistrationComponent::network_ready_() const {
  return network::is_connected();
}

void MarstekRegistrationComponent::enter_state_(State s) {
  this->state_ = s;
  this->last_attempt_ms_ = millis();
}

void MarstekRegistrationComponent::tick_state_() {
  switch (this->state_) {
    case State::WAIT_FOR_NETWORK: {
      if (!this->network_ready_()) return;
      this->enter_state_(State::FETCH_TOKEN);
      return;
    }
    case State::FETCH_TOKEN: {
      const std::string url = this->build_url_(
          "/app/Solar/v2_get_device.php",
          {{"mailbox", this->mailbox_}, {"pwd", this->md5_hex_(this->password_)}});
      std::string body;
      if (!this->http_get_json_(url, &body)) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      // Parse: expect code=="2", token: <string>, data: [solar_devices].
      std::string token;
      std::vector<DeviceRecord> solar;
      bool ok = json::parse_json(body, [&](JsonObject root) -> bool {
        const std::string code = read_code(root);
        if (code != "2") {
          const char *msg = root["msg"].as<const char *>();
          ESP_LOGE(TAG, "Token fetch failed (code=%s): %s", code.empty() ? "?" : code.c_str(),
                   msg ? msg : "(no msg)");
          return false;
        }
        const char *t = root["token"].as<const char *>();
        if (t == nullptr || t[0] == '\0') return false;
        token = t;
        if (root["data"].is<JsonArray>()) {
          for (JsonObject d : root["data"].as<JsonArray>()) {
            DeviceRecord r;
            if (d["devid"].is<const char *>()) r.devid = d["devid"].as<const char *>();
            if (d["mac"].is<const char *>()) r.mac = d["mac"].as<const char *>();
            if (d["type"].is<const char *>()) r.type = d["type"].as<const char *>();
            solar.push_back(std::move(r));
          }
        }
        return true;
      });
      if (!ok || token.empty()) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      this->token_ = std::move(token);
      this->devices_ = std::move(solar);
      this->enter_state_(State::FETCH_DEVICES);
      return;
    }
    case State::FETCH_DEVICES: {
      // EMS device list — merged with the solar list per Python's
      // _fetch_token_and_devices. The merge only matters if we'd want to
      // surface the salt/version fields; for our decision logic (find an
      // entry whose devid AND mac start with MANAGED_MAC_PREFIX) the
      // type field is enough, so we accept either source.
      const std::string url = this->build_url_("/ems/api/v1/getDeviceList",
                                               {{"mailbox", this->mailbox_},
                                                {"token", this->token_}});
      std::string body;
      if (!this->http_get_json_(url, &body)) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      bool ok = json::parse_json(body, [&](JsonObject root) -> bool {
        if (!root["data"].is<JsonArray>()) return true;  // empty is OK
        for (JsonObject d : root["data"].as<JsonArray>()) {
          DeviceRecord r;
          if (d["devid"].is<const char *>()) r.devid = d["devid"].as<const char *>();
          if (d["mac"].is<const char *>()) r.mac = d["mac"].as<const char *>();
          if (d["type"].is<const char *>()) r.type = d["type"].as<const char *>();
          // Avoid duplicates by devid — solar list already has these
          // entries; only add EMS-only ones.
          bool seen = false;
          for (const auto &existing : this->devices_) {
            if (!r.devid.empty() && existing.devid == r.devid) {
              seen = true;
              break;
            }
          }
          if (!seen) this->devices_.push_back(std::move(r));
        }
        return true;
      });
      if (!ok) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      this->enter_state_(State::DECIDE);
      return;
    }
    case State::DECIDE: {
      const std::string expected = desired_type(this->device_type_);
      std::string existing = this->find_existing_managed_(expected);
      if (!existing.empty()) {
        ESP_LOGI(TAG, "Marstek managed %s already exists, reusing MAC=%s",
                 this->device_type_.c_str(), existing.c_str());
        this->apply_mac_(existing);
        this->persist_mac_(existing);
        this->enter_state_(State::DONE);
        return;
      }
      this->candidate_mac_ = this->generate_new_id_();
      if (this->candidate_mac_.empty()) {
        ESP_LOGE(TAG, "Could not generate unique managed MAC after 200 attempts");
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      ESP_LOGI(TAG, "Creating managed %s device (devid=mac=%s)", this->device_type_.c_str(),
               this->candidate_mac_.c_str());
      this->enter_state_(State::ADD_DEVICE);
      return;
    }
    case State::ADD_DEVICE: {
      const std::string suffix = this->candidate_mac_.size() >= 4
                                     ? this->candidate_mac_.substr(this->candidate_mac_.size() - 4)
                                     : std::string("0000");
      const std::string bt_name = "MST-SMR_" + suffix;
      const std::string url = this->build_url_(
          "/app/Solar/v2_add_device.php",
          {{"name", desired_name(this->device_type_)}, {"mailbox", this->mailbox_},
           {"devid", this->candidate_mac_}, {"mac", this->candidate_mac_},
           {"type", desired_type(this->device_type_)}, {"token", this->token_},
           {"access", "1"}, {"bluetooth_name", bt_name}, {"position", "{}"},
           {"timeZone", this->timezone_}, {"version", "121"}});
      // Add-device call gets the full Marstek-app header set: Content-Type
      // and Accept declare JSON, and the `token:` header carries the same
      // value we already encoded in the query string. Mirrors
      // marstek_api.py:210-215 — some backend versions reject the call if
      // any of these are missing even though the wire payload is identical.
      const std::vector<std::pair<std::string, std::string>> add_headers = {
          {"Content-Type", "application/json"},
          {"token", this->token_},
      };
      std::string body;
      if (!this->http_get_json_(url, &body, add_headers)) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      bool ok = json::parse_json(body, [&](JsonObject root) -> bool {
        const std::string code = read_code(root);
        if (code != "1" && code != "2") {
          const char *msg = root["msg"].as<const char *>();
          ESP_LOGE(TAG, "Add device failed (code=%s): %s", code.empty() ? "?" : code.c_str(),
                   msg ? msg : "(no msg)");
          return false;
        }
        return true;
      });
      if (!ok) {
        this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
        this->enter_state_(State::ERROR_BACKOFF);
        return;
      }
      this->enter_state_(State::REFRESH_DEVICES);
      return;
    }
    case State::REFRESH_DEVICES: {
      // Re-fetch token + device list once for confirmation, exactly like
      // ensure_managed_fake_device's final block. The Marstek API needs a
      // fresh token here (the previous one may be stale).
      this->token_.clear();
      this->devices_.clear();
      this->enter_state_(State::FETCH_TOKEN);
      return;
    }
    case State::CONFIRM: {
      // Reached only via REFRESH_DEVICES → FETCH_TOKEN → FETCH_DEVICES →
      // DECIDE; DECIDE handles "found existing" by setting DONE itself.
      // If we got here it means decide didn't find our newly-created
      // device, which is a soft warning per Python.
      ESP_LOGW(TAG, "Created %s but could not confirm — retrying later",
               this->device_type_.c_str());
      this->backoff_deadline_ms_ = millis() + this->retry_interval_ms_;
      this->enter_state_(State::ERROR_BACKOFF);
      return;
    }
    case State::DONE:
    case State::IDLE_PERSISTED:
    case State::ERROR_BACKOFF:
    case State::FATAL:
      return;
  }
}

bool MarstekRegistrationComponent::http_get_json_(
    const std::string &url, std::string *out_body,
    const std::vector<std::pair<std::string, std::string>> &extra_headers) {
  std::vector<http_request::Header> headers = {
      {"User-Agent", "Dart/2.19 (dart:io)"},
      {"Accept", "application/json"},
  };
  // Caller-supplied headers (Content-Type, token, etc.) — owned-by-string
  // pairs are converted to the const-char-pointer Header struct just before
  // the call so the strings stay alive for the request duration.
  for (const auto &h : extra_headers) headers.push_back({h.first.c_str(), h.second.c_str()});
  auto container = this->http_->get(url, headers);
  if (container == nullptr || container->status_code < 200 || container->status_code >= 300) {
    ESP_LOGW(TAG, "HTTP %d from %s", container ? container->status_code : -1, url.c_str());
    if (container != nullptr) container->end();
    return false;
  }
  // Read up to 8 KB into a heap buffer — Marstek responses are tiny
  // (~hundreds of bytes); 8 KB is several headroom for the device list.
  constexpr size_t MAX_BODY = 8192;
  std::vector<uint8_t> buf;
  buf.resize(std::min<size_t>(container->content_length ? container->content_length : MAX_BODY,
                              MAX_BODY));
  if (buf.empty()) {
    container->end();
    out_body->clear();
    return true;
  }
  auto result = http_request::http_read_fully(
      container.get(), buf.data(), buf.size(),
      /*chunk_size=*/512, /*timeout_ms=*/this->http_->get_timeout());
  // Capture bytes_read BEFORE end() — some backends invalidate the
  // container's counters once end() runs.
  const size_t bytes_read = container->get_bytes_read();
  container->end();
  if (result.status != http_request::HttpReadStatus::OK) {
    ESP_LOGW(TAG, "HTTP read failed for %s (status=%d, err=%d)", url.c_str(),
             static_cast<int>(result.status), result.error_code);
    return false;
  }
  out_body->assign(reinterpret_cast<const char *>(buf.data()), bytes_read);
  return true;
}

bool MarstekRegistrationComponent::http_get_json_(const std::string &url, std::string *out_body) {
  return this->http_get_json_(url, out_body, {});
}

std::string MarstekRegistrationComponent::build_url_(
    const std::string &path,
    const std::vector<std::pair<std::string, std::string>> &params) const {
  std::string url = strip_trailing_slash(this->base_url_) + path;
  if (params.empty()) return url;
  url.push_back('?');
  bool first = true;
  for (const auto &kv : params) {
    if (!first) url.push_back('&');
    first = false;
    url.append(url_encode(kv.first));
    url.push_back('=');
    url.append(url_encode(kv.second));
  }
  return url;
}

std::string MarstekRegistrationComponent::md5_hex_(const std::string &input) const {
  md5::MD5Digest d;
  d.init();
  d.add(reinterpret_cast<const uint8_t *>(input.data()), input.size());
  d.calculate();
  char hex[33];
  d.get_hex(hex);
  return to_lower_str(std::string(hex, 32));
}

std::string MarstekRegistrationComponent::random_hex_(int n) const {
  static const char *DIGITS = "0123456789abcdef";
  std::string out;
  out.reserve(n);
  uint32_t r = 0;
  for (int i = 0; i < n; ++i) {
    if ((i & 7) == 0) r = ::esphome::random_uint32();
    out.push_back(DIGITS[(r >> ((i & 7) * 4)) & 0xF]);
  }
  return out;
}

std::string MarstekRegistrationComponent::find_existing_managed_(
    const std::string &expected_type) const {
  for (const auto &d : this->devices_) {
    if (d.type != expected_type) continue;
    if (!starts_with_lower(d.devid, MANAGED_MAC_PREFIX)) continue;
    if (!starts_with_lower(d.mac, MANAGED_MAC_PREFIX)) continue;
    return to_lower_str(d.mac);
  }
  return {};
}

std::string MarstekRegistrationComponent::generate_new_id_() const {
  for (int attempt = 0; attempt < 200; ++attempt) {
    std::string candidate = std::string(MANAGED_MAC_PREFIX) + this->random_hex_(6);
    bool collision = false;
    for (const auto &d : this->devices_) {
      if (to_lower_str(d.devid) == candidate || to_lower_str(d.mac) == candidate) {
        collision = true;
        break;
      }
    }
    if (!collision) return candidate;
  }
  return {};
}

void MarstekRegistrationComponent::persist_mac_(const std::string &mac) {
  StoredMac s{};
  // memcpy + explicit null avoids gcc's -Wstringop-truncation warning
  // (which esp32-arduino's xtensa-gcc-with-Werror treats as a hard error
  // when strncpy is followed by manual termination — the classic
  // "you might have just truncated without intending to" pattern).
  const size_t n = std::min(mac.size(), sizeof(s.mac_hex) - 1);
  std::memcpy(s.mac_hex, mac.data(), n);
  s.mac_hex[n] = '\0';
  s.valid = 0xA5;
  if (!this->pref_.save(&s)) {
    ESP_LOGW(TAG, "Could not persist Marstek MAC to flash");
  }
}

std::string MarstekRegistrationComponent::load_persisted_mac_() {
  StoredMac s{};
  if (!this->pref_.load(&s)) return {};
  if (s.valid != 0xA5) return {};
  s.mac_hex[sizeof(s.mac_hex) - 1] = '\0';
  std::string mac(s.mac_hex);
  if (mac.size() != 12) return {};
  return to_lower_str(mac);
}

void MarstekRegistrationComponent::apply_mac_(const std::string &mac) {
  if (mac == this->applied_mac_) return;
  this->applied_mac_ = mac;
  // Ct002's set_ct_mac is a public setter — fine to call after setup().
  // Subsequent CT002 responses use the new MAC. The MQTT-insights
  // component subscribes to App topics at its setup(), so a runtime
  // MAC change after that point won't be picked up — users with
  // mqtt_insights AND marstek_registration enabled should expect to
  // reboot once after first-time registration.
  this->ct002_->set_ct_mac(mac);
}

void MarstekRegistrationComponent::dump_config() {
  ESP_LOGCONFIG(TAG, "Marstek Registration:");
  ESP_LOGCONFIG(TAG, "  Base URL: %s", this->base_url_.c_str());
  ESP_LOGCONFIG(TAG, "  Mailbox: %s", this->mailbox_.c_str());
  ESP_LOGCONFIG(TAG, "  Device type: %s (cloud: %s)", this->device_type_.c_str(),
                desired_type(this->device_type_).c_str());
  ESP_LOGCONFIG(TAG, "  Timezone: %s", this->timezone_.c_str());
  ESP_LOGCONFIG(TAG, "  Force re-register: %s", YESNO(this->force_reregister_));
  ESP_LOGCONFIG(TAG, "  Applied MAC: %s",
                this->applied_mac_.empty() ? "(pending)" : this->applied_mac_.c_str());
}

}  // namespace marstek_registration
}  // namespace ct002
}  // namespace esphome

#endif  // USE_CT002_MARSTEK_REGISTRATION
