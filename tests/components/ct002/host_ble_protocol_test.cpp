// Host-gcc tests for the CT002/CT003 BLE command protocol in
// esphome/components/ct002/ble_protocol.{h,cpp}. The request frames below are
// the exact bytes the Marstek app sends; the reply layouts are the ones
// documented in docs/ct002-ct003-ble.md.

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <initializer_list>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "esphome/components/ct002/ble_protocol.h"

using namespace esphome::ct002::ble;

namespace {

std::vector<uint8_t> bytes(std::initializer_list<int> values) {
  std::vector<uint8_t> out;
  for (int v : values) out.push_back(static_cast<uint8_t>(v));
  return out;
}

std::vector<uint8_t> text(const std::string &s) { return {s.begin(), s.end()}; }

Frame request(uint8_t cmd, const std::vector<uint8_t> &payload) {
  const auto wire = build_frame(cmd, payload);
  auto frame = parse_frame(wire.data(), wire.size());
  EXPECT_TRUE(frame.has_value());
  return frame.value_or(Frame{});
}

Snapshot ct002_snapshot() {
  Snapshot s;
  s.model = Model::CT002;
  s.ct_type = "HME-4";
  s.device_id = "02b25012abcd";
  s.phase_w = {123.4f, -56.6f, 0.0f};
  s.wifi_connected = true;
  s.rssi_dbm = -61;
  s.ssid = "home";
  return s;
}

Snapshot ct003_snapshot() {
  Snapshot s = ct002_snapshot();
  s.model = Model::CT003;
  s.ct_type = "HME-3";
  return s;
}

int16_t le16(const std::vector<uint8_t> &f, size_t at) {
  return static_cast<int16_t>(static_cast<uint16_t>(f[at] | (f[at + 1] << 8)));
}

int32_t le32(const std::vector<uint8_t> &f, size_t at) {
  return static_cast<int32_t>(static_cast<uint32_t>(f[at]) | (static_cast<uint32_t>(f[at + 1]) << 8) |
                              (static_cast<uint32_t>(f[at + 2]) << 16) | (static_cast<uint32_t>(f[at + 3]) << 24));
}

std::string reply_text(const std::vector<uint8_t> &reply) {
  return std::string(reply.begin() + 4, reply.end() - 1);
}

// Every reply must be a well-formed frame carrying the request's command.
void expect_valid_reply(const std::vector<uint8_t> &reply, uint8_t cmd) {
  ASSERT_GE(reply.size(), FRAME_OVERHEAD);
  EXPECT_EQ(reply[0], FRAME_HEAD);
  EXPECT_EQ(reply[1], reply.size());
  EXPECT_EQ(reply[2], FRAME_MARK);
  EXPECT_EQ(reply[3], cmd);
  EXPECT_EQ(reply.back(), xor_checksum(reply.data(), reply.size() - 1));
}

bool rejected(const std::vector<uint8_t> &wire) { return !parse_frame(wire.data(), wire.size()).has_value(); }

}  // namespace

// ── Framing ─────────────────────────────────────────────────────────────

TEST(BleFrame, BuildsTheAppsRequestsByteForByte) {
  EXPECT_EQ(build_frame(CMD_STATUS, {0x01}), bytes({0x73, 0x06, 0x23, 0x03, 0x01, 0x54}));
  EXPECT_EQ(build_frame(CMD_IDENTITY, {0x01}), bytes({0x73, 0x06, 0x23, 0x04, 0x01, 0x53}));
  EXPECT_EQ(build_frame(CMD_READ_SSID, {0x01}), bytes({0x73, 0x06, 0x23, 0x08, 0x01, 0x5f}));
  EXPECT_EQ(build_frame(CMD_LINKED_BATTERIES, {0x00}), bytes({0x73, 0x06, 0x23, 0x12, 0x00, 0x44}));
}

TEST(BleFrame, ParsesAValidRequest) {
  const auto wire = bytes({0x73, 0x06, 0x23, 0x04, 0x01, 0x53});
  auto frame = parse_frame(wire.data(), wire.size());
  ASSERT_TRUE(frame.has_value());
  EXPECT_EQ(frame->cmd, CMD_IDENTITY);
  EXPECT_EQ(frame->payload, bytes({0x01}));
}

