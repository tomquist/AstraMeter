// Bluetooth (BLE) command protocol of a real CT002/CT003, as the Marstek app
// speaks it. See docs/ct002-ct003-ble.md for the wire format and where each
// detail comes from.
//
// ESPHome-only: the Python stack has no BLE peripheral, so there is nothing
// to mirror (see the check-ct002-parity skill). No ESPHome dependencies, so
// host-gcc gtest drives it (tests/components/ct002/host_ble_protocol_test.cpp);
// the GATT server around it lives in ble_server.{h,cpp}.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace esphome {
namespace ct002 {
namespace ble {

// Frame: [0x73][LEN][0x23][CMD][payload...][XOR], LEN counting the whole frame
// and XOR covering every byte before it.
inline constexpr uint8_t FRAME_HEAD = 0x73;
inline constexpr uint8_t FRAME_MARK = 0x23;
inline constexpr size_t FRAME_OVERHEAD = 5;
// A real meter drops frames longer than this.
inline constexpr size_t MAX_REQUEST_LEN = 128;
// LEN is a single byte.
inline constexpr size_t MAX_FRAME_LEN = 255;
inline constexpr size_t MAX_PAYLOAD_LEN = MAX_FRAME_LEN - FRAME_OVERHEAD;

// Command bytes a real meter answers. Anything else is ignored without a reply.
enum Command : uint8_t {
  CMD_STATUS = 0x03,
  CMD_IDENTITY = 0x04,
  CMD_SET_WIFI = 0x05,
  CMD_FACTORY_RESET = 0x06,
  CMD_READ_SSID = 0x08,
  CMD_REBOOT = 0x09,
  CMD_LINKED_BATTERIES = 0x12,
  CMD_ACTIVE_POWER = 0x16,
};

// Separator between SSID and password in a CMD_SET_WIFI payload.
inline constexpr const char *WIFI_SEPARATOR = "<.,.>";

// Firmware versions reported in CMD_IDENTITY / CMD_STATUS: the newest
// published release per model at the time of writing (HME-4 v124, HME-3
// v122), so the app's onboarding firmware check finds nothing to offer.
inline constexpr uint8_t CT002_FIRMWARE_VERSION = 124;
inline constexpr uint8_t CT003_FIRMWARE_VERSION = 122;
// Wi-Fi module version string (`fc_ver`). Same value the MQTT responder
// reports as fc4_v.
inline constexpr const char *MODULE_VERSION = "202409090159";

// Advertised-name prefixes. The suffix is the last four characters of the
// device ID.
inline constexpr const char *CT002_NAME_PREFIX = "MST-TPM";
inline constexpr const char *CT003_NAME_PREFIX = "MST-SMR";

enum class Model : uint8_t { CT002, CT003 };

// HME-3 is a CT003; every other type the component accepts is a CT002.
Model model_for_type(const std::string &ct_type);

uint8_t xor_checksum(const uint8_t *data, size_t len);

// Build a frame around `payload`. Payloads longer than MAX_PAYLOAD_LEN are
// truncated, since LEN cannot describe them.
std::vector<uint8_t> build_frame(uint8_t cmd, const std::vector<uint8_t> &payload);

struct Frame {
  uint8_t cmd{0};
  std::vector<uint8_t> payload;
};

// Validate a received frame the way the meter does: head byte, marker byte,
// LEN matching the bytes received and at most MAX_REQUEST_LEN, and the XOR.
// Returns nullopt for anything else.
std::optional<Frame> parse_frame(const uint8_t *data, size_t len);

// Reassembles frames from GATT writes. The app writes a frame in one go when
// the MTU allows, but a small MTU splits it across writes; like the meter,
// this treats the writes as one byte stream, syncs on FRAME_HEAD and drops a
// partial frame after REASSEMBLY_TIMEOUT_MS without new bytes.
inline constexpr uint32_t REASSEMBLY_TIMEOUT_MS = 1000;

class FrameAssembler {
 public:
  // Feed one write; returns every complete, valid frame it finished.
  std::vector<Frame> feed(const uint8_t *data, size_t len, uint32_t now_ms);
  void reset() { this->buffer_.clear(); }

 private:
  std::vector<uint8_t> buffer_;
  uint32_t last_byte_ms_{0};
};

// "MST-TPM_abcd" / "MST-SMR_abcd" for the given model and device ID.
std::string advertised_name(Model model, const std::string &device_id);

// Normalize a MAC-like identifier to 12 lowercase hex characters ("" if it
// is not one).
std::string normalize_id(const std::string &raw);

struct BatteryRow {
  std::string type;   // battery device type, e.g. "HMG-50"
  std::string id;     // battery MAC / ID
  std::string ip;     // last seen IP
  std::string phase;  // "A" / "B" / "C" (first character is used)
};

// Everything a reply can be built from, gathered by the caller at the time
// a request arrives.
struct Snapshot {
  Model model{Model::CT002};
  std::string ct_type{"HME-4"};
  std::string device_id;  // 12 lowercase hex characters
  std::array<float, 3> phase_w{0.0f, 0.0f, 0.0f};
  bool wifi_connected{false};
  int rssi_dbm{0};
  std::string ssid;  // network the device is connected to
  std::vector<BatteryRow> batteries;
};

// What the caller has to do after a request, besides sending `reply`.
enum class Action : uint8_t {
  NONE,
  REBOOT,
};

struct WifiCredentials {
  std::string ssid;
  std::string password;
};

struct Result {
  std::vector<uint8_t> reply;  // empty: send nothing
  Action action{Action::NONE};
  std::optional<WifiCredentials> wifi;  // set by CMD_SET_WIFI
};

// Payload builders, exposed for tests.
std::vector<uint8_t> status_payload(const Snapshot &snapshot);
std::string identity_text(const Snapshot &snapshot);
std::string linked_batteries_text(const std::vector<BatteryRow> &rows, size_t max_len);
std::optional<WifiCredentials> parse_wifi_payload(const std::vector<uint8_t> &payload);

// Answers requests for one connection. Stateful only in remembering the SSID
// the app provisioned, which CMD_READ_SSID reports back: the app confirms
// Wi-Fi setup by reading back the network it just sent.
class Responder {
 public:
  Result handle(const Frame &frame, const Snapshot &snapshot);
  const std::string &provisioned_ssid() const { return this->provisioned_ssid_; }

 private:
  std::string provisioned_ssid_;
};

}  // namespace ble
}  // namespace ct002
}  // namespace esphome
