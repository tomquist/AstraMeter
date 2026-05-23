// AstraMeter Marstek cloud registration component.
//
// Mirrors src/astrameter/marstek_api.py adapted for ESPHome:
//
//   * The Python helper is invoked once at process start by config_loader.py
//     and blocks until done. On ESP32 we run the same flow as a state
//     machine driven by loop() ticks so the watchdog stays fed and the
//     rest of the device stays responsive while we talk to the cloud.
//   * The Python flow: fetch token, list devices, look for an existing
//     "managed" entry (devid+mac starting with `02b250`), and create one
//     via v2_add_device.php if missing. Same here.
//   * The MAC is the only piece that flows back to ct002 — we set
//     ct002.ct_mac so the CT002 responses and the Marstek MQTT topic
//     identity line up with the cloud-side device record. The MAC is
//     persisted via ESPPreferences so the second boot skips the HTTP
//     flow entirely (the Marstek cloud is rate-limited; re-running
//     registration on every boot is rude and slow).
//
// **Does NOT** ship the chunked-HTTP-in-loop pattern from the plan: we
// use ESPHome's http_request component, which already feeds the
// watchdog mid-read via http_read_fully. Each HTTP call blocks for a
// few seconds; the state machine just gates BETWEEN calls so the
// component yields back to other components between requests.
#pragma once

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "esphome/components/http_request/http_request.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"

#include "../ct002/ct002.h"

namespace esphome {
namespace marstek_registration {

// MAC prefix used by all our managed devices. Mirrors MANAGED_MAC_PREFIX
// in src/astrameter/marstek_api.py. Devices with this prefix are
// considered "owned" by AstraMeter and are reused across boots.
inline constexpr const char *MANAGED_MAC_PREFIX = "02b250";

// What we persist to ESPPreferences. The struct lives in flash so we
// keep it small and stable — changing the layout means orphaned data
// from older firmware. If you ever need to evolve this, bump the
// PREFS_TYPE_ID below so the old slot is ignored.
struct StoredMac {
  // 12 hex chars + null terminator. The trailing byte is the validity
  // flag (0xA5 = "this slot was written by us") so we can distinguish
  // empty flash from a deliberately-stored MAC without relying on
  // calloc-vs-malloc semantics of the preferences backend.
  char mac_hex[13];
  uint8_t valid;
};
// Stable preferences key. Mirrors hash_base32(...) used by the wider
// ESPHome ecosystem; ours just needs to be unique-per-component.
inline constexpr uint32_t PREFS_TYPE_ID = 0xC70021A5;

class MarstekRegistrationComponent : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  // After ct002 (so we can read its current ct_mac for change-detection)
  // and after http_request (so the http client is initialised).
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

  // Configuration setters — wired by the codegen.
  void set_ct002(ct002::CT002Component *c) { this->ct002_ = c; }
  void set_http(http_request::HttpRequestComponent *h) { this->http_ = h; }
  void set_base_url(const std::string &v) { this->base_url_ = v; }
  void set_mailbox(const std::string &v) { this->mailbox_ = v; }
  void set_password(const std::string &v) { this->password_ = v; }
  void set_timezone(const std::string &v) { this->timezone_ = v; }
  void set_device_type(const std::string &v) { this->device_type_ = v; }
  void set_retry_interval_ms(uint32_t v) { this->retry_interval_ms_ = v; }
  void set_force_reregister(bool v) { this->force_reregister_ = v; }

 protected:
  // State machine. Mirrors the linear flow in
  // marstek_api.py::ensure_managed_fake_device: token → list → maybe-add
  // → confirm. We split each Python helper into request-issue + parse
  // steps so the loop() can yield between them.
  enum class State {
    IDLE_PERSISTED,   // MAC loaded from prefs; nothing to do.
    WAIT_FOR_NETWORK, // First-boot or force_reregister — wait until http is up.
    FETCH_TOKEN,
    FETCH_DEVICES,
    DECIDE,           // Inspect device list; either DONE or ADD_DEVICE.
    ADD_DEVICE,
    REFRESH_DEVICES,
    CONFIRM,
    DONE,
    ERROR_BACKOFF,    // Transient error; sleep retry_interval_ms then restart.
    FATAL,            // Non-recoverable (bad credentials, malformed URL). Stop.
  };

  void enter_state_(State s);
  void tick_state_();
  bool network_ready_() const;

  // HTTP — perform one GET, returning true if a 2xx JSON body was read
  // into *out_body. On failure, logs and returns false. Blocks for up to
  // http_->get_timeout() while the request runs; ESPHome's http_read_fully
  // feeds the watchdog internally.
  bool http_get_json_(const std::string &url, std::string *out_body);

  // Build the full URL: base_url + path + "?" + percent-encoded params.
  std::string build_url_(const std::string &path,
                         const std::vector<std::pair<std::string, std::string>> &params) const;

  // MD5(password) lowercased hex — Marstek's password salt.
  std::string md5_hex_(const std::string &input) const;

  // Lowercased 6-hex-char random suffix. Uses esphome::random_uint32 so
  // it survives without a true RNG; collisions are caught by re-trying
  // when the candidate is already in the device list.
  std::string random_hex_(int n) const;

  // Inspect parsed device list for an entry whose devid+mac start with
  // MANAGED_MAC_PREFIX AND match the expected ct_type ("HME-4"/"HME-3").
  // Returns the matching MAC, or "" if none.
  std::string find_existing_managed_(const std::string &expected_type) const;
  // Generate a new devid/mac candidate that doesn't collide with the
  // current devices_. Returns "" after 200 unsuccessful attempts
  // (mirrors marstek_api.py::_generate_new_id).
  std::string generate_new_id_() const;

  // Persist on success. Mirrors the implicit "remember next boot" from
  // a successful config_loader run on the Python side.
  void persist_mac_(const std::string &mac);
  // Returns the MAC loaded from prefs at setup(), or "" if none.
  std::string load_persisted_mac_();

  // Push the chosen MAC into ct002 and the wider system. Idempotent.
  void apply_mac_(const std::string &mac);

  // Parsed device record from the merged solar/ems list. Tiny subset —
  // we only need devid/mac/type for decision-making; other fields are
  // logged on the Python side but not used.
  struct DeviceRecord {
    std::string devid;
    std::string mac;
    std::string type;
  };

  // Configuration.
  ct002::CT002Component *ct002_{nullptr};
  http_request::HttpRequestComponent *http_{nullptr};
  std::string base_url_;
  std::string mailbox_;
  std::string password_;
  std::string timezone_{"Europe/Berlin"};
  std::string device_type_{"ct002"};
  uint32_t retry_interval_ms_{60000};
  bool force_reregister_{false};

  // State.
  State state_{State::WAIT_FOR_NETWORK};
  std::string token_;
  std::vector<DeviceRecord> devices_;
  std::string candidate_mac_;
  uint32_t backoff_deadline_ms_{0};
  uint32_t last_attempt_ms_{0};
  ESPPreferenceObject pref_;
  std::string applied_mac_;
};

}  // namespace marstek_registration
}  // namespace esphome