TEST(BleFrame, RejectsWhatTheMeterRejects) {
  EXPECT_TRUE(rejected(bytes({0x74, 0x06, 0x23, 0x04, 0x01, 0x52})));  // wrong head
  EXPECT_TRUE(rejected(bytes({0x73, 0x06, 0x24, 0x04, 0x01, 0x54})));  // wrong marker
  EXPECT_TRUE(rejected(bytes({0x73, 0x06, 0x23, 0x04, 0x01, 0x00})));  // bad XOR
  EXPECT_TRUE(rejected(bytes({0x73, 0x07, 0x23, 0x04, 0x01, 0x53})));  // LEN != bytes received
  EXPECT_TRUE(rejected(bytes({0x73, 0x04, 0x23, 0x04})));              // shorter than a frame
  std::vector<uint8_t> oversized = build_frame(CMD_SET_WIFI, std::vector<uint8_t>(MAX_REQUEST_LEN, 'a'));
  EXPECT_TRUE(rejected(oversized));
  EXPECT_FALSE(rejected(bytes({0x73, 0x05, 0x23, 0x04, 0x51})));  // empty payload is fine
}

TEST(BleFrame, TruncatesPayloadsLenCannotDescribe) {
  const auto frame = build_frame(CMD_LINKED_BATTERIES, std::vector<uint8_t>(400, 'x'));
  EXPECT_EQ(frame.size(), MAX_FRAME_LEN);
  expect_valid_reply(frame, CMD_LINKED_BATTERIES);
}

// ── Identity and naming ─────────────────────────────────────────────────

TEST(BleIdentity, ModelFollowsTheCtType) {
  EXPECT_EQ(model_for_type("HME-3"), Model::CT003);
  EXPECT_EQ(model_for_type("HME-4"), Model::CT002);
}

TEST(BleIdentity, AdvertisedNameUsesTheModelPrefixAndLastFourOfTheId) {
  EXPECT_EQ(advertised_name(Model::CT002, "02b25012abcd"), "MST-TPM_abcd");
  EXPECT_EQ(advertised_name(Model::CT003, "02b25012abcd"), "MST-SMR_abcd");
}

TEST(BleIdentity, NormalizesMacSpellings) {
  EXPECT_EQ(normalize_id("02:B2:50:12:AB:CD"), "02b25012abcd");
  EXPECT_EQ(normalize_id("02-b2-50-12-ab-cd"), "02b25012abcd");
  EXPECT_EQ(normalize_id("02b25012abcd"), "02b25012abcd");
  EXPECT_EQ(normalize_id("02b25012abc"), "");
  EXPECT_EQ(normalize_id("02b25012abcz"), "");
}

TEST(BleIdentity, ManagedIdCarriesThePrefixAndFollowsTheChip) {
  const std::array<uint8_t, 6> chip{0x24, 0x0a, 0xc4, 0x12, 0x34, 0x56};
  const std::string id = managed_id(chip);
  EXPECT_EQ(id.size(), 12u);
  EXPECT_EQ(id.substr(0, 6), "02b250");
  EXPECT_EQ(normalize_id(id), id);
  EXPECT_EQ(managed_id(chip), id);  // the same on every boot
  // Chips from another OUI with the same low bytes still get their own ID.
  EXPECT_NE(managed_id({0x30, 0xae, 0xa4, 0x12, 0x34, 0x56}), id);
  EXPECT_NE(managed_id({0x24, 0x0a, 0xc4, 0x12, 0x34, 0x57}), id);
}

TEST(BleIdentity, AnswersWhatTheAppParses) {
  Responder responder;
  const auto result = responder.handle(request(CMD_IDENTITY, {0x01}), ct002_snapshot());
  expect_valid_reply(result.reply, CMD_IDENTITY);
  EXPECT_EQ(reply_text(result.reply),
            "type=HME-4,id=02b25012abcd,mac=02b25012abcd,dev_ver=124,fc_ver=202409090159");
  // The app gives up on a reply of 20 bytes or less, or one with fewer than
  // three comma-separated fields.
  EXPECT_GT(result.reply.size(), 20u);
}

