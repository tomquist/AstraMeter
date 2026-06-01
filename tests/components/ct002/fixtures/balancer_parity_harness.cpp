// Host-gcc differential harness for LoadBalancer parity testing.
//
// Reads a command stream from stdin and drives a single, stateful
// LoadBalancer instance — mirroring exactly what the Python test does to its
// own LoadBalancer — so that multi-poll, time-dependent behaviour (saturation
// EMA, efficiency deprioritization, probe/rotation, weight fade) is compared
// across stacks, not just one-shot splits. Linking the real
// esphome/components/ct002/balancer.cpp makes this a true cross-stack guard.
//
// Commands (one per line, whitespace separated):
//   cfg <fair> <min_eff> <rot_interval> <sat_threshold> <sat_alpha> \
//       <sat_min_target> <sat_grace> <sat_enabled>
//        (Re)create the balancer with the given config. Must be the first
//        command. Saturation grace defaults are applied per consumer lazily.
//   clock <seconds>           Set the mock clock to an absolute value.
//   advance <seconds>         Advance the mock clock.
//   target <cid> <mode> <manual> <grid> <n> [<cid> <dev> <phase> <power>]xN
//        Call compute_target for <cid> and print the resulting three phase
//        targets. <mode> is auto|manual|inactive. The N reports describe the
//        whole pool for this tick (inactive/manual sets are derived from each
//        report's own mode is NOT modeled here — callers pass explicit modes
//        per target call, matching how the Python harness drives it).
//   sat <cid>                 Print the consumer's saturation score.
//   last <cid>                Print the consumer's last_target (or "none").
//
// Output: one line per target/sat/last command.

#include <iostream>
#include <memory>
#include <sstream>
#include <string>
#include <vector>

#include "esphome/components/ct002/balancer.h"

using esphome::ct002::BalancerConfig;
using esphome::ct002::ConsumerMode;
using esphome::ct002::ConsumerModeKind;
using esphome::ct002::ConsumerReport;
using esphome::ct002::LoadBalancer;
using esphome::ct002::ReportMap;

namespace {

double g_clock = 0.0;

ConsumerMode parse_mode(const std::string &mode_str, float manual_value) {
  if (mode_str == "manual") return ConsumerMode{ConsumerModeKind::MANUAL, manual_value};
  if (mode_str == "inactive") return ConsumerMode{ConsumerModeKind::INACTIVE};
  return ConsumerMode{ConsumerModeKind::AUTO};
}

}  // namespace

int main() {
  std::unique_ptr<LoadBalancer> balancer;
  std::string line;
  std::cout.setf(std::ios::fixed);
  std::cout.precision(4);

  while (std::getline(std::cin, line)) {
    if (line.empty()) continue;
    std::istringstream in(line);
    std::string cmd;
    in >> cmd;

    if (cmd == "cfg") {
      int fair = 1, sat_enabled = 0;
      float min_eff = 0.0f, rot = 900.0f, sat_threshold = 0.4f, sat_alpha = 0.15f,
            sat_min_target = 20.0f, sat_grace = 90.0f;
      in >> fair >> min_eff >> rot >> sat_threshold >> sat_alpha >> sat_min_target >>
          sat_grace >> sat_enabled;
      BalancerConfig cfg;
      cfg.fair_distribution = (fair != 0);
      cfg.min_efficient_power = min_eff;
      cfg.efficiency_rotation_interval = rot;
      cfg.efficiency_saturation_threshold = sat_threshold;
      balancer = std::make_unique<LoadBalancer>(
          cfg, sat_alpha, sat_min_target, /*sat_decay=*/0.995f, sat_grace,
          /*sat_stall=*/60.0f, sat_enabled != 0, []() { return g_clock; }, nullptr);
    } else if (cmd == "clock") {
      in >> g_clock;
    } else if (cmd == "advance") {
      double d = 0.0;
      in >> d;
      g_clock += d;
    } else if (cmd == "target") {
      std::string cid, mode_str;
      float manual = 0.0f, grid = 0.0f;
      int n = 0;
      in >> cid >> mode_str >> manual >> grid >> n;
      ReportMap reports;
      for (int i = 0; i < n; ++i) {
        std::string rc, dev, phase;
        float power = 0.0f;
        in >> rc >> dev >> phase >> power;
        reports[rc] = ConsumerReport{dev, phase, power};
      }
      const auto out = balancer->compute_target(cid, parse_mode(mode_str, manual), reports,
                                                grid, {}, {}, {});
      std::cout << out[0] << " " << out[1] << " " << out[2] << "\n";
    } else if (cmd == "sat") {
      std::string cid;
      in >> cid;
      std::cout << balancer->get_saturation(cid) << "\n";
    } else if (cmd == "last") {
      std::string cid;
      in >> cid;
      const auto lt = balancer->get_last_target(cid);
      if (lt.has_value()) {
        std::cout << *lt << "\n";
      } else {
        std::cout << "none\n";
      }
    }
  }
  return 0;
}
