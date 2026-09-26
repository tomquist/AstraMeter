#include "sml_power.h"

#include <cmath>
#include <cstring>
#include <vector>

namespace esphome {
namespace tibber_pulse {

namespace {

constexpr uint8_t START_ESCAPE[8] = {0x1b, 0x1b, 0x1b, 0x1b, 0x01, 0x01, 0x01, 0x01};
constexpr uint8_t END_ESCAPE[5] = {0x1b, 0x1b, 0x1b, 0x1b, 0x1a};
constexpr uint8_t ESCAPE[4] = {0x1b, 0x1b, 0x1b, 0x1b};
// smllib's get_obis() finds list entries by this prefix: a seven-element list
// whose first element is a six-byte octet string starting with A=1
// (electricity). Searching for it instead of walking the message tree is what
// the Python decoder does, so a telegram decodes — or fails to — the same way.
constexpr uint8_t LIST_ENTRY_PREFIX[3] = {0x77, 0x07, 0x01};

// Nested lists inside a list entry are only ever an SML_Time choice, two
// levels deep. Anything much deeper is garbage, and recursion must stay
// bounded on an ESP32 stack.
constexpr int MAX_DEPTH = 8;

/// First index >= *from* where *needle* starts, or `len` when absent.
size_t find(const uint8_t *data, size_t len, size_t from, const uint8_t *needle, size_t needle_len) {
  if (needle_len > len) return len;
  for (size_t i = from; i + needle_len <= len; i++) {
    if (std::memcmp(data + i, needle, needle_len) == 0) return i;
  }
  return len;
}

enum class Kind : uint8_t { NONE, END, BOOL, INT, UINT, OCTET, LIST };

struct Value {
  Kind kind{Kind::NONE};
  // INT / UINT: the raw big-endian bytes. OCTET: the string. LIST: unused.
  size_t start{0};
  size_t size{0};
  // LIST: element count.
  size_t count{0};
};

/// Read one type-length field and, for a scalar, its value; for a list, only
/// the header (its elements follow). Mirrors smllib's SmlFrame.get_value().
bool read_value(const uint8_t *buf, size_t len, size_t *pos, Value *out) {
  const size_t tl_start = *pos;
  if (tl_start >= len) return false;
  uint8_t v = buf[tl_start];
  size_t next = tl_start + 1;
  if (v == 0x01) {  // optional value not set
    out->kind = Kind::NONE;
    *pos = next;
    return true;
  }
  if (v == 0x00) {  // EndOfSmlMsg
    out->kind = Kind::END;
    *pos = next;
    return true;
  }
  if (v == 0x42) {  // boolean
    if (next + 1 > len) return false;
    out->kind = Kind::BOOL;
    out->start = next;
    out->size = 1;
    *pos = next + 1;
    return true;
  }
  bool more = (v & 0x80) != 0;
  const uint8_t type = v & 0x70;
  size_t size = v & 0x0f;
  while (more) {
    if (next >= len || size > (SIZE_MAX >> 4)) return false;
    v = buf[next++];
    size = (size << 4) | (v & 0x0f);
    more = (v & 0x80) != 0;
  }
  if (type == 0x70) {  // list: the size is its element count
    out->kind = Kind::LIST;
    out->count = size;
    *pos = next;
    return true;
  }
  // Scalars count their own type-length bytes in the size.
  const size_t header = next - tl_start;
  if (size < header || size > len - tl_start) return false;
  out->start = next;
  out->size = size - header;
  *pos = tl_start + size;
  switch (type) {
    case 0x50:
      out->kind = Kind::INT;
      return true;
    case 0x60:
      out->kind = Kind::UINT;
      return true;
    case 0x00:
      out->kind = Kind::OCTET;
      return true;
    default:
      return false;  // smllib: "Unknown data type"
  }
}

/// Skip a list's elements (the header is already read), however nested.
bool skip_list(const uint8_t *buf, size_t len, size_t *pos, size_t count, int depth) {
  if (depth > MAX_DEPTH) return false;
  for (size_t i = 0; i < count; i++) {
    Value child;
    if (!read_value(buf, len, pos, &child)) return false;
    if (child.kind == Kind::LIST && !skip_list(buf, len, pos, child.count, depth + 1)) return false;
  }
  return true;
}

/// A scalar integer as a number: signed for INT, unsigned for UINT. False for
/// anything else, or for more bytes than a 64-bit integer holds.
bool as_integer(const uint8_t *buf, const Value &v, bool *is_signed, int64_t *s, uint64_t *u) {
  if ((v.kind != Kind::INT && v.kind != Kind::UINT) || v.size > 8) return false;
  uint64_t raw = 0;
  for (size_t i = 0; i < v.size; i++) raw = (raw << 8) | buf[v.start + i];
  *u = raw;
  *is_signed = v.kind == Kind::INT;
  if (*is_signed) {
    // Sign-extend the short big-endian integer.
    if (v.size > 0 && v.size < 8 && (buf[v.start] & 0x80)) raw |= ~uint64_t{0} << (v.size * 8);
    *s = static_cast<int64_t>(raw);
  } else {
    *s = 0;
  }
  return true;
}

bool as_double(const uint8_t *buf, const Value &v, double *out) {
  bool is_signed;
  int64_t s;
  uint64_t u;
  if (!as_integer(buf, v, &is_signed, &s, &u)) return false;
  *out = is_signed ? static_cast<double>(s) : static_cast<double>(u);
  return true;
}

struct Register {
  bool present{false};
  bool numeric{false};
  bool has_unit{false};
  int64_t unit{0};
  int64_t scaler{0};
  double value{0.0};
};

/// Parse the list entry whose header starts at *pos* and fill *reg* when its
/// name is *obis*. Returns false on malformed bytes.
bool parse_entry(const uint8_t *buf, size_t len, size_t *pos, const ObisCode *const wanted[4], Register regs[4]) {
  Value list;
  if (!read_value(buf, len, pos, &list) || list.kind != Kind::LIST) return false;
  Value elems[7];
  const size_t n = list.count < 7 ? list.count : 7;
  for (size_t i = 0; i < list.count; i++) {
    Value v;
    if (!read_value(buf, len, pos, &v)) return false;
    if (v.kind == Kind::LIST && !skip_list(buf, len, pos, v.count, 1)) return false;
    if (i < n) elems[i] = v;
  }
  if (n < 6) return true;  // too short to be an SML_ListEntry: nothing to report
  const Value &name = elems[0];
  if (name.kind != Kind::OCTET || name.size != 6) return true;
  for (int r = 0; r < 4; r++) {
    if (std::memcmp(buf + name.start, wanted[r]->data(), 6) != 0) continue;
    Register reg;
    reg.present = true;
    bool is_signed;
    int64_t s;
    uint64_t u;
    if (as_integer(buf, elems[3], &is_signed, &s, &u)) {
      reg.has_unit = true;
      reg.unit = is_signed ? s : static_cast<int64_t>(u);
    }
    if (as_integer(buf, elems[4], &is_signed, &s, &u)) reg.scaler = is_signed ? s : static_cast<int64_t>(u);
    reg.numeric = as_double(buf, elems[5], &reg.value);
    // A register named twice: the later one wins, as in the Python dict.
    regs[r] = reg;
  }
  return true;
}

/// The register's reading in watts, applying its 10^scaler exponent.
DecodeStatus to_watts(const Register &reg, double *out) {
  if (!reg.has_unit || reg.unit != SML_UNIT_WATT) return DecodeStatus::WRONG_UNIT;
  if (!reg.numeric) return DecodeStatus::MALFORMED;
  if (reg.scaler == 0) {
    *out = reg.value;
  } else if (reg.scaler < 0) {
    // Divide rather than multiply by 10^-n, so 170000 * 10^-3 lands on 170
    // exactly instead of 170.00000000000003.
    *out = reg.value / std::pow(10.0, static_cast<double>(-reg.scaler));
  } else {
    *out = reg.value * std::pow(10.0, static_cast<double>(reg.scaler));
  }
  return DecodeStatus::OK;
}

}  // namespace

const char *decode_status_str(DecodeStatus status) {
  switch (status) {
    case DecodeStatus::OK:
      return "ok";
    case DecodeStatus::NO_FRAME:
      return "no complete SML frame";
    case DecodeStatus::CRC_MISMATCH:
      return "CRC mismatch";
    case DecodeStatus::MALFORMED:
      return "malformed SML";
    case DecodeStatus::WRONG_UNIT:
      return "power register not in W";
    case DecodeStatus::NO_POWER:
      return "no power register";
  }
  return "unknown";
}

uint16_t crc16_x25(const uint8_t *data, size_t len) {
  uint16_t crc = 0xffff;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (int bit = 0; bit < 8; bit++) crc = (crc & 1) ? (crc >> 1) ^ 0x8408 : crc >> 1;
  }
  return crc ^ 0xffff;
}

DecodeStatus decode_sml_power(const uint8_t *data, size_t len, const ObisSelection &obis, PowerReading *out) {
  // Locate the frame the way smllib's SmlStreamReader.get_frame() does.
  const size_t start = find(data, len, 0, START_ESCAPE, sizeof(START_ESCAPE));
  if (start == len) return DecodeStatus::NO_FRAME;
  const uint8_t *msg = data + start;
  const size_t avail = len - start;
  // The end escape, skipping any preceded by an escaped escape sequence.
  size_t end = 0;
  bool found = false;
  for (size_t from = 0;;) {
    end = find(msg, avail, from, END_ESCAPE, sizeof(END_ESCAPE));
    if (end == avail) break;
    if (end < 4 || std::memcmp(msg + end - 4, ESCAPE, 4) != 0) {
      found = true;
      break;
    }
    from = end + 1;
  }
  // End escape (5) + padding count (1) + CRC (2).
  if (!found || avail - end < 8) return DecodeStatus::NO_FRAME;
  const size_t msg_len = end + 8;

  // The CRC covers everything up to itself and goes out low byte first.
  const uint16_t crc = crc16_x25(msg, msg_len - 2);
  if (msg[msg_len - 2] != (crc & 0xff) || msg[msg_len - 1] != (crc >> 8)) return DecodeStatus::CRC_MISMATCH;

  // Strip the escapes and the fill bytes, then undo byte stuffing.
  const size_t padding = msg[msg_len - 3];
  if (8 + padding + 8 > msg_len) return DecodeStatus::MALFORMED;
  const size_t body_len = msg_len - 8 - (8 + padding);
  std::vector<uint8_t> body;
  body.reserve(body_len);
  for (size_t i = 8; i < 8 + body_len;) {
    if (i + 8 <= 8 + body_len && std::memcmp(msg + i, ESCAPE, 4) == 0 && std::memcmp(msg + i + 4, ESCAPE, 4) == 0) {
      body.insert(body.end(), ESCAPE, ESCAPE + 4);
      i += 8;
    } else {
      body.push_back(msg[i++]);
    }
  }

  // Collect the four registers from every list entry in the frame.
  const ObisCode *const wanted[4] = {&obis.total, &obis.l1, &obis.l2, &obis.l3};
  Register regs[4];
  const uint8_t *buf = body.data();
  const size_t blen = body.size();
  for (size_t pos = 0;;) {
    pos = find(buf, blen, pos, LIST_ENTRY_PREFIX, sizeof(LIST_ENTRY_PREFIX));
    if (pos == blen) break;
    // Continue after the entry, so its payload is never searched again.
    if (!parse_entry(buf, blen, &pos, wanted, regs)) return DecodeStatus::MALFORMED;
  }

  // Pick the registers the way EnergyStats.from_sml_frame() does: each phase
  // register that is present must be in watts; all three present wins.
  PowerReading reading;
  bool phases_complete = true;
  for (int p = 0; p < 3; p++) {
    const Register &reg = regs[1 + p];
    if (!reg.present) {
      phases_complete = false;
      continue;
    }
    const DecodeStatus status = to_watts(reg, &reading.phases[p]);
    if (status != DecodeStatus::OK) return status;
  }
  reading.has_phases = phases_complete;
  const Register &total = regs[0];
  if (total.present) {
    const DecodeStatus status = to_watts(total, &reading.total);
    // Python never looks at the aggregate when all three phases are present,
    // so only let a bad one fail the telegram when it would be the reading.
    if (status == DecodeStatus::OK) {
      reading.has_total = true;
    } else if (!phases_complete) {
      return status;
    }
  }
  if (!reading.has_total && phases_complete) {
    reading.total = reading.phases[0] + reading.phases[1] + reading.phases[2];
    reading.has_total = true;
  }
  if (!reading.has_total) return DecodeStatus::NO_POWER;
  *out = reading;
  return DecodeStatus::OK;
}

}  // namespace tibber_pulse
}  // namespace esphome
