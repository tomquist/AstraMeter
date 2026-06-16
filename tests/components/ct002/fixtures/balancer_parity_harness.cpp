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
//       <sat_min_target> <sat_grace> <sat_enabled> [<min_dc_output> \
//       [<pace_base_step> <pace_max_step> \
//       [<concentrate_deadband> [<import_trim_w>]]]]
//        (Re)create the balancer with the given config. Must be the first
//        command. The trailing global MIN_DC_OUTPUT is optional (absent = 0);
//        the trailing ramp-pacing pair is optional too (absent = the struct
//        defaults, i.e. pacing on at 50/200).
//        Saturation grace defaults are applied per consumer lazily.
//   clock <seconds>           Set the mock clock to an absolute value.
//   advance <seconds>         Advance the mock clock.
//   target <cid> <mode> <manual> <grid> <n> [<cid> <dev> <phase> <power> <md> <eww>]xN
//        Each report carries a per-consumer min_dc_output token <md> (always
//        emitted; < 0 means "unset" / inherit the global) and an
//        efficiency-window weight token <eww> (always emitted; 1.0 = neutral).
//        Call compute_target for <cid> and print the resulting three phase
//        targets. <mode> is auto|manual|inactive. The N reports describe the
//        whole pool for this tick (inactive/manual sets are derived from each
//        report's own mode is NOT modeled here — callers pass explicit modes
//        per target call, matching how the Python harness drives it).
//   sat <cid>                 Print the consumer's saturation score.
//   last <cid>                Print the consumer's last_target (or "none").
//   intent <cid>              Print the consumer's last_intent (or "none").
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
            sat_min_target = 20.0f, sat_grace = 90.0f, min_dc = 0.0f;
      in >> fair >> min_eff >> rot >> sat_threshold >> sat_alpha >> sat_min_target >>
          sat_grace >> sat_enabled;
      // Optional trailing global MIN_DC_OUTPUT (0 / absent = disabled).
      in >> min_dc;
      BalancerConfig cfg;
      cfg.fair_distribution = (fair != 0);
      cfg.min_efficient_power = min_eff;
      cfg.efficiency_rotation_interval = rot;
      cfg.efficiency_saturation_threshold = sat_threshold;
      cfg.min_dc_output = min_dc;
      // Optional trailing ramp-pacing pair (absent = keep the struct
      // defaults; assigning on a failed extraction would zero them and
      // silently disable pacing).
      float pace_base = 0.0f, pace_max = 0.0f;
      if (in >> pace_base >> pace_max) {
        cfg.pace_base_step = pace_base;
        cfg.pace_max_step = pace_max;
        // Optional trailing deadband-concentration threshold (absent = keep
        // the BalancerConfig struct default).
        float conc = 0.0f;
        if (in >> conc) {
          cfg.concentrate_deadband = conc;
          // Optional trailing steady-import trim (absent = keep the struct
          // default).
          float trim = 0.0f;
          if (in >> trim) {
            cfg.import_trim_w = trim;
            // Optional trailing efficiency demand-smoothing alpha (absent = keep
            // the struct default).
            float demand_alpha = 0.0f;
            if (in >> demand_alpha) cfg.efficiency_demand_alpha = demand_alpha;
          }
        }
      }
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
        float power = 0.0f, md = -1.0f, eww = 1.0f;
        in >> rc >> dev >> phase >> power >> md >> eww;
        ConsumerReport r{dev, phase, power};
        if (md >= 0.0f) r.min_dc_output = md;
        r.efficiency_window_weight = eww;
        reports[rc] = r;
      }
      // sample_id mirrors the meter reading (as in production, where it is the
      // grid values): a changed grid is a fresh sample, so the grid-state
      // predictor's meter-correction / trust-adaptation branch is exercised.
      const std::vector<float> sample_id{grid};
      const auto out = balancer->compute_target(cid, parse_mode(mode_str, manual), reports,
                                                grid, {}, {}, sample_id);
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
    } else if (cmd == "intent") {
      std::string cid;
      in >> cid;
      const auto li = balancer->get_last_intent(cid);
      if (li.has_value()) {
        std::cout << *li << "\n";
      } else {
        std::cout << "none\n";
      }
    }
  }
  return 0;
}
