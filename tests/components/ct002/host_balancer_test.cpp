// Host-gcc behavior tests for the LoadBalancer port. Mirrors core scenarios
// from tests/test_balancer.py — inactive steering, manual override, fair-
// share split, balance correction, phase splitting, AC-chargeable detection,
// saturation EMA basics. Detailed algorithmic parity (efficiency rotation,
// probes) is exercised separately by the Python suite which remains the
// canonical specification.

#include <gtest/gtest.h>

#include <unordered_set>
#include <utility>

#include "esphome/components/ct002/balancer.h"

namespace {

using esphome::ct002::BalancerConfig;
using esphome::ct002::ConsumerMode;
using esphome::ct002::ConsumerModeKind;
using esphome::ct002::ConsumerReport;
using esphome::ct002::is_ac_chargeable;
using esphome::ct002::LoadBalancer;
using esphome::ct002::needs_dc_output_floor;
using esphome::ct002::NetOutputW;
using esphome::ct002::ReportMap;
using esphome::ct002::to_grid_reading;

LoadBalancer make_balancer(BalancerConfig cfg = {}, double *clock = nullptr) {
  static double dummy = 0.0;
  if (clock == nullptr) clock = &dummy;
  return LoadBalancer(cfg, /*sat_alpha=*/0.15f, /*sat_min_target=*/20.0f,
                      /*sat_decay=*/0.995f, /*sat_grace=*/90.0f,
                      /*sat_stall=*/60.0f, /*sat_enabled=*/false,
                      [clock]() { return *clock; }, nullptr);
}

TEST(ToGridReading, ConvertsAbsoluteTargetToMeterReading) {
  // Mirrors tests/test_balancer.py TestToGridReading: the single audited
  // boundary that turns an absolute net-output target into the grid reading a
  // battery adds to its own output (positive = grid import).
  EXPECT_FLOAT_EQ(to_grid_reading(NetOutputW(25.0f), 10.0f), 15.0f);
  EXPECT_FLOAT_EQ(to_grid_reading(NetOutputW(0.0f), 200.0f), -200.0f);
}

TEST(ToGridReading, ReportedPlusReadingLandsOnTarget) {
  for (const auto &tc : {std::pair<float, float>{25.0f, 10.0f},
                         std::pair<float, float>{0.0f, 200.0f},
                         std::pair<float, float>{-100.0f, 50.0f}}) {
    const float reading = to_grid_reading(NetOutputW(tc.first), tc.second);
    EXPECT_FLOAT_EQ(tc.second + reading, tc.first);
  }
}

TEST(IsAcChargeable, IdentifiesVenusPrefixes) {
  EXPECT_TRUE(is_ac_chargeable("HMG-50"));
  EXPECT_TRUE(is_ac_chargeable("hmg-50"));
  EXPECT_TRUE(is_ac_chargeable("VNSE3"));
  EXPECT_TRUE(is_ac_chargeable("VNSA"));
  // B2500 family (DC-only, external inverter) is not AC-chargeable.
  EXPECT_FALSE(is_ac_chargeable("HMA-2"));
  EXPECT_FALSE(is_ac_chargeable("HMJ-1"));
  EXPECT_FALSE(is_ac_chargeable("HMK-1"));
  // Jupiter (built-in inverter, DC battery) is not AC-chargeable either.
  EXPECT_FALSE(is_ac_chargeable("HMN-1"));
  // Unknown/empty types are assumed modern AC-coupled batteries (issue #425
  // device-capabilities model): the former fail-closed-to-DC default was
  // intentionally dropped.
  EXPECT_TRUE(is_ac_chargeable("HME-4"));
  EXPECT_TRUE(is_ac_chargeable(""));
}

TEST(NeedsDcOutputFloor, OnlyExternalInverterFamilies) {
  // B2500 family: no built-in inverter, no AC input -> floor applies.
  EXPECT_TRUE(needs_dc_output_floor("HMA-2"));
  EXPECT_TRUE(needs_dc_output_floor("HMJ-1"));
  EXPECT_TRUE(needs_dc_output_floor("HMK-1"));
  // Built-in inverter or AC input -> excluded.
  EXPECT_FALSE(needs_dc_output_floor("HMG-50"));   // Venus
  EXPECT_FALSE(needs_dc_output_floor("VNSD"));      // Venus D (built-in + DC)
  EXPECT_FALSE(needs_dc_output_floor("HMN-1"));     // Jupiter
  EXPECT_FALSE(needs_dc_output_floor(""));          // unknown -> assumed AC
}

TEST(LoadBalancer, InactiveSteersConsumerOutputToZero) {
  auto b = make_balancer();
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 200.0f};
  const auto out = b.compute_target("a", ConsumerMode{ConsumerModeKind::INACTIVE}, reports,
                                    0.0f, {}, {}, {});
  // Steer to zero on phase A: -reported on A, zeros elsewhere.
  EXPECT_FLOAT_EQ(out[0], -200.0f);
  EXPECT_FLOAT_EQ(out[1], 0.0f);
  EXPECT_FLOAT_EQ(out[2], 0.0f);
}

