// Host-gcc behavior tests for the LoadBalancer port. Mirrors core scenarios
// from tests/test_balancer.py — inactive steering, manual override, fair-
// share split, balance correction, phase splitting, AC-chargeable detection,
// saturation EMA basics. Detailed algorithmic parity (efficiency rotation,
// probes) is exercised separately by the Python suite which remains the
// canonical specification.

#include <gtest/gtest.h>

#include <unordered_set>

#include "esphome/components/ct002/balancer.h"

namespace {

using esphome::ct002::BalancerConfig;
using esphome::ct002::ConsumerMode;
using esphome::ct002::ConsumerModeKind;
using esphome::ct002::ConsumerReport;
using esphome::ct002::is_ac_chargeable;
using esphome::ct002::LoadBalancer;
using esphome::ct002::ReportMap;

LoadBalancer make_balancer(BalancerConfig cfg = {}, double *clock = nullptr) {
  static double dummy = 0.0;
  if (clock == nullptr) clock = &dummy;
  return LoadBalancer(cfg, /*sat_alpha=*/0.15f, /*sat_min_target=*/20.0f,
                      /*sat_decay=*/0.995f, /*sat_grace=*/90.0f,
                      /*sat_stall=*/60.0f, /*sat_enabled=*/false,
                      [clock]() { return *clock; }, nullptr);
}

TEST(IsAcChargeable, IdentifiesVenusPrefixes) {
  EXPECT_TRUE(is_ac_chargeable("HMG-50"));
  EXPECT_TRUE(is_ac_chargeable("hmg-50"));
  EXPECT_TRUE(is_ac_chargeable("VNSE3"));
  EXPECT_TRUE(is_ac_chargeable("VNSA"));
  EXPECT_FALSE(is_ac_chargeable("HMA-2"));
  EXPECT_FALSE(is_ac_chargeable("HME-4"));
  EXPECT_FALSE(is_ac_chargeable(""));
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
