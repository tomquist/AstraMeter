// Host-gcc parity test for esphome/components/ct002/protocol.{h,cpp}. Mirrors
// the Python parity test (tests/test_ct002_protocol_parity.py) against the
// same canonical wire bytes — drift in either implementation is caught here
// or there.

#include <gtest/gtest.h>

#include "esphome/components/ct002/protocol.h"
#include "host_protocol_test_vectors.h"

namespace {

using esphome::ct002::build_payload;
using esphome::ct002::parse_request;
using esphome::ct002::RESPONSE_LABEL_COUNT;

class ProtocolGoldenVectors : public ::testing::TestWithParam<ct002_test::GoldenVector> {};

TEST_P(ProtocolGoldenVectors, BuildPayloadMatchesCanonical) {
  const auto &vec = GetParam();
  const auto actual = build_payload(vec.fields);
  ASSERT_EQ(actual.size(), vec.wire.size())
      << "Size mismatch for vector: " << vec.description;
  for (size_t i = 0; i < actual.size(); ++i) {
    ASSERT_EQ(actual[i], vec.wire[i])
        << "Byte " << i << " mismatch for vector: " << vec.description;
  }
}

TEST_P(ProtocolGoldenVectors, ParseRequestRoundTrips) {
  const auto &vec = GetParam();
  std::string error;
  auto parsed = parse_request(vec.wire.data(), vec.wire.size(), &error);
  ASSERT_TRUE(parsed.has_value())
      << "parse_request rejected canonical bytes for '" << vec.description
      << "': " << error;
  ASSERT_EQ(parsed->size(), vec.fields.size())
      << "Field count mismatch for vector: " << vec.description;
  for (size_t i = 0; i < parsed->size(); ++i) {
    ASSERT_EQ((*parsed)[i], vec.fields[i])
        << "Field " << i << " mismatch for vector: " << vec.description;
  }
}

INSTANTIATE_TEST_SUITE_P(All, ProtocolGoldenVectors,
                         ::testing::ValuesIn(ct002_test::load_golden_vectors()));

TEST(ProtocolChecksumSpaceTolerance, AcceptsSpaceHighNibble) {
  for (const auto &vec : ct002_test::load_golden_vectors()) {
    if (!vec.exercise_space_tolerance) {
      continue;
    }
    auto mutated = vec.wire;
    mutated[mutated.size() - 2] = ' ';
    std::string error;
    auto parsed = parse_request(mutated.data(), mutated.size(), &error);
    ASSERT_TRUE(parsed.has_value())
        << "parse_request rejected space-prefixed checksum for '"
        << vec.description << "': " << error;
    ASSERT_EQ(*parsed, vec.fields);
  }
}

TEST(ProtocolResponseLabels, CountIs24) {
  EXPECT_EQ(RESPONSE_LABEL_COUNT, 24u);
}

TEST(ProtocolParseRequest, RejectsCorruptedChecksum) {
  // Build a canonical frame, then flip the last byte to a different hex char
  // and assert parse rejects it.
  std::vector<std::string> fields = {
      "HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "0", "0",
  };
  auto wire = build_payload(fields);
  wire.back() = (wire.back() == '0') ? '1' : '0';
  std::string error;
  auto parsed = parse_request(wire.data(), wire.size(), &error);
  EXPECT_FALSE(parsed.has_value());
  EXPECT_EQ(error, "Checksum mismatch");
}

TEST(ProtocolParseRequest, RejectsMissingSOH) {
  std::vector<std::string> fields = {"HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "0", "0"};
  auto wire = build_payload(fields);
  wire[0] = 0xAA;
  std::string error;
  auto parsed = parse_request(wire.data(), wire.size(), &error);
  EXPECT_FALSE(parsed.has_value());
  EXPECT_EQ(error, "Missing SOH/STX");
}

}  // namespace
