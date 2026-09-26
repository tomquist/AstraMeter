// The firmware's SML decoder, held to the Python one: every vector in
// host_sml_vectors.h is a telegram together with what
// astrameter.powermeter.sml.parse_sml_powers made of it (see
// _gen_sml_vectors.py).
#include <gtest/gtest.h>

#include <cstring>

#include "esphome/components/tibber_pulse/sml_power.h"
#include "host_sml_vectors.h"

using esphome::tibber_pulse::crc16_x25;
using esphome::tibber_pulse::decode_sml_power;
using esphome::tibber_pulse::decode_status_str;
using esphome::tibber_pulse::DecodeStatus;
using esphome::tibber_pulse::ObisSelection;
using esphome::tibber_pulse::PowerReading;
using tibber_pulse_test::sml_vectors;

namespace {

TEST(SmlPower, MatchesPythonDecoder) {
  ASSERT_GE(sml_vectors().size(), 20u);
  for (const auto &v : sml_vectors()) {
    SCOPED_TRACE(v.name);
    PowerReading reading;
    const DecodeStatus status = decode_sml_power(v.telegram.data(), v.telegram.size(), v.obis, &reading);
    ASSERT_EQ(status, v.status) << decode_status_str(status) << " vs " << decode_status_str(v.status);
    if (status != DecodeStatus::OK) continue;
    ASSERT_TRUE(reading.has_total);
    EXPECT_DOUBLE_EQ(reading.total, v.total);
    if (v.python_powers.size() == 3) {
      ASSERT_TRUE(reading.has_phases);
      for (int p = 0; p < 3; p++) EXPECT_DOUBLE_EQ(reading.phases[p], v.python_powers[p]) << "phase " << p;
    } else {
      ASSERT_EQ(v.python_powers.size(), 1u);
      EXPECT_FALSE(reading.has_phases);
      EXPECT_DOUBLE_EQ(reading.total, v.python_powers[0]);
    }
  }
}

const tibber_pulse_test::SmlVector &vector_named(const char *name) {
  for (const auto &v : sml_vectors()) {
    if (std::strcmp(v.name, name) == 0) return v;
  }
  throw std::runtime_error(name);
}

TEST(SmlPower, DefaultRegistersAreThePythonDefaults) {
  // The vectors name Python's defaults explicitly; the firmware's own must match.
  const ObisSelection firmware;
  const ObisSelection &python = vector_named("per_phase_and_total").obis;
  EXPECT_EQ(firmware.total, python.total);
  EXPECT_EQ(firmware.l1, python.l1);
  EXPECT_EQ(firmware.l2, python.l2);
  EXPECT_EQ(firmware.l3, python.l3);
}

TEST(SmlPower, StuffedVectorReallyCarriesAnEscapedEscape) {
  // Guard the vector itself: without eight 0x1b in a row it tests nothing.
  const auto &t = vector_named("byte_stuffed_payload").telegram;
  const uint8_t stuffed[8] = {0x1b, 0x1b, 0x1b, 0x1b, 0x1b, 0x1b, 0x1b, 0x1b};
  bool found = false;
  for (size_t i = 8; i + 8 < t.size() - 8; i++) found |= std::memcmp(t.data() + i, stuffed, 8) == 0;
  EXPECT_TRUE(found);
}

TEST(SmlPower, OutputUntouchedOnFailure) {
  const auto &v = vector_named("crc_mismatch");
  PowerReading reading;
  reading.total = 99.0;
  reading.has_total = true;
  EXPECT_EQ(decode_sml_power(v.telegram.data(), v.telegram.size(), v.obis, &reading), DecodeStatus::CRC_MISMATCH);
  EXPECT_DOUBLE_EQ(reading.total, 99.0);
}

TEST(SmlPower, EveryTruncationFailsCleanly) {
  // Bytes off the network can stop anywhere; no prefix may crash or decode.
  const auto &v = vector_named("per_phase_and_total");
  for (size_t n = 0; n < v.telegram.size(); n++) {
    PowerReading reading;
    EXPECT_NE(decode_sml_power(v.telegram.data(), n, v.obis, &reading), DecodeStatus::OK) << n;
  }
}

TEST(SmlPower, CorruptedBodiesNeverCrash) {
  // Flip each body byte and re-seal the CRC, so the parser — not the CRC
  // check — meets the damage. Any status is fine; reading out of bounds is not
  // (run under -fsanitize=address to catch it, as CMakeLists does).
  const auto &v = vector_named("nested_time_list");
  for (size_t i = 8; i + 8 < v.telegram.size(); i++) {
    for (uint8_t flip : {0x01, 0x80, 0xff}) {
      std::vector<uint8_t> t = v.telegram;
      t[i] ^= flip;
      const uint16_t crc = crc16_x25(t.data(), t.size() - 2);
      t[t.size() - 2] = crc & 0xff;
      t[t.size() - 1] = crc >> 8;
      PowerReading reading;
      decode_sml_power(t.data(), t.size(), v.obis, &reading);
    }
  }
}

TEST(SmlPower, Crc16X25CheckValue) {
  // The catalogued check value of CRC-16/X-25 over "123456789".
  const char *check = "123456789";
  EXPECT_EQ(crc16_x25(reinterpret_cast<const uint8_t *>(check), 9), 0x906e);
}

}  // namespace