TEST(BleIdentity, Ct003ReportsItsOwnTypeAndVersion) {
  Responder responder;
  const auto result = responder.handle(request(CMD_IDENTITY, {0x01}), ct003_snapshot());
  EXPECT_EQ(reply_text(result.reply),
            "type=HME-3,id=02b25012abcd,mac=02b25012abcd,dev_ver=122,fc_ver=202409090159");
}

// ── Status ──────────────────────────────────────────────────────────────

TEST(BleStatus, Ct002LayoutMatchesTheRealMeter) {
  Responder responder;
  const auto f = responder.handle(request(CMD_STATUS, {0x01}), ct002_snapshot()).reply;
  expect_valid_reply(f, CMD_STATUS);
  ASSERT_EQ(f.size(), 24u);
  EXPECT_EQ(f[4], CT002_FIRMWARE_VERSION);
  EXPECT_EQ(le16(f, 5), 0);
  EXPECT_EQ(le16(f, 7), 0);
  EXPECT_EQ(le16(f, 9), 0);
  EXPECT_EQ(le16(f, 11), 123);   // phase A, rounded
  EXPECT_EQ(le16(f, 13), -57);   // phase B, rounded, signed
  EXPECT_EQ(le16(f, 15), 0);     // phase C
  EXPECT_EQ(le16(f, 17), 67);    // total of the unrounded phases
  EXPECT_EQ(f[19], 1);           // Wi-Fi connected
  EXPECT_EQ(f[20], 1);
  EXPECT_EQ(static_cast<int8_t>(f[21]), -61);
  EXPECT_EQ(f[22], 0);           // no reversed phases
}

TEST(BleStatus, Ct002ReportsWifiDownWithoutRssi) {
  Snapshot s = ct002_snapshot();
  s.wifi_connected = false;
  Responder responder;
  const auto f = responder.handle(request(CMD_STATUS, {0x01}), s).reply;
  EXPECT_EQ(f[19], 0);
  EXPECT_EQ(f[21], 0);
}

TEST(BleStatus, Ct002ClampsToInt16) {
  Snapshot s = ct002_snapshot();
  s.phase_w = {40000.0f, -40000.0f, 0.0f};
  Responder responder;
  const auto f = responder.handle(request(CMD_STATUS, {0x01}), s).reply;
  EXPECT_EQ(le16(f, 11), 32767);
  EXPECT_EQ(le16(f, 13), -32768);
}

TEST(BleStatus, Ct003LayoutMatchesTheRealMeter) {
  Snapshot s = ct003_snapshot();
  s.phase_w = {1500.0f, -200.0f, 40000.0f};
  Responder responder;
  const auto f = responder.handle(request(CMD_STATUS, {0x01}), s).reply;
  expect_valid_reply(f, CMD_STATUS);
  ASSERT_EQ(f.size(), 42u);
  EXPECT_EQ(f[4], CT003_FIRMWARE_VERSION);
  EXPECT_EQ(le32(f, 5), 41300);   // total, no 16-bit clamp on CT003
  EXPECT_EQ(le32(f, 9), 0);       // energy (low half)
  EXPECT_EQ(le32(f, 13), 0);      // energy (high half)
  EXPECT_EQ(f[17], 1);            // Wi-Fi connected
  EXPECT_EQ(f[18], 1);
  EXPECT_EQ(static_cast<int8_t>(f[19]), -61);
  EXPECT_EQ(f[23], 1);            // one meter
  EXPECT_EQ(f[24], 0);            // no error
  EXPECT_EQ(le32(f, 28), 1500);
  EXPECT_EQ(le32(f, 32), -200);
  EXPECT_EQ(le32(f, 36), 40000);
  EXPECT_EQ(f[40], 0);
}

TEST(BleStatus, NonFinitePowerReportsZero) {
  Snapshot s = ct002_snapshot();
  s.phase_w = {std::numeric_limits<float>::quiet_NaN(), std::numeric_limits<float>::infinity(), 10.0f};
  Responder responder;
  const auto f = responder.handle(request(CMD_STATUS, {0x01}), s).reply;
  EXPECT_EQ(le16(f, 11), 0);
  EXPECT_EQ(le16(f, 13), 0);
  EXPECT_EQ(le16(f, 15), 10);
  EXPECT_EQ(le16(f, 17), 0);  // the total is not finite either
}

