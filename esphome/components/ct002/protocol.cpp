#include "protocol.h"

#include <cctype>
#include <cstdio>
#include <cstring>

namespace esphome {
namespace ct002 {

const char *const RESPONSE_LABELS[24] = {
    "meter_dev_type",  "meter_mac_code",  "hhm_dev_type",   "hhm_mac_code",
    "A_phase_power",   "B_phase_power",   "C_phase_power",  "total_power",
    "A_chrg_nb",       "B_chrg_nb",       "C_chrg_nb",      "ABC_chrg_nb",
    "wifi_rssi",       "info_idx",        "x_chrg_power",   "A_chrg_power",
    "B_chrg_power",    "C_chrg_power",    "ABC_chrg_power", "x_dchrg_power",
    "A_dchrg_power",   "B_dchrg_power",   "C_dchrg_power",  "ABC_dchrg_power",
};

uint8_t calculate_checksum(const uint8_t *data, size_t len) {
  uint8_t xor_val = 0;
  for (size_t i = 0; i < len; ++i) {
    xor_val ^= data[i];
  }
  return xor_val;
}

size_t compute_length(size_t payload_without_length_size) {
  // Mirrors Python: base = SOH + STX + body + ETX + checksum(2 bytes).
  // Caller passes the size of body only (after SEPARATOR is prepended) —
  // we add the 1+1+1+2 framing bytes here ourselves to match the Python
  // contract where `payload_without_length` is the joined message bytes.
  const size_t base_size = 1 + 1 + payload_without_length_size + 1 + 2;
  for (size_t length_digits = 1; length_digits <= 4; ++length_digits) {
    const size_t total = base_size + length_digits;
    char buf[8];
    const int written = std::snprintf(buf, sizeof(buf), "%zu", total);
    if (written > 0 && static_cast<size_t>(written) == length_digits) {
      return total;
    }
  }
  return 0;
}

std::vector<uint8_t> build_payload(const std::vector<std::string> &fields) {
  std::string message_str;
  for (const auto &f : fields) {
    message_str.push_back(SEPARATOR);
    message_str.append(f);
  }
  const size_t total = compute_length(message_str.size());
  if (total == 0) {
    return {};
  }
  std::vector<uint8_t> payload;
  payload.reserve(total);
  payload.push_back(SOH);
  payload.push_back(STX);
  char len_buf[8];
  const int len_written = std::snprintf(len_buf, sizeof(len_buf), "%zu", total);
  for (int i = 0; i < len_written; ++i) {
    payload.push_back(static_cast<uint8_t>(len_buf[i]));
  }
  for (char c : message_str) {
    payload.push_back(static_cast<uint8_t>(c));
  }
  payload.push_back(ETX);
  const uint8_t checksum = calculate_checksum(payload.data(), payload.size());
  char cs_buf[3];
  std::snprintf(cs_buf, sizeof(cs_buf), "%02x", checksum);
  payload.push_back(static_cast<uint8_t>(cs_buf[0]));
  payload.push_back(static_cast<uint8_t>(cs_buf[1]));
  return payload;
}

static void set_error(std::string *out, const char *msg) {
  if (out != nullptr) {
    *out = msg;
  }
}

std::optional<std::vector<std::string>> parse_request(const uint8_t *data, size_t len,
                                                      std::string *error_out) {
  if (len < 10) {
    set_error(error_out, "Too short");
    return std::nullopt;
  }
  if (data[0] != SOH || data[1] != STX) {
    set_error(error_out, "Missing SOH/STX");
    return std::nullopt;
  }
  size_t sep_index = 0;
  bool found_sep = false;
  for (size_t i = 2; i < len; ++i) {
    if (data[i] == static_cast<uint8_t>(SEPARATOR)) {
      sep_index = i;
      found_sep = true;
      break;
    }
  }
  if (!found_sep) {
    set_error(error_out, "No separator after length");
    return std::nullopt;
  }
  size_t length = 0;
  for (size_t i = 2; i < sep_index; ++i) {
    const uint8_t b = data[i];
    if (b < '0' || b > '9') {
      set_error(error_out, "Invalid length field");
      return std::nullopt;
    }
    length = length * 10 + (b - '0');
  }
  if (length != len) {
    set_error(error_out, "Length mismatch");
    return std::nullopt;
  }
  if (data[len - 3] != ETX) {
    set_error(error_out, "Missing ETX");
    return std::nullopt;
  }
  const uint8_t xor_val = calculate_checksum(data, len - 2);
  char expected[3];
  std::snprintf(expected, sizeof(expected), "%02x", xor_val);
  const uint8_t actual_hi = data[len - 2];
  const uint8_t actual_lo = data[len - 1];
  const bool exact = (static_cast<char>(std::tolower(actual_hi)) == expected[0]) &&
                     (static_cast<char>(std::tolower(actual_lo)) == expected[1]);
  // Real-firmware quirk: tolerate a leading space in the high nibble.
  const bool space_tolerant = (actual_hi == ' ') &&
                              (static_cast<char>(std::tolower(actual_lo)) == expected[1]);
  if (!exact && !space_tolerant) {
    set_error(error_out, "Checksum mismatch");
    return std::nullopt;
  }
  std::vector<std::string> fields;
  std::string current;
  // Python: `data[sep_index:-3].decode("ascii").split("|")[1:]`. data[sep_index]
  // is the leading '|' so skip it; iterate through the byte before ETX.
  for (size_t i = sep_index + 1; i < len - 3; ++i) {
    const uint8_t b = data[i];
    if (b >= 0x80) {
      set_error(error_out, "Invalid ASCII encoding");
      return std::nullopt;
    }
    if (b == static_cast<uint8_t>(SEPARATOR)) {
      fields.push_back(std::move(current));
      current.clear();
    } else {
      current.push_back(static_cast<char>(b));
    }
  }
  fields.push_back(std::move(current));
  return fields;
}

}  // namespace ct002
}  // namespace esphome