TEST(LoadBalancer, ManualSetsTargetMinusReported) {
  auto b = make_balancer();
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 100.0f};
  ConsumerMode mode{ConsumerModeKind::MANUAL, 400.0f};
  const auto out = b.compute_target("a", mode, reports, 0.0f, {}, {}, {});
  // target = 400 - 100 = 300 on phase A; split by phase (only A active).
  EXPECT_FLOAT_EQ(out[0], 300.0f);
}

TEST(LoadBalancer, AutoSplitsGridAcrossConsumersOnSamePhase) {
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 0.0f;  // pin the raw split math, not ramp pacing
  auto b = make_balancer(cfg);
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 0.0f};
  reports["b"] = ConsumerReport{"HMA-2", "A", 0.0f};
  const auto out = b.compute_target("a", ConsumerMode{}, reports, 400.0f, {}, {}, {});
  // fair_share = grid_total / num_effective participants, then split by
  // phase. Both consumers on phase A with eff_part=1.0; total_eff=2, so
  // for "a": fair_share = 400/2 = 200. Phase A gets 100% of weights.
  EXPECT_FLOAT_EQ(out[0], 200.0f);
  EXPECT_FLOAT_EQ(out[1], 0.0f);
  EXPECT_FLOAT_EQ(out[2], 0.0f);
}

TEST(LoadBalancer, AutoSplitHonoursDistributionWeight) {
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 0.0f;  // pin the raw split math, not ramp pacing
  auto b = make_balancer(cfg);
  ReportMap reports;
  // Weights 1.5 vs 1.0 → a ~60:40 split of the 500 W demand.
  reports["a"] = ConsumerReport{"HMA-2", "A", 0.0f, 1.5f};
  reports["b"] = ConsumerReport{"HMA-2", "A", 0.0f, 1.0f};
  const auto a_out = b.compute_target("a", ConsumerMode{}, reports, 500.0f, {}, {}, {});
  const auto b_out = b.compute_target("b", ConsumerMode{}, reports, 500.0f, {}, {}, {});
  // share = eff_part(1.0) * weight; total share = 2.5.
  // a: 500 * 1.5/2.5 = 300; b: 500 * 1.0/2.5 = 200.
  EXPECT_FLOAT_EQ(a_out[0], 300.0f);
  EXPECT_FLOAT_EQ(b_out[0], 200.0f);
}

TEST(LoadBalancer, ZeroWeightTakesNoShare) {
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 0.0f;  // pin the raw split math, not ramp pacing
  auto b = make_balancer(cfg);
  ReportMap reports;
  // Weight 0 → battery parked at 0 W; the other absorbs the full demand.
  reports["a"] = ConsumerReport{"HMA-2", "A", 0.0f, 0.0f};
  reports["b"] = ConsumerReport{"HMA-2", "A", 0.0f, 1.0f};
  const auto a_out = b.compute_target("a", ConsumerMode{}, reports, 400.0f, {}, {}, {});
  const auto b_out = b.compute_target("b", ConsumerMode{}, reports, 400.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(a_out[0], 0.0f);
  EXPECT_FLOAT_EQ(b_out[0], 400.0f);
}

TEST(LoadBalancer, AutoSplitAcrossPhases) {
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 0.0f;  // pin the raw split math, not ramp pacing
  auto b = make_balancer(cfg);
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 0.0f};
  reports["b"] = ConsumerReport{"HMA-2", "B", 0.0f};
  const auto out = b.compute_target("a", ConsumerMode{}, reports, 400.0f, {}, {}, {});
  // Two consumers, one on A, one on B. fair_share for "a" = 400/2 = 200,
  // then split: A and B each get half of 200 = 100.
  EXPECT_FLOAT_EQ(out[0], 100.0f);
  EXPECT_FLOAT_EQ(out[1], 100.0f);
  EXPECT_FLOAT_EQ(out[2], 0.0f);
}