// ── Wi-Fi ───────────────────────────────────────────────────────────────

TEST(BleWifi, ReadsBackTheConnectedNetworkBeforeProvisioning) {
  Responder responder;
  const auto f = responder.handle(request(CMD_READ_SSID, {0x01}), ct002_snapshot()).reply;
  expect_valid_reply(f, CMD_READ_SSID);
  EXPECT_EQ(reply_text(f), "home");
}

TEST(BleWifi, EchoesTheProvisionedNetworkSoTheAppSeesSetupSucceed) {
  Responder responder;
  const auto set = responder.handle(request(CMD_SET_WIFI, text("Guest Net<.,.>s3cret,pw")), ct002_snapshot());
  expect_valid_reply(set.reply, CMD_SET_WIFI);
  ASSERT_TRUE(set.wifi.has_value());
  EXPECT_EQ(set.wifi->ssid, "Guest Net");
  EXPECT_EQ(set.wifi->password, "s3cret,pw");
  const auto f = responder.handle(request(CMD_READ_SSID, {0x01}), ct002_snapshot()).reply;
  EXPECT_EQ(reply_text(f), "Guest Net");
}

TEST(BleWifi, PayloadWithoutSeparatorIsAnOpenNetwork) {
  auto creds = parse_wifi_payload(text("cafe"));
  ASSERT_TRUE(creds.has_value());
  EXPECT_EQ(creds->ssid, "cafe");
  EXPECT_EQ(creds->password, "");
}

TEST(BleWifi, EmptySsidIsIgnored) {
  Responder responder;
  const auto result = responder.handle(request(CMD_SET_WIFI, text("<.,.>pw")), ct002_snapshot());
  EXPECT_TRUE(result.reply.empty());
  EXPECT_FALSE(result.wifi.has_value());
}

// ── Linked batteries ───────────────────────────────────────────────────

TEST(BleBatteries, ListsEveryBatteryInTheMetersFormat) {
  Snapshot s = ct002_snapshot();
  s.batteries = {{"HMG-50", "aabbccddeeff", "192.168.1.20", "A"}, {"VNSE3-0", "001122334455", "", "C"}};
  Responder responder;
  const auto f = responder.handle(request(CMD_LINKED_BATTERIES, {0x00}), s).reply;
  expect_valid_reply(f, CMD_LINKED_BATTERIES);
  EXPECT_EQ(reply_text(f),
            "type=HMG-50,sid=aabbccddeeff,ip=192.168.1.20,phpos=A;"
            "type=VNSE3-0,sid=001122334455,ip=0.0.0.0,phpos=C;");
}

TEST(BleBatteries, SanitizesSeparatorsAndStopsAtWholeEntries) {
  EXPECT_EQ(linked_batteries_text({{"a,b", "x=y", "1;2", ""}}, 200), "type=a_b,sid=x_y,ip=1_2,phpos=-;");
  const std::vector<BatteryRow> rows(10, BatteryRow{"HMG-50", "aabbccddeeff", "192.168.1.20", "A"});
  const std::string capped = linked_batteries_text(rows, 120);
  EXPECT_LE(capped.size(), 120u);
  EXPECT_EQ(capped.back(), ';');
}

TEST(BleBatteries, NoBatteriesIsAnEmptyList) {
  Responder responder;
  const auto f = responder.handle(request(CMD_LINKED_BATTERIES, {0x00}), ct002_snapshot()).reply;
  expect_valid_reply(f, CMD_LINKED_BATTERIES);
  EXPECT_EQ(f.size(), FRAME_OVERHEAD);
}

// ── Diagnostics, resets, unknown commands ───────────────────────────────

TEST(BleDiagnostics, Ct002ActivePowerIsThreeSignedPhases) {
  Responder responder;
  const auto f = responder.handle(request(CMD_ACTIVE_POWER, {0x01}), ct002_snapshot()).reply;
  expect_valid_reply(f, CMD_ACTIVE_POWER);
  ASSERT_EQ(f.size(), 11u);
  EXPECT_EQ(le16(f, 4), 123);
  EXPECT_EQ(le16(f, 6), -57);
  EXPECT_EQ(le16(f, 8), 0);
}

