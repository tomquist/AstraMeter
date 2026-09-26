// Instantaneous power out of one SML 1.04 telegram, as the Tibber Pulse
// Bridge serves it: the raw binary the meter's optical port emitted, escape
// sequence to CRC.
//
// Dependency-free (std only) so the host gtest
// (tests/components/tibber_pulse/host_sml_power_test.cpp) can drive it with
// the same bytes the Python decoder is tested against. Mirrors
// src/astrameter/powermeter/sml.py::parse_sml_powers: the frame is located and
// CRC-checked the way smllib's SmlStreamReader does it, and the registers are
// picked the way EnergyStats.from_sml_frame picks them.
//
// ESPHome ships an SML parser of its own (esphome/components/sml), but it can
// only be loaded together with a UART — its component schema requires one and
// its sources include uart.h — so a firmware reading SML over HTTP cannot use
// it. Its list walker also indexes child nodes without bounds checks, which is
// safe behind a serial CRC check but not for bytes off the network.
#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace esphome {
namespace tibber_pulse {

/// An OBIS register as SML carries it: six bytes, A-B:C.D.E*F.
using ObisCode = std::array<uint8_t, 6>;

// Defaults match src/astrameter/powermeter/sml.py (German eHZ-style meters).
/// 1-0:16.7.0*255 — aggregate instantaneous active power.
constexpr ObisCode OBIS_POWER_TOTAL = {0x01, 0x00, 0x10, 0x07, 0x00, 0xff};
/// 1-0:36.7.0*255 / 56.7.0 / 76.7.0 — per-phase active power L1..L3.
constexpr ObisCode OBIS_POWER_L1 = {0x01, 0x00, 0x24, 0x07, 0x00, 0xff};
constexpr ObisCode OBIS_POWER_L2 = {0x01, 0x00, 0x38, 0x07, 0x00, 0xff};
constexpr ObisCode OBIS_POWER_L3 = {0x01, 0x00, 0x4c, 0x07, 0x00, 0xff};

/// DLMS unit code for watts.
constexpr uint8_t SML_UNIT_WATT = 27;

struct ObisSelection {
  ObisCode total{OBIS_POWER_TOTAL};
  ObisCode l1{OBIS_POWER_L1};
  ObisCode l2{OBIS_POWER_L2};
  ObisCode l3{OBIS_POWER_L3};
};

enum class DecodeStatus : uint8_t {
  OK,
  /// No complete frame (start escape ... end escape + CRC) in the bytes.
  NO_FRAME,
  CRC_MISMATCH,
  /// The frame's CRC holds but its contents are not well-formed SML.
  MALFORMED,
  /// A selected register is present but not in watts.
  WRONG_UNIT,
  /// A well-formed telegram without the selected power registers.
  NO_POWER,
};

const char *decode_status_str(DecodeStatus status);

struct PowerReading {
  /// All three phase registers were present; `phases` holds them.
  bool has_phases{false};
  double phases[3]{0.0, 0.0, 0.0};
  /// `total` holds the aggregate register, or the phase sum without one.
  bool has_total{false};
  double total{0.0};
};

/// Decode the first SML frame in *data*. `*out` is written only on OK.
///
/// Which registers count follows the Python source: the three phases when all
/// three are present (the aggregate is then only reported as the total), else
/// the aggregate alone. One deliberate divergence: where Python reports a
/// telegram with none of the registers as a single 0 W phase, this returns
/// NO_POWER, so the firmware publishes nothing rather than a made-up zero and
/// ct002's max_sensor_age notices the missing readings.
DecodeStatus decode_sml_power(const uint8_t *data, size_t len, const ObisSelection &obis, PowerReading *out);

/// CRC-16/X-25 (reflected 0x1021, init and xorout 0xffff), as SML transport v1
/// uses it and smllib checks it. Exposed for the tests.
uint16_t crc16_x25(const uint8_t *data, size_t len);

}  // namespace tibber_pulse
}  // namespace esphome
