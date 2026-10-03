#include "ble_protocol.h"

#include <cctype>
#include <cmath>
#include <cstring>
#include <limits>

namespace esphome {
namespace ct002 {
namespace ble {

namespace {

template<typename T> T clamp_round(float value) {
  if (!std::isfinite(value)) return 0;
  const double rounded = std::round(static_cast<double>(value));
  if (rounded > static_cast<double>(std::numeric_limits<T>::max())) return std::numeric_limits<T>::max();
  if (rounded < static_cast<double>(std::numeric_limits<T>::min())) return std::numeric_limits<T>::min();
  return static_cast<T>(rounded);
}

void put_le16(std::vector<uint8_t> &out, size_t at, int16_t value) {
  const auto u = static_cast<uint16_t>(value);
  out[at] = static_cast<uint8_t>(u & 0xFF);
  out[at + 1] = static_cast<uint8_t>(u >> 8);
}

void put_le32(std::vector<uint8_t> &out, size_t at, int32_t value) {
  const auto u = static_cast<uint32_t>(value);
  for (size_t i = 0; i < 4; ++i) out[at + i] = static_cast<uint8_t>((u >> (8 * i)) & 0xFF);
}

uint8_t rssi_byte(const Snapshot &s) {
  if (!s.wifi_connected) return 0;
  int rssi = s.rssi_dbm;
  if (rssi < -127) rssi = -127;
  if (rssi > 0) rssi = 0;
  return static_cast<uint8_t>(static_cast<int8_t>(rssi));
}

// Characters that would break the app's `k=v,k=v;` parsing.
std::string sanitize_field(const std::string &value) {
  std::string out;
  out.reserve(value.size());
  for (char c : value) {
    out.push_back((c == ',' || c == ';' || c == '=') ? '_' : c);
  }
  return out;
}

std::vector<uint8_t> text_bytes(const std::string &text) { return {text.begin(), text.end()}; }

}  // namespace

Model model_for_type(const std::string &ct_type) { return ct_type == "HME-3" ? Model::CT003 : Model::CT002; }

uint8_t xor_checksum(const uint8_t *data, size_t len) {
  uint8_t x = 0;
  for (size_t i = 0; i < len; ++i) x ^= data[i];
  return x;
}

std::vector<uint8_t> build_frame(uint8_t cmd, const std::vector<uint8_t> &payload) {
  const size_t body = payload.size() > MAX_PAYLOAD_LEN ? MAX_PAYLOAD_LEN : payload.size();
  std::vector<uint8_t> frame;
  frame.reserve(body + FRAME_OVERHEAD);
  frame.push_back(FRAME_HEAD);
  frame.push_back(static_cast<uint8_t>(body + FRAME_OVERHEAD));
  frame.push_back(FRAME_MARK);
  frame.push_back(cmd);
  frame.insert(frame.end(), payload.begin(), payload.begin() + static_cast<std::ptrdiff_t>(body));
  frame.push_back(xor_checksum(frame.data(), frame.size()));
  return frame;
}

std::optional<Frame> parse_frame(const uint8_t *data, size_t len) {
  if (data == nullptr || len < FRAME_OVERHEAD || len > MAX_REQUEST_LEN) return std::nullopt;
  if (data[0] != FRAME_HEAD || data[2] != FRAME_MARK) return std::nullopt;
  if (data[1] != len) return std::nullopt;
  if (xor_checksum(data, len - 1) != data[len - 1]) return std::nullopt;
  Frame frame;
  frame.cmd = data[3];
  frame.payload.assign(data + 4, data + len - 1);
  return frame;
}

std::vector<Frame> FrameAssembler::feed(const uint8_t *data, size_t len, uint32_t now_ms) {
  std::vector<Frame> frames;
  if (!this->buffer_.empty() && now_ms - this->last_byte_ms_ > REASSEMBLY_TIMEOUT_MS) this->buffer_.clear();
  if (len > 0) this->last_byte_ms_ = now_ms;
  for (size_t i = 0; i < len; ++i) {
    const uint8_t byte = data[i];
    // Sync on the head byte; anything before it is noise.
    if (this->buffer_.empty() && byte != FRAME_HEAD) continue;
    this->buffer_.push_back(byte);
    if (this->buffer_.size() < 2) continue;
    const size_t want = this->buffer_[1];
    if (want < FRAME_OVERHEAD || want > MAX_REQUEST_LEN) {
      this->buffer_.clear();
      continue;
    }
    if (this->buffer_.size() < want) continue;
    if (auto frame = parse_frame(this->buffer_.data(), this->buffer_.size())) frames.push_back(std::move(*frame));
    this->buffer_.clear();
  }
  return frames;
}

std::string normalize_id(const std::string &raw) {
  std::string out;
  for (char c : raw) {
    if (c == ':' || c == '-' || c == ' ') continue;
    if (!std::isxdigit(static_cast<unsigned char>(c))) return "";
    out.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
  }
  return out.size() == 12 ? out : "";
}

std::string advertised_name(Model model, const std::string &device_id) {
  const std::string suffix = device_id.size() >= 4 ? device_id.substr(device_id.size() - 4) : device_id;
  return std::string(model == Model::CT003 ? CT003_NAME_PREFIX : CT002_NAME_PREFIX) + "_" + suffix;
}

std::vector<uint8_t> status_payload(const Snapshot &s) {
  const float total = s.phase_w[0] + s.phase_w[1] + s.phase_w[2];
  const uint8_t wifi = s.wifi_connected ? 1 : 0;
  if (s.model == Model::CT003) {
    // 37 bytes; frame offset = index + 4.
    std::vector<uint8_t> p(37, 0);
    p[0] = CT003_FIRMWARE_VERSION;
    put_le32(p, 1, clamp_round<int32_t>(total));
    // p[5..12]: cumulative energy. AstraMeter's ESPHome build keeps no
    // energy counter, so it stays 0.
    p[13] = wifi;
    // Second connection state: meaning unconfirmed; a healthy meter is
    // assumed to report it alongside Wi-Fi.
    p[14] = wifi;
    p[15] = rssi_byte(s);
    p[19] = 1;  // number of meters
    p[20] = 0;  // error code
    put_le32(p, 24, clamp_round<int32_t>(s.phase_w[0]));
    put_le32(p, 28, clamp_round<int32_t>(s.phase_w[1]));
    put_le32(p, 32, clamp_round<int32_t>(s.phase_w[2]));
    return p;
  }
  // CT002: 19 bytes; frame offset = index + 4.
  std::vector<uint8_t> p(19, 0);
  p[0] = CT002_FIRMWARE_VERSION;
  // p[1..6]: three per-phase u16 readings (probably voltages) that the app
  // ignores. AstraMeter has no voltage, so they stay 0.
  put_le16(p, 7, clamp_round<int16_t>(s.phase_w[0]));
  put_le16(p, 9, clamp_round<int16_t>(s.phase_w[1]));
  put_le16(p, 11, clamp_round<int16_t>(s.phase_w[2]));
  put_le16(p, 13, clamp_round<int16_t>(total));
  p[15] = wifi;
  p[16] = wifi;  // second connection state, see the CT003 branch
  p[17] = rssi_byte(s);
  p[18] = s.direction_bits & DIRECTION_BITS_MASK;  // phases with reversed direction
  return p;
}

std::string identity_text(const Snapshot &s) {
  const unsigned version = s.model == Model::CT003 ? CT003_FIRMWARE_VERSION : CT002_FIRMWARE_VERSION;
  return "type=" + sanitize_field(s.ct_type) + ",id=" + s.device_id + ",mac=" + s.device_id +
         ",dev_ver=" + std::to_string(version) + ",fc_ver=" + MODULE_VERSION;
}

std::string linked_batteries_text(const std::vector<BatteryRow> &rows, size_t max_len) {
  std::string out;
  for (const auto &row : rows) {
    std::string ip = row.ip.empty() ? "0.0.0.0" : row.ip;
    const char phase = row.phase.empty() ? '-' : row.phase[0];
    std::string entry = "type=" + sanitize_field(row.type) + ",sid=" + sanitize_field(row.id) +
                        ",ip=" + sanitize_field(ip) + ",phpos=" + phase + ";";
    if (out.size() + entry.size() > max_len) break;
    out += entry;
  }
  return out;
}

std::optional<WifiCredentials> parse_wifi_payload(const std::vector<uint8_t> &payload) {
  const std::string text(payload.begin(), payload.end());
  WifiCredentials creds;
  const size_t at = text.find(WIFI_SEPARATOR);
  if (at == std::string::npos) {
    creds.ssid = text;  // open network
  } else {
    creds.ssid = text.substr(0, at);
    creds.password = text.substr(at + std::strlen(WIFI_SEPARATOR));
  }
  if (creds.ssid.empty()) return std::nullopt;
  return creds;
}

Result Responder::handle(const Frame &frame, const Snapshot &s) {
  Result result;
  switch (frame.cmd) {
    case CMD_STATUS:
      result.reply = build_frame(CMD_STATUS, status_payload(s));
      break;
    case CMD_IDENTITY:
      result.reply = build_frame(CMD_IDENTITY, text_bytes(identity_text(s)));
      break;
    case CMD_SET_WIFI: {
      auto creds = parse_wifi_payload(frame.payload);
      if (!creds.has_value()) break;
      this->provisioned_ssid_ = creds->ssid;
      result.wifi = std::move(creds);
      result.reply = build_frame(CMD_SET_WIFI, {0x01});
      break;
    }
    case CMD_READ_SSID: {
      const std::string &ssid = this->provisioned_ssid_.empty() ? s.ssid : this->provisioned_ssid_;
      result.reply = build_frame(CMD_READ_SSID, text_bytes(ssid));
      break;
    }
    case CMD_FACTORY_RESET:
      this->reset();
      result.action = Action::REBOOT;
      break;
    case CMD_REBOOT:
      result.action = Action::REBOOT;
      break;
    case CMD_LINKED_BATTERIES: {
      // Whole entries only, as many as fit one notification.
      size_t room = s.max_frame_len < MAX_FRAME_LEN ? s.max_frame_len : MAX_FRAME_LEN;
      room = room > FRAME_OVERHEAD ? room - FRAME_OVERHEAD : 0;
      result.reply = build_frame(CMD_LINKED_BATTERIES, text_bytes(linked_batteries_text(s.batteries, room)));
      break;
    }
    case CMD_ACTIVE_POWER: {
      if (s.model != Model::CT002) break;
      std::vector<uint8_t> p(6, 0);
      put_le16(p, 0, clamp_round<int16_t>(s.phase_w[0]));
      put_le16(p, 2, clamp_round<int16_t>(s.phase_w[1]));
      put_le16(p, 4, clamp_round<int16_t>(s.phase_w[2]));
      result.reply = build_frame(CMD_ACTIVE_POWER, p);
      break;
    }
    case CMD_SET_DIRECTION: {
      // CT002 only. The meter applies a value of 0-7, ignores anything else,
      // and answers with the bits now in effect; the app takes any answer as
      // success, so a refused change gets no answer at all.
      if (s.model != Model::CT002 || !s.direction_change_allowed || frame.payload.empty()) break;
      uint8_t bits = s.direction_bits & DIRECTION_BITS_MASK;
      if (frame.payload[0] <= DIRECTION_BITS_MASK) {
        bits = frame.payload[0];
        result.direction = bits;
      }
      result.reply = build_frame(CMD_SET_DIRECTION, {bits});
      break;
    }
    default:
      break;
  }
  return result;
}

}  // namespace ble
}  // namespace ct002
}  // namespace esphome