TEST(BleDiagnostics, Ct003HasNoPhaseDiagnostics) {
  Responder responder;
  EXPECT_TRUE(responder.handle(request(CMD_ACTIVE_POWER, {0x01}), ct003_snapshot()).reply.empty());
}

// ── Current-direction reversal (0x17) ───────────────────────────────────

static Snapshot direction_snapshot(uint8_t bits, bool allowed) {
  Snapshot s = ct002_snapshot();
  s.direction_bits = bits;
  s.direction_change_allowed = allowed;
  return s;
}

TEST(BleDirection, AppliesAndEchoesTheNewBitsWhenAllowed) {
  Responder responder;
  const auto result = responder.handle(request(CMD_SET_DIRECTION, {0x05}), direction_snapshot(0, true));
  ASSERT_TRUE(result.direction.has_value());
  EXPECT_EQ(*result.direction, 0x05);
  expect_valid_reply(result.reply, CMD_SET_DIRECTION);
  ASSERT_EQ(result.reply.size(), 6u);
  EXPECT_EQ(result.reply[4], 0x05);
}

TEST(BleDirection, OutOfRangeValueKeepsAndReportsTheCurrentBits) {
  // Like the meter: values above 7 are ignored, the answer is what is in effect.
  Responder responder;
  const auto result = responder.handle(request(CMD_SET_DIRECTION, {0x08}), direction_snapshot(0x02, true));
  EXPECT_FALSE(result.direction.has_value());
  expect_valid_reply(result.reply, CMD_SET_DIRECTION);
  EXPECT_EQ(result.reply[4], 0x02);
}

TEST(BleDirection, RefusedWithoutPermissionGetsNoReply) {
  // The app counts any answer as success, so a refusal must stay silent.
  Responder responder;
  const auto result = responder.handle(request(CMD_SET_DIRECTION, {0x01}), direction_snapshot(0, false));
  EXPECT_FALSE(result.direction.has_value());
  EXPECT_TRUE(result.reply.empty());
}

TEST(BleDirection, Ct003AndEmptyPayloadGetNoReply) {
  Responder responder;
  Snapshot ct003 = ct003_snapshot();
  ct003.direction_change_allowed = true;
  EXPECT_TRUE(responder.handle(request(CMD_SET_DIRECTION, {0x01}), ct003).reply.empty());
  EXPECT_TRUE(responder.handle(request(CMD_SET_DIRECTION, {}), direction_snapshot(0, true)).reply.empty());
}

TEST(BleDirection, Ct002StatusReportsTheReversedPhases) {
  const auto p = status_payload(direction_snapshot(0x06, false));
  ASSERT_EQ(p.size(), 19u);
  EXPECT_EQ(p[18], 0x06);  // frame byte 22
}

TEST(BleReset, ResetAndRebootRestartWithoutReplying) {
  Responder responder;
  responder.handle(request(CMD_SET_WIFI, text("lab<.,.>pw")), ct002_snapshot());
  const auto reboot = responder.handle(request(CMD_REBOOT, {0x01}), ct002_snapshot());
  EXPECT_TRUE(reboot.reply.empty());
  EXPECT_EQ(reboot.action, Action::REBOOT);
  EXPECT_EQ(responder.provisioned_ssid(), "lab");
  const auto reset = responder.handle(request(CMD_FACTORY_RESET, {0x01}), ct002_snapshot());
  EXPECT_TRUE(reset.reply.empty());
  EXPECT_EQ(reset.action, Action::REBOOT);
  EXPECT_EQ(responder.provisioned_ssid(), "");
}

TEST(BleUnknown, UnhandledCommandsGetNoReply) {
  Responder responder;
  for (uint8_t cmd : {0x07, 0x10, 0x11, 0x14, 0x15, 0x17, 0x50, 0x51}) {
    const auto result = responder.handle(request(cmd, {0x01}), ct002_snapshot());
    EXPECT_TRUE(result.reply.empty()) << "cmd 0x" << std::hex << int(cmd);
    EXPECT_EQ(result.action, Action::NONE);
  }
}

