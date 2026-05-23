#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace esphome {
namespace ct002 {

inline constexpr uint8_t SOH = 0x01;
inline constexpr uint8_t STX = 0x02;
inline constexpr uint8_t ETX = 0x03;
inline constexpr char SEPARATOR = '|';

// Indexed by ResponseField; the C++ port keeps the label list in the same
// order as the Python source so cross-talk and balancer code that uses
// positional access (e.g. response[15] = A_chrg_power) maps 1:1.
extern const char *const RESPONSE_LABELS[24];
inline constexpr size_t RESPONSE_LABEL_COUNT = 24;

uint8_t calculate_checksum(const uint8_t *data, size_t len);

// Computes the total payload length (incl. the length digits themselves)
// for a payload whose pre-length body — SOH + STX + body + ETX + checksum —
// would have the given size. Returns 0 if the payload is too large to
// encode (matches Python's ValueError, but ESPHome avoids exceptions).
size_t compute_length(size_t payload_without_length_size);

// Builds a CT002 UDP response frame from a list of ascii fields. The output
// is the wire-format bytes ready to write to the socket.
std::vector<uint8_t> build_payload(const std::vector<std::string> &fields);

// Parses an incoming UDP request frame. Returns the field list on success,
// or std::nullopt with an error message on failure. The parser tolerates
// a leading space in the checksum's high nibble (a real-firmware quirk).
std::optional<std::vector<std::string>> parse_request(const uint8_t *data, size_t len,
                                                      std::string *error_out = nullptr);

}  // namespace ct002
}  // namespace esphome