TEST(LoadBalancer, PaceReadingCapsGrowsAndResets) {
  // Mirrors tests/test_balancer.py TestPaceReading: the auto-path reading is
  // capped at pace_base_step, the cap doubles only while the battery tracks
  // (moved >= PACE_TRACKING_DELTA_W toward the command), follows the error
  // down, and resets on direction reversal. The frozen test clock makes the
  // time-based law reduce to per-poll semantics (dt = 0 -> one reference
  // period).
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 50.0f;
  cfg.pace_max_step = 200.0f;
  // Exercise pacing against the raw grid; the adaptive predictor (on by
  // default) would act on a different, predicted grid (mirrors the Python
  // TestPaceReading helper, which disables it for the same reason).
  cfg.grid_predict_trust = 0.0f;
  auto b = make_balancer(cfg);
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMG-50", "A", 0.0f};
  // First poll: 600 W demand capped to the base step.
  auto out = b.compute_target("a", ConsumerMode{}, reports, 600.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 50.0f);
  // Battery did not move (startup delay): cap must stay at the base step.
  out = b.compute_target("a", ConsumerMode{}, reports, 600.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 50.0f);
  // Battery tracks (+50 W): cap doubles to 100.
  reports["a"].power = 50.0f;
  out = b.compute_target("a", ConsumerMode{}, reports, 550.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 100.0f);
  // Tracks again (+100 W): cap doubles to 200 (the configured max).
  reports["a"].power = 150.0f;
  out = b.compute_target("a", ConsumerMode{}, reports, 450.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 200.0f);
  // Tracks again, but the max holds.
  reports["a"].power = 350.0f;
  out = b.compute_target("a", ConsumerMode{}, reports, 250.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 200.0f);
  // Error fits under the cap: passes through, cap follows it down.
  reports["a"].power = 520.0f;
  out = b.compute_target("a", ConsumerMode{}, reports, 80.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 80.0f);
  // Direction reversal: cap resets to the base step.
  reports["a"].power = 600.0f;
  out = b.compute_target("a", ConsumerMode{}, reports, -300.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], -50.0f);
}

TEST(LoadBalancer, GridPredictorCreditsDeliveredOutput) {
  // Mirrors tests/test_balancer.py TestGridPredictor (output-crediting case).
  // Pacing and oscillation damping off, single consumer,
  // fair_distribution=false → the returned reading equals the predicted grid
  // the control path acted on. sample_id = {grid} mirrors production (the meter
  // reading). The trust-adaptation path is validated against Python by the
  // differential parity suite, which now threads a grid-derived sample_id.
  BalancerConfig cfg;
  cfg.fair_distribution = false;
  cfg.pace_base_step = 0.0f;
  cfg.osc_damp_max = 0.0f;
  cfg.grid_predict_trust = 0.5f;
  auto b = make_balancer(cfg);
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMG-50", "A", 0.0f};
  auto step = [&](float reported, float grid) {
    reports["a"].power = reported;
    return b.compute_target("a", ConsumerMode{}, reports, grid, {}, {},
                            std::vector<float>{grid})[0];
  };
  // First sample returns the raw grid (predictor seeds its estimate).
  EXPECT_FLOAT_EQ(step(0.0f, 300.0f), 300.0f);
  // Same grid → same sample → only output crediting: estimate falls by the
  // pool's reported output change, so the loop commands only the remainder.
  EXPECT_NEAR(step(120.0f, 300.0f), 180.0f, 1e-3f);
  EXPECT_NEAR(step(300.0f, 300.0f), 0.0f, 1e-3f);
}

TEST(LoadBalancer, DcOnlyBatteryClampedToZeroUnderSurplus) {
  auto b = make_balancer();
  ReportMap reports;
  reports["hma"] = ConsumerReport{"HMA-2", "A", 0.0f};      // DC-only
  reports["hmg"] = ConsumerReport{"HMG-50", "A", 0.0f};     // AC-chargeable
  // grid surplus: grid_total = -200 → charge territory.
  const auto out = b.compute_target("hma", ConsumerMode{}, reports, -200.0f, {}, {}, {});
  EXPECT_FLOAT_EQ(out[0], 0.0f);
  EXPECT_FLOAT_EQ(out[1], 0.0f);
  EXPECT_FLOAT_EQ(out[2], 0.0f);
}

TEST(LoadBalancer, RemoveConsumerClearsState) {
  auto b = make_balancer();
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 100.0f};
  // Touch consumer through compute_target so internal state gets created.
  b.compute_target("a", ConsumerMode{ConsumerModeKind::INACTIVE}, reports, 0.0f, {}, {}, {});
  EXPECT_TRUE(b.get_last_target("a").has_value());
  b.remove_consumer("a");
  EXPECT_FALSE(b.get_last_target("a").has_value());
}

TEST(LoadBalancer, ResetConsumerClearsLastTarget) {
  auto b = make_balancer();
  ReportMap reports;
  reports["a"] = ConsumerReport{"HMA-2", "A", 100.0f};
  b.compute_target("a", ConsumerMode{ConsumerModeKind::MANUAL, 300.0f}, reports, 0.0f,
                   {}, {}, {});
  ASSERT_TRUE(b.get_last_target("a").has_value());
  b.reset_consumer("a");
  EXPECT_FALSE(b.get_last_target("a").has_value());
}

}  // namespace