// ── Reassembly across writes ────────────────────────────────────────────

TEST(BleAssembler, WholeFrameInOneWrite) {
  FrameAssembler assembler;
  const auto wire = build_frame(CMD_STATUS, {0x01});
  const auto frames = assembler.feed(wire.data(), wire.size(), 0);
  ASSERT_EQ(frames.size(), 1u);
  EXPECT_EQ(frames[0].cmd, CMD_STATUS);
}

TEST(BleAssembler, FrameSplitAcrossSmallWrites) {
  FrameAssembler assembler;
  const auto wire = build_frame(CMD_SET_WIFI, text("a-long-network-name<.,.>and-a-password"));
  std::vector<Frame> frames;
  for (size_t at = 0; at < wire.size(); at += 20) {
    const size_t n = std::min<size_t>(20, wire.size() - at);
    auto got = assembler.feed(wire.data() + at, n, static_cast<uint32_t>(at));
    frames.insert(frames.end(), got.begin(), got.end());
  }
  ASSERT_EQ(frames.size(), 1u);
  EXPECT_EQ(parse_wifi_payload(frames[0].payload)->ssid, "a-long-network-name");
}

TEST(BleAssembler, TwoFramesInOneWriteAndNoiseBefore) {
  FrameAssembler assembler;
  std::vector<uint8_t> wire = {0x00, 0xff};
  const auto a = build_frame(CMD_IDENTITY, {0x01});
  const auto b = build_frame(CMD_STATUS, {0x01});
  wire.insert(wire.end(), a.begin(), a.end());
  wire.insert(wire.end(), b.begin(), b.end());
  const auto frames = assembler.feed(wire.data(), wire.size(), 0);
  ASSERT_EQ(frames.size(), 2u);
  EXPECT_EQ(frames[0].cmd, CMD_IDENTITY);
  EXPECT_EQ(frames[1].cmd, CMD_STATUS);
}

TEST(BleAssembler, DropsAPartialFrameAfterTheTimeout) {
  FrameAssembler assembler;
  const auto wire = build_frame(CMD_STATUS, {0x01});
  EXPECT_TRUE(assembler.feed(wire.data(), 3, 0).empty());
  // The rest arrives too late: the stale start is dropped, and the tail
  // alone is not a frame.
  EXPECT_TRUE(assembler.feed(wire.data() + 3, wire.size() - 3, 5000).empty());
  const auto frames = assembler.feed(wire.data(), wire.size(), 5100);
  EXPECT_EQ(frames.size(), 1u);
}

TEST(BleAssembler, BadChecksumIsDroppedAndTheStreamRecovers) {
  FrameAssembler assembler;
  auto bad = build_frame(CMD_STATUS, {0x01});
  bad.back() ^= 0xff;
  EXPECT_TRUE(assembler.feed(bad.data(), bad.size(), 0).empty());
  const auto good = build_frame(CMD_STATUS, {0x01});
  EXPECT_EQ(assembler.feed(good.data(), good.size(), 10).size(), 1u);
}

TEST(BleBatteries, ListFitsTheNegotiatedMtu) {
  Snapshot s = ct002_snapshot();
  s.batteries.assign(6, BatteryRow{"HMG-50", "aabbccddeeff", "192.168.1.20", "A"});
  s.max_frame_len = 182;  // iOS-sized MTU 185, minus the ATT header
  Responder responder;
  const auto f = responder.handle(request(CMD_LINKED_BATTERIES, {0x00}), s).reply;
  expect_valid_reply(f, CMD_LINKED_BATTERIES);
  EXPECT_LE(f.size(), 182u);
  const std::string listed = reply_text(f);
  EXPECT_EQ(listed.back(), ';');
  EXPECT_LT(listed.size(), 6u * 54u);  // not all six fit
}

TEST(BleWifi, ResetForgetsTheProvisionedNetwork) {
  Responder responder;
  responder.handle(request(CMD_SET_WIFI, text("lab<.,.>pw")), ct002_snapshot());
  responder.reset();
  const auto f = responder.handle(request(CMD_READ_SSID, {0x01}), ct002_snapshot()).reply;
  EXPECT_EQ(reply_text(f), "home");
}
