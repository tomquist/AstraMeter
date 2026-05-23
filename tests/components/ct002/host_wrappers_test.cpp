// Host-gcc behavior tests for the four cross-phase filter wrappers
// (Hampel, Smoothing, Deadband, PID). Verifies the C++ port reproduces the
// canonical behaviors documented in src/astrameter/powermeter/wrappers/*.py
// — outlier rejection, EMA seed/dedup, deadband clamp, PID anti-windup,
// distribution back across phases.
//
// The Python originals have their own pytest battery; these gtests are
// the C++-side guard that the port matches.

#include <gtest/gtest.h>

#include <memory>
#include <vector>

#define CT002_HOST_TEST 1
#include "esphome/components/ct002/wrapper_base.h"
#include "esphome/components/ct002/hampel.h"
#include "esphome/components/ct002/pid.h"
#include "esphome/components/ct002/smoothing.h"

namespace {

using esphome::ct002::DeadbandPowermeter;
using esphome::ct002::HampelPowermeter;
using esphome::ct002::PidMode;
using esphome::ct002::PidPowermeter;
using esphome::ct002::Powermeter;
using esphome::ct002::SmoothedPowermeter;

// A stub source whose values can be set per call. Returns whatever the
// caller stashes in `current` — simulates the SensorBackedPowermeter that
// the production pipeline uses as its head.
class StubSource : public Powermeter {
 public:
  std::vector<float> current;
  std::vector<float> get_powermeter_watts() override { return this->current; }
};

float vsum(const std::vector<float> &v) {
  float s = 0.0f;
  for (float x : v)
    s += x;
  return s;
}

// ---- Hampel ----

TEST(Hampel, PassesThroughWhileWindowFilling) {
  StubSource src;
  HampelPowermeter h(&src, /*window=*/5, /*n_sigma=*/3.0f, /*min_threshold=*/0.0f);
  for (int i = 0; i < 4; ++i) {
    src.current = {100.0f, 0.0f, 0.0f};
    auto out = h.get_powermeter_watts();
    ASSERT_EQ(out.size(), 3u);
    EXPECT_FLOAT_EQ(vsum(out), 100.0f);
  }
}

TEST(Hampel, RejectsOutlierAfterWindowFills) {
  StubSource src;
  HampelPowermeter h(&src, /*window=*/5, /*n_sigma=*/3.0f, /*min_threshold=*/10.0f);
  // Steady at 100 W total, then a 10 kW spike.
  for (int i = 0; i < 5; ++i) {
    src.current = {100.0f, 0.0f, 0.0f};
    h.get_powermeter_watts();
  }
  src.current = {10000.0f, 0.0f, 0.0f};
  auto out = h.get_powermeter_watts();
  ASSERT_EQ(out.size(), 3u);
  // Should snap back to the median (~100 W), not pass through 10000.
  EXPECT_NEAR(vsum(out), 100.0f, 1e-3);
}

TEST(Hampel, AcceptsValueWithinThreshold) {
  StubSource src;
  HampelPowermeter h(&src, /*window=*/5, /*n_sigma=*/3.0f, /*min_threshold=*/10.0f);
  for (int i = 0; i < 5; ++i) {
    src.current = {100.0f, 0.0f, 0.0f};
    h.get_powermeter_watts();
  }
  src.current = {105.0f, 0.0f, 0.0f};
  auto out = h.get_powermeter_watts();
  EXPECT_NEAR(vsum(out), 105.0f, 1e-3);
}

TEST(Hampel, RedistributesProportionally) {
  StubSource src;
  HampelPowermeter h(&src, /*window=*/3, /*n_sigma=*/3.0f, /*min_threshold=*/10.0f);
  for (int i = 0; i < 3; ++i) {
    src.current = {50.0f, 30.0f, 20.0f};  // total 100
    h.get_powermeter_watts();
  }
  // Outlier: same ratios, 10x magnitude.
  src.current = {5000.0f, 3000.0f, 2000.0f};
  auto out = h.get_powermeter_watts();
  ASSERT_EQ(out.size(), 3u);
  // After rejection, total snaps back to median (100), but phase ratios
  // (5:3:2) are preserved.
  EXPECT_NEAR(out[0], 50.0f, 1e-3);
  EXPECT_NEAR(out[1], 30.0f, 1e-3);
  EXPECT_NEAR(out[2], 20.0f, 1e-3);
}

// ---- Smoothing ----

TEST(Smoothing, SeedsOnFirstSample) {
  StubSource src;
  SmoothedPowermeter s(&src, /*alpha=*/0.3f, /*max_step=*/0.0f);
  src.current = {200.0f, 0.0f, 0.0f};
  auto out = s.get_powermeter_watts();
  // First call seeds value_ = raw_total; distribute with ratio == 1.
  EXPECT_FLOAT_EQ(out[0], 200.0f);
  ASSERT_TRUE(s.smoothed_value().has_value());
  EXPECT_DOUBLE_EQ(*s.smoothed_value(), 200.0);
}

TEST(Smoothing, EmaConverges) {
  StubSource src;
  SmoothedPowermeter s(&src, /*alpha=*/0.5f, /*max_step=*/0.0f);
  src.current = {0.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();  // seed at 0
  src.current = {400.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();  // sign-flip → catchup alpha; expect value ≈ 200
  ASSERT_TRUE(s.smoothed_value().has_value());
  EXPECT_NEAR(*s.smoothed_value(), 200.0, 1e-6);
}

TEST(Smoothing, MaxStepClampsDelta) {
  StubSource src;
  SmoothedPowermeter s(&src, /*alpha=*/0.5f, /*max_step=*/50.0f);
  src.current = {0.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();
  src.current = {1000.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();
  ASSERT_TRUE(s.smoothed_value().has_value());
  // Delta clamped to +50; sign flip → catchup_alpha = min(0.5, 2.0) = 0.5.
  EXPECT_NEAR(*s.smoothed_value(), 50.0, 1e-6);
}

TEST(Smoothing, DedupsRepeatedSample) {
  StubSource src;
  SmoothedPowermeter s(&src, /*alpha=*/0.5f, /*max_step=*/0.0f);
  src.current = {0.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();
  src.current = {100.0f, 0.0f, 0.0f};
  s.get_powermeter_watts();
  const double after_first = *s.smoothed_value();
  // Same input twice — second call must be a dedup hit, value unchanged.
  s.get_powermeter_watts();
  EXPECT_DOUBLE_EQ(*s.smoothed_value(), after_first);
}

// ---- Deadband ----

TEST(Deadband, ZeroesBelowThreshold) {
  StubSource src;
  DeadbandPowermeter d(&src, /*deadband=*/50.0f);
  src.current = {10.0f, -20.0f, 5.0f};  // |sum| = 5 < 50
  auto out = d.get_powermeter_watts();
  ASSERT_EQ(out.size(), 3u);
  EXPECT_FLOAT_EQ(out[0], 0.0f);
  EXPECT_FLOAT_EQ(out[1], 0.0f);
  EXPECT_FLOAT_EQ(out[2], 0.0f);
}

TEST(Deadband, PassesAtOrAboveThreshold) {
  StubSource src;
  DeadbandPowermeter d(&src, /*deadband=*/50.0f);
  src.current = {30.0f, 25.0f, 0.0f};  // |sum| = 55 >= 50
  auto out = d.get_powermeter_watts();
  EXPECT_FLOAT_EQ(vsum(out), 55.0f);
}

TEST(Deadband, ZeroDeadbandPassesEverything) {
  StubSource src;
  DeadbandPowermeter d(&src, /*deadband=*/0.0f);
  src.current = {0.1f, 0.0f, 0.0f};
  auto out = d.get_powermeter_watts();
  EXPECT_FLOAT_EQ(out[0], 0.1f);
}

// ---- PID ----

TEST(Pid, ZeroErrorYieldsZeroOutputInBias) {
  StubSource src;
  PidPowermeter p(&src, /*kp=*/0.5f, /*ki=*/0.0f, /*kd=*/0.0f, /*output_max=*/800.0f,
                  PidMode::BIAS);
  float t = 0.0f;
  p.set_clock([&]() { return t; });
  src.current = {0.0f, 0.0f, 0.0f};
  auto out = p.get_powermeter_watts();
  EXPECT_FLOAT_EQ(vsum(out), 0.0f);
}

TEST(Pid, ProportionalAppliesNegativeOfMeasurement) {
  StubSource src;
  PidPowermeter p(&src, /*kp=*/0.5f, /*ki=*/0.0f, /*kd=*/0.0f, /*output_max=*/800.0f,
                  PidMode::BIAS);
  float t = 0.0f;
  p.set_clock([&]() { return t; });
  src.current = {200.0f, 0.0f, 0.0f};  // grid import 200
  auto out = p.get_powermeter_watts();
  // bias: raw + per_phase. p_term = 0.5 * -200 = -100. per_phase = -100/3.
  // Expected total = 200 + (-100) = 100.
  EXPECT_NEAR(vsum(out), 100.0f, 1e-3);
}

TEST(Pid, OutputClampedAtOutputMax) {
  StubSource src;
  PidPowermeter p(&src, /*kp=*/5.0f, /*ki=*/0.0f, /*kd=*/0.0f, /*output_max=*/300.0f,
                  PidMode::REPLACE);
  float t = 0.0f;
  p.set_clock([&]() { return t; });
  src.current = {1000.0f, 0.0f, 0.0f};
  auto out = p.get_powermeter_watts();
  // p_term = 5 * -1000 = -5000; clamped to -300; per_phase = -100; total = -300.
  EXPECT_NEAR(vsum(out), -300.0f, 1e-3);
}

TEST(Pid, IntegralWindsUpAndSettles) {
  StubSource src;
  PidPowermeter p(&src, /*kp=*/0.0f, /*ki=*/0.5f, /*kd=*/0.0f, /*output_max=*/500.0f,
                  PidMode::REPLACE);
  float t = 0.0f;
  p.set_clock([&]() { return t; });

  src.current = {0.0f, 0.0f, 0.0f};
  p.get_powermeter_watts();  // seed: dt=0, integral stays 0

  // Sustained 100 W error over 4 s; integral = 0.5 * (100 * 4) = 200, output = 200.
  src.current = {100.0f, 0.0f, 0.0f};
  t = 4.0f;
  auto out = p.get_powermeter_watts();
  // error = -100; integral accumulates +error*dt = -400; i_term = 0.5 * -400 = -200.
  // Clamped only if |output|>max; here |output|=200<500, so accepted.
  EXPECT_NEAR(vsum(out), -200.0f, 1e-3);
}

}  // namespace
